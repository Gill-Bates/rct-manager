//
// tests/e2e/listener-restart.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Background listener restart against the REAL entry point (`run.py serve --env-file`, no pre-bound
// socket): the port is changed in the Settings UI, the service re-executes itself and the page follows
// it to the new origin. Companion of tests/test_restart_integration.py. Replaces listener_restart.mjs.
// Each test owns its service, a temporary settings.env and database under /tmp/rct-playwright; the
// repository settings.env and data/ are never touched. Tagged @restart: keep out of the parallel pool.
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { PY, ROOT, TMP_ROOT, freePort, stopProcess } from "./support/gateway.mjs";
import { elapse } from "./support/timing.mjs";
import { NEW_PASSWORD, expect, test as base } from "./support/fixtures.mjs";

const SECRET = "e2e-listener-" + "x".repeat(40);

const answers = async (port) => {
	try { return (await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) })).ok; } catch { return false; }
};

// The real command line of the Docker image and the systemd unit, with an isolated data directory.
async function startService(tag, extraEnv = {}) {
	fs.mkdirSync(TMP_ROOT, { recursive: true, mode: 0o700 });
	const dir = fs.mkdtempSync(path.join(TMP_ROOT, `${tag}-`));
	const port = await freePort();
	const envFile = path.join(dir, "settings.env");
	fs.writeFileSync(envFile, `BIND_PORT=${port}\nADMIN_DB_PATH=${path.join(dir, "rct.db")}\nHMAC_SECRET=${SECRET}\n`, { mode: 0o600 });
	const log = path.join(dir, "server.log");
	const env = { ...process.env, PYTHONUNBUFFERED: "1", ...extraEnv };
	for (const name of ["RCT_API_RESTARTED_AT", "RCT_API_BOUND_FD"]) delete env[name];
	if (!("RCT_API_CONTAINER" in extraEnv)) delete env.RCT_API_CONTAINER;
	const proc = spawn(PY, ["run.py", "serve", "--env-file", envFile], { cwd: ROOT, env, detached: true, stdio: ["ignore", "pipe", "pipe"] });
	let out = "";
	proc.stdout.on("data", (d) => { out += d; fs.appendFileSync(log, d); });
	proc.stderr.on("data", (d) => fs.appendFileSync(log, d));
	const service = { proc, port, dir, output: () => out, password: "" };
	try {
		await expect.poll(() => answers(port), { message: "the service starts", timeout: 30_000, intervals: [100, 200, 500] }).toBe(true);
		service.password = /One-time password:\s+(\S+)/.exec(out)?.[1] ?? "";
		if (!service.password) throw new Error("first-start password was not printed in the banner");
	} catch (error) {
		await stopProcess(proc);
		fs.rmSync(dir, { recursive: true, force: true });
		throw error;
	}
	return service;
}

const test = base.extend({
	// Factory: starts services and stops/cleans all of them after the test (only the ones started here).
	service: async ({}, use) => {
		const started = [];
		try {
			await use(async (tag, extraEnv) => { const s = await startService(tag, extraEnv); started.push(s); return s; });
		} finally {
			for (const s of started) {
				await stopProcess(s.proc);
				fs.rmSync(s.dir, { recursive: true, force: true });
			}
		}
	},
});

async function login(page, base, service) {
	await page.goto(`${base}/login`);
	await page.locator("#password").fill(service.password);
	await page.locator("#login-form button[type=submit]").click();
	await page.waitForURL("**/change-password");
	await page.locator("#current-password").fill(service.password);
	await page.locator("#new-password").fill(NEW_PASSWORD);
	await page.locator("#confirm-password").fill(NEW_PASSWORD);
	await page.locator("#change-password-form button[type=submit]").click();
	await page.waitForURL("**/ui/dashboard");
	// The one-time usage notice locks the admin pages until accepted (the former driver lacked this step).
	await page.locator("#disclaimer-accept").waitFor({ state: "visible" });
	await Promise.all([page.waitForNavigation(), page.click("#disclaimer-accept")]);
}

async function typePort(page, value) {
	await page.locator("#setting-bind_port").fill(String(value));
	await page.locator("#setting-bind_port").dispatchEvent("change");
	await page.waitForSelector("#confirm-modal.show");
}

const savedPort = (page) => page.evaluate(async () => (await (await fetch("/admin/api/settings")).json()).settings.bind_port);

async function settingsPage(browser, service, options = { viewport: { width: 1280, height: 900 }, locale: "en-GB" }) {
	const base = `http://127.0.0.1:${service.port}`;
	const context = await browser.newContext(options);
	const page = await context.newPage();
	const errors = [];
	page.on("pageerror", (error) => errors.push(error.message));
	await login(page, base, service);
	await page.goto(`${base}/ui/settings`);
	await page.waitForSelector("#setting-bind_port");
	return { page, base, errors, context };
}

