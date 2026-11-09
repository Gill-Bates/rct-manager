//
// tests/e2e/admin-energy-setup.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Energy page before any hardware verification: SOC strategy labels, the guided setup checklist, the
// pending Manual mode and the Expert-only verification form. Replaces admin_flow.mjs lines 1951-2127.
// One story on an own energy gateway (the device ships unverified and the story verifies it at the end).
// The former driver skipped the form checks silently (the form lives in Expert mode, which stays off);
// here Expert mode is switched on, so the form checks are hard assertions.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { installEnergyMocks } from "./support/energy-mocks.mjs";
import { trackProblems } from "./support/problems.mjs";
test.describe.configure({ mode: "serial" });
test.describe("energy setup before verification @energy", () => {
	const fresh = freshGateway("energy");
	let context;
	let page;
	let watch;
	let mocks;
	const verifyPuts = [];
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser, { viewport: { width: 1280, height: 900 } }));
		watch = trackProblems(page, browserName, fresh.gateway.base);
		mocks = await installEnergyMocks(page);
		page.on("request", (r) => { if (r.method() === "PUT" && r.url().endsWith("/hardware-verification")) verifyPuts.push(r.postDataJSON()); });
	});
	test.afterAll(async () => { await context?.close(); });

	test("inverter SOC strategy labels: unknown code, unavailable, Constant is not Off @energy", async () => {
		await page.goto("/ui/energy");
		const strategies = page.locator("#energy-hardware-strategies");
		await expect(strategies, "unknown inverter SOC strategy shows its raw code").toContainText(/Unknown \(code 127\)/);
		mocks.strategy = { ...mocks.strategy, code: null };
		await page.reload();
		await expect(strategies, "unavailable inverter SOC strategy shows n/a").toContainText(/Inverter SOC strategy: n\/a/);
		mocks.strategy = { ...mocks.strategy, code: 1, label: "Constant" };
		await page.reload();
		await expect(strategies, "new inverter with Constant strategy shows its label").toContainText(/Constant \(code 1\)/);
		await expect(page.locator('.energy-mode-option[aria-checked="true"]'), "new inverter with Constant strategy is not shown as Off").toHaveCount(0);
		await expect(page.locator(".energy-mode-evidence"), "new inverter is shown as not yet managed").toContainText(/Not yet managed/);
	});

	test("setup checklist guides the first open step in Basic mode @energy", async () => {
		await page.waitForSelector(".energy-panel");
		await expect.poll(() => page.locator(".energy-setup:not([hidden])").count(), { message: "Setup checklist is shown while hardware is unverified", timeout: 15_000 }).toBeGreaterThanOrEqual(1);
		// Wait until the inverter has connected, so the connection-first branch order advances from
		// "Not connected." to the hardware-unverified setup block.
		await expect(page.locator(".energy-setup-list .energy-setup-item.is-met", { hasText: "Inverter connected" }), "the setup list shows the inverter as connected").toBeVisible({ timeout: 20_000 });
		const setupText = await page.locator(".energy-panel").first().innerText();
		expect(setupText, "a single guided setup block is shown").toMatch(/Manual battery control setup/);
		expect(setupText, "no old duplicated \"needs setup\" line").not.toMatch(/Manual control needs setup/);
		expect(setupText, "no old \"Complete setup\" call to action").not.toMatch(/Complete setup/);
		expect(await page.locator("#energy-expert-mode").isChecked(), "Expert mode exists and is OFF on load").toBe(false);
		await expect(page.locator(".energy-expert"), "Expert mode content is not rendered on load").toHaveCount(0);
		await expect(page.locator(".energy-diagnostics"), "Basic mode has no diagnostics").toHaveCount(0);
		expect(setupText, "Basic mode has no engineering mode, SoC policy or revoke action").not.toMatch(/Engineering mode|SoC target policy|Revoke verification/);
		const shownStep = await page.locator(".energy-setup-step").first().innerText();
		expect(shownStep, "only the first open setup step is shown").toMatch(/Write access|Power limits|Hardware verification/);
		expect(shownStep, "the shown setup step has no engineering mode").not.toMatch(/Engineering mode/);
		expect(setupText, "no raw capability names leak onto the Operate surface").not.toMatch(/write_path_convention|battery_power_sign_convention|grid_power_sign_convention/);
	});

	test("Manual is pending while the setup is open; Off is never pending @energy", async () => {
		const modeAnswer = () => page.waitForResponse((r) => r.request().method() === "PUT" && r.url().endsWith("/mode"), { timeout: 15_000 });
		const [manual] = await Promise.all([modeAnswer(), page.locator(".energy-mode-switch [role=radio]").nth(1).click()]);
		expect(manual.ok(), `Manual can be selected while the setup is open (HTTP ${manual.status()})`).toBe(true);
		await expect(page.locator(".energy-mode-option.is-selected.is-pending"), "Manual with an open setup is marked pending").toHaveCount(1, { timeout: 15_000 });
		await expect(page.locator(".energy-setup-lead:not([hidden])").first(), "the pending Manual says battery control unlocks when verification completes")
			.toContainText(/Manual is selected\..*battery control unlocks when it completes/s);
		const [off] = await Promise.all([modeAnswer(), page.locator(".energy-mode-switch [role=radio]").first().click()]);
		expect(off.ok(), `Off is accepted (HTTP ${off.status()})`).toBe(true);
		await page.waitForSelector(".energy-mode-option.is-selected:not(.is-pending)", { timeout: 15_000 });
		await expect(page.locator(".energy-setup-lead:not([hidden])"), "Off is never shown as pending").toHaveCount(0);
	});

	test("hardware verification is status-only in Basic mode and a form in Expert mode @energy", async () => {
		await page.waitForSelector(".energy-setup-step");
		const shownStep = await page.locator(".energy-setup-step").first().innerText();
		await expect(page.locator(".energy-setup-step form"), "Basic mode shows no hardware verification form").toHaveCount(0);
		expect(shownStep, "Basic mode shows no strategy code or byte width fields").not.toMatch(/Strategy code|Byte width/);
		// Hardware verification runs automatically: the Basic step is status-only and offers no button.
		await expect(page.locator(".energy-setup-step button"), "the Basic hardware step has no \"Verify hardware\" button").toHaveCount(0);
		await expect(page.locator(".energy-setup-step").first(), "the Basic hardware step names the automatic check").toContainText(/Waiting for a safe time|Checking this inverter/);
		expect(await page.locator("#energy-expert-mode").isChecked(), "the Basic hardware step leaves Expert mode off").toBe(false);
		// Expert mode is switched on by the user, never by the setup step.
		await page.locator("#energy-expert-mode").check();
		const form = page.locator(".energy-expert form").filter({ hasText: "Device model" });
		await expect(form, "the verification form lives in Expert mode").toHaveCount(1);
		const blank = await form.evaluate((node) => ({
			signs: [...node.querySelectorAll("select")].map((select) => select.value),
			checked: [...node.querySelectorAll("input[type=checkbox]")].filter((box) => box.checked).length,
		}));
		expect(blank.signs.length, "the verification form has two sign selects").toBe(2);
		expect(blank.signs.every((value) => value === ""), `the verification form starts with empty sign selects ${JSON.stringify(blank)}`).toBe(true);
		expect(blank.checked, "no evidence box is ticked").toBe(0);
		await form.getByLabel("Device model").fill("Simulator");
		await form.getByLabel("Firmware").fill("1.0");
		await form.getByLabel("Strategy code").fill("2");
		await form.getByLabel("Enum byte width").fill("1");
		await form.getByLabel("Bool byte width").fill("1");
		await form.getByLabel("Evidence note").fill("e2e form");
		await form.getByLabel("Write frame layout verified").check();
		await form.getByLabel("Apply sequence verified").check();
		await form.getByLabel("I verified these values on the hardware").check();
		await form.locator("button[type=submit]").click();
		await expect(form.locator(".is-invalid").first(), "submitting without choosing the sign conventions is rejected on the field").toBeVisible();
		expect(verifyPuts, "the rejected form sent no verification request").toEqual([]);
		await page.locator("#energy-expert-mode").uncheck();
	});

	test("the hardware verification endpoint accepts one complete attestation @energy", async () => {
		// A fresh dispatch store ships unverified hardware; verify it through the atomic admin endpoint.
		const status = await page.evaluate(async () => {
			const { csrf_token: csrf } = await (await fetch("/admin/api/session", { credentials: "same-origin" })).json();
			const response = await fetch("/admin/api/energy/devices/sim/hardware-verification", {
				method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
				body: JSON.stringify({
					verified_device_model: "Simulator", verified_firmware: "1.0", note: "e2e", soc_strategy_external_code: 2,
					enum_byte_width: 1, bool_byte_width: 1, write_frame_layout_verified: true, apply_sequence_verified: true,
					battery_discharge_positive: true, grid_import_positive: true, soc_target_unit: "ratio",
				}),
			});
			return response.status;
		});
		expect(status, "the hardware verification endpoint accepts one complete attestation").toBe(200);
	});

	test("the setup story raised no console, network or dialog problems @energy", async () => {
		expect(watch.problems, "no console errors, page errors, failed requests, HTTP >= 400 or native dialogs").toEqual([]);
		expect([...watch.external], "no request leaves the gateway").toEqual([]);
	});
});
