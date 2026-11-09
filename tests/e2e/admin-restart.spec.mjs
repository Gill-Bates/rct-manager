//
// tests/e2e/admin-restart.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Settings and session persist across a gateway restart. Replaces admin_flow.mjs lines 2819-2830. The former
// driver relied on the log level and the exposed-metric order set earlier in the same run; this spec sets both
// itself. Own gateway: it restarts the server process on the same database and ports.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
// The restart drops the page's health probes; only these exact failures of the health URL are expected.
const expectedRestartProblem = (problem, base) => problem.includes(`${base}/health`) && (
	/^requestfailed: GET .*ERR_CONNECTION_REFUSED/.test(problem)
	|| /^console error: Failed to load resource: net::ERR_CONNECTION_REFUSED/.test(problem)
	|| problem.startsWith("http 503: GET ")
	|| /^console error: Failed to load resource: the server responded with a status of 503/.test(problem)
);
test.describe("restart persistence @restart", () => {
	const fresh = freshGateway("default");
	test("settings and session persist across a server restart @restart", async ({ browser, browserName }) => {
		const { context, page } = await fresh.newPage(browser);
		try {
			const { base } = fresh.gateway;
			const watch = trackProblems(page, browserName, base);
			const names = () => page.$$eval("#exposed-list .metric-row", (rows) => rows.map((r) => r.dataset.name));
			// Arrange: a changed log level and a reversed exposed-metric order, both set through the UI contract.
			await page.goto("/ui/settings");
			await page.locator("#setting-log_level").selectOption("DEBUG");
			await expect.poll(async () => (await (await context.request.get(`${base}/admin/api/settings`)).json()).settings.log_level,
				{ message: "the log level is saved before the restart", timeout: 8000 }).toBe("DEBUG");
			await page.goto("/ui/prometheus");
			await page.waitForSelector("#exposed-list .metric-row");
			const initial = await names();
			const reversed = [...initial].reverse();
			const parameters = await (await context.request.get(`${base}/admin/api/parameters`)).json();
			const session = await (await context.request.get(`${base}/admin/api/session`)).json();
			const saved = await context.request.put(`${base}/admin/api/parameters`, {
				headers: { "X-CSRF-Token": session.csrf_token, Origin: base },
				data: { exposed_names: reversed, write_names: parameters.write_names },
			});
			expect(saved.status(), "the new exposed order is saved before the restart").toBe(200);
			await page.reload();
			await page.waitForSelector("#exposed-list .metric-row");
			const afterDrag = await names();
			expect(afterDrag, "the exposed order differs from the default before the restart").not.toEqual(initial);
			// Act: restart only the gateway process.
			await page.goto("/ui/dashboard");
			const restartProblems = watch.problems.length;
			const restarted = await fresh.gateway.restart();
			watch.scrub(restartProblems, (p) => expectedRestartProblem(p, base));
			expect(restarted.output(), "no initial password printed after restart").not.toMatch(/FIRST START/);
			await page.goto("/ui/prometheus");
			expect(page.url(), "session survives restart").toMatch(/\/ui\/prometheus$/);
			await page.waitForSelector("#exposed-list .metric-row");
			expect(await names(), "order persists after server restart").toEqual(afterDrag);
			expect((await (await context.request.get(`${base}/admin/api/settings`)).json()).settings.log_level, "log level persisted").toBe("DEBUG");
			// Nothing but the expected restart noise was raised.
			await page.waitForLoadState("load");
			expect(watch.problems, "no console errors, failed requests or dialogs beyond the expected restart noise").toEqual([]);
			expect([...watch.external], "no request leaves the gateway").toEqual([]);
		} finally {
			await context.close();
		}
	});
});
