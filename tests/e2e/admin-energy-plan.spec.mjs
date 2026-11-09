//
// tests/e2e/admin-energy-plan.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Energy page planning tabs: visibility over the Energy Manager mode matrix, the charging-plan chart, header,
// KPIs and the pending-plan polling. Replaces admin_flow.mjs lines 2360-2623. All plan data is stubbed.
// Own energy gateway with the required write registers approved and the hardware verified (the compact
// panel only renders for a ready device). The former driver also mocked the device readings; that mock was
// bypassed by the later device routes and is not needed here.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { chartsIn, clickThemeUntil, installPlanMocks } from "./support/energy-mocks.mjs";
import { acceptHardwareVerification, approveRequiredWrites } from "./support/energy-setup.mjs";
import { reportInverterAs, trackProblems } from "./support/problems.mjs";
const PLANNING_TABS = ["plan", "spot-market", "weather"];
test.describe.configure({ mode: "serial" });
test.describe("energy planning tabs and charging plan @energy", () => {
	const fresh = freshGateway("energy");
	let context;
	let page;
	let watch;
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser, { viewport: { width: 1280, height: 900 } }));
		watch = trackProblems(page, browserName, fresh.gateway.base);
		expect(await approveRequiredWrites(page.request), "required write registers approved").toBe(200);
		expect(await acceptHardwareVerification(page.request), "hardware verification accepted").toBe(200);
	});
	test.afterAll(async () => { await context?.close(); });

	// Tab visibility over the Energy Manager mode matrix. The planning tabs Charging plan, Spot Market and
	// Weather are shown together only while at least one inverter is Manual; External does not count.
	test("planning tabs: no first-paint flash, hidden for Off and External, shown for Manual @energy", async () => {
		let planMode = "off";
		await reportInverterAs(page, () => planMode);
		const tabItemHidden = (name) => page.evaluate((n) => document.querySelector(`#energy-tab-${n}`).parentElement.hidden, name);
		const paneHidden = (name) => page.evaluate((n) => document.querySelector(`#energy-pane-${n}`).hidden, name);
		// Mark the document before any energy JS runs so the first paint can be sampled.
		await page.addInitScript(() => {
			const flash = [];
			const sample = () => { for (const n of ["plan", "spot-market", "weather"]) { const li = document.querySelector(`#energy-tab-${n}`)?.parentElement; if (li && !li.hidden) flash.push(n); } };
			document.addEventListener("DOMContentLoaded", sample, { once: true });
			window.__energyTabFlash = flash;
		});
		await page.goto("/ui/energy");
		expect(await page.evaluate(() => window.__energyTabFlash), "no first-paint flash: the three planning tabs are hidden at DOMContentLoaded").toEqual([]);
		for (const [mode, visible] of [["off", false], ["external", false], ["manual", true]]) {
			planMode = mode;
			await page.reload();
			await page.waitForFunction((v) => document.querySelector("#energy-tab-plan").parentElement.hidden === !v, visible);
			for (const name of PLANNING_TABS) {
				expect(await tabItemHidden(name), `${mode}: ${name} tab <li> is ${visible ? "visible" : "hidden"}`).toBe(!visible);
				expect(await paneHidden(name), `${mode}: ${name} pane is ${visible ? "visible" : "hidden"}`).toBe(!visible);
			}
		}
		// With a Manual inverter, open each planning tab, then drop to External: the active tab must fall back
		// to Inverter and all three tabs hide again.
		for (const name of PLANNING_TABS) {
			planMode = "manual";
			await page.reload();
			await page.waitForFunction(() => document.querySelector("#energy-tab-plan").parentElement.hidden === false);
			await page.locator(`#energy-tab-${name}`).click();
			await page.waitForFunction((n) => document.querySelector(`#energy-tab-${n}`).classList.contains("active"), name);
			planMode = "external";
			await page.evaluate(() => window.refreshPlanDevices && window.refreshPlanDevices());
			await page.waitForFunction(() => document.querySelector("#energy-tab-inverter").classList.contains("active"));
			expect(await page.locator("#energy-tab-inverter").evaluate((t) => t.classList.contains("active")),
				`active ${name} tab falls back to Inverter when the last Manual inverter leaves Manual`).toBe(true);
			expect(await tabItemHidden(name), `the ${name} tab hides when the last Manual inverter leaves Manual`).toBe(true);
		}
		await page.unroute(/\/admin\/api\/energy\/(planning-)?devices$/);
	});

	test.describe("charging plan", () => {
		let mocks;
		const planChartState = () => page.evaluate(() => {
			const canvas = document.querySelector("#energy-plan-schedule canvas");
			return canvas && {
				role: canvas.getAttribute("role"), label: canvas.getAttribute("aria-label"), w: canvas.clientWidth, h: canvas.clientHeight,
				tick: window.Chart.getChart(canvas).options.scales.x.ticks.color,
			};
		});
		const device = () => page.locator("#energy-plan-device");
		const requestPlan = () => Promise.all([page.waitForResponse((r) => r.url().includes("/dynamic-plan")), device().dispatchEvent("change")]);
		const expectRequests = (n, message) => expect.poll(() => mocks.planRequests, { message, timeout: 5000, intervals: [100] }).toBe(n);
		test("plan chart renders accessibly at 1280, 390 and 320 px without horizontal overflow @energy", async () => {
			await reportInverterAs(page, () => "manual");
			mocks = await installPlanMocks(page);
			await page.goto("/ui/energy");
			await page.locator("#energy-tab-plan").click();
			await page.waitForSelector("#energy-plan-schedule canvas");
			await page.waitForSelector("#energy-consumption canvas");
			for (const width of [1280, 390, 320]) {
				await page.setViewportSize({ width, height: 844 });
				// Chart.js resizes through a ResizeObserver, so wait until the canvas follows the viewport.
				await page.waitForFunction((w) => { const c = document.querySelector("#energy-plan-schedule canvas").clientWidth; return c > 100 && c <= w; }, width);
				const state = await planChartState();
				expect(state.role, `plan chart is an image at ${width}px`).toBe("img");
				expect(state.label.length, `plan chart has an accessible name at ${width}px`).toBeGreaterThan(10);
				expect(state.w, `plan chart is wider than 100px at ${width}px`).toBeGreaterThan(100);
				expect(state.h, `plan chart is taller than 100px at ${width}px`).toBeGreaterThan(100);
				const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
				expect(overflow, `plan chart page has no horizontal scroll at ${width}px`).toBeLessThanOrEqual(0);
				// The compact Charging Plan header must not force horizontal overflow at 320/390px.
				const headOverflow = await page.evaluate(() => { const el = document.querySelector(".energy-plan-head"); return el ? el.scrollWidth - el.clientWidth : 0; });
				expect(headOverflow, `plan header has no horizontal overflow at ${width}px`).toBeLessThanOrEqual(1);
			}
			await page.setViewportSize({ width: 1280, height: 844 });
		});

		test("schedule details, header and KPI tiles @energy", async () => {
			await expect(page.locator("#energy-plan-schedule details:not([open]) summary"), "plan schedule has collapsed all-intervals details").toHaveText("Show all intervals");
			await expect(page.locator("#energy-plan-schedule details table").first(), "the collapsed details hide their table").toBeHidden();
			await expect(page.locator("#energy-plan-charge-schedule"), "there is no charge-window table").toHaveCount(0);
			await page.locator("#energy-plan-schedule details summary").click();
			expect(await page.locator("#energy-plan-schedule details table tbody tr").count(), "schedule details open to a table with rows").toBeGreaterThanOrEqual(1);
			await expect(page.locator("#energy-plan-schedule details th.text-end"), "schedule details table has two right-aligned numeric columns").toHaveCount(2);
			await page.locator("#energy-plan-schedule details summary").click();
			await page.locator("#energy-plan-schedule details summary").focus();
			await page.keyboard.press("Enter");
			const openedByKey = await page.locator("#energy-plan-schedule details").evaluate((d) => d.open);
			await page.keyboard.press("Enter");
			expect(openedByKey, "Show all intervals opens with the keyboard").toBe(true);
			expect(await page.locator("#energy-plan-schedule details").evaluate((d) => d.open), "Show all intervals closes with the keyboard").toBe(false);
			const switchGap = await page.evaluate(() => {
				const input = document.querySelector("#energy-plan-expert-mode").getBoundingClientRect();
				const label = document.querySelector('label[for="energy-plan-expert-mode"]').getBoundingClientRect();
				return Math.round(label.left - input.right);
			});
			expect(switchGap, `plan Expert mode label keeps a gap to its switch (${switchGap}px)`).toBeGreaterThanOrEqual(6);
			const title = page.locator(".energy-plan-head-title");
			expect(await title.innerText(), "plan header omits the removed read-only disclaimer").not.toMatch(/Read-only preview|No inverter commands/);
			await expect(page.locator("#energy-plan-readonly-note, #energy-plan-readonly-detail"), "the read-only note elements do not exist").toHaveCount(0);
			expect(await title.innerText(), "plan header is never labelled Disabled").not.toMatch(/Disabled/);
			await expect(page.locator("#energy-plan-manager-state"), "plan header has no Energy Manager mode badge").toHaveCount(0);
			expect(await title.innerText(), "plan header names no Manual or External control").not.toMatch(/Manual control|External control/);
			await expect(page.locator("#energy-plan-confidence-note"), "the removed \"Preliminary plan\" note element does not exist").toHaveCount(0);
			expect(await page.locator("#energy-plan-card").innerText(), "the removed \"Preliminary plan\" note text does not exist").not.toMatch(/Preliminary plan|limited consumption history/i);
			// Real backend contract value (adaptive_history + normal) must render as the normal-confidence chip.
			expect((await page.locator("#energy-plan-confidence").innerText()).trim(), "plan header shows Normal confidence from the real forecast contract value").toBe("Normal confidence");
			const hero = await page.locator("#energy-plan-result .energy-plan-highlights .spot-market-highlight-caption").allInnerTexts();
			expect(hero.map((t) => t.toLowerCase()).join("|"), "plan summary shows four plan KPI tiles in the pinned order").toBe("target soc|charge power|total plan grid energy|next grid charge");
			const batteryLine = await page.locator("#energy-plan-result .energy-plan-battery").first().innerText();
			expect(batteryLine, "Current battery state heading").toMatch(/Current battery state/);
			expect(batteryLine, "Current battery state shows SoC, remaining and usable kWh as a separate input line").toMatch(/33 % SoC · 3[.,]42 kWh remaining \/ 10[.,]37 kWh usable/);
			const columnsAt = async (width) => {
				await page.setViewportSize({ width, height: 844 });
				return page.evaluate(() => new Set([...document.querySelectorAll(".energy-plan-highlights .spot-market-highlight")].map((t) => Math.round(t.getBoundingClientRect().left))).size);
			};
			await expect.poll(() => columnsAt(1280), { message: "KPI tiles: 4 columns on desktop" }).toBe(4);
			await expect.poll(() => columnsAt(800), { message: "KPI tiles: 2 columns on tablet" }).toBe(2);
			await expect.poll(() => columnsAt(390), { message: "KPI tiles: stacked on mobile" }).toBe(1);
			await page.setViewportSize({ width: 1280, height: 844 });
			await expect.poll(() => page.evaluate(() => {
				const w = (id) => Math.round(document.getElementById(id).getBoundingClientRect().width);
				const widths = { result: w("energy-plan-result"), schedule: w("energy-plan-schedule"), consumption: w("energy-consumption-section") };
				return widths.result === widths.schedule && widths.schedule === widths.consumption ? "equal" : JSON.stringify(widths);
			}), { message: "summary, schedule and consumption history use the full card width" }).toBe("equal");
		});

		test("plan chart: one instance after reload, plain-language tooltip, theme-aware ticks @energy", async () => {
			await requestPlan();
			await expect.poll(() => chartsIn(page, "#energy-plan-schedule"), { message: "plan chart: exactly one Chart instance after reload" }).toBe(1);
			await page.locator("#energy-plan-schedule canvas").scrollIntoViewIfNeeded();
			const box = await page.locator("#energy-plan-schedule canvas").boundingBox();
			await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.5);
			await page.waitForFunction(() => window.Chart.getChart(document.querySelector("#energy-plan-schedule canvas")).tooltip.opacity > 0);
			const tooltip = await page.evaluate(() => window.Chart.getChart(document.querySelector("#energy-plan-schedule canvas")).tooltip.body.flatMap((b) => b.lines).join(" | "));
			expect(tooltip, "plan chart tooltip names the slot reason in plain language").toMatch(/(Solar covers|Battery can cover|protect the battery reserve|reach the target SoC|Negative price)/);
			const tickBefore = (await planChartState()).tick;
			const themeWas = await page.locator("html").getAttribute("data-bs-theme");
			await clickThemeUntil(page, themeWas === "dark" ? "light" : "dark");
			await page.waitForFunction((before) => window.Chart.getChart(document.querySelector("#energy-plan-schedule canvas")).options.scales.x.ticks.color !== before, tickBefore);
			expect((await planChartState()).tick, "theme toggle changes the plan chart tick colour").not.toBe(tickBefore);
			await clickThemeUntil(page, themeWas);
		});

		test("pending plans show a calm waiting state and are polled; configuration states keep their text @energy", async () => {
			await page.clock.install();
			mocks.planStatus = "spot_prices_unavailable";
			await device().dispatchEvent("change");
			await page.waitForSelector('#energy-plan-result [role="status"] .material-icons');
			const waitText = await page.locator('#energy-plan-result [role="status"]').first().innerText();
			expect(waitText, "a pending plan says it is being prepared").toMatch(/Plan is being prepared.*Please check back in a few minutes/s);
			expect(await page.locator("#energy-plan-result .material-icons").first().innerText(), "a pending plan shows an hourglass").toBe("hourglass_empty");
			expect(await page.getByText("Usable battery capacity").first().isVisible(), "a pending plan shows no readiness list in Basic mode").toBe(false);
			// The readiness card fell away entirely: every not-ok status now shows only the hourglass,
			// including battery_state_unavailable. Polling still continues.
			mocks.planStatus = "battery_state_unavailable";
			await requestPlan();
			await page.waitForSelector('#energy-plan-result [role="status"] .material-icons');
			const batteryText = await page.locator("#energy-plan-result").innerText();
			expect(batteryText, "a missing battery plan now shows the hourglass").toMatch(/Plan is being prepared.*Please check back in a few minutes/s);
			expect(batteryText, "a missing battery plan shows no readiness card").not.toMatch(/Usable battery capacity/);
			expect(batteryText, "a missing battery plan shows no connection hint").not.toMatch(/Check that the inverter is connected/);
			await expect(page.locator('#energy-plan-result [role="status"]'), "exactly one waiting status is shown").toHaveCount(1);
			expect(await page.getByText("A verified capacity override can be entered in Advanced diagnostics.").first().isVisible(), "no verified-capacity override hint survives in any mode").toBe(false);
			mocks.planStatus = "spot_prices_unavailable";
			await requestPlan();
			let before = mocks.planRequests;
			await page.clock.runFor(10_000);
			await expectRequests(before + 1, "a pending plan is re-requested automatically after 10 s");
			await page.clock.runFor(10_000);
			await expectRequests(before + 2, "polling never stacks timers: one request per interval");
			mocks.planStatus = "ok";
			await device().dispatchEvent("change");
			await page.waitForSelector("#energy-plan-schedule canvas");
			before = mocks.planRequests;
			await page.clock.runFor(60_000);
			await expectRequests(before, "polling stops on a device change and once the plan is ok");
			// spot_disabled also shows the hourglass (every not-ok status does); it is still not polled.
			mocks.planStatus = "spot_disabled";
			await device().dispatchEvent("change");
			await page.waitForSelector('#energy-plan-result [role="status"] .material-icons');
			before = mocks.planRequests;
			await page.clock.runFor(60_000);
			await expect(page.locator("#energy-plan-result .plan-loading"), "spot_disabled shows the hourglass").toHaveCount(1);
			await expectRequests(before, "spot_disabled is never polled");
			expect(await page.getByText("Usable battery capacity").first().isVisible(), "spot_disabled shows no readiness list").toBe(false);
			// solar_not_configured: hourglass PLUS a Weather-setup badge pointing at the postal code / PV data.
			mocks.planStatus = "solar_not_configured";
			await device().dispatchEvent("change");
			await page.waitForSelector("#energy-plan-result .plan-setup-badge");
			const solarText = await page.locator("#energy-plan-result").innerText();
			expect(solarText, "solar_not_configured shows the hourglass text").toMatch(/Plan is being prepared/);
			expect(solarText, "solar_not_configured shows the Weather-setup badge").toMatch(/Weather setup required/);
			expect(solarText, "solar_not_configured points at the postal code").toMatch(/postal code/);
			expect(await page.locator("#energy-plan-result .badge").count(), "solar_not_configured shows a badge").toBeGreaterThanOrEqual(1);
			mocks.planStatus = "solar_forecast_unavailable";
			await device().dispatchEvent("change");
			await page.waitForSelector('#energy-plan-result [role="status"] .material-icons');
			await page.waitForFunction(() => !document.querySelector("#energy-plan-result .plan-setup-badge"));
			await expect(page.locator("#energy-plan-result .plan-setup-badge"), "a configured-but-not-fetched forecast shows a plain hourglass, no badge").toHaveCount(0);
			mocks.planStatus = "ok";
			await device().dispatchEvent("change");
			await page.waitForSelector("#energy-plan-schedule canvas");
		});
	});

	test("the planning story raised no console, network or dialog problems @energy", async () => {
		expect(watch.problems, "no console errors, page errors, failed requests, HTTP >= 400 or native dialogs").toEqual([]);
		expect([...watch.external], "no request leaves the gateway").toEqual([]);
	});
});
