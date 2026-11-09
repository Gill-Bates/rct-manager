//
// tests/e2e/admin-parameter-tables.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Writable (Inverters) and exposed (Prometheus) parameter tables: wording, page size, add, row menu, pager parity.
// Replaces admin_flow.mjs lines 1621-1706. Own gateway: the tests grant and revoke write access.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
const PAGE_SIZE = 15;
test.describe("parameter tables @parameters", () => {
	test.describe.configure({ mode: "serial" });
	const fresh = freshGateway("default");
	let context;
	let page;
	let tracked;
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser));
		tracked = trackProblems(page, browserName, fresh.gateway.base);
	});
	test.afterAll(async () => { await context?.close(); });
	// Pager parity: Prometheus and Inverters share one table, so the same walk must hold on both
	// (15 rows per page, "x–y of N", Previous disabled on the first and Next on the last page).
	async function pagerWalk(prefix) {
		const read = async () => ({
			range: (await page.locator(`#${prefix}-range`).innerText()).trim(),
			rows: await page.locator(`#${prefix}-list .metric-row`).count(),
			prevDisabled: await page.locator(`#${prefix}-prev`).isDisabled(),
			nextDisabled: await page.locator(`#${prefix}-next`).isDisabled(),
		});
		// A click is complete when the range text changes; reading earlier would repeat the previous page.
		const step = async (button, previousRange) => {
			await page.click(`#${prefix}-${button}`);
			await expect.poll(async () => (await page.locator(`#${prefix}-range`).innerText()).trim(), { message: `${prefix} pager moved off ${previousRange}` }).not.toBe(previousRange);
			return read();
		};
		const forward = [await read()];
		while (!forward.at(-1).nextDisabled && forward.length < 100) forward.push(await step("next", forward.at(-1).range));
		const backward = [forward.at(-1)];
		while (!backward.at(-1).prevDisabled && backward.length < 100) backward.push(await step("prev", backward.at(-1).range));
		const total = Number(/ of (\d+)$/.exec(forward[0].range)?.[1]);
		const pages = Math.ceil(total / PAGE_SIZE);
		const expected = (k) => ({
			range: `${k * PAGE_SIZE + 1}–${Math.min((k + 1) * PAGE_SIZE, total)} of ${total}`, rows: Math.min(PAGE_SIZE, total - k * PAGE_SIZE),
			prevDisabled: k === 0, nextDisabled: k === pages - 1,
		});
		const walked = (states, order) => states.length === pages && states.every((s, i) => JSON.stringify(s) === JSON.stringify(expected(order(i))));
		return { total, pages, forwardOk: walked(forward, (i) => i), backwardOk: walked(backward, (i) => pages - 1 - i), forward };
	}
	test("writable parameter table: wording, page size, add, row menu @parameters", async () => {
		await page.goto("/ui/inverters");
		await page.waitForSelector("#setting-enable_write_support");
		expect(await page.locator("#setting-enable_write_support").count(), "inverters page retains write access").toBe(1);
		expect(await page.locator(".device-settings").count(), "inverter editor moved to the dashboard").toBe(0);
		expect(await page.locator("#writable-list").count(), "inverters page has the writable list").toBe(1);
		expect(await page.locator("#exposed-list").count(), "exposed list is not on the inverters page").toBe(0);
		// The writable parameters use the same table, search, pager and row menu as the Prometheus page.
		await page.waitForSelector("#writable-list .metric-row, #writable-list td");
		expect((await page.locator("#writable-range").innerText()).trim(), "writable table has the Prometheus pager wording").toMatch(/^(\d+–\d+ of \d+|0 parameters)$/);
		expect(await page.locator("#writable-list .metric-row").count(), "writable table shows at most 15 rows").toBeLessThanOrEqual(15);
		await page.click("#open-add-writable");
		await page.waitForSelector('#add-writable-modal.show #writable-available-list input[type="checkbox"]');
		const grantee = await page.locator('#writable-available-list input[type="checkbox"]').first().getAttribute("value");
		await page.locator('#writable-available-list input[type="checkbox"]').first().check();
		await page.click("#confirm-add-writable");
		await page.waitForSelector("#add-writable-modal", { state: "hidden" });
		await page.fill("#writable-search", grantee);
		await expect(page.locator(`#writable-list .metric-row[data-name="${grantee}"]`), "added parameter appears in the writable table").toHaveCount(1);
		await page.click(`#writable-list .metric-row[data-name="${grantee}"] .metric-menu-toggle`);
		await page.click(`#writable-list button[aria-label="Remove write access: ${grantee}"]`);
		await page.waitForFunction((name) => !document.querySelector(`#writable-list .metric-row[data-name="${name}"]`), grantee);
		await expect(page.locator("#writable-list td.text-secondary"), "row menu removes write access").toHaveCount(1);
	});
	test("Prometheus and Inverters pagers walk the same way; grants restored @parameters", async () => {
		await page.goto("/ui/prometheus");
		await page.waitForSelector("#exposed-list .metric-row");
		const exposedPager = await pagerWalk("exposed");
		expect(exposedPager.pages, `Prometheus has more than one page of metrics (${exposedPager.total})`).toBeGreaterThan(1);
		expect(exposedPager.forwardOk, `Prometheus pager walks forward with the x–y of N wording and disabled ends ${JSON.stringify(exposedPager.forward)}`).toBe(true);
		expect(exposedPager.backwardOk, "Prometheus pager walks back to the first page").toBe(true);
		await page.goto("/ui/inverters");
		await page.waitForSelector("#writable-list td");
		const initialGrants = await page.evaluate(async () => (await (await fetch("/admin/api/parameters", { credentials: "same-origin" })).json()).write_names);
		const initialRange = (await page.locator("#writable-range").innerText()).trim();
		await page.click("#open-add-writable");
		await page.waitForSelector('#add-writable-modal.show #writable-available-list input[type="checkbox"]');
		const grantable = await page.locator('#writable-available-list input[type="checkbox"]').count();
		const wanted = Math.min(grantable, 2 * PAGE_SIZE + 2);
		for (let i = 0; i < wanted; i++) await page.locator('#writable-available-list input[type="checkbox"]').nth(i).check();
		const grantSaved = page.waitForResponse((r) => r.url().endsWith("/admin/api/parameters") && r.request().method() === "PUT");
		await page.click("#confirm-add-writable");
		await page.waitForSelector("#add-writable-modal", { state: "hidden" });
		await grantSaved;
		// The "Parameters saved" toast sits over the pager until it leaves.
		await page.waitForFunction(() => !document.getElementById("toast-region").children.length, null, { timeout: 15000 });
		const writablePager = await pagerWalk("writable");
		expect(writablePager.pages, `Inverters pager has enough parameters for three pages (${writablePager.total} parameters)`).toBeGreaterThanOrEqual(3);
		expect(writablePager.forwardOk, `Inverters pager walks forward with the same wording and disabled ends as Prometheus ${JSON.stringify(writablePager.forward)}`).toBe(true);
		expect(writablePager.backwardOk, "Inverters pager walks back to the first page").toBe(true);
		expect(exposedPager.forward[0].rows, "Prometheus first page has the page size").toBe(PAGE_SIZE);
		expect(writablePager.forward[0].rows, "Prometheus and Inverters use the same page size").toBe(PAGE_SIZE);
		// Restore the grants this walk started with.
		const restored = await page.evaluate(async (grants) => {
			const { csrf_token: csrf } = await (await fetch("/admin/api/session", { credentials: "same-origin" })).json();
			const current = await (await fetch("/admin/api/parameters", { credentials: "same-origin" })).json();
			return (await fetch("/admin/api/parameters", {
				method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
				body: JSON.stringify({ exposed_names: current.exposed_names, write_names: grants }),
			})).status;
		}, initialGrants);
		expect(restored, "write grants restored after the pager walk").toBe(200);
		await page.goto("/ui/inverters");
		await page.waitForFunction(() => document.getElementById("writable-range")?.textContent.trim());
		expect((await page.locator("#writable-range").innerText()).trim(), "Inverters pager shows its original range after the restore").toBe(initialRange);
		expect(tracked.problems, "no console errors, failed requests or dialogs").toEqual([]);
	});
});
