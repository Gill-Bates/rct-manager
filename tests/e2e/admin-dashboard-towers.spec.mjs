//
// tests/e2e/admin-dashboard-towers.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Device card content: inverter image, battery slice stacks, status badges, per-tower subcards and the
// module-count rendering contract. Replaces admin_flow.mjs lines 286-343, 362-532 and 534-646.
// The simulator reports two battery towers (5 and 4 modules) with different SoC and status values.
import { expect, test } from "./support/fixtures.mjs";
import { forcePoll, layoutWhen, openDashboard, readLayout, waitForTowers } from "./support/dashboard.mjs";
import { settle } from "./support/settle.mjs";
const TOWER_HEIGHT_CAP = 207;
const DEVICES = "**/admin/api/devices";
// Rewrites the batteries of every device in the admin payload (the simulator reports one fixed pair).
const patchBatteries = (page, mutate) => page.route(DEVICES, async (route) => {
	const response = await route.fetch();
	const data = await response.json();
	for (const device of data.devices) mutate(device.batteries || []);
	await route.fulfill({ response, json: data });
});
const trustModuleCount = (page, modules) => patchBatteries(page, (batteries) => batteries.forEach((battery) => {
	battery.module_count = modules;
	battery.module_count_status = "ok";
	battery.populated_module_slots = [...Array(modules).keys()];
}));
test.describe("dashboard device card @dashboard", () => {
	test.beforeEach(async ({ adminPage: page, towersReady }) => {
		expect(towersReady, "both battery towers are available on the gateway").toBe(true);
		await openDashboard(page);
		await waitForTowers(page);
	});
	test("inverter SVG, battery slice stacks and shared status badge component @dashboard", async ({ adminPage: page }) => {
		// The inverter image sits beside the power card's readings; each battery tower is a stack of
		// top + (N-1) middle + bottom slices (app/admin/static/img/battery_{top,middle,bottom}.svg).
		await page.waitForFunction(() => {
			const images = [...document.querySelectorAll(".device-visual img")];
			return images.length >= 3 && images.every((image) => image.complete);
		});
		const images = await page.locator(".device-visual img").evaluateAll((list) => list.map((image) => ({
			src: new URL(image.src).pathname,
			alt: image.alt,
			loaded: image.complete && image.naturalWidth > 0,
			leftOfReadings: image.closest(".device-subcard-body").lastElementChild.getBoundingClientRect().left >= image.getBoundingClientRect().right,
		})));
		const inverter = images.filter((image) => image.src === "/admin/static/img/rct-inverter.svg");
		const slices = images.filter((image) => /\/admin\/static\/img\/battery_(top|middle|bottom)\.svg$/.test(image.src));
		const towerCount = await page.locator(".device-subcard-battery").count();
		expect(towerCount, "the simulator shows two battery towers").toBe(2);
		expect(inverter.length, "device card shows one inverter SVG").toBe(1);
		expect(slices.length, "at least top + bottom slice per tower").toBeGreaterThanOrEqual(2 * towerCount);
		expect(slices.filter((image) => image.src.endsWith("battery_top.svg")).length, "one top slice per tower").toBe(towerCount);
		expect(slices.filter((image) => image.src.endsWith("battery_bottom.svg")).length, "one bottom slice per tower").toBe(towerCount);
		for (const image of images) {
			expect(image.alt, `decorative image ${image.src} has an empty alt`).toBe("");
			expect(image.loaded, `image ${image.src} is loaded`).toBe(true);
			expect(image.leftOfReadings, `image ${image.src} sits left of its readings`).toBe(true);
		}
		// DOM order within the stack: top, then middles, then bottom (visual stack order top-down).
		const order = await page.locator(".device-battery-stack").first().evaluate((stack) =>
			[...stack.querySelectorAll("img")].map((image) => new URL(image.src).pathname.split("/").pop()));
		expect(order[0], "battery slice stack starts with the top slice").toBe("battery_top.svg");
		expect(order[order.length - 1], "battery slice stack ends with the bottom slice").toBe("battery_bottom.svg");
		for (const name of order.slice(1, -1)) expect(name, "slices between top and bottom are middle slices").toBe("battery_middle.svg");
		const texts = await page.$$eval(".device-subcard-head .status-badge", (list) => list.map((b) => b.textContent.trim()));
		expect(texts.some((t) => /feed in/i.test(t)), `inverter status badge is fully readable (feed_in test value): ${JSON.stringify(texts)}`).toBe(true);
		expect(texts.some((t) => /^balancing$/i.test(t)), `battery status badge is fully readable (balancing test value): ${JSON.stringify(texts)}`).toBe(true);
		// Every status badge shares one component: identical metrics, only the tone colours differ, and each
		// tone keeps WCAG AA text contrast (4.5:1) against the badge background composited on the page.
		const metrics = await page.evaluate(() => {
			const parse = (c) => (c.match(/[\d.]+/g) || []).map(Number);
			const lum = ([r, g, b]) => { const v = [r, g, b].map((x) => { x /= 255; return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; }); return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
			const under = (n) => { for (let e = n.parentElement; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.length < 4 || c[3] > 0) return c; } return [255, 255, 255, 1]; };
			return [...document.querySelectorAll(".device-item .status-badge")].map((n) => {
				const cs = getComputedStyle(n), dot = getComputedStyle(n, "::before");
				const fg = parse(cs.color), bg = parse(cs.backgroundColor), host = under(n);
				const a = bg.length > 3 ? bg[3] : 1;
				const mix = [0, 1, 2].map((k) => bg[k] * a + host[k] * (1 - a));
				const [hi, lo] = [lum(fg), lum(mix)].sort((x, y) => y - x);
				return {
					text: n.textContent.trim(), size: [cs.fontSize, cs.fontWeight, cs.padding, cs.borderRadius, cs.lineHeight, dot.width, dot.height].join("|"),
					height: Math.round(n.getBoundingClientRect().height * 10) / 10, opacity: getComputedStyle(n.closest(".flow-badge") || n).opacity, contrast: (hi + .05) / (lo + .05),
				};
			});
		});
		expect(metrics.length, "at least three status badges are on the card").toBeGreaterThanOrEqual(3);
		expect(new Set(metrics.map((b) => b.size)).size, `status badges share size, padding, radius, typography and dot: ${JSON.stringify(metrics.map((b) => [b.text, b.size]))}`).toBe(1);
		expect(new Set(metrics.map((b) => b.height)).size, `status badges share one height: ${JSON.stringify(metrics.map((b) => [b.text, b.height]))}`).toBe(1);
		for (const b of metrics) {
			expect(b.opacity, `status badge "${b.text}" is never dimmed`).toBe("1");
			expect(b.contrast, `status badge "${b.text}" keeps text contrast of at least 4.5:1`).toBeGreaterThanOrEqual(4.5);
		}
	});
	test("two simulated towers render distinct subcards; stale readings show no stale labels @dashboard", async ({ adminPage: page }) => {
		// Two distinct battery subcards with their own soc/temperature, not two copies of the same reading
		// (the duplicate-values regression this harness exists to catch).
		const towers = await page.evaluate(() => [...document.querySelectorAll(".device-subcard-battery")].map((card) => ({
			charge: card.querySelector(".device-charge-value")?.textContent.trim(),
			temperature: [...card.querySelectorAll(".device-metric-card")].find((cell) => cell.querySelector("dt")?.textContent.trim() === "Temperature")
				?.querySelector("dd")?.textContent.trim(),
			middleSlices: card.querySelectorAll('.device-battery-stack img[src$="battery_middle.svg"]').length,
		})));
		expect(towers.length, "two battery subcards").toBe(2);
		expect(towers[0].charge, `distinct charge levels ${JSON.stringify(towers)}`).not.toBe(towers[1].charge);
		expect(towers[0].temperature, `distinct temperatures ${JSON.stringify(towers)}`).not.toBe(towers[1].temperature);
		expect(towers[0].middleSlices, `distinct module counts ${JSON.stringify(towers)}`).not.toBe(towers[1].middleSlices);
		let stale = 0;
		await page.route(DEVICES, async (route) => {
			const response = await route.fetch();
			const data = await response.json();
			for (const device of data.devices) for (const metric of device.metrics) { metric.stale = true; stale += 1; }
			await route.fulfill({ response, json: data });
		});
		await page.reload();
		await page.waitForFunction(() => document.getElementById("grid-power")?.textContent.includes("250")
			&& [...document.querySelectorAll(".device-subcard-head .status-badge")].some((badge) => badge.textContent.trim().length > 0));
		// The route must have served the page: poll the counter instead of sleeping.
		await expect.poll(() => stale, { message: "the stale rewrite served the reload", timeout: 20_000 }).toBeGreaterThan(0);
		const ui = await page.locator("#dashboard-grid, .device-visual").evaluateAll((nodes) =>
			nodes.map((node) => `${node.textContent} ${[...node.querySelectorAll("[title]")].map((child) => child.title).join(" ")}`).join(" "));
		expect(ui, "stale API readings display values without stale labels or titles").not.toMatch(/\bstale\b/i);
		await page.unroute(DEVICES);
	});
	test("device card header, power subcard and per-tower subcards @dashboard", async ({ adminPage: page }) => {
		const layout = await readLayout(page);
		expect(layout.headHeight, "card header is compact").toBeLessThanOrEqual(60);
		expect(layout.power.title, "the power subcard is labelled").toBe("Inverter / Power");
		expect(layout.power.columns, "the power subcard carries the inverter facts in two columns").toBe(2);
		for (const label of ["PV A", "PV B", "Grid power", "AC power"]) expect(layout.power.labels, `power subcard shows ${label}`).toContain(label);
		expect(layout.power.chip, "the inverter status is shown on the power subcard chip").toMatch(/feed in/i);
		expect(layout.towers.map((t) => t.title), "one subcard per battery tower, titled Battery 1 and Battery 2").toEqual(["Battery 1", "Battery 2"]);
		expect(layout.towers[0].charge, "tower 1 shows its own charge level (55 %)").toMatch(/^55\s*%$/);
		expect(layout.towers[1].charge, "tower 2 shows its own charge level (42 %)").toMatch(/^42\s*%$/);
		expect(layout.towers[0].chip, "tower 1 shows its own status (balancing)").toMatch(/^balancing$/i);
		expect(layout.towers[1].chip, "tower 2 shows its own status (syncing)").toMatch(/^syncing$/i);
		expect(layout.towers[0].chip, "each tower shows its own status, not a shared one").not.toBe(layout.towers[1].chip);
		expect(layout.towers[0].middles, "tower 1 has 5 modules: 4 middle slices").toBe(4);
		expect(layout.towers[1].middles, "tower 2 has 4 modules: 3 middle slices").toBe(3);
		for (const tower of layout.towers) expect(tower.slices, `${tower.title}: slices = middles + top + bottom`).toBe(tower.middles + 2);
		// battery_cycles, battery_soc_target and power_mng_bat_next_calib_date have no placeholder equivalent in the
		// catalog, so they are shown once (first tower) instead of being duplicated or invented for the second.
		for (const tower of layout.towers) expect(tower.labels, `${tower.title}: temperature is per tower`).toContain("Temperature");
		for (const label of ["Charge cycles", "SOC target"]) expect(layout.towers[0].labels, `device-wide ${label} appears on the first tower`).toContain(label);
		for (const label of ["Charge cycles", "SOC target", "Next calibration"]) expect(layout.towers[1].labels, `device-wide ${label} appears only once`).not.toContain(label);
		// The slices are flush (no transparent trailing space inside them) and the shadow sits on the assembled tower.
		for (const tower of layout.towers) {
			for (const seam of tower.seams) expect(Math.abs(seam), `${tower.title}: battery slices butt together with no visible seam`).toBeLessThanOrEqual(0.5);
			expect(tower.sliceWidths.length, `${tower.title}: all slices share one width ${JSON.stringify(tower.sliceWidths)}`).toBe(1);
			expect(tower.stackShadow, `${tower.title}: one drop shadow is applied to the assembled tower`).toMatch(/drop-shadow/);
		}
		// The two subcards no longer share one 4rem image column.
		expect(layout.power.bodyTracks[0], "the inverter image column is wider than the battery tower column").toBeGreaterThan(layout.towers[0].bodyTracks[0]);
		expect(layout.power.bodyTracks[0], "the inverter image column is at least 120px").toBeGreaterThanOrEqual(120);
		expect(layout.power.image.width, "the inverter image is at least 110px wide").toBeGreaterThanOrEqual(110);
		expect(layout.power.image.height, "the inverter image is at least 120px high").toBeGreaterThanOrEqual(120);
		expect(layout.chargeSize, "charge level and the power values keep comparable sizes").toBeGreaterThanOrEqual(layout.power.valueSize);
		expect(Math.abs(layout.towers[0].box.width - layout.towers[1].box.width), "battery cards share their row evenly").toBeLessThanOrEqual(1);
		// The inverter state is shown exactly once per card, not repeated in the header.
		const mentions = await page.evaluate(() => [...document.querySelectorAll(".device-item *")]
			.filter((n) => !n.children.length && /feed in/i.test(n.textContent)).map((n) => `${n.tagName}.${n.className}|${n.parentElement?.className}|${n.textContent.trim()}`));
		expect(mentions.length, `inverter state is shown exactly once per card: ${JSON.stringify(mentions)}`).toBe(1);
	});
	test("module counts 2..6, anomaly notes and pending placeholder @dashboard", async ({ adminPage: page }) => {
		// Both are driven through the admin payload, because the simulated inverter reports one fixed pair of towers.
		const towersOf = async (signal, message) => (await layoutWhen(page, (layout) => layout.towers.length > 0 && layout.towers.every(signal), message)).towers;
		const towerGeometry = async (signal, message) => {
			await forcePoll(page);
			const towers = await towersOf(signal, message);
			await settle(page, "tower geometry settles");
			return (await readLayout(page)).towers;
		};
		// The dashboard keeps a tower's last trusted module count when a later answer is not 'ok' (resilience
		// by design), so a first-time anomaly/pending answer needs a view without that memory and a stored snapshot.
		const freshDashboardView = async () => {
			await page.evaluate(() => localStorage.removeItem("rct.dashboard.snapshot"));
			await page.reload();
			await page.waitForSelector(".device-item .device-battery-stack, .device-item .device-subcard");
		};
		// Graphic contract (owner-mandated): the bottom slice is itself a module, so N modules are drawn as
		// 1 top cap + N-1 middle segments + 1 bottom; every count up to the hardware maximum of 6.
		for (const modules of [2, 3, 4, 5, 6]) {
			await trustModuleCount(page, modules);
			const towers = await towerGeometry((tower) => tower.middles === modules - 1, `towers render ${modules} modules`);
			await page.unroute(DEVICES);
			expect(towers.length, `${modules}-module towers are drawn`).toBeGreaterThan(0);
			for (const tower of towers) {
				expect(tower.middles, `a ${modules}-module tower shows all its modules`).toBe(modules - 1);
				expect(tower.stack.height, `a ${modules}-module tower stays within the ${TOWER_HEIGHT_CAP}px height cap`).toBeLessThanOrEqual(TOWER_HEIGHT_CAP + 1);
				expect(tower.stack.height, `a ${modules}-module tower has a height`).toBeGreaterThan(0);
				expect(tower.stack.width, `a ${modules}-module tower has a width`).toBeGreaterThan(0);
				expect(tower.stack.width, `a ${modules}-module tower is at most 80px wide`).toBeLessThanOrEqual(80);
			}
		}
		await trustModuleCount(page, 6);
		const six = await towerGeometry((tower) => tower.middles === 5, "towers render 6 modules");
		await page.unroute(DEVICES);
		expect(six.length, "6-module towers are drawn").toBeGreaterThan(0);
		for (const tower of six) {
			expect(tower.middles, "a trusted 6-module tower has 5 middles").toBe(5);
			expect(tower.slices, "a trusted 6-module tower is top + 5 middles + bottom").toBe(7);
			expect(tower.noteHidden, `a trusted 6-module tower has no capping note (${tower.noteText})`).toBe(true);
			expect(tower.stack.height, "a trusted 6-module tower stays within the height cap").toBeLessThanOrEqual(TOWER_HEIGHT_CAP + 1);
		}
		// Taller towers must get narrower rather than taller: that is what keeps the cap without dropping modules.
		const widthAt = async (modules) => {
			await trustModuleCount(page, modules);
			const towers = await towerGeometry((tower) => tower.middles === modules - 1, `towers render ${modules} modules`);
			await page.unroute(DEVICES);
			return towers[0].stack.width;
		};
		const widthTwo = await widthAt(2);
		const widthFive = await widthAt(5);
		expect(widthFive, `a five-module tower is drawn narrower than a two-module one instead of taller (${widthTwo} vs ${widthFive})`).toBeLessThan(widthTwo);
		// Seven populated slots are a data anomaly (the catalog array has seven elements, the documented hardware takes
		// six modules), and a gap in the middle is one too. Neither may render a seven-storey tower.
		for (const [name, slots] of [["seven populated slots", [0, 1, 2, 3, 4, 5, 6]], ["a gap in the middle", [0, 1, 4]]]) {
			await patchBatteries(page, (batteries) => batteries.forEach((battery) => {
				battery.module_count = null;
				battery.module_count_status = "anomaly";
				battery.populated_module_slots = slots;
			}));
			await freshDashboardView();
			const towers = await towerGeometry((tower) => tower.slices === 0 && tower.noteHidden === false, `${name}: diagnostic note shown`);
			await page.unroute(DEVICES);
			expect(towers.length, `${name}: towers are present`).toBeGreaterThan(0);
			for (const tower of towers) {
				expect(tower.slices, `${name} renders no tower`).toBe(0);
				expect(tower.noteHidden, `${name} renders a diagnostic note`).toBe(false);
				expect(tower.charge, `${name} keeps the card readings`).toMatch(/^55\s*%$|^42\s*%$/);
			}
		}
		// A scan still in progress (module_count_status 'pending', no trusted count yet) must render a neutral
		// "detecting modules" placeholder, never a fabricated one-module tower nor the "layout unclear" anomaly text.
		await patchBatteries(page, (batteries) => batteries.forEach((battery) => {
			battery.module_count = null;
			battery.module_count_status = "pending";
			battery.populated_module_slots = [];
		}));
		await freshDashboardView();
		const pending = await towerGeometry((tower) => tower.slices === 0 && tower.noteHidden === false, "pending placeholder shown");
		await page.unroute(DEVICES);
		expect(pending.length, "pending: towers are present").toBeGreaterThan(0);
		for (const tower of pending) {
			expect(tower.slices, "a mid-scan tower renders no one-module tower").toBe(0);
			expect(tower.noteHidden, "a mid-scan tower renders a placeholder").toBe(false);
			expect(tower.noteText, "a mid-scan tower renders a neutral \"detecting modules\" placeholder").toMatch(/detecting modules/i);
		}
		await forcePoll(page);
	});
});
