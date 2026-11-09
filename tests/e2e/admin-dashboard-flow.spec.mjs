//
// tests/e2e/admin-dashboard-flow.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Dashboard values from the simulator cache and the per-card flow graphic. Replaces admin_flow.mjs
// lines 158-263 and 345-360 (the stored-layout check of 265-285 belongs to admin-gridstack).
import { expect, test } from "./support/fixtures.mjs";
import { forcePoll, openDashboard, pollOnce } from "./support/dashboard.mjs";
import { trackProblems } from "./support/problems.mjs";
test.describe("dashboard values and flow graphic @dashboard", () => {
	test("cached values are rendered as whole numbers with locale separators @dashboard", async ({ adminPage: page, gateway, browserName }) => {
		const watch = trackProblems(page, browserName, gateway.base);
		await openDashboard(page);
		const badge = await page.evaluate(() => {
			const card = document.querySelector(".device-item");
			const node = card?.querySelector(".status-badge-success");
			if (!node) return null;
			const cardRect = card.getBoundingClientRect();
			const badgeRect = node.getBoundingClientRect();
			return {
				text: node.textContent.trim(),
				top: badgeRect.top - cardRect.top,
				right: cardRect.right - badgeRect.right, // the remove button sits right of the badge
				dot: getComputedStyle(node, "::before").content !== "none",
			};
		});
		expect(badge, "connected device shows a status badge").not.toBeNull();
		expect(badge.text, "connected device shows a top-right status badge: text").toBe("Connected");
		expect(badge.top, "connected device badge sits at the top of the card").toBeLessThan(40);
		expect(badge.right, "connected device badge sits at the right of the card").toBeLessThan(100);
		expect(badge.dot, "connected device badge has a dot").toBe(true);
		const kpi = Object.fromEntries(await Promise.all(["pv-power", "grid-power", "battery-soc", "device-count", "connected-count"]
			.map(async (id) => [id, (await page.locator(`#${id}`).innerText()).trim()])));
		// Dashboard values are whole numbers; the separator follows the browser locale (en-GB context here).
		expect(kpi["pv-power"], "PV value from cache (1234.5 + 800 W) is rounded to a whole number").toMatch(/^2,035\s*W$/);
		const grid = await page.evaluate(() => ({
			text: document.getElementById("grid-power").textContent.trim(),
			label: document.querySelector("#grid-power .device-flow")?.getAttribute("aria-label"),
			card: document.querySelector(".device-item .device-flow")?.getAttribute("aria-label"),
			cardText: [...document.querySelectorAll(".device-item dt")].find((dt) => dt.textContent === "Grid power")?.nextElementSibling.textContent.trim(),
		}));
		expect(grid.text, "grid value (-250 W feed-in) is shown as magnitude with a feed-in indicator").toMatch(/^arrow_upward\s*250\s*W$/);
		expect(grid.text, "grid value carries no minus sign").not.toMatch(/[-−]/);
		expect(grid.label, "grid tile label").toBe("Feeding into the grid");
		expect(grid.card, "grid card label").toBe("Feeding into the grid");
		expect(grid.cardText, "grid value on the card").toMatch(/^arrow_upward\s*250\s*W$/);
		expect(kpi["battery-soc"], "battery ratio 0.55 shown as 55 %").toMatch(/^55\s*%/);
		expect(watch.problems, "no console, network or dialog problems").toEqual([]);
	});
	test("flow graphic: placement, animation, labels, status badges and colours @dashboard", async ({ adminPage: page }) => {
		await openDashboard(page);
		// Fed from device.energy_flow on /admin/api/devices, no second energy/devices fetch, present in every card
		// above the inverter/battery subcards, animated because the simulator's readings are all non-zero.
		await page.waitForSelector(".device-item .device-flow-graphic:not([hidden]) .energy-flow-svg");
		const flow = await page.evaluate(() => {
			const card = document.querySelector(".device-item");
			const box = card.querySelector(".device-flow-graphic");
			const cards = card.querySelector(".device-visual-cards");
			return {
				present: Boolean(box?.querySelector(".energy-flow-svg")),
				hidden: box?.hidden,
				aboveCards: Boolean(box && cards && box.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING),
				activeLines: [...box.querySelectorAll(".flow-line")].filter((line) => !line.classList.contains("is-idle")).length,
			};
		});
		expect(flow.present, "dashboard device card shows the flow graphic").toBe(true);
		expect(flow.hidden, "the flow graphic is not hidden").toBe(false);
		expect(flow.aboveCards, "the flow graphic sits above the inverter/battery subcards").toBe(true);
		expect(flow.activeLines, "the dashboard flow graphic animates from non-zero energy_flow data").toBeGreaterThan(0);
		// Label and animation must agree: a Charging/Discharging/Import/Export label means an active line.
		const consistency = await page.evaluate(() => {
			const lines = [...document.querySelector(".device-item .energy-flow-svg").querySelectorAll(".flow-line")];
			const moving = (index) => !lines[index].classList.contains("is-idle");
			const badge = (name) => [...document.querySelectorAll(".device-item .device-flow-graphic .flow-badge")]
				.find((b) => b.querySelector(".flow-badge-caption").textContent === name)?.querySelector(".flow-badge-state").textContent;
			return {
				battery: { word: badge("Battery"), moving: moving(2), charge: lines[2].classList.contains("flow-charge"), discharge: lines[2].classList.contains("flow-discharge") },
				grid: { word: badge("Grid"), moving: moving(1) },
			};
		});
		const batteryActive = ["Charging", "Discharging"].includes(consistency.battery.word);
		expect(consistency.battery.moving, `battery line animates exactly when the battery label says Charging/Discharging (${consistency.battery.word})`).toBe(batteryActive);
		if (consistency.battery.word === "Charging") expect(consistency.battery.charge, "a Charging label has the charge direction").toBe(true);
		if (consistency.battery.word === "Discharging") expect(consistency.battery.discharge, "a Discharging label has the discharge direction").toBe(true);
		expect(consistency.grid.moving, `grid line animates exactly when the grid label says Import/Feed-in (${consistency.grid.word})`)
			.toBe(["Import", "Feed-in"].includes(consistency.grid.word));
		// Status badges under the graphic must agree with the node labels (same readings and threshold).
		const badges = await page.evaluate(() => {
			const nodes = [...document.querySelectorAll(".device-item .device-flow-graphic .flow-badge:not([hidden])")];
			return {
				count: nodes.length,
				colored: nodes.every((b) => b.querySelector(".flow-badge-state").matches(".status-badge-success, .status-badge-neutral")),
			};
		});
		expect(badges.count, "dashboard flow graphic shows status badges").toBeGreaterThan(0);
		expect(badges.colored, "each flow status badge is success or neutral").toBe(true);
		// Colour semantics probed on disposable elements: the simulator's battery leg can be idle, so
		// flow-charge/flow-discharge may not be applied to any current DOM node.
		const colors = await page.evaluate(() => {
			const read = (cls) => {
				const probe = document.createElement("div");
				probe.className = `flow-line ${cls}`;
				document.body.append(probe);
				const value = getComputedStyle(probe).getPropertyValue("--flow-color").trim();
				probe.remove();
				return value;
			};
			return { pv: read("flow-pv"), discharge: read("flow-discharge"), import: read("flow-import"), charge: read("flow-charge") };
		});
		expect(colors.discharge, "discharge and PV/export share the same (green) flow colour").toBe(colors.pv);
		expect(colors.import, "import differs from the green flow colour").not.toBe(colors.pv);
		expect(colors.charge, "charge differs from the green flow colour").not.toBe(colors.pv);
		expect(colors.import, "import and charge use distinct flow colours").not.toBe(colors.charge);
	});
	test("dashboard poll makes no energy/devices request @dashboard", async ({ adminPage: page }) => {
		await openDashboard(page);
		const energyLegs = [];
		page.on("request", (r) => { if (r.url().includes("/admin/api/energy/devices")) energyLegs.push(r.url()); });
		// A completed forced poll (response received and rendered) replaces the former fixed 300 ms wait.
		await pollOnce(page);
		expect(energyLegs, "the dashboard poll makes no energy/devices request").toEqual([]);
	});
	test("forced stale readings do not dim flow badges; no external requests @dashboard", async ({ adminPage: page, gateway, browserName }) => {
		const watch = trackProblems(page, browserName, gateway.base);
		await openDashboard(page);
		let staleServed = 0;
		await page.route("**/admin/api/devices*", async (route) => {
			const response = await route.fetch();
			const mark = (node) => { if (node && typeof node === "object") { if ("stale" in node) node.stale = true; Object.values(node).forEach(mark); } return node; };
			staleServed += 1;
			await route.fulfill({ response, json: mark(await response.json()) });
		});
		await forcePoll(page);
		await expect.poll(() => staleServed, { message: "the stale rewrite served at least one poll", timeout: 20_000 }).toBeGreaterThan(0);
		// Measurement window (reason: flicker across several polls cannot be seen in one sample): 90 samples every
		// 250 ms in the page, the same 22.5 s as the former driver.
		const seen = await page.evaluate(() => new Promise((resolve) => {
			const states = new Set();
			let count = 0;
			const sample = () => {
				document.querySelectorAll(".device-item .flow-badge:not([hidden])")
					.forEach((n) => states.add(`${getComputedStyle(n).opacity}|${getComputedStyle(n.querySelector(".flow-badge-state")).color}`));
				if (++count >= 90) resolve([...states]); else setTimeout(sample, 250);
			};
			sample();
		}));
		await page.unroute("**/admin/api/devices*");
		expect(seen.length, "flow badges were sampled").toBeGreaterThan(0);
		for (const state of seen) expect(state.startsWith("1|"), `flow badge keeps full opacity while readings are forced stale (${state})`).toBe(true);
		expect(seen.length, `one colour per badge while readings are forced stale: ${JSON.stringify(seen)}`).toBeLessThanOrEqual(4);
		expect([...watch.external], "no external requests").toEqual([]);
		expect(watch.problems, "no console, network or dialog problems").toEqual([]);
	});
});
