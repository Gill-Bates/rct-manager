//
// tests/e2e/support/energy-setup.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Operator-style API setup of the Energy page for the layout audits: approve the required write
// registers and accept the hardware verification of the simulated inverter (the compact flow view and
// the full controls only render for a hardware-verified device). Both calls are idempotent.
import { expect } from "@playwright/test";
const VERIFICATION = {
	verified_device_model: "Simulator", verified_firmware: "1.0", note: "e2e", soc_strategy_external_code: 2,
	enum_byte_width: 1, bool_byte_width: 1, write_frame_layout_verified: true, apply_sequence_verified: true,
	battery_discharge_positive: true, grid_import_positive: true, soc_target_unit: "ratio",
};
async function csrf(request) {
	const response = await request.get("/admin/api/session");
	expect(response.status(), "admin session answers").toBe(200);
	// State-changing admin calls must look same-origin (Origin header) and echo the CSRF token.
	return { "X-CSRF-Token": (await response.json()).csrf_token, Origin: new URL(response.url()).origin };
}
/** PUT the required write registers as approved (as an operator would in Settings). */
export async function approveRequiredWrites(request) {
	const headers = await csrf(request);
	const { exposed_names: exposed } = await (await request.get("/admin/api/parameters")).json();
	const [{ required_write_names: required }] = await (await request.get("/admin/api/energy/devices")).json();
	const response = await request.put("/admin/api/parameters", { headers, data: { exposed_names: exposed, write_names: required } });
	return response.status();
}
/** PUT an accepted hardware verification for the simulated inverter `sim`. */
export async function acceptHardwareVerification(request) {
	const headers = await csrf(request);
	const response = await request.put("/admin/api/energy/devices/sim/hardware-verification", { headers, data: VERIFICATION });
	return response.status();
}
/** Run `fn(request)` with an API request context that carries the admin session. */
export async function withAdminRequest(browser, gateway, adminState, fn) {
	const context = await browser.newContext({ storageState: adminState, baseURL: gateway.base });
	try {
		return await fn(context.request);
	} finally {
		await context.close();
	}
}
