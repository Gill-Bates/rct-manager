//
// tests/e2e/admin-rate-limit.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Admin-session rate limit: several Energy-page tabs polling for 95 s stay below the limit (no 429);
// a 700-request flood still answers 429 with Retry-After and the client is admitted again within one
// window. Replaces admin_rate_limit.mjs. The flood uses the default limit of 600 requests per minute.
import { elapse } from "./support/timing.mjs";
import { expect, test } from "./support/fixtures.mjs";

test.use({ gatewayMode: "energy" });

const TABS = 3;
const SOAK_MS = 95_000;

test("multi-tab Energy polling soak stays below the admin-session limit @rate-limit @slow", async ({ browser, gateway, adminState }) => {
	test.setTimeout(240_000); // 95 s soak plus start-up
	const limited = [];
	const polls = { count: 0 };
	const contexts = [];
	const pages = [];
	try {
		// Every tab shares one browser session: the strictest case for one session budget.
		for (let i = 0; i < TABS; i++) {
			const context = await browser.newContext({ storageState: adminState, baseURL: gateway.base, locale: "en-GB" });
			contexts.push(context);
			const page = await context.newPage();
			page.on("response", (response) => {
				if (!response.url().includes("/admin/api/")) return;
				polls.count += 1;
				if (response.status() === 429) limited.push(response.url());
			});
			await page.goto("/ui/energy");
			pages.push(page);
		}
		await elapse(SOAK_MS, "95 s soak: tabs must keep polling for the full minute-plus window");
		expect(polls.count, `tabs kept polling during the soak (${polls.count} admin API responses)`).toBeGreaterThan(TABS * 20);
		expect(limited, "no 429 for multi-tab Energy polling").toEqual([]);
		await expect(pages[0].locator("#energy-list"), "Energy page shows no retry note").not.toContainText(/Retrying/);
	} finally {
		for (const context of contexts) await context.close();
	}
});

test("request flood answers 429 with Retry-After and the client recovers @rate-limit @slow", async ({ adminPage: page }) => {
	test.setTimeout(240_000); // up to one 60 s window of recovery
	await page.goto("/ui/energy");
	await expect(page.locator("#energy-list")).toBeVisible();
	// Flood: 700 requests in one burst exceed the default 600 per minute.
	const flood = await page.evaluate(async () => {
		const codes = [];
		let retryAfter = null;
		for (let i = 0; i < 700; i++) {
			const response = await fetch("/admin/api/devices", { credentials: "same-origin" });
			codes.push(response.status);
			if (response.status === 429 && retryAfter === null) retryAfter = response.headers.get("Retry-After");
		}
		return { limited: codes.filter((code) => code === 429).length, retryAfter };
	});
	expect(flood.limited, `flood is answered with 429 (${flood.limited} of 700)`).toBeGreaterThan(0);
	expect(Number(flood.retryAfter), `429 carries Retry-After (${flood.retryAfter})`).toBeGreaterThanOrEqual(1);
	// The open tab refills freed slots, so poll until admitted: it must happen within one window (70 s).
	await elapse(Number(flood.retryAfter) * 1000, "Retry-After must pass before the next request is admitted");
	await expect.poll(() => page.evaluate(async () => (await fetch("/admin/api/devices", { credentials: "same-origin" })).status),
		{ message: "client is admitted again within one window", timeout: 70_000, intervals: [2000] }).toBe(200);
	await expect(page.locator("#energy-list"), "Energy page recovered after the flood")
		.not.toContainText(/Energy data unavailable|Retrying/, { timeout: 8000 });
});
