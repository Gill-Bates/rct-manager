//
// tests/e2e/energy-auto-verification.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Visible contract of the automatic hardware verification (commit b5d1742): the setup step is
// status-only, names the automatic check and offers no start action, dialog or stepper. Replaces the
// retired assistant_intro.mjs, which tested the removed guided dialog (no spinner or reduced-motion
// contract exists in the new flow: the step shows text only).
import { expect, test } from "./support/fixtures.mjs";

test.use({ gatewayMode: "energy" });

test("automatic verification step is status-only with no start action @assistant @theme-matrix @viewport-matrix", async ({ adminPage: page }) => {
	const errors = [];
	page.on("pageerror", (error) => errors.push(error.message));
	await page.goto("/ui/energy");
	await page.waitForSelector(".energy-panel");
	await page.locator(".energy-mode-switch [role=radio]").nth(1).click();
	const step = page.locator(".energy-setup-step", { hasText: "Hardware verification" });
	await expect(step, "the setup lists the hardware verification step").toBeVisible();
	await expect(step, "the step says the check runs automatically in the background")
		.toContainText(/checks this inverter automatically in the background|Checking this inverter now|Waiting for a safe time|automatic check will retry|cannot complete/);
	await expect(page.getByRole("button", { name: /Verify hardware|Start verification/ }), "no manual start action").toHaveCount(0);
	await expect(page.locator(".energy-assistant"), "no guided dialog or stepper exists").toHaveCount(0);
	expect(errors, "no script errors").toEqual([]);
});
