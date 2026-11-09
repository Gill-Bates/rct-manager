//
// tests/e2e/admin-auth.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// First login of a fresh gateway: one-time password in the start banner, redirect to /login, forced
// password change, one-time usage notice. Replaces admin_flow.mjs lines 113-114 and 139-156.
// The tests form one story on their own gateway (not the worker gateway): the first password can be
// used exactly once, and the `adminState` fixture of the other specs consumes it on the shared one.
import fs from "node:fs";
import path from "node:path";
import { expect, test, NEW_PASSWORD } from "./support/fixtures.mjs";
import { startGateway } from "./support/gateway.mjs";
import { trackProblems } from "./support/problems.mjs";
import { frames } from "./support/settle.mjs";
test.describe.configure({ mode: "serial" });
test.describe("first login @auth", () => {
	let gateway;
	let context;
	let page;
	let watch;
	let firstPassword;
	test.beforeAll(async ({ browser, browserName }) => {
		gateway = await startGateway("default");
		firstPassword = gateway.firstPassword();
		context = await browser.newContext({ baseURL: gateway.base, viewport: { width: 1280, height: 800 }, locale: "en-GB", colorScheme: "light" });
		page = await context.newPage();
		watch = trackProblems(page, browserName, gateway.base);
	});
	test.afterAll(async () => {
		await context?.close();
		await gateway?.stop();
	});
	test("first start prints a one-time password and no admin token @auth", async () => {
		expect(Boolean(firstPassword), "initial password read from the first-start banner").toBe(true);
		expect(firstPassword.length, "initial password read from the first-start banner (non-empty)").toBeGreaterThan(0);
		// The banner is the gateway's captured console output; the log file holds the same text.
		const banner = fs.readFileSync(path.join(gateway.logDir, "server.log"), "utf8");
		expect(banner, "no admin token printed at first start").not.toMatch(/Initial admin token/);
	});
	test("root redirects to login; first login forces the password change @auth", async () => {
		await page.goto("/");
		await expect(page, "/ redirects to /login").toHaveURL(/\/login$/);
		await page.fill("#password", firstPassword);
		await page.click("#login-form button[type=submit]");
		await page.waitForURL("**/change-password");
		await expect(page, "first login forces password change").toHaveURL(/\/change-password$/);
		await page.goto("/ui/dashboard");
		await expect(page, "dashboard blocked while change is pending").toHaveURL(/\/change-password$/);
		await page.fill("#current-password", firstPassword);
		await page.fill("#new-password", NEW_PASSWORD);
		await page.fill("#confirm-password", NEW_PASSWORD);
		await page.click("#change-password-form button[type=submit]");
		await page.waitForURL("**/ui/dashboard");
		await expect(page, "password change lands on dashboard").toHaveURL(/\/ui\/dashboard$/);
	});
	test("usage notice cannot be dismissed with Escape and is accepted @auth", async () => {
		const modal = page.locator("#disclaimer-modal.show");
		await expect(modal, "the usage notice is shown after the first login").toBeVisible();
		await page.keyboard.press("Escape");
		await frames(page);
		await expect(modal, "the usage notice cannot be dismissed with Escape").toBeVisible();
		await Promise.all([page.waitForNavigation(), page.click("#disclaimer-accept")]);
		await expect(page.locator("#disclaimer-modal.show"), "the usage notice is accepted once and gone").toHaveCount(0);
		await expect(page.locator("#dashboard-grid"), "the dashboard is usable after accepting the notice").toBeVisible();
		await page.reload();
		await expect(page.locator("#disclaimer-modal.show"), "the accepted notice does not return").toHaveCount(0);
	});
	test("the whole first-login flow raised no console, network or dialog problems @auth", async () => {
		expect(watch.problems, "no console errors, page errors, failed requests, HTTP >= 400 or native dialogs").toEqual([]);
		expect([...watch.external], "no external requests").toEqual([]);
	});
});
