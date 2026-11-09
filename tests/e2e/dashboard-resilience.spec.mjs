//
// tests/e2e/dashboard-resilience.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// A failed, empty or "connecting" poll must not rebuild or shrink the Overview. Values stay until they
// are older than the next expected poll, then read n/a; the layout never changes.
// Replaces dashboard_resilience.mjs. Every test starts from the full simulator dashboard.
import { elapse } from "./support/timing.mjs";
import { expect, test } from "./support/fixtures.mjs";

const VALUE_MAX_AGE_MS = 20_000; // keep in sync with VALUE_MAX_AGE_MS in admin.js
const DEVICES = "**/admin/api/devices";

// The shape of the dashboard: everything a collapse would remove, plus the grid's vertical position.
const inventory = (page) => page.evaluate(() => ({
	cards: document.querySelectorAll(".device-item").length,
	subcards: [...document.querySelectorAll(".device-subcard-title")].map((e) => e.textContent.trim()),
	slices: document.querySelectorAll(".device-battery-slice").length,
	flow: Boolean(document.querySelector(".device-flow-graphic:not([hidden]) .energy-flow-svg")),
	cells: document.querySelectorAll(".device-metric-card").length,
	gridTop: Math.round(document.getElementById("dashboard-grid").getBoundingClientRect().top),
	pv: document.getElementById("pv-power").textContent.replace(/\s+/g, " ").trim(),
	firstCell: document.querySelector(".device-metric-card dd")?.textContent.replace(/\s+/g, " ").trim(),
	status: document.querySelector(".device-item .device-head-main")?.textContent.replace(/\s+/g, " ").trim(),
	banner: (() => { const b = document.querySelector(".dashboard-offline-banner"); return b && !b.hidden ? b.textContent : null; })(),
}));

// restart-like answer: 200, inverter connecting, nothing cached.
const emptyPayload = (route) => route.fetch().then(async (response) => {
	const body = await response.json();
	for (const device of body.devices) { device.status = "starting"; device.metrics = []; device.batteries = []; device.energy_flow = null; }
	return route.fulfill({ response, json: body });
});

