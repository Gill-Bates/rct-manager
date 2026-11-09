//
// tests/e2e/playwright.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Playwright Test configuration of the browser E2E suite. Local runs go through the wrapper:
//   tools/run-limited.sh npx playwright test --config tests/e2e/playwright.config.mjs --project chromium
//
// Environment (all optional, validated here):
//   RCT_E2E_BROWSERS  comma list of chromium,webkit,firefox (default chromium)
//   RCT_E2E_SCHEMES   comma list of light,dark (default light); dark adds a "<engine>-dark" project
//                     that runs only specs tagged @theme-matrix
//   RCT_E2E_WORKERS   worker count; default 2 on CI, else the local budget of tools/test-budget.mjs
//   RCT_E2E_OUTPUT    output directory (default <tmpdir>/rct-e2e-<pid>, always outside the repository)
//   RCT_E2E_JUNIT     write a JUnit XML report to this file
//   RCT_E2E_JSON      write a JSON report to this file
import { defineConfig } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { currentBudget } from "../../tools/test-budget.mjs";

function listFromEnv(name, supported, fallback) {
	const values = (process.env[name] || fallback).split(",").map((item) => item.trim()).filter(Boolean);
	const unknown = values.filter((item) => !supported.includes(item));
	if (!values.length || unknown.length) {
		throw new Error(`Unsupported ${name}: ${unknown.join(", ") || "empty"} (allowed: ${supported.join(", ")})`);
	}
	return [...new Set(values)];
}

const browsers = listFromEnv("RCT_E2E_BROWSERS", ["chromium", "firefox", "webkit"], "chromium");
const schemes = listFromEnv("RCT_E2E_SCHEMES", ["light", "dark"], "light");

const reporters = [["list"]];
if (process.env.RCT_E2E_JUNIT) reporters.push(["junit", { outputFile: process.env.RCT_E2E_JUNIT }]);
if (process.env.RCT_E2E_JSON) reporters.push(["json", { outputFile: process.env.RCT_E2E_JSON }]);

// CI keeps 2 workers; locally the count comes from the shared budget (cores, cgroup, RAM, load).
// RCT_E2E_WORKERS overrides both.
const workers = (() => {
	const raw = process.env.RCT_E2E_WORKERS;
	if (raw !== undefined && raw !== "") {
		if (!/^[1-9][0-9]{0,3}$/.test(raw)) throw new Error(`RCT_E2E_WORKERS must be a positive integer, got ${JSON.stringify(raw)}`);
		return Number(raw);
	}
	return process.env.CI ? 2 : currentBudget().playwrightWorkers;
})();

if (!process.env.CI && !process.env.RCT_RUN_LIMITED && !process.argv.includes("--list")) {
	console.error("Warning: local Playwright runs need tools/run-limited.sh to acquire a shared test slot.");
}

const projects = [];
for (const name of browsers) {
	if (schemes.includes("light")) {
		projects.push({ name, use: { browserName: name, colorScheme: "light" } });
	}
	if (schemes.includes("dark")) {
		projects.push({ name: `${name}-dark`, grep: /@theme-matrix/, use: { browserName: name, colorScheme: "dark" } });
	}
}

export default defineConfig({
	forbidOnly: true,
	testDir: path.dirname(fileURLToPath(import.meta.url)),
	testMatch: "*.spec.mjs",
	// Budget, not a fix for slow tests: 120 s covers a gateway start (up to 90 s worker fixture timeout is
	// separate) plus the slowest non-soak test; the energy simulator needs ~10 s to apply a charge.
	// Soak specs (@slow) set their own, longer timeout in the spec.
	timeout: 120_000,
	// 25 min stays below the 45 min CI job limit and fails fast on a hung run.
	globalTimeout: 1_500_000,
	// Auto-retrying assertions: the dashboard poll interval and the simulator refresh are a few seconds.
	expect: { timeout: 10_000 },
	fullyParallel: true,
	workers,
	retries: 0,
	reporter: reporters,
	outputDir: process.env.RCT_E2E_OUTPUT || path.join(os.tmpdir(), `rct-e2e-${process.pid}`),
	use: {
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
		video: "retain-on-failure",
	},
	projects,
});
