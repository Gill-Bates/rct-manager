//
// tests/e2e/tsdb-console-ui.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// The four Status & Log console UX changes (full timestamp, per-level colours, terminal look,
// viewport-height growth) plus the QuestDB Apply bar overlap guard. The real admin GUI is driven; the
// real events.on('tsdb.log') -> appendTsdbConsoleLine render path is fed through a mocked SSE stream, and
// computed styles and geometry are read back, so the evidence comes from the shipped code.
// Matrix: RCT_E2E_VIEWPORT_WIDTH (default 1280), RCT_E2E_VIEWPORT_HEIGHT (default 1600), colour scheme
// (default dark here, as in the former driver). Replaces tsdb_console_ui.mjs.
import { expect, test } from "./support/fixtures.mjs";
test.use({ colorScheme: "dark" });
const WIDTH = Number(process.env.RCT_E2E_VIEWPORT_WIDTH || 1280);
const HEIGHT = Number(process.env.RCT_E2E_VIEWPORT_HEIGHT || 1600);
// The log lines the console will render, one per level, with ISO-8601 UTC timestamps.
const SAMPLE = [
	{ timestamp: "2026-10-11T19:46:12Z", level: "INFO", logger: "app.export.pusher", message: "Export loop started" },
	{ timestamp: "2026-10-11T19:46:13Z", level: "DEBUG", logger: "app.export.questdb_admin", message: "Downsampling check skipped" },
	{ timestamp: "2026-10-11T19:46:15Z", level: "WARNING", logger: "app.export.pusher", message: "Backend slow, retrying" },
	{ timestamp: "2026-10-11T19:46:17Z", level: "ERROR", logger: "app.api.app_factory", message: "Metrics export failed: connection refused" },
];
// A longer burst (after the four level samples) so the console content exceeds its height and internal
// tailing is provable: 80 extra INFO lines with increasing timestamps.
const BURST = Array.from({ length: 80 }, (_, i) => ({
	timestamp: `2026-10-11T19:${String(47 + Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}Z`,
	level: "INFO", logger: "app.export.pusher", message: `tick ${i} metrics flushed`,
}));
const FRAMES = [...SAMPLE, ...BURST];
const frames = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const shot = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: false }), contentType: "image/png" });
// Serve a controlled SSE stream for the central dispatcher: one frame per entry, then the stream ends.
async function mockLogStream(page) {
	await page.route("**/admin/api/events", (route) => route.fulfill({
		status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
		body: FRAMES.map((e) => `event: tsdb.log\ndata: ${JSON.stringify(e)}\n\n`).join("") + ": keep-alive\n\n",
	}));
}
// TSDB page: enable export and pick a backend so the Status & Log console renders.
async function openTsdb(page) {
	await page.goto("/ui/tsdb");
	await page.waitForSelector("#settings-sections");
	const enableToggle = page.locator("#setting-metrics_export_enabled");
	await enableToggle.waitFor();
	if (!(await enableToggle.isChecked())) await enableToggle.click();
	const dbType = page.locator("#setting-db_type");
	await dbType.waitFor();
	await dbType.selectOption("questdb");
	await page.waitForSelector("#tsdb-mini-console");
}
async function consoleReport(page) {
	await openTsdb(page);
	// Let the mocked SSE frames arrive and render (four level samples + the 80-line burst).
	await page.waitForFunction((n) => document.querySelectorAll("#tsdb-mini-console .tsdb-console-line").length >= n, FRAMES.length);
	await frames(page);
	const report = await page.evaluate(() => {
		const con = document.getElementById("tsdb-mini-console");
		const lines = [...con.querySelectorAll(".tsdb-console-line")];
		const styleOf = (node) => getComputedStyle(node);
		const perLine = lines.map((line) => {
			const time = line.querySelector(".tsdb-console-time");
			const logger = line.querySelector('span[data-role="logger"]');
			return {
				level: line.dataset.level, time: time?.textContent, timeColor: styleOf(time).color,
				loggerText: logger?.textContent, loggerColor: logger ? styleOf(logger).color : null,
				lineColor: styleOf(line).color, fontFamily: styleOf(line).fontFamily,
			};
		});
		// Geometry: console bottom vs viewport, and whether the page itself scrolls (double scrollbar).
		const conRect = con.getBoundingClientRect();
		const footer = document.querySelector(".admin-footer");
		const footerRect = footer ? footer.getBoundingClientRect() : null;
		const main = document.getElementById("main-content");
		return {
			perLine,
			consoleBottom: Math.round(conRect.bottom),
			consoleHeight: Math.round(conRect.height),
			viewportHeight: window.innerHeight,
			footerTop: footerRect ? Math.round(footerRect.top) : null,
			// Document scroll: the page must NOT gain a scrollbar.
			pageScrolls: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
			pageOverflowPx: document.documentElement.scrollHeight - document.documentElement.clientHeight,
			// Console internal scroll + auto-scroll pinning (the 500-line tail behaviour).
			consoleScrollable: con.scrollHeight > con.clientHeight + 1,
			lineCount: lines.length,
			pinnedToBottom: (con.scrollHeight - con.clientHeight - con.scrollTop) <= 24,
			lastMessage: lines[lines.length - 1]?.textContent || "",
			consoleOverflowY: getComputedStyle(con).overflowY,
			// The flex-column content area and whether IT (not the document) scrolls in the deficit regime.
			mainDisplay: getComputedStyle(main).display,
			mainFlexDir: getComputedStyle(main).flexDirection,
			mainScrollable: main.scrollHeight > main.clientHeight + 1,
		};
	});
	await test.info().attach("tsdb-report.json", { body: JSON.stringify(report, null, 2), contentType: "application/json" });
	return report;
}
test.describe("Status & Log console @tsdb-console @theme-matrix @viewport-matrix", () => {
	test.beforeEach(async ({ adminPage: page }) => {
		await page.setViewportSize({ width: WIDTH, height: HEIGHT });
		await mockLogStream(page);
	});
	test("console lines: timestamp, level colours, logger, monospace, scrolling @tsdb-console", async ({ adminPage: page }) => {
		const report = await consoleReport(page);
		// Change 1: full date+time timestamp (ISO-8601 UTC to seconds) on every line.
		const tsRe = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
		expect(report.perLine.length >= 4 && report.perLine.every((l) => tsRe.test(l.time || "")),
			`every line shows a full date+time timestamp (YYYY-MM-DDTHH:MM:SSZ): ${JSON.stringify(report.perLine.map((l) => l.time))}`).toBe(true);
		// Change 2: per-level colours and a visually distinct timestamp + logger.
		const byLevel = Object.fromEntries(report.perLine.map((l) => [l.level, l]));
		const distinctColors = new Set([byLevel.ERROR?.lineColor, byLevel.WARNING?.lineColor, byLevel.INFO?.lineColor].filter(Boolean));
		expect(distinctColors.size, `ERROR, WARNING and INFO lines use different colours: ${JSON.stringify({ error: byLevel.ERROR?.lineColor, warning: byLevel.WARNING?.lineColor, info: byLevel.INFO?.lineColor })}`).toBe(3);
		expect(report.perLine.every((l) => l.timeColor && l.timeColor !== l.lineColor),
			`the timestamp colour differs from the message colour (dimmed/accent): ${JSON.stringify(report.perLine.map((l) => [l.level, l.timeColor, l.lineColor]))}`).toBe(true);
		expect(report.perLine.every((l) => l.loggerText && l.loggerColor)
			// On a dimmed DEBUG line the whole line shares the accent tone; on the others the logger is distinct.
			&& report.perLine.filter((l) => l.level !== "DEBUG").every((l) => l.loggerColor !== l.lineColor),
		`the logger name is rendered on every line and dimmed (accent colour, distinct from a non-dim message): ${JSON.stringify(report.perLine.map((l) => [l.level, l.loggerText, l.loggerColor, l.lineColor]))}`).toBe(true);
		// Change 3: terminal / monospace look.
		expect(report.perLine.every((l) => /mono/i.test(l.fontFamily)), `console lines render in a monospace font (${report.perLine[0]?.fontFamily})`).toBe(true);
		// Behaviour preserved: with 84 frames the console tails internally and auto-scroll pins to the newest line.
		expect(report.consoleScrollable && report.pinnedToBottom && /tick 79/.test(report.lastMessage),
			`the console scrolls internally and auto-scroll pins to the newest line: ${JSON.stringify({ consoleScrollable: report.consoleScrollable, pinnedToBottom: report.pinnedToBottom, lastMessage: report.lastMessage, lineCount: report.lineCount })}`).toBe(true);
	});
	test("console grows with the viewport: surplus and deficit regimes @tsdb-console", async ({ adminPage: page }) => {
		const report = await consoleReport(page);
		// Change 4: the console fills vertical space down toward the viewport/footer with no double scrollbar.
		expect(report.mainDisplay === "flex" && report.mainFlexDir === "column",
			`#main-content becomes a flex column so the console can grow: ${JSON.stringify({ display: report.mainDisplay, dir: report.mainFlexDir })}`).toBe(true);
		// Two layout regimes, following the parameter-table convention:
		//  - surplus (grid shorter than the viewport): the console expands toward the footer, the page does not scroll;
		//  - deficit (the settings cards alone are taller than the viewport): the content area scrolls once and the
		//    console keeps a usable minimum height.
		// Either way exactly one scroll region is active; the document itself never scrolls.
		expect(report.pageScrolls, "the document body never gains a scrollbar (console/content area scrolls instead)").toBe(false);
		const wideDesktop = WIDTH >= 992; // the lg breakpoint (sidebar shown)
		if (!report.mainScrollable) {
			// Surplus: the console took the leftover height (a real terminal pane, not the old ~88px fixed box).
			const minConsole = wideDesktop ? 180 : 110;
			expect(report.footerTop !== null && report.consoleHeight >= minConsole && report.consoleBottom <= report.footerTop,
				`the console grows to fill the vertical surplus (terminal pane, within the viewport): ${JSON.stringify({ consoleHeight: report.consoleHeight, consoleBottom: report.consoleBottom, footerTop: report.footerTop, minConsole })}`).toBe(true);
			expect(report.mainScrollable === false && /auto|scroll/.test(report.consoleOverflowY),
				`surplus regime: the console is the only scroll region (content area does not scroll): ${JSON.stringify({ mainScrollable: report.mainScrollable, consoleScrollable: report.consoleScrollable })}`).toBe(true);
		} else {
			// Deficit: the minimum is 6rem under the 0.9 content zoom (~86px rendered).
			expect(report.consoleHeight >= 80 && /auto|scroll/.test(report.consoleOverflowY) && report.pageScrolls === false,
				`deficit regime: the content area scrolls once and the console keeps a usable minimum height: ${JSON.stringify({ consoleHeight: report.consoleHeight, mainScrollable: report.mainScrollable, pageScrolls: report.pageScrolls })}`).toBe(true);
		}
		await shot(page, "tsdb-console");
	});
	test("console tile boundary equals Inverters @tsdb-console", async ({ adminPage: page }) => {
		const report = await consoleReport(page);
		// Inverters reference: the console tile must end at the same viewport-bottom boundary as the Inverters
		// param-card. Compare the tiles' bottom edges and the gap each leaves to the footer.
		await page.goto("/ui/inverters");
		await page.waitForSelector(".param-card");
		await frames(page);
		const inv = await page.evaluate(() => {
			const card = document.querySelector(".param-card");
			const footer = document.querySelector(".admin-footer");
			return {
				cardBottom: Math.round(card.getBoundingClientRect().bottom),
				footerTop: footer ? Math.round(footer.getBoundingClientRect().top) : null,
				pageScrolls: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
			};
		});
		await test.info().attach("inverters-report.json", { body: JSON.stringify(inv, null, 2), contentType: "application/json" });
		await shot(page, "inverters-card");
		const consoleGutter = report.footerTop - report.consoleBottom;
		const invGutter = inv.footerTop - inv.cardBottom;
		// Same bottom-boundary behaviour as the Inverters param-card: both tiles fill down to the footer region and
		// neither makes the document scroll. The console's gutter is larger only by the TSDB-only apply bar and the
		// grid gap (bounded ~80px). Only meaningful in the surplus regime; in the deficit regime the content area
		// scrolls and the console's bottom is measured mid-scroll, so the no-scroll guarantee is asserted instead.
		if (!report.mainScrollable) {
			expect(inv.footerTop !== null && inv.pageScrolls === false && report.pageScrolls === false && consoleGutter >= 0 && (consoleGutter - invGutter) <= 80,
				`the console tile ends at the Inverters full-height boundary (fills to the footer region, no page scroll): ${JSON.stringify({ consoleGutter, invGutter, invPageScrolls: inv.pageScrolls, tsdbPageScrolls: report.pageScrolls })}`).toBe(true);
		} else {
			expect(inv.footerTop !== null && inv.pageScrolls === false && report.pageScrolls === false,
				`deficit layout matches Inverters behaviour (neither the TSDB nor the Inverters page scrolls the document): ${JSON.stringify({ invPageScrolls: inv.pageScrolls, tsdbPageScrolls: report.pageScrolls })}`).toBe(true);
		}
	});
	// Regression guard for the Apply bar: at 1280x900 with the QuestDB form, neither the export cards nor the grid
	// may cover "Apply changes" (the grid once shrank below its content), for manual and non-manual downsampling.
	// Geometry plus a trial click, which fails if another element intercepts it. One test, both modes in order, as
	// in the former driver: the Apply button is disabled until the form is dirty, and the manual pass leaves the
	// draft dirty for the second pass (an isolated "medium" run waits on a disabled button).
	test("1280x900 QuestDB: Apply changes is not covered (manual, medium) @tsdb-console", async ({ adminPage: page }) => {
		await page.setViewportSize({ width: 1280, height: 900 });
		await openTsdb(page); // export enabled and QuestDB selected
		for (const mode of ["manual", "medium"]) {
			await page.selectOption("#setting-questdb_downsampling", mode);
			await page.fill("#setting-questdb_hostname", "questdb.example.test");
			if (mode === "manual") {
				await page.fill("#setting-questdb_raw_retention_days", "7");
				await page.press("#setting-questdb_raw_retention_days", "Tab");
			}
			const apply = page.locator("#export-apply");
			await apply.scrollIntoViewIfNeeded();
			await frames(page);
			const overlap = await page.evaluate(() => {
				const a = document.getElementById("export-apply").getBoundingClientRect();
				const hit = (r) => r.left < a.right && r.right > a.left && r.top < a.bottom && r.bottom > a.top;
				const boxes = [document.querySelector("#main-content > .settings-grid"), ...document.querySelectorAll("#main-content > .settings-grid > .card")];
				return boxes.filter((n) => n && hit(n.getBoundingClientRect())).map((n) => n.className || n.tagName);
			});
			let intercepted = "";
			try { await apply.click({ trial: true, timeout: 3000 }); } catch (e) { intercepted = String(e.message).split("\n").slice(0, 3).join(" "); }
			expect(overlap.length === 0 && intercepted === "", `1280x900 QuestDB (${mode}): Apply changes is not covered by the grid or its cards: ${JSON.stringify({ overlap, intercepted })}`).toBe(true);
		}
	});
});
