//
// tests/e2e/admin-prometheus.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Prometheus page: exposed metrics editor (remove, add, keyboard sort, drag and drop, persistence).
// Replaces admin_flow.mjs lines 1340-1387. Own gateway: the story rewrites the exposed list.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
test.describe("prometheus editor @prometheus", () => {
	const fresh = freshGateway("default");
	test("exposed metrics: remove, add, keyboard sort, drag and drop, persistence @prometheus", async ({ browser, browserName }) => {
		const { context, page } = await fresh.newPage(browser);
		try {
			const tracked = trackProblems(page, browserName, fresh.gateway.base);
			const toastText = async () => (await page.locator("#toast-region").innerText()).trim();
			const names = () => page.$$eval("#exposed-list .metric-row", (rows) => rows.map((r) => r.dataset.name));
			const exposedApi = () => page.evaluate(async () => (await (await fetch("/admin/api/parameters", { cache: "no-store" })).json()).exposed_names);
			await page.goto("/ui/prometheus");
			await page.waitForSelector("#exposed-list .metric-row");
			const initialNames = await names();
			const initialCount = Number((await page.locator("#prometheus-count").innerText()).split(" ")[0]);
			expect(initialNames.length, `exposed table shows at least 4 defaults (${initialNames.length})`).toBeGreaterThanOrEqual(4);
			expect(initialNames.length, `exposed table shows at most 15 defaults (${initialNames.length})`).toBeLessThanOrEqual(15);
			expect(await page.locator(".prometheus-summary").innerText(), "Prometheus summary shows the endpoint").toContain("/metrics");
			expect(await page.locator("#prometheus-count").innerText(), "Prometheus summary shows the count").toContain("metrics");
			const removed = initialNames[0];
			await page.click(`#exposed-list .metric-row[data-name="${removed}"] .metric-menu-toggle`);
			await page.click(`#exposed-list button[aria-label="Remove: ${removed}"]`);
			expect(await names(), "remove drops the item").not.toContain(removed);
			await expect.poll(async () => (await exposedApi()).includes(removed), { message: "the removal is saved", timeout: 8000 }).toBe(false);
			await page.click("#open-add-metrics");
			await page.waitForSelector("#add-metrics-modal.show");
			await page.fill("#parameter-search", removed);
			await page.locator(`#available-list input[aria-label="Add ${removed}"]`).check();
			await page.click("#confirm-add-metrics");
			await page.waitForSelector("#add-metrics-modal", { state: "hidden" });
			await expect(page.locator("#prometheus-count"), "add restores the metric in the editor").toHaveText(`${initialCount} metrics`);
			const second = (await names())[1];
			await page.click(`#exposed-list .metric-row[data-name="${second}"] .metric-menu-toggle`);
			await page.focus(`#exposed-list button[aria-label="Move up: ${second}"]`);
			await page.keyboard.press("Enter");
			expect((await names())[0], "keyboard move up reorders").toBe(second);
			expect(await page.evaluate(() => document.activeElement?.getAttribute("aria-label") || ""), "focus stays on the moved item button").toContain(second);
			const list = await names();
			await page.evaluate(([src, dst]) => { // synthetic HTML5 drag events: headless Chromium does not start native drags reliably
				const dt = new DataTransfer();
				const row = (n) => document.querySelector(`#exposed-list .metric-row[data-name="${n}"]`);
				row(src).querySelector(".metric-drag-handle").dispatchEvent(new DragEvent("dragstart", { dataTransfer: dt, bubbles: true }));
				row(dst).dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }));
				row(dst).dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
			}, [list.at(-1), list[0]]);
			const afterDrag = await names();
			expect(afterDrag[0], `drag and drop reorders (${afterDrag.slice(0, 2).join(",")})`).toBe(list.at(-1));
			await page.waitForFunction(() => document.getElementById("toast-region").textContent.includes("Parameters saved"), null, { timeout: 8000 });
			expect(await toastText(), "parameter change is saved without any restart wording").not.toMatch(/restart/i);
			// #save-state keeps data-state="saved" after a successful save (it shows no text, the toast does).
			await page.waitForFunction(() => document.getElementById("save-state").dataset.state === "saved", null, { timeout: 8000 });
			expect(await exposedApi(), "added metric is persisted").toContain(removed);
			await page.reload();
			await page.waitForSelector("#exposed-list .metric-row");
			expect(await names(), "order persists after reload").toEqual(afterDrag);
			expect(tracked.problems, "no console errors, failed requests or dialogs").toEqual([]);
			expect([...tracked.external], "no request leaves the gateway").toEqual([]);
		} finally {
			await context.close();
		}
	});
});
