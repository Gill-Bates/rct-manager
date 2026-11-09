//
// tests/e2e/support/dashboard.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Dashboard helpers shared by the admin-dashboard specs (state signals instead of fixed sleeps).
import { expect } from "@playwright/test";
/** A dashboard poll without waiting out the 10 s timer; the dashboard listens for visibilitychange. */
export const forcePoll = (page) => page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
/** Load /ui/dashboard and wait for the full simulator state (cards, flow graphic, cached KPI values). */
export async function openDashboard(page) {
	await page.goto("/ui/dashboard");
	await page.waitForFunction(() => document.querySelectorAll(".device-subcard").length >= 3
		&& document.querySelector(".device-flow-graphic:not([hidden])")
		&& /2,035/.test(document.getElementById("pv-power")?.textContent || "")
		&& /250/.test(document.getElementById("grid-power")?.textContent || "")
		&& !document.getElementById("dashboard-grid").classList.contains("dashboard-grid--loading"), null, { timeout: 60_000 });
}
/** Wait until both simulated battery towers are rendered with charge level, status and temperature. */
export const waitForTowers = (page) => page.waitForFunction(() => {
	const cards = [...document.querySelectorAll(".device-subcard-battery")];
	return cards.length >= 2 && cards.every((card) => /\d/.test(card.querySelector(".device-charge-value")?.textContent || "")
		&& (card.querySelector(".device-subcard-head .status-badge")?.textContent.trim() || "").length > 0
		&& [...card.querySelectorAll("dt")].some((dt) => dt.textContent.trim() === "Temperature"));
}, null, { timeout: 45_000 });
/** Force a poll, wait for its response and for the stored snapshot to advance (the render happened). */
export async function pollOnce(page, status = 200) {
	const before = await page.evaluate(() => JSON.parse(localStorage.getItem("rct.dashboard.snapshot") || "null")?.at || 0);
	const response = page.waitForResponse((r) => new URL(r.url()).pathname === "/admin/api/devices" && r.status() === status, { timeout: 20_000 });
	await forcePoll(page);
	await response;
	if (status === 200) {
		await page.waitForFunction((previous) => (JSON.parse(localStorage.getItem("rct.dashboard.snapshot") || "null")?.at || 0) > previous, before, { timeout: 20_000 });
	}
}
/** Geometry/content of the first device card, measured in the browser. */
export const readLayout = (page) => page.evaluate(() => {
	const text = (node) => (node ? node.textContent.trim() : null);
	const box = (node) => {
		const r = node.getBoundingClientRect();
		return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: Math.round(r.width), height: Math.round(r.height) };
	};
	const labels = (node) => [...node.querySelectorAll("dt")].map((dt) => dt.textContent.trim());
	const title = (card) => text(card.querySelector(".device-subcard-title span:last-of-type"));
	const chip = (card) => {
		const node = card.querySelector(".device-subcard-head .status-badge");
		return node && !node.hidden ? text(node) : null;
	};
	const card = document.querySelector(".device-item");
	const visual = card.querySelector(".device-visual");
	const power = card.querySelector(".device-subcard-power");
	const powerImage = power.querySelector(".device-subcard-image");
	const readTower = (tower) => {
		const stack = tower.querySelector(".device-battery-stack");
		const slices = [...stack.querySelectorAll("img")];
		return {
			title: title(tower), chip: chip(tower),
			charge: text(tower.querySelector(".device-charge-value")),
			labels: labels(tower),
			cells: tower.querySelectorAll(".device-metric-card").length,
			middles: slices.filter((image) => image.src.endsWith("battery_middle.svg")).length,
			slices: slices.length,
			stack: box(stack),
			// Seams: consecutive slices must touch exactly, with no transparent strip between them.
			seams: slices.slice(1).map((image, index) => Math.round((image.getBoundingClientRect().top
				- slices[index].getBoundingClientRect().bottom) * 100) / 100),
			sliceWidths: [...new Set(slices.map((image) => Math.round(image.getBoundingClientRect().width)))],
			// One shadow for the whole tower, none on the individual slices.
			stackShadow: getComputedStyle(stack).filter,
			noteHidden: tower.querySelector(".device-battery-note").hidden,
			noteText: text(tower.querySelector(".device-battery-note")),
			box: box(tower),
			bodyTracks: getComputedStyle(tower.querySelector(".device-subcard-body")).gridTemplateColumns
				.split(" ").map((track) => Math.round(parseFloat(track))),
		};
	};
	return {
		visualColumns: getComputedStyle(visual).gridTemplateColumns.split(" ").length,
		batteryCountVar: visual.style.getPropertyValue("--battery-count").trim(),
		headHeight: Math.round(card.querySelector(".device-head").getBoundingClientRect().height),
		power: {
			title: title(power), chip: chip(power), labels: labels(power),
			cells: power.querySelectorAll(".device-metric-card").length,
			columns: getComputedStyle(power.querySelector(".device-metric-grid")).gridTemplateColumns.split(" ").length,
			image: box(powerImage),
			bodyTracks: getComputedStyle(power.querySelector(".device-subcard-body")).gridTemplateColumns
				.split(" ").map((track) => Math.round(parseFloat(track))),
			box: box(power),
			valueSize: parseFloat(getComputedStyle(power.querySelector(".device-metric-card dd")).fontSize),
		},
		towers: [...card.querySelectorAll(".device-subcard-battery")].map(readTower),
		chargeSize: parseFloat(getComputedStyle(card.querySelector(".device-charge-value")).fontSize),
	};
});
/** Poll until `predicate(layout)` holds, then return that layout (render signal, not a sleep). */
export async function layoutWhen(page, predicate, message) {
	let last = null;
	await expect.poll(async () => {
		last = await readLayout(page);
		return predicate(last);
	}, { message, timeout: 20_000, intervals: [100, 200, 400] }).toBe(true);
	return last;
}
