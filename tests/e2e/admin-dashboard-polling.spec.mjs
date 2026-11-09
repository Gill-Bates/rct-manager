//
// tests/e2e/admin-dashboard-polling.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Dashboard polling and failure handling: forced poll, collapsible card, toast de-duplication and
// animation, 429 load shedding, offline banner, reconnect modal. Replaces admin_flow.mjs lines 752-958
// (the CSP row 806 lives in admin-shell.spec.mjs). All faults are injected with route mocks in the page,
// so the shared worker gateway is never changed.
import { expect, test } from "./support/fixtures.mjs";
import { forcePoll, openDashboard, pollOnce } from "./support/dashboard.mjs";
import { expectedStatus, trackProblems } from "./support/problems.mjs";
import { frames } from "./support/settle.mjs";
import { elapse } from "./support/timing.mjs";
const DEVICES = "**/admin/api/devices";
const shot = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
const settledX = (matrix) => (matrix === "none" ? 0 : Number(matrix.match(/matrix\(([^)]*)\)/)?.[1].split(",")[4] ?? NaN));
test.describe("dashboard polling @polling", () => {
	test.beforeEach(async ({ adminPage: page }) => {
		await openDashboard(page);
	});
	test("forced poll patches the card in place; no manual refresh button @polling", async ({ adminPage: page }) => {
		// The visibilitychange dispatch takes the same automatic loadDashboard() path as the 10 s timer and the
		// tab-focus handler; parameters are not part of the loop.
		await page.evaluate(() => { document.querySelector(".device-item").dataset.probe = "kept"; });
		const paths = [];
		const onRequest = (request) => paths.push(new URL(request.url()).pathname);
		page.on("request", onRequest);
		await pollOnce(page);
		page.off("request", onRequest);
		await expect(page.locator('.device-item[data-probe="kept"]'), "forced refresh patches the inverter card in place").toHaveCount(1);
		expect(paths, "a poll requested the devices endpoint").toContain("/admin/api/devices");
		expect(paths, "periodic refresh does not request /admin/api/parameters").not.toContain("/admin/api/parameters");
		await expect(page.locator("#refresh-devices"), "no manual refresh button remains on the dashboard").toHaveCount(0);
	});
	test("collapsible device card persists across poll and reload @polling", async ({ adminPage: page }) => {
		const state = () => page.evaluate(() => {
			const card = document.querySelector(".device-item");
			const button = card.querySelector(".device-toggle");
			const body = document.getElementById(button.getAttribute("aria-controls"));
			const visible = (node) => node.getBoundingClientRect().height > 0;
			return {
				expanded: button.getAttribute("aria-expanded"), bodyVisible: visible(body),
				headVisible: visible(card.querySelector(".device-head")), titleVisible: visible(card.querySelector(".device-head h3")),
				stored: localStorage.getItem("rct-admin.collapsedDevices"),
			};
		});
		const expanded = await state();
		expect(expanded.expanded, "device card is expanded by default (aria-expanded)").toBe("true");
		expect(expanded.bodyVisible, "device card is expanded by default (body visible)").toBe(true);
		await page.locator(".device-item .device-toggle").first().focus();
		await page.keyboard.press("Enter");
		await expect.poll(async () => (await state()).bodyVisible, { message: "the body collapses" }).toBe(false);
		const collapsed = await state();
		expect(collapsed.expanded, "collapsing flips aria-expanded").toBe("false");
		expect(collapsed.headVisible, "collapsing keeps the header").toBe(true);
		expect(collapsed.titleVisible, "collapsing keeps the title").toBe(true);
		expect(collapsed.stored || "", "collapsed state is persisted in localStorage").toMatch(/\[".+"\]/);
		await pollOnce(page);
		expect((await state()).expanded, "a poll cycle does not reset the collapse").toBe("false");
		await page.reload();
		await page.waitForSelector(".device-item .device-toggle");
		const reloaded = await state();
		expect(reloaded.expanded, "collapsed state survives a page reload (aria-expanded)").toBe("false");
		expect(reloaded.bodyVisible, "collapsed state survives a page reload (body hidden)").toBe(false);
		expect(reloaded.headVisible, "collapsed state survives a page reload (header kept)").toBe(true);
		await page.locator(".device-item .device-toggle").first().click();
		await expect.poll(async () => (await state()).bodyVisible, { message: "the body expands" }).toBe(true);
		const restored = await state();
		expect(restored.expanded, "expanding restores aria-expanded").toBe("true");
		expect(restored.stored, "expanding clears the stored id").toBe("[]");
	});
	test("failed poll toasts once; slide-in and slide-out @polling", async ({ adminPage: page, gateway, browserName }) => {
		// A failing automatic poll (a server error here, not a 429, which is load shedding) must toast once with
		// the slide-in animation, and a second consecutive failure must not stack another identical toast.
		const watch = trackProblems(page, browserName, gateway.base);
		await expect(page.locator("#dashboard-updated"), 'no "Updated" timestamp line on the dashboard').toHaveCount(0);
		const start = watch.problems.length;
		const outageText = "e2e dashboard outage";
		let served = 0;
		await page.route(DEVICES, (route) => {
			served += 1;
			return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: outageText }) });
		});
		await forcePoll(page);
		const toast = page.locator("#toast-region .alert-danger", { hasText: outageText });
		await expect(toast.first(), "the failed poll raises a toast").toBeVisible({ timeout: 8000 });
		await expect(page.locator("#dashboard-staleness"), "no stale-data banner element exists on the page").toHaveCount(0);
		// The slide-in transition takes 300 ms (admin.css): wait for the settled state, not a fixed time.
		const animation = () => toast.first().evaluate((node) => {
			const style = getComputedStyle(node);
			return { visible: node.classList.contains("is-visible"), visibility: style.visibility, transform: style.transform };
		}).catch(() => null);
		await expect.poll(async () => {
			const a = await animation();
			return Boolean(a?.visible && a.visibility === "visible" && Math.abs(settledX(a.transform)) < 1);
		}, { message: "surviving toast slid in from the right and is visible (settled X offset 0)", timeout: 5000 }).toBe(true);
		expect(await toast.count(), "a failed poll produces exactly one toast").toBe(1);
		const before = served;
		await forcePoll(page);
		await expect.poll(() => served, { message: "the second automatic failure was served", timeout: 10_000 }).toBeGreaterThan(before);
		await frames(page);
		expect(await toast.count(), "a second consecutive automatic failure does not stack another toast").toBe(1);
		// Slide-out: dismissing the toast marks it leaving, moves it back to the right edge, then removes it.
		const leaving = toast.first();
		await leaving.locator(".btn-close").click();
		// The transform only starts moving on the next frame: observe per frame until it moved.
		const leave = await leaving.evaluate((node) => new Promise((resolve) => {
			const deadline = performance.now() + 1500;
			const poll = () => {
				const state = { leaving: node.classList.contains("is-leaving"), transform: getComputedStyle(node).transform };
				const x = Number(state.transform.match(/matrix\(([^)]*)\)/)?.[1].split(",")[4] ?? 0);
				if (x > 0 || performance.now() > deadline) resolve(state); else requestAnimationFrame(poll);
			};
			poll();
		}));
		expect(leave.leaving, "dismissed toast is marked leaving").toBe(true);
		expect(settledX(leave.transform), "dismissed toast slides out to the right").toBeGreaterThan(0);
		await expect(toast, "the dismissed toast is removed").toHaveCount(0, { timeout: 2000 });
		await page.unroute(DEVICES);
		await pollOnce(page);
		watch.scrub(start, expectedStatus(`${gateway.base}/admin/api/devices`, 503));
		expect(watch.problems, "no unexpected console, network or dialog problems").toEqual([]);
	});
	test("429 on automatic poll is load shedding: stale banner, cooldown, recovery @polling", async ({ adminPage: page, gateway, browserName }) => {
		// No toast, the dashboard goes stale ("Too many requests, retrying") and the page-wide cooldown keeps
		// further polls local until Retry-After is over; the next poll afterwards recovers and clears the note.
		const watch = trackProblems(page, browserName, gateway.base);
		const start = watch.problems.length;
		const retryAfterS = 6;
		const rate = { served: 0, shedding: true, firstAt: 0, recoveredAt: 0 };
		await page.route(DEVICES, (route) => {
			if (!rate.shedding) { rate.recoveredAt ||= Date.now(); return route.continue(); }
			rate.served += 1;
			rate.firstAt ||= Date.now();
			return route.fulfill({ status: 429, headers: { "Retry-After": String(retryAfterS) }, contentType: "application/json", body: JSON.stringify({ detail: "The request rate or the failed-authentication limit was exceeded." }) });
		});
		const dangerToasts = () => page.locator("#toast-region .alert-danger").count();
		const banner = page.locator(".dashboard-offline-banner");
		await forcePoll(page);
		await expect(banner, "a 429 marks the dashboard stale with the \"Too many requests, retrying\" banner").toContainText("Too many requests, retrying", { timeout: 8000 });
		// Measurement window (reason: absence of a toast cannot be awaited): a toast, if one were raised, would
		// appear within the render of the failed poll; the former driver waited 700 ms here.
		await elapse(700, "no toast appears after the 429");
		expect(await dangerToasts(), "a 429 on an automatic poll shows no toast").toBe(0);
		await expect(banner, "the stale banner is visible").toBeVisible();
		for (let i = 0; i < 3; i += 1) { await forcePoll(page); await frames(page); }
		await elapse(750, "polls during the Retry-After cooldown stay local (the former driver waited 3 x 250 ms)");
		expect(rate.served, "during the Retry-After cooldown no further devices request is sent").toBe(1);
		expect(await dangerToasts(), "repeated polls during the cooldown still show no toast").toBe(0);
		await expect(banner, "repeated polls during the cooldown keep the stale banner").toBeVisible();
		rate.shedding = false;
		await expect.poll(async () => {
			await forcePoll(page);
			return Boolean(rate.recoveredAt) && !(await banner.isVisible());
		}, { message: "after the cooldown the dashboard polls again and the stale banner is gone", timeout: 15_000, intervals: [400] }).toBe(true);
		expect(rate.recoveredAt - rate.firstAt, "the first request after the 429 waited at least Retry-After").toBeGreaterThanOrEqual((retryAfterS - 0.5) * 1000);
		expect(await dangerToasts(), "recovering from a 429 raised no toast either").toBe(0);
		await page.unroute(DEVICES);
		watch.scrub(start, expectedStatus(`${gateway.base}/admin/api/devices`, 429));
		expect(watch.problems, "no unexpected console, network or dialog problems").toEqual([]);
	});
	test("failed load keeps last KPI values under the offline banner @polling", async ({ adminPage: page, gateway, browserName }) => {
		// A failed load keeps the last known KPI values and shows the inline offline banner with the time of the last good snapshot.
		const watch = trackProblems(page, browserName, gateway.base);
		const kpiBefore = await page.locator("#pv-power").innerText();
		const start = watch.problems.length;
		await page.route(DEVICES, (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Administration is unavailable" }) }));
		await forcePoll(page);
		await page.waitForFunction(() => document.querySelector(".dashboard-offline-banner:not([hidden])"), null, { timeout: 20_000 });
		const banner = await page.evaluate(() => {
			const b = document.querySelector(".dashboard-offline-banner");
			return { position: getComputedStyle(b).position, text: b.textContent };
		});
		expect(banner.position, "the offline banner is inline").not.toBe("fixed");
		expect(banner.text, "the offline banner names the server error and the last good data time").toMatch(/Server error \(503\).*\d\d:\d\d/);
		expect(await page.locator("#pv-power").innerText(), "the last known KPI values are kept, not blanked").toBe(kpiBefore);
		await shot(page, "dashboard-failed-poll");
		await page.unroute(DEVICES);
		await forcePoll(page);
		await page.waitForFunction(() => !document.querySelector("#devices-list .text-danger"), null, { timeout: 20_000 });
		watch.scrub(start, (p) => p.includes(`${gateway.base}/admin/api/devices`)
			&& (p.startsWith("http 503: GET ") || (p.startsWith("console ") && p.includes("503"))));
		expect(watch.problems, "no unexpected console, network or dialog problems").toEqual([]);
	});
	test("connection loss modal closes on health probe, also after foreground return @polling", async ({ adminPage: page, gateway, browserName }) => {
		// Going offline opens the blocking modal (toasts cleared, page inert); the return of the connection is
		// detected by the /health probe, which closes the modal again.
		const watch = trackProblems(page, browserName, gateway.base);
		const context = page.context();
		const modalOpen = page.locator("#reconnect-modal.show");
		const reconnecting = () => page.evaluate(() => document.body.classList.contains("is-reconnecting"));
		const start = watch.problems.length;
		await context.setOffline(true);
		await expect(modalOpen, "connection loss opens the reconnect modal").toBeVisible({ timeout: 8000 });
		expect(await reconnecting(), "the page is marked reconnecting").toBe(true);
		const dialog = await page.evaluate(() => {
			const el = document.getElementById("reconnect-modal");
			return { modal: el.getAttribute("aria-modal"), role: el.getAttribute("role"), label: document.getElementById(el.getAttribute("aria-labelledby"))?.textContent.length };
		});
		expect(dialog.modal, "reconnect modal is aria-modal").toBe("true");
		expect(dialog.role, "reconnect modal has the dialog role").toBe("dialog");
		expect(dialog.label, "reconnect modal is labelled by a non-empty title").toBeGreaterThan(0);
		await shot(page, "connection-lost");
		await context.setOffline(false);
		await expect(modalOpen, "reconnect modal closes once the server answers again").toHaveCount(0, { timeout: 15_000 });
		expect(await reconnecting(), "reconnecting state is cleared once the server answers again").toBe(false);
		watch.scrub(start, (p) => p.startsWith("requestfailed: GET ")
			&& (p.includes(`${gateway.base}/health`) || p.includes(`${gateway.base}/admin/api/devices`))
			&& p.includes("ERR_INTERNET_DISCONNECTED"));
		// A probe that fails while the tab is hidden schedules no retry; returning to the foreground must resume
		// probing so the modal closes without any page-level polling.
		const setVisibility = (value) => page.evaluate((state) => {
			Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
			document.dispatchEvent(new Event("visibilitychange"));
		}, value);
		const probeStart = watch.problems.length;
		let healthFailures = 0;
		page.on("requestfailed", (r) => { if (new URL(r.url()).pathname === "/health") healthFailures += 1; });
		await page.route("**/health", (route) => route.abort());
		// Bootstrap ignores show() while the previous hide transition still runs: wait until it is over.
		await expect(page.locator(".modal-backdrop"), "the previous hide transition is over").toHaveCount(0);
		await page.evaluate(() => window.RCTReconnect.start());
		await expect(modalOpen, "the reconnect modal opens for the failing probe").toBeVisible({ timeout: 8000 });
		await expect.poll(() => healthFailures, { message: "the first probe failed", timeout: 8000 }).toBeGreaterThan(0);
		await setVisibility("hidden");
		// Measurement window (reason: the absence of a further probe cannot be awaited): the former driver waited 3.5 s,
		// longer than the retry delay of at most 3.6 s x 0.8, so a scheduled retry would have run and failed while hidden.
		await elapse(3500, "a retry scheduled before the tab was hidden has its chance to run while hidden");
		await page.unroute("**/health");
		await setVisibility("visible");
		await expect(modalOpen, "reconnect modal closes after returning to the foreground once the server is back").toHaveCount(0, { timeout: 15_000 });
		expect(await reconnecting(), "reconnecting state is cleared after returning to the foreground").toBe(false);
		await page.evaluate(() => { delete document.visibilityState; });
		watch.scrub(probeStart, (p) => p.includes(`${gateway.base}/health`) && (
			(p.startsWith("requestfailed: GET ") && p.includes("ERR_FAILED"))
			|| p.startsWith("console error: Failed to load resource: net::ERR_FAILED")));
		expect(watch.problems, "no unexpected console, network or dialog problems").toEqual([]);
	});
});