test.describe("listener restart @restart", () => {
	test("occupied port is refused and triggers no restart @restart", async ({ browser, service: startOne }) => {
		const plain = await startOne("plain");
		const { page, context } = await settingsPage(browser, plain);
		// Occupied port: held by this process for the whole step, so nothing can race for it.
		const holder = net.createServer();
		await new Promise((resolve) => holder.listen(0, "127.0.0.1", resolve));
		try {
			await typePort(page, holder.address().port);
			await page.click("#confirm-accept");
			// Negative window: nothing may happen. 2.5 s is longer than the 1.5 s save debounce.
			await elapse(2500, "a refused save must stay refused after the 1.5 s debounce");
			expect(await savedPort(page), "nothing persisted: the saved port is still the old one").toBe(plain.port);
			await expect(page.locator("body.is-reconnecting"), "no reconnect state").toHaveCount(0);
			expect(await answers(plain.port), "old port still serves").toBe(true);
			expect(plain.output(), "an occupied port triggers no restart").not.toMatch(/Restarting in the background/);
		} finally {
			await new Promise((resolve) => holder.close(resolve));
			await context.close();
		}
	});

	test("port change restarts the listener in the same process and the page follows @restart", async ({ browser, service: startOne }) => {
		const plain = await startOne("plain");
		const pid = plain.proc.pid;
		const { page, errors, context } = await settingsPage(browser, plain);
		try {
			const newPort = await freePort();
			const newBase = `http://127.0.0.1:${newPort}`;
			await typePort(page, newPort);
			const confirmText = await page.locator("#confirm-modal .modal-body").innerText();
			expect(confirmText, "outside a container the confirmation says nothing about Docker").not.toMatch(/docker/i);
			await page.click("#confirm-accept");
			const started = Date.now();
			await page.waitForSelector("#reconnect-modal.show");
			const modalAt = Date.now() - started;
			const title = await page.locator("#reconnect-title").innerText();
			const modalText = await page.locator("#reconnect-modal").innerText();
			expect(title, 'the "Applying settings" modal appears').toMatch(/Applying settings/);
			expect(modalText, "no restart wording in the modal").not.toMatch(/restart/i);
			await expect.poll(async () => !(await answers(plain.port)), { message: "the old port stops answering", timeout: 30_000, intervals: [100] }).toBe(true);
			const oldDownAt = Date.now() - started;
			await expect.poll(() => answers(newPort), { message: "the new port answers", timeout: 30_000, intervals: [100] }).toBe(true);
			const newUpAt = Date.now() - started;
			expect(oldDownAt, `old port stops, then the new port answers: down ${oldDownAt} ms, up ${newUpAt} ms, modal ${modalAt} ms`).toBeLessThanOrEqual(newUpAt);
			try {
				await page.waitForURL(`${newBase}/ui/settings`, { timeout: 30_000 });
			} catch (error) {
				await test.info().attach("listener-restart-stuck.png", { body: await page.screenshot(), contentType: "image/png" });
				throw new Error(`${error.message} (page is at ${page.url()}: ${(await page.locator("#reconnect-modal").innerText()).replace(/\s+/g, " ")})`);
			}
			await page.waitForSelector("#setting-bind_port");
			expect(page.url(), "the page followed the new origin without a login").not.toContain("/login");
			expect(await page.inputValue("#setting-bind_port"), "the new port is shown after the reload").toBe(String(newPort));
			expect(await savedPort(page), "the new port is persisted server-side").toBe(newPort);
			expect(await answers(plain.port), "the old port is gone").toBe(false);
			expect(plain.proc.exitCode, "same process: still running").toBeNull();
			expect((plain.output().match(new RegExp(`starting \\(.*\\), pid=${pid}\\b`, "g")) || []).length,
				"same process: the PID is unchanged across the restart").toBe(2);
			expect(plain.output(), "the restart logged a graceful hand-over").toMatch(/Shutdown finished/);
			expect(plain.output(), "the restart logged no traceback or re-exec failure").not.toMatch(/Traceback|Re-exec failed/);
			expect(await page.locator("main").innerText(), "no restart wording anywhere on the page").not.toMatch(/restart/i);
			expect(errors, "no script errors").toEqual([]);
			await test.info().attach("listener-restart-new-origin.png", { body: await page.screenshot(), contentType: "image/png" });
		} finally {
			await context.close();
		}
	});

	test("container mode confirmation names the port mapping; declining changes nothing @restart", async ({ browser, service: startOne }) => {
		const boxed = await startOne("container", { RCT_API_CONTAINER: "1" });
		const { page, context } = await settingsPage(browser, boxed, { locale: "en-GB" });
		try {
			const help = await page.locator("#setting-bind_port-help").innerText();
			expect(help, "the port help names the Docker port mapping").toMatch(/port mapping/);
			expect(help, "the port help names BIND_PORT").toMatch(/BIND_PORT/);
			await typePort(page, await freePort());
			const warning = await page.locator("#confirm-modal .modal-body").innerText();
			expect(warning, "the confirmation warns about the published port mapping").toMatch(/published Docker port mapping/);
			expect(warning, "the confirmation names BIND_PORT").toMatch(/BIND_PORT/);
			expect(warning, "the confirmation warns about the health check").toMatch(/unhealthy/);
			await page.click("#confirm-cancel");
			await page.waitForSelector("#confirm-modal", { state: "hidden" });
			// Negative window: nothing may happen after a cancel, longer than the 1.5 s save debounce.
			await elapse(2200, "a declined change must stay unsaved after the 1.5 s debounce");
			expect(await savedPort(page), "declining the confirmation leaves the saved port unchanged").toBe(boxed.port);
			expect(await answers(boxed.port), "declining the confirmation leaves the service up").toBe(true);
		} finally {
			await context.close();
		}
	});
});
