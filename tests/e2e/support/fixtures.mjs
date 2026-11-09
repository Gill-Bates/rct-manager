//
// tests/e2e/support/fixtures.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Playwright fixtures of the browser E2E suite.
//   gatewayMode  worker option: "default" | "energy" | "planning" (set per spec with test.use)
//   gateway      worker fixture: isolated gateway + simulated inverter, started once per worker
//   adminState   worker fixture: storageState after the first login and password change (done once)
//   adminPage    test fixture: page in a fresh logged-in context (viewport RCT_E2E_VIEWPORT_WIDTH or 1280 x 900,
//                project colour scheme, en-GB)
// Specs that cover the first login itself use `gateway` and `browser` directly, not adminState.
import { test as base, expect } from "@playwright/test";
import { seedAdminState, startGateway } from "./gateway.mjs";

export { expect };
export { NEW_PASSWORD } from "./gateway.mjs";

export const test = base.extend({
	gatewayMode: ["default", { option: true, scope: "worker" }],
	gateway: [async ({ gatewayMode }, use) => {
		const gateway = await startGateway(gatewayMode);
		try {
			await use(gateway);
		} finally {
			await gateway.stop();
		}
	}, { scope: "worker", timeout: 90_000 }],
	adminState: [async ({ gateway, browser }, use) => {
		await use(await seedAdminState(browser, gateway));
	}, { scope: "worker", timeout: 90_000 }],
	adminPage: async ({ gateway, adminState, browser, colorScheme }, use) => {
		const context = await browser.newContext({
			storageState: adminState,
			baseURL: gateway.base,
			viewport: { width: Number(process.env.RCT_E2E_VIEWPORT_WIDTH || 1280), height: 900 },
			locale: "en-GB",
			colorScheme: colorScheme || "light",
		});
		try {
			await use(await context.newPage());
		} finally {
			await context.close();
		}
	},
});
