//
// tests/e2e/support/gateway.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Start and stop the isolated gateway (tests/e2e/run_server.py) and the simulated inverter
// (tests/e2e/fake_inverter.py) for Playwright specs. Only processes started here are ever signalled.
// Everything binds 127.0.0.1, uses free ports and a temporary database under the temp root; the
// repository data/ directory and settings.env are never read or written.
import { expect } from "@playwright/test";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
export const PY = process.env.PYTHON || path.join(ROOT, ".venv/bin/python");
// Temporary files live outside the repository; RCT_E2E_TMP overrides the default root.
export const TMP_ROOT = process.env.RCT_E2E_TMP || path.join(os.tmpdir(), "rct-playwright");
export const NEW_PASSWORD = "e2e-a-much-stronger-password";
export const MODES = ["default", "energy", "planning"];

export function freePort() {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once("error", reject);
		server.listen(0, "127.0.0.1", () => {
			const { port } = server.address();
			server.close(() => resolve(port));
		});
	});
}

function spawnPy(args, logFile) {
	const log = fs.openSync(logFile, "a");
	const proc = spawn(PY, ["-m", ...args], {
		cwd: ROOT,
		env: { ...process.env, PYTHONUNBUFFERED: "1" },
		stdio: ["ignore", "pipe", log],
	});
	fs.closeSync(log);
	let output = "";
	proc.stdout.on("data", (chunk) => {
		output += chunk;
		fs.appendFileSync(logFile, chunk);
	});
	return { proc, output: () => output };
}

// SIGTERM first, SIGKILL after 15 s (the contract of the former driver scripts); resolves once the
// process has exited.
export async function stopProcess(proc) {
	if (proc.exitCode !== null || proc.signalCode !== null) return;
	const exited = new Promise((resolve) => proc.once("exit", resolve));
	proc.kill("SIGTERM");
	const timer = setTimeout(() => proc.kill("SIGKILL"), 15_000);
	await exited;
	clearTimeout(timer);
}

/**
 * Start a gateway in the given mode ("default", "energy" or "planning") and wait until it answers.
 * Returns { base, httpPort, devicePort, logDir, firstPassword(), stop() }.
 */
export async function startGateway(mode = "default") {
	if (!MODES.includes(mode)) throw new Error(`unknown gateway mode ${JSON.stringify(mode)}`);
	fs.mkdirSync(TMP_ROOT, { recursive: true, mode: 0o700 });
	const dir = fs.mkdtempSync(path.join(TMP_ROOT, `gw-${mode}-`));
	const db = path.join(dir, "e2e.db");
	const httpPort = await freePort();
	const devicePort = await freePort();
	const base = `http://127.0.0.1:${httpPort}`;
	const started = [];
	const stop = async () => {
		// Server first so it stops polling the simulator, then the simulator; all are awaited.
		for (const item of started.reverse()) await stopProcess(item.proc);
		fs.rmSync(dir, { recursive: true, force: true });
	};
	try {
		const fake = spawnPy(["tests.e2e.fake_inverter", String(devicePort)], path.join(dir, "fake.log"));
		started.push(fake);
		const args = ["tests.e2e.run_server", db, String(httpPort), String(devicePort)];
		if (mode !== "default") args.push(mode);
		let server;
		const launchServer = async () => {
			server = spawnPy(args, path.join(dir, "server.log"));
			started.push(server);
			const current = server;
			await expect.poll(async () => {
				if (current.proc.exitCode !== null) throw new Error(`gateway exited early with code ${current.proc.exitCode}`);
				try {
					return (await fetch(`${base}/health`)).ok;
				} catch {
					return false;
				}
			}, { message: "gateway answers /health", timeout: 30_000, intervals: [100, 200, 500] }).toBe(true);
		};
		await launchServer();
		const firstPassword = () => {
			const password = /One-time password:\s+(\S+)/.exec(server.output())?.[1];
			if (!password) throw new Error("first-start password was not printed in the banner");
			return password;
		};
		// Stop only the gateway process (SIGTERM, SIGKILL after 15 s) and start it again on the same database
		// and ports; the simulator keeps running. Returns the console output of the new process.
		const restart = async () => {
			await stopProcess(server.proc);
			started.splice(started.indexOf(server), 1);
			await launchServer();
			return { output: () => server.output() };
		};
		return { base, httpPort, devicePort, logDir: dir, mode, firstPassword, restart, stop };
	} catch (error) {
		await stop();
		throw error;
	}
}

/**
 * First login of the fresh gateway: one-time password, forced password change and the one-time usage
 * notice. Returns the Playwright storageState of the logged-in admin session.
 */
export async function seedAdminState(browser, gateway) {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "en-GB", colorScheme: "light" });
	try {
		const page = await context.newPage();
		const password = gateway.firstPassword();
		await page.goto(`${gateway.base}/login`);
		await page.fill("#password", password);
		await page.click("#login-form button[type=submit]");
		await page.waitForURL("**/change-password");
		await page.fill("#current-password", password);
		await page.fill("#new-password", NEW_PASSWORD);
		await page.fill("#confirm-password", NEW_PASSWORD);
		await page.click("#change-password-form button[type=submit]");
		await page.waitForURL("**/ui/dashboard");
		// The one-time usage notice locks the admin API until accepted.
		await page.locator("#disclaimer-accept").waitFor({ state: "visible" });
		await Promise.all([page.waitForNavigation(), page.click("#disclaimer-accept")]);
		return await context.storageState();
	} finally {
		await context.close();
	}
}
