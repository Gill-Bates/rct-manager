//
// tests/e2e/support/fresh.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Isolated gateway for a stateful spec file (tokens, settings, write grants, restarts). The shared worker
// `gateway` fixture is reused by other spec files of the same worker, so a spec that changes server state
// starts its own gateway (own temp database and simulator) in beforeAll and stops it in afterAll.
import { expect, test } from "@playwright/test";
import { seedAdminState, startGateway } from "./gateway.mjs";
/**
 * Registers beforeAll/afterAll for a describe block. Returns a handle: `fresh.gateway`, `fresh.state`
 * (storageState after the first login) and `fresh.newPage(browser, options)` for a logged-in page.
 */
export function freshGateway(mode = "default") {
	const fresh = {
		gateway: null,
		state: null,
		async newPage(browser, options = {}) {
			const context = await browser.newContext({
				storageState: fresh.state,
				baseURL: fresh.gateway.base,
				viewport: { width: Number(process.env.RCT_E2E_VIEWPORT_WIDTH || 1280), height: 800 },
				locale: "en-GB",
				colorScheme: "light",
				...options,
			});
			return { context, page: await context.newPage() };
		},
	};
	test.beforeAll(async ({ browser }) => {
		test.setTimeout(120_000);
		fresh.gateway = await startGateway(mode);
		fresh.state = await seedAdminState(browser, fresh.gateway);
	});
	test.afterAll(async () => {
		await fresh.gateway?.stop();
		fresh.gateway = null;
		expect(fresh.gateway, "gateway stopped").toBeNull();
	});
	return fresh;
}
