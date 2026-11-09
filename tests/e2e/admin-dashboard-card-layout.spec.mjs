//
// tests/e2e/admin-dashboard-card-layout.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Dashboard card layout at narrow widths and the serial number line of the device header.
// Replaces admin_flow.mjs lines 648-750 (reflow, stacked subcards, footer clearance, serial number).
import { expect, test } from "./support/fixtures.mjs";
import { forcePoll, openDashboard, waitForTowers } from "./support/dashboard.mjs";
import { settle } from "./support/settle.mjs";
const MIN_FOOTER_GAP = 8;
test.describe("dashboard card layout @dashboard @layout", () => {
	test.beforeEach(async ({ adminPage: page, towersReady }) => {
		expect(towersReady, "both battery towers are available on the gateway").toBe(true);
		await openDashboard(page);
		await waitForTowers(page);
	});
	const resize = async (page, width, height) => {
		await page.setViewportSize({ width, height });
		await settle(page, `layout settles at ${width}px`);
	};
	test("narrow cards reflow, subcards stack, content clears the fixed footer @dashboard @layout", async ({ adminPage: page }) => {
		await resize(page, 1000, 800);
		const twoCards = await page.evaluate(() => {
			const grid = document.querySelector(".device-grid");
			const copy = grid.firstElementChild.cloneNode(true);
			grid.append(copy);
			const result = [...grid.querySelectorAll(".device-item")].map((card) => {
				const subcards = [...card.querySelectorAll(".device-subcard")].map((node) => node.getBoundingClientRect());
				return {
					width: Math.round(card.getBoundingClientRect().width),
					subcards: subcards.length,
					// Below the 56rem container width the subcards reflow to two columns, so the last one wraps onto a second row.
					wrapped: subcards.some((rect) => rect.top > subcards[0].bottom - 1),
				};
			});
			copy.remove();
			return result;
		});
		expect(twoCards.length, "two cards are measured").toBe(2);
		for (const card of twoCards) {
			expect(card.subcards, `a card has three subcards: ${JSON.stringify(twoCards)}`).toBe(3);
			expect(card.wrapped, `two narrow cards reflow their subcards onto more than one row at 1000px: ${JSON.stringify(twoCards)}`).toBe(true);
		}
		await resize(page, 390, 844);
		const narrow = await page.evaluate(() => ({
			overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
			cardHeight: Math.round(document.querySelector(".device-item").getBoundingClientRect().height),
		}));
		expect(narrow.overflow, `no horizontal overflow at 390px: ${JSON.stringify(narrow)}`).toBe(0);
		// Below the 19rem container width (@container on .device-subcard) .device-subcard-body collapses to a single
		// track, so the illustration moves above the readings, stays centred and never widens the card.
		const measureImages = () => page.evaluate(() => {
			const columns = (node) => getComputedStyle(node).gridTemplateColumns.split(" ").length;
			return [...document.querySelectorAll(".device-subcard")].map((card) => {
				const body = card.querySelector(".device-subcard-body");
				const visual = card.querySelector(".device-subcard-image, .device-battery-stack").getBoundingClientRect();
				const readings = card.querySelector(".device-metric-grid, .device-subcard-readings");
				const cardBox = card.getBoundingClientRect();
				return {
					kind: card.classList.contains("device-subcard-power") ? "power" : "battery",
					tracks: columns(body),
					width: Math.round(visual.width), height: Math.round(visual.height),
					stacked: card.clientWidth - parseFloat(getComputedStyle(card).paddingLeft) - parseFloat(getComputedStyle(card).paddingRight) <= 19 * 16,
					inside: visual.left >= cardBox.left - 1 && visual.right <= cardBox.right + 1,
					aboveReadings: visual.bottom <= readings.getBoundingClientRect().top + 1,
					centred: Math.abs((visual.left - cardBox.left) - (cardBox.right - visual.right)) <= 2,
				};
			});
		});
		for (const width of [390, 320]) {
			await resize(page, width, 844);
			const images = await measureImages();
			const detail = JSON.stringify(images);
			expect(images.length, `three subcards at ${width}px`).toBe(3);
			for (const image of images) {
				expect(image.tracks, `${image.kind} subcard body follows the container layout at ${width}px: ${detail}`).toBe(image.stacked ? 1 : 2);
				expect(image.width, `${image.kind} illustration has a width at ${width}px`).toBeGreaterThan(0);
				expect(image.height, `${image.kind} illustration has a height at ${width}px`).toBeGreaterThan(0);
				expect(image.inside, `${image.kind} illustration stays inside the card at ${width}px: ${detail}`).toBe(true);
				if (image.stacked) {
					expect(image.aboveReadings, `${image.kind} illustration sits above the readings at ${width}px: ${detail}`).toBe(true);
					expect(image.centred, `${image.kind} illustration is centred at ${width}px: ${detail}`).toBe(true);
				}
			}
			if (width === 320) {
				for (const image of images) expect(image.stacked, `${image.kind} subcard is stacked (illustration above readings) at 320px: ${detail}`).toBe(true);
			}
		}
		// The footer is fixed to the viewport bottom while .app-shell reserves --rct-footer-height there, so the last
		// card must keep a visible gap above it; `reserveShortfall` must be <= 0 so a taller final element cannot slide under it.
		for (const width of [390, 320]) {
			await resize(page, width, 844);
			// WebKit applies the resized layout late: scroll until the page really is at the bottom.
			await expect.poll(async () => {
				await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
				return page.evaluate(() => Math.abs(window.scrollY + window.innerHeight - document.documentElement.scrollHeight) <= 1);
			}, { message: `page is scrolled to the bottom at ${width}px`, timeout: 10_000 }).toBe(true);
			const bottom = await page.evaluate(() => {
				const content = document.querySelector("#devices-list .device-item:last-child").getBoundingClientRect();
				const footer = document.querySelector(".admin-footer").getBoundingClientRect();
				const reserved = parseFloat(getComputedStyle(document.querySelector(".app-shell")).paddingBottom);
				return {
					contentBottom: content.bottom, footerTop: footer.top, footerHeight: footer.height, footerBottom: footer.bottom,
					gap: footer.top - content.bottom, reserved, innerHeight: window.innerHeight,
					reserveShortfall: footer.height - reserved,
					clipped: footer.top < 0 || footer.bottom > window.innerHeight + 1,
				};
			});
			const detail = JSON.stringify(bottom);
			expect(bottom.gap, `dashboard content clears the fixed footer at ${width}px: ${detail}`).toBeGreaterThanOrEqual(MIN_FOOTER_GAP);
			expect(Math.abs(bottom.footerBottom - bottom.innerHeight), `the footer sits at the viewport bottom at ${width}px: ${detail}`).toBeLessThanOrEqual(1);
			expect(bottom.clipped, `the footer is not clipped at ${width}px: ${detail}`).toBe(false);
			expect(bottom.reserved, `the shell reserves space for the footer at ${width}px: ${detail}`).toBeGreaterThan(0);
			expect(bottom.reserveShortfall, `the reserve covers the footer height at ${width}px: ${detail}`).toBeLessThanOrEqual(0);
		}
	});
	test("serial number line in the device header @dashboard", async ({ adminPage: page }) => {
		// The serial number sits in the second header line between address and last connection, and is omitted
		// without a stray separator or "undefined" while the device has not reported one.
		const metaLine = () => page.evaluate(() => {
			const meta = document.querySelector(".device-item .device-head-meta");
			return { items: [...meta.children].map((node) => node.textContent.trim()), text: meta.textContent, scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth };
		});
		await page.setViewportSize({ width: 1280, height: 800 });
		await forcePoll(page);
		await expect(page.locator(".device-item .device-head-meta"), "the serial number is reported").toContainText("S/N: SIM1234567", { timeout: 20_000 });
		const withSerial = await metaLine();
		expect(withSerial.items.length, `the device header has address, serial number and last connection: ${JSON.stringify(withSerial.items)}`).toBe(3);
		expect(withSerial.items[0], "the first header item is the address with a port").toMatch(/:\d+$/);
		expect(withSerial.items[1], "the serial number sits between address and last connection").toBe("S/N: SIM1234567");
		expect(withSerial.items[2].startsWith("Last connection: "), `the last connection comes last: ${JSON.stringify(withSerial.items)}`).toBe(true);
		await resize(page, 390, 844);
		const narrowSerial = await metaLine();
		expect(narrowSerial.scrollW, `the serial number line wraps at 390px without horizontal scroll: ${JSON.stringify(narrowSerial)}`).toBeLessThanOrEqual(narrowSerial.clientW);
		await page.setViewportSize({ width: 1280, height: 800 });
		await page.route("**/admin/api/devices", async (route) => {
			const response = await route.fetch();
			const data = await response.json();
			for (const device of data.devices) device.serial_number = null;
			await route.fulfill({ response, json: data });
		});
		await forcePoll(page);
		await expect(page.locator(".device-item .device-head-meta"), "the unknown serial number disappears from the header").not.toContainText("S/N", { timeout: 20_000 });
		const withoutSerial = await metaLine();
		expect(withoutSerial.items.length, `an unknown serial number leaves two header items: ${JSON.stringify(withoutSerial.items)}`).toBe(2);
		expect(withoutSerial.text, "an unknown serial number leaves no stray text").not.toMatch(/undefined|null|S\/N/);
		expect(withoutSerial.items[0].length, "the address item is not empty").toBeGreaterThan(0);
		expect(withoutSerial.items[1].startsWith("Last connection: "), `the last connection follows the address: ${JSON.stringify(withoutSerial.items)}`).toBe(true);
		await page.unroute("**/admin/api/devices");
	});
});
