//
// tests/e2e/admin-gridstack.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// GridStack dashboard layout: an overlapping stored layout reloads with the tiles in place; edit mode saves on
// Done, discards on Escape, loads in a fresh context and resets. Replaces admin_flow.mjs lines 265-285 and
// 2694-2817. Own gateway: the story writes the dashboard layout.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
import { elapse } from "./support/timing.mjs";
test.describe.configure({ mode: "serial" });
test.describe("dashboard layout editing @gridstack", () => {
	const fresh = freshGateway("default");
	let context;
	let page;
	let watch;
	let movedPosition;
	const layoutPut = { last: null, count: 0 };
	const pvWidgetReady = () => page.waitForFunction(() => document.querySelector('#dashboard-grid .grid-stack-item[data-widget-id="pv-power"]')?.gridstackNode);
	const pvPosition = () => page.evaluate(() => {
		const node = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]').gridstackNode;
		return { x: node.x, y: node.y, w: node.w, h: node.h };
	});
	const layoutPutDone = () => page.waitForResponse((r) => r.url().endsWith("/admin/api/dashboard-layout") && r.request().method() === "PUT");
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser));
		watch = trackProblems(page, browserName, fresh.gateway.base);
	});
	test.afterAll(async () => { await context?.close(); });

	// A stored layout with overlapping boxes (tiles at y=3 under the inverter widget at y=4) must reload with
	// the tiles where they were placed and the inverter widget moved below them, never the reverse.
	test("an overlapping stored layout reloads with the tiles above the inverter widget @gridstack", async () => {
		await page.goto("/ui/dashboard");
		await pvWidgetReady();
		const overlapping = [
			["device-count", 0, 0, 2, 2], ["connected-count", 2, 0, 2, 2], ["pv-power", 4, 0, 2, 2], ["metric-count", 6, 0, 2, 2],
			["grid-power", 8, 0, 2, 2], ["tsdb-status", 10, 0, 2, 2], ["battery-soc", 0, 3, 2, 2], ["house-power", 2, 3, 2, 2], ["devices", 0, 4, 12, 8],
		].map(([id, x, y, w, h]) => ({ id, x, y, w, h, visible: true }));
		await page.evaluate((widgets) => window.RCTAdmin.api("dashboard-layout", { method: "PUT", body: JSON.stringify({ version: 1, widgets }) }), overlapping);
		await page.reload();
		const ids = ["battery-soc", "house-power", "devices"];
		await page.waitForFunction((list) => list.every((id) =>
			document.querySelector(`#dashboard-grid .grid-stack-item[data-widget-id="${id}"]`)?.gridstackNode), ids);
		const grid = await page.evaluate((list) => Object.fromEntries(list.map((id) => {
			const node = document.querySelector(`#dashboard-grid .grid-stack-item[data-widget-id="${id}"]`).gridstackNode;
			return [id, { x: node.x, y: node.y, h: node.h }];
		})), ids);
		for (const id of ["battery-soc", "house-power"]) {
			expect(grid[id].y + grid[id].h, `reload keeps the ${id} tile above the inverter widget ${JSON.stringify(grid)}`).toBeLessThanOrEqual(grid.devices.y);
		}
		expect(grid["battery-soc"].x, "the Battery level tile keeps its column").toBe(0);
		expect(grid["house-power"].x, "the House consumption tile keeps its column").toBe(2);
		await page.evaluate(() => window.RCTAdmin.api("dashboard-layout", { method: "DELETE" }));
		await page.reload();
	});

	test("edit mode: no write before Done, Done saves once, the position persists @gridstack", async ({ browser }) => {
		await page.goto("/ui/dashboard");
		await pvWidgetReady();
		const editToggle = page.locator("#dashboard-edit-toggle");
		expect(await editToggle.isVisible(), "dashboard starts in view mode with the edit toggle").toBe(true);
		expect(await page.locator(".dashboard-widget-header").first().isVisible(), "the drag handle is hidden in view mode").toBe(false);
		expect(await page.locator("#dashboard-edit-done").isVisible(), "Done is hidden in view mode").toBe(false);
		await editToggle.click();
		await page.waitForSelector(".dashboard-editing");
		await expect(page.locator("#dashboard-add-widget:visible"), "edit mode shows Add widget").toHaveCount(1);
		await expect(page.locator("#dashboard-reset-layout:visible"), "edit mode shows Reset layout").toHaveCount(1);
		await expect(page.locator("#dashboard-edit-done:visible"), "edit mode shows Done").toHaveCount(1);
		await page.route("**/admin/api/dashboard-layout", async (route) => {
			if (route.request().method() === "PUT") {
				layoutPut.last = route.request().postDataJSON();
				layoutPut.count += 1;
			}
			await route.continue();
		});
		// GridStack's own API moves/resizes the widget (equivalent to a completed drag/resize).
		movedPosition = await page.evaluate(() => {
			const grid = document.querySelector("#dashboard-grid").gridstack;
			const el = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]');
			grid.update(el, { x: 6, y: 6, w: 4, h: 3 });
			const node = el.gridstackNode;
			return { x: node.x, y: node.y, w: node.w, h: node.h };
		});
		// Negative window: the layout is saved on Done only, so no autosave may arrive after the move.
		await elapse(700, "no autosave is sent while editing (longer than the editor's save debounce)");
		expect(layoutPut.count, "editing does not write the layout before Done").toBe(0);
		await Promise.all([layoutPutDone(), page.locator("#dashboard-edit-done").click()]);
		const saved = layoutPut.last?.widgets?.find((w) => w.id === "pv-power");
		expect(saved, `Done saves the moved/resized layout via PUT /admin/api/dashboard-layout ${JSON.stringify({ movedPosition, put: layoutPut.last })}`)
			.toMatchObject(movedPosition);
		await expect(page.locator("#toast-region"), "Done shows the \"Dashboard layout saved.\" toast").toContainText("Dashboard layout saved.");
		const toast = (await page.locator("#toast-region").innerText()).trim();
		expect((toast.match(/Dashboard layout saved\./g) || []).length, "the saved toast shows once").toBe(1);
		await Promise.all([page.waitForResponse((r) => r.url().endsWith("/admin/api/dashboard-layout") && r.request().method() === "GET"), page.reload()]);
		await pvWidgetReady();
		// The stored layout is applied after its GET answered; wait for the settled position.
		await expect.poll(() => pvPosition(), { message: "the moved/resized position persists after a browser reload", timeout: 5000 }).toEqual(movedPosition);
		// A fresh context has its own empty browser cache and localStorage but shares the logged-in session.
		const freshContext = await browser.newContext({ viewport: { width: 1280, height: 800 } });
		try {
			await freshContext.addCookies(await context.cookies());
			const freshPage = await freshContext.newPage();
			await freshPage.goto(`${fresh.gateway.base}/ui/dashboard`);
			await freshPage.waitForFunction(() => document.querySelector('.grid-stack-item[data-widget-id="pv-power"]')?.gridstackNode);
			await expect.poll(() => freshPage.evaluate(() => {
				const node = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]').gridstackNode;
				return { x: node.x, y: node.y, w: node.w, h: node.h };
			}), { message: "saved layout loads in a fresh browser context without localStorage or cache", timeout: 5000 }).toEqual(movedPosition);
		} finally {
			await freshContext.close();
		}
	});

	test("Escape discards the edit without confirmation or write @gridstack", async () => {
		const editToggle = page.locator("#dashboard-edit-toggle");
		await editToggle.click();
		await page.waitForSelector(".dashboard-editing");
		await page.evaluate(() => {
			const grid = document.querySelector("#dashboard-grid").gridstack;
			grid.update(document.querySelector('.grid-stack-item[data-widget-id="pv-power"]'), { x: 0, y: 0, w: 2, h: 2 });
		});
		await page.locator("#dashboard-add-widget").click();
		await page.locator("#add-widget-modal").getByRole("button", { name: "Remove widget: PV power" }).click();
		await page.locator("#add-widget-modal").getByRole("button", { name: "Close" }).last().click();
		await page.locator("#add-widget-modal").waitFor({ state: "hidden" });
		await page.keyboard.press("Escape");
		expect(await editToggle.isVisible(), "Escape closes edit mode: the edit toggle is back").toBe(true);
		await expect(page.locator(".dashboard-editing"), "Escape closes edit mode").toHaveCount(0);
		await expect(page.locator("#confirm-modal.show"), "Escape closes edit mode without a confirmation").toHaveCount(0);
		expect(await pvPosition(), "Escape restores the saved position").toEqual(movedPosition);
		expect(await page.locator('.grid-stack-item[data-widget-id="pv-power"]').isVisible(), "Escape restores the widget visibility").toBe(true);
		expect(layoutPut.count, "Escape does not write the discarded layout").toBe(1);
		await page.unroute("**/admin/api/dashboard-layout");
	});

	test("Reset layout restores the default position, Escape undoes it, Done persists it @gridstack", async () => {
		const editToggle = page.locator("#dashboard-edit-toggle");
		await editToggle.click();
		await page.waitForSelector(".dashboard-editing");
		await page.locator("#dashboard-reset-layout").click();
		// The default position is applied immediately; the confirmation would open right after the click.
		await expect.poll(() => pvPosition(), { message: "reset layout restores the default pv-power position immediately" }).toEqual({ x: 0, y: 0, w: 2, h: 2 });
		await elapse(400, "a confirmation dialog would open within this window after Reset layout");
		await expect(page.locator("#confirm-modal.show"), "reset layout does not ask for confirmation").toHaveCount(0);
		await page.keyboard.press("Escape");
		expect(await pvPosition(), "Escape after Reset layout restores the prior saved layout").toEqual(movedPosition);
		await editToggle.click();
		await page.waitForSelector(".dashboard-editing");
		await page.locator("#dashboard-reset-layout").click();
		await Promise.all([layoutPutDone(), page.locator("#dashboard-edit-done").click()]);
		await page.reload();
		await pvWidgetReady();
		await expect.poll(() => pvPosition(), { message: "default position survives a reload after reset" }).toEqual({ x: 0, y: 0, w: 2, h: 2 });
		const scriptProblems = watch.problems.filter((p) => /dashboard\.js|gridstack/i.test(p));
		expect(scriptProblems, "dashboard GridStack flow raised no script errors").toEqual([]);
	});

	test("the layout story raised no console, network or dialog problems @gridstack", async () => {
		expect(watch.problems, "no console errors, page errors, failed requests, HTTP >= 400 or native dialogs").toEqual([]);
		expect([...watch.external], "no request leaves the gateway").toEqual([]);
	});
});