// Loads the dashboard and returns the inventory of the full state plus a problem collector.
async function loadFull(page) {
	const problems = [];
	// WebKit-only noise that also appears on /login (same filter as the former admin_flow collector).
	const webkitNoise = (text) => test.info().project.use.browserName === "webkit"
		&& /^Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'|\/health due to access control checks|Blocked by Web Inspector/.test(text);
	page.on("console", (m) => { if (!webkitNoise(m.text()) && ["error", "warning"].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
	page.on("pageerror", (e) => { if (!webkitNoise(e.message)) problems.push(`pageerror: ${e.message}`); });
	await page.goto("/ui/dashboard");
	await page.waitForFunction(() => document.querySelectorAll(".device-subcard").length >= 3
		&& document.querySelector(".device-flow-graphic:not([hidden])")
		&& /2,035/.test(document.getElementById("pv-power").textContent)
		&& !document.getElementById("dashboard-grid").classList.contains("dashboard-grid--loading"), null, { timeout: 60_000 });
	await page.evaluate(() => { document.querySelector(".device-item").dataset.probe = "kept"; });
	return { full: await inventory(page), problems };
}

// Forces one poll and waits for its response (replaces response wait plus a fixed 400 ms render sleep:
// callers wait for the rendered signal they assert on).
async function poll(page, status) {
	const response = page.waitForResponse((r) => new URL(r.url()).pathname === "/admin/api/devices" && r.status() === status, { timeout: 20_000 });
	await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
	await response;
}

const shot = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
const expectNoProblems = (problems) => expect(problems.filter((p) => !/status of 503|Failed to load resource/.test(p)),
	"no unexpected console or page errors").toEqual([]);

test.describe("dashboard resilience @resilience @theme-matrix @viewport-matrix", () => {
	test("simulator renders the full dashboard @resilience", async ({ adminPage: page }) => {
		const { full, problems } = await loadFull(page);
		await shot(page, "resilience-0-full");
		expect(full.subcards.length, `three cards: ${JSON.stringify(full)}`).toBe(3);
		expect(full.slices, "two towers render battery slices").toBeGreaterThan(0);
		expect(full.flow, "flow graphic is visible").toBe(true);
		expect(full.pv, "PV power shows the simulator value").toMatch(/2,035/);
		expectNoProblems(problems);
	});

	test("empty answer keeps layout and values; n/a after max age; TSDB tile @resilience", async ({ adminPage: page }) => {
		test.setTimeout(150_000);
		const { full, problems } = await loadFull(page);
		await page.route(DEVICES, emptyPayload);
		await poll(page, 200);
		await expect.poll(async () => (await inventory(page)).status, { message: "card shows the connecting status" }).toMatch(/Connecting/);
		const connecting = await inventory(page);
		await shot(page, "resilience-1-connecting");
		expect(JSON.stringify({ ...connecting, status: 0 }),
			"an empty connecting answer keeps the layout and the last values").toBe(JSON.stringify({ ...full, status: 0 }));
		// The next expected poll brings nothing again: the values turn n/a, the layout stays.
		await elapse(VALUE_MAX_AGE_MS + 1500, "values must be older than VALUE_MAX_AGE_MS (20 s) before the next poll");
		await poll(page, 200);
		await expect.poll(async () => (await inventory(page)).pv, { message: "PV reads n/a once older than the next expected poll" }).toBe("n/a");
		const expired = await inventory(page);
		await shot(page, "resilience-2-expired");
		expect(expired.firstCell, "first metric cell reads n/a").toBe("n/a");
		expect({ cards: expired.cards, subcards: expired.subcards, slices: expired.slices, flow: expired.flow, cells: expired.cells, gridTop: expired.gridTop },
			"the layout is unchanged while the values are n/a")
			.toEqual({ cards: full.cards, subcards: full.subcards, slices: full.slices, flow: true, cells: full.cells, gridTop: full.gridTop });
		// The TSDB tile follows the same rule: no export value reads n/a, the reason is only a tooltip.
		const tile = await page.evaluate(() => {
			const label = document.getElementById("tsdb-status-label");
			return { text: label.textContent, title: label.title };
		});
		expect(tile.text, "the TSDB tile reads n/a without an export value").toBe("n/a");
		expect(tile.title, "the reason is secondary (tooltip)").not.toBe("");
		expectNoProblems(problems);
	});

	test("503 note leaves the grid intact @resilience", async ({ adminPage: page }) => {
		const { full, problems } = await loadFull(page);
		await page.route(DEVICES, (route) => route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"unavailable"}' }));
		await poll(page, 503);
		await expect.poll(async () => (await inventory(page)).banner, { message: "503 note is shown" }).toMatch(/Server error \(503\)/);
		const failed = await inventory(page);
		await shot(page, "resilience-3-503");
		expect(failed.gridTop, "the grid did not move").toBe(full.gridTop);
		expect(failed.subcards.length, "no card was removed").toBe(full.subcards.length);
		expect(failed.slices, "no battery slice was removed").toBe(full.slices);
		expectNoProblems(problems);
	});

	test("reload against empty answer restores stored layout; recovery in place @resilience", async ({ adminPage: page }) => {
		const { full, problems } = await loadFull(page);
		await page.route(DEVICES, emptyPayload);
		await page.reload();
		await page.waitForFunction(() => !document.getElementById("dashboard-grid").classList.contains("dashboard-grid--loading"), null, { timeout: 20_000 });
		const reloaded = await inventory(page);
		await shot(page, "resilience-4-reload-connecting");
		expect(reloaded.subcards.length, "stored cards are restored").toBe(full.subcards.length);
		expect(reloaded.slices, "stored battery slices are restored").toBe(full.slices);
		expect(reloaded.flow, "stored flow graphic is restored").toBe(true);
		expect(reloaded.gridTop, "grid position is restored").toBe(full.gridTop);
		// Valid data returns: values update in place, no rebuild.
		await page.unroute(DEVICES);
		await page.evaluate(() => { document.querySelector(".device-item").dataset.probe = "kept"; });
		await poll(page, 200);
		await page.waitForFunction(() => /2,035/.test(document.getElementById("pv-power").textContent), null, { timeout: 20_000 });
		const back = await inventory(page);
		expect(back.subcards.length, "valid data keeps the cards").toBe(full.subcards.length);
		expect(back.slices, "valid data keeps the battery slices").toBe(full.slices);
		expect(back.banner, "no banner after recovery").toBeNull();
		await expect(page.locator('.device-item[data-probe="kept"]'), "values update in the existing card, no rebuild").toHaveCount(1);
		expectNoProblems(problems);
	});
});
