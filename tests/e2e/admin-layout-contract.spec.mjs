//
// tests/e2e/admin-layout-contract.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Layout contract of the admin pages: one card gap token, shared Prometheus/Inverters geometry, viewport
// height behaviour and a mutation self-test of the gap measurement.
// Replaces admin_flow.mjs lines 1030-1192. #main-content is zoomed (--content-zoom), so rect distances are
// divided by that zoom to get CSS px. The default gateway runs without the Energy Manager, so the
// /ui/energy visits deliberately get its 503 (scrubbed with expectedStatus, as before).
import { expect, test } from "./support/fixtures.mjs";
import { expectedStatus, trackProblems } from "./support/problems.mjs";
import { settle } from "./support/settle.mjs";
const THEME_KEY = "rct-admin-theme";
const GAP_TOLERANCE = 1;
const STACKED_PAGES = [
	["/ui/prometheus", "#exposed-list tr"], ["/ui/inverters", "#settings-sections .card, #writable-list tr"],
	["/ui/tsdb", "#settings-sections .card"], ["/ui/tokens", "#tokens-title"],
	["/ui/settings", "#settings-sections .card"], ["/ui/energy", "#energy-list > *"], ["/ui/about", ".about-top-row .card"],
];
async function openParamTable(page, url, prefix) {
	await page.goto(url);
	await page.waitForSelector(`#${prefix}-list tr`, { state: "attached" });
	await settle(page, `${url} settles`);
}
const stackedGeometry = (page) => page.evaluate(() => {
	const main = document.querySelector("#main-content");
	const zoom = parseFloat(getComputedStyle(main).zoom) || 1;
	// Resolve the token through a probe outside the zoomed content, so the unit never matters.
	const probe = document.body.appendChild(document.createElement("div"));
	probe.style.cssText = "position:absolute;visibility:hidden;width:var(--rct-card-gap)";
	const token = parseFloat(getComputedStyle(probe).width);
	probe.remove();
	const visible = (node) => { const r = node.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
	const heading = main.querySelector(".page-heading");
	const container = heading.parentElement;
	const blocks = [...container.children].filter((node) => node.matches(".card, .settings-grid, .energy-grid, .energy-tabs, #export-actions, .about-top-row, .prometheus-dependent-settings") && visible(node));
	// The About row is spaced from its heading by its own mt-1, not by the sibling rule.
	const chain = blocks[0]?.matches(".about-top-row") ? blocks : [heading, ...blocks];
	const gaps = [];
	chain.slice(1).forEach((node, i) => gaps.push({ where: `${chain[i].className || chain[i].id} -> ${node.className || node.id}`, gap: (node.getBoundingClientRect().top - chain[i].getBoundingClientRect().bottom) / zoom }));
	// Tiles inside a grid or the About row: the nearest neighbour below and to the right.
	const tiles = [...container.querySelectorAll(":scope > .settings-grid > *, :scope > .energy-grid > *, :scope > .about-top-row .card")].filter(visible);
	const rects = tiles.map((node) => node.getBoundingClientRect());
	const overlap = (a0, a1, b0, b1) => Math.min(a1, b1) - Math.max(a0, b0);
	rects.forEach((a, i) => {
		const below = rects.filter((b, j) => j !== i && b.top >= a.bottom - 0.5 && overlap(a.left, a.right, b.left, b.right) > 1);
		if (below.length) gaps.push({ where: `tile ${i} below`, gap: (Math.min(...below.map((b) => b.top)) - a.bottom) / zoom });
		const beside = rects.filter((b, j) => j !== i && b.left >= a.right - 0.5 && overlap(a.top, a.bottom, b.top, b.bottom) > 1);
		if (beside.length) gaps.push({ where: `tile ${i} beside`, gap: (Math.min(...beside.map((b) => b.left)) - a.right) / zoom });
	});
	const grid = container.querySelector(":scope > .settings-grid, :scope > .energy-grid");
	const gridGap = grid ? { row: parseFloat(getComputedStyle(grid).rowGap), column: parseFloat(getComputedStyle(grid).columnGap) } : null;
	return { zoom, token, gaps, gridGap, blocks: blocks.length, tiles: tiles.length, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
});
const frameOf = (page, prefix) => page.evaluate((pfx) => {
	const edges = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
	const card = document.getElementById(`${pfx}-list`).closest(".card");
	const container = document.querySelector(".page-heading").parentElement;
	const add = card.querySelector(".btn-primary");
	const pager = document.getElementById(`${pfx}-range`).parentElement;
	return {
		container: edges(container), heading: edges(document.querySelector(".page-heading")), card: edges(card),
		wrap: edges(card.querySelector(".param-table-wrap")), search: edges(document.getElementById(`${pfx}-search`)),
		pager: edges(pager), footer: edges(document.querySelector(".admin-footer")),
		rows: document.querySelectorAll(`#${pfx}-list .metric-row`).length,
		pageOverflowY: document.documentElement.scrollHeight - document.documentElement.clientHeight,
		addFromRight: card.getBoundingClientRect().right - add.getBoundingClientRect().right, addFromLeft: add.getBoundingClientRect().left - card.getBoundingClientRect().left,
		addFromTop: add.getBoundingClientRect().top - card.getBoundingClientRect().top, cardHeight: card.getBoundingClientRect().height,
	};
}, prefix);
test.describe("layout contract @layout", () => {
	for (const scheme of ["light", "dark"]) {
		for (const width of [1280, 390]) {
			test(`card gap token, shared geometry and no horizontal scroll (${scheme} @${width}) @layout`, async ({ adminPage: page, gateway, browserName }) => {
				const tracked = trackProblems(page, browserName, gateway.base);
				await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [THEME_KEY, scheme]);
				await page.setViewportSize({ width, height: 844 });
				for (const [url, ready] of STACKED_PAGES) {
					const label = `${url} @${width} ${scheme}`;
					await page.goto(url);
					await page.waitForSelector(ready, { state: "attached" });
					await settle(page, `${label} settles`);
					const geometry = await stackedGeometry(page);
					if (url === "/ui/prometheus" && width === 1280) {
						expect(await page.evaluate(() => document.documentElement.getAttribute("data-bs-theme")), `theme ${scheme} is active for the layout contract`).toBe(scheme);
						expect(geometry.token, "card gap token is 20px").toBe(20);
					}
					expect(geometry.zoom, `content zoom is applied to ${label}`).toBeGreaterThan(0);
					expect(geometry.zoom, `content zoom is below 1 on ${label}`).toBeLessThan(1);
					expect(geometry.blocks, `stacked blocks are present on ${label}`).toBeGreaterThanOrEqual(1);
					const off = geometry.gaps.filter((g) => Math.abs(g.gap - geometry.token) > GAP_TOLERANCE);
					expect(geometry.gaps.length, `card gaps were measured on ${label}`).toBeGreaterThan(0);
					expect(off, `every card gap equals the token on ${label}`).toEqual([]);
					if (geometry.gridGap) {
						expect(Math.abs(geometry.gridGap.row - geometry.token), `grid row gap equals the token on ${label} ${JSON.stringify(geometry.gridGap)}`).toBeLessThanOrEqual(0.5);
						expect(Math.abs(geometry.gridGap.column - geometry.token), `grid column gap equals the token on ${label} ${JSON.stringify(geometry.gridGap)}`).toBeLessThanOrEqual(0.5);
					}
					expect(geometry.overflow, `no horizontal page scroll on ${label}`).toBeLessThanOrEqual(0);
				}
				// Prometheus and Inverters are one layout: same container edges, same card width, same Add action corner.
				const frames = {};
				for (const [url, prefix] of [["/ui/prometheus", "exposed"], ["/ui/inverters", "writable"]]) {
					await openParamTable(page, url, prefix);
					frames[prefix] = await frameOf(page, prefix);
				}
				const [pm, inv] = [frames.exposed, frames.writable];
				const label = `@${width} ${scheme}`;
				const near = (a, b) => Math.abs(a - b) <= 1;
				expect(near(pm.container.left, inv.container.left) && near(pm.container.right, inv.container.right), `Prometheus and Inverters share the container edges ${label} ${JSON.stringify([pm.container, inv.container])}`).toBe(true);
				expect(near(pm.heading.left, inv.heading.left) && near(pm.heading.right, inv.heading.right), `Prometheus and Inverters headings align ${label}`).toBe(true);
				expect(near(pm.card.left, inv.card.left) && near(pm.card.right, inv.card.right) && near(pm.card.width, inv.card.width), `Prometheus and Inverters table cards have the same edges and width ${label} ${JSON.stringify([pm.card, inv.card])}`).toBe(true);
				expect(near(pm.wrap.left, inv.wrap.left) && near(pm.wrap.right, inv.wrap.right) && near(pm.search.left, inv.search.left) && near(pm.search.width, inv.search.width), `table wrapper and search share their edges ${label}`).toBe(true);
				if (width === 1280) {
					const sparse = { rows: inv.rows, cardBottom: inv.card.bottom, pagerBottom: inv.pager.bottom, footerTop: inv.footer.top, pageOverflowY: inv.pageOverflowY };
					const message = `sparse Inverters table fills the available viewport and keeps its pager at the bottom ${label} ${JSON.stringify(sparse)}`;
					expect(inv.rows, message).toBeLessThanOrEqual(1);
					expect(inv.footer.top - inv.card.bottom, message).toBeGreaterThanOrEqual(0);
					expect(inv.footer.top - inv.card.bottom, message).toBeLessThanOrEqual(40);
					expect(inv.card.bottom - inv.pager.bottom, message).toBeGreaterThanOrEqual(0);
					expect(inv.card.bottom - inv.pager.bottom, message).toBeLessThanOrEqual(40);
					expect(inv.pageOverflowY, message).toBeLessThanOrEqual(1);
				}
				// Centered against a title block of different height, so only the horizontal corner and the header band are compared.
				const addInside = (f) => f.addFromRight >= 0 && f.addFromLeft >= 0 && f.addFromTop >= 0 && f.addFromTop < f.cardHeight / 2;
				expect(addInside(pm) && addInside(inv), `the primary Add action sits in the card header band ${label} ${JSON.stringify([pm, inv].map((f) => [f.addFromLeft, f.addFromRight, f.addFromTop]))}`).toBe(true);
				if (width >= 768) {
					expect(near(pm.addFromRight, inv.addFromRight) && pm.addFromRight < 40, `the primary Add action is top right on both pages ${label} ${JSON.stringify([pm.addFromRight, inv.addFromRight])}`).toBe(true);
				}
				// This server runs without the Energy Manager, so the /ui/energy visit above deliberately got its 503.
				tracked.scrub(0, expectedStatus(`${gateway.base}/admin/api/energy/devices`, 503));
				expect(tracked.problems, "no unexpected console errors, failed requests or HTTP errors").toEqual([]);
			});
		}
	}
	// Viewport height contract: the Inverters page is the reference. Every param_table page ends its card right above
	// the footer once scrolled to the bottom, and a short page fills exactly the viewport (no scroll caused by the card).
	test("Prometheus and Inverters pages end above the footer and fill short viewports @layout", async ({ adminPage: page }) => {
		const heightProbe = (prefix) => page.evaluate((pfx) => {
			window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
			const card = document.getElementById(`${pfx}-list`).closest(".card");
			const footer = document.querySelector(".admin-footer").getBoundingClientRect();
			const de = document.documentElement;
			return { cardToFooter: footer.top - card.getBoundingClientRect().bottom, docOverflowY: de.scrollHeight - de.clientHeight, footerTop: footer.top, cardBottom: card.getBoundingClientRect().bottom, rows: document.querySelectorAll(`#${pfx}-list .metric-row`).length };
		}, prefix);
		for (const [width, height] of [[1280, 720], [1280, 900], [1536, 864], [1280, 1200], [1280, 2200], [390, 844]]) {
			await page.setViewportSize({ width, height });
			const probes = {};
			for (const [url, prefix] of [["/ui/prometheus", "exposed"], ["/ui/inverters", "writable"]]) {
				await openParamTable(page, url, prefix);
				probes[prefix] = await heightProbe(prefix);
			}
			const [pm, inv] = [probes.exposed, probes.writable];
			const label = `@${width}x${height}`;
			const detail = JSON.stringify([pm, inv]);
			expect(Math.abs(pm.cardToFooter - inv.cardToFooter), `Prometheus card ends above the footer exactly like Inverters when scrolled to the bottom ${label} ${detail}`).toBeLessThanOrEqual(1);
			expect(pm.cardToFooter, `Prometheus card ends above the footer ${label} ${detail}`).toBeGreaterThanOrEqual(0);
			if (height === 2200) {
				expect(pm.docOverflowY, `short Prometheus page fills the viewport ${label} ${detail}`).toBeLessThanOrEqual(1);
				expect(inv.docOverflowY, `short Inverters page fills the viewport ${label} ${detail}`).toBeLessThanOrEqual(1);
				expect(Math.abs(pm.cardBottom - inv.cardBottom), `short Prometheus page fills the viewport like Inverters ${label} ${detail}`).toBeLessThanOrEqual(1);
			}
		}
	});
	test("card gap measurement rejects a 7px mutation (self-test) @layout", async ({ adminPage: page }) => {
		await page.goto("/ui/prometheus");
		await page.waitForSelector("#exposed-list tr", { state: "attached" });
		await settle(page, "prometheus page settles");
		// A constructable stylesheet is not blocked by the page's style-src CSP, unlike an injected <style> tag.
		await page.evaluate(() => {
			const sheet = new CSSStyleSheet();
			sheet.replaceSync("#main-content > .card { margin-top: calc(var(--rct-card-gap) + 7px) !important; }");
			window.__gapMutation = sheet;
			document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
		});
		const mutatedGap = await stackedGeometry(page);
		expect(mutatedGap.gaps.some(({ gap }) => Math.abs(gap - mutatedGap.token) > GAP_TOLERANCE), `card gap measurement rejects a 7px margin mutation ${JSON.stringify(mutatedGap.gaps)}`).toBe(true);
		await page.evaluate(() => { document.adoptedStyleSheets = document.adoptedStyleSheets.filter((sheet) => sheet !== window.__gapMutation); });
		const restoredGap = await stackedGeometry(page);
		expect(restoredGap.gaps.length, "gaps are measured after the mutation is removed").toBeGreaterThan(0);
		expect(restoredGap.gaps.filter(({ gap }) => Math.abs(gap - restoredGap.token) > GAP_TOLERANCE), `card gap measurement passes after the mutation is removed ${JSON.stringify(restoredGap.gaps)}`).toEqual([]);
	});
});
