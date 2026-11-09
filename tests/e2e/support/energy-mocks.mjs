//
// tests/e2e/support/energy-mocks.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Route mocks of the Energy page specs (former admin_flow.mjs section 6e). The data is stubbed so the
// specs exercise the shipped rendering only.
const reading = (value) => ({ value, age_seconds: 1, stale: false });

/**
 * Measured values for the panel's own readings-derived text and the hardware SOC strategy list.
 * Returns the mutable state: `state.noLimits` drops the limits of every device, `state.strategy` is the
 * answer of /admin/api/energy/hardware-strategies.
 */
export async function installEnergyMocks(page) {
	const state = {
		noLimits: false,
		strategy: { device_id: "sim", device_name: "RCT inverter", code: 127, label: null, stale: false },
	};
	await page.route("**/admin/api/energy/devices", async (route) => {
		if (route.request().method() !== "GET") return route.continue();
		// A fetch that outlives the energy server must not become an unhandled rejection that hides the real failure.
		try {
			const response = await route.fetch();
			const list = await response.json();
			for (const device of list) {
				if (state.noLimits) device.limits = null;
				device.readings = {
					battery_soc_percent: reading(54), grid_power_w: reading(1200), pv_power_w: reading(1500),
					house_load_w: reading(800), battery_power_w: reading(-900),
				};
			}
			await route.fulfill({ response, json: list });
		} catch {
			await route.abort().catch(() => { });
		}
	});
	await page.route("**/admin/api/energy/hardware-strategies", (route) => route.fulfill({ json: [state.strategy] }));
	return state;
}

/** Complete hardware attestation of the simulated inverter (same body as the former driver). */
export const ATTESTATION = {
	verified_device_model: "Simulator", verified_firmware: "1.0", note: "e2e", soc_strategy_external_code: 2,
	enum_byte_width: 1, bool_byte_width: 1, write_frame_layout_verified: true, apply_sequence_verified: true,
	battery_discharge_positive: true, grid_import_positive: true, soc_target_unit: "ratio",
};

/** One slot of the stubbed charging plan. */
export const slot = (start, end, action, watts, soc, reason) => ({ start, end, action, max_power_w: watts, target_soc_percent: soc, reason });

/** The stubbed charging plan of the planning/charts specs. */
export function stubPlan() {
	return {
		device_id: "sim", status: "ok", detail: null, created_at: "2026-10-16T11:00:00+00:00", valid_until: "2026-10-17T14:00:00+00:00",
		horizon_hours: 26, preview_only: true, window: {}, forecast_basis: "adaptive_history", forecast_confidence: "normal",
		battery: { soc_percent: 33, usable_capacity_wh: 10370, remaining_energy_wh: 3422.1, max_charge_power_w: 3000, max_discharge_power_w: 3000, measured_at: "2026-10-16T11:59:00+00:00" },
		slots: [
			slot("2026-10-16T12:00:00+00:00", "2026-10-16T15:15:00+00:00", "idle", null, null, "forecast PV covers demand"),
			slot("2026-10-16T15:15:00+00:00", "2026-10-16T17:15:00+00:00", "charge", 3000, 62, "forecast deficit, charged as late as permitted"),
			slot("2026-10-16T17:15:00+00:00", "2026-10-17T01:00:00+00:00", "idle", null, null, "battery reserve covers demand"),
			slot("2026-10-17T01:00:00+00:00", "2026-10-17T03:30:00+00:00", "charge", 4200, 90, "negative spot price, forecast deficit"),
			slot("2026-10-17T03:30:00+00:00", "2026-10-17T13:45:00+00:00", "idle", null, null, "battery reserve covers demand"),
		],
	};
}

/** Midnight UTC of today and four consecutive consumption days before it. */
export function stubDay() {
	const day = new Date();
	day.setUTCHours(0, 0, 0, 0);
	return day;
}
export function consumptionDays(day) {
	return [4, 3, 2, 1].map((back, index) => ({
		date: new Date(day.getTime() - back * 86400000).toISOString().slice(0, 10),
		energy_wh: 9000 + index * 500, source: index % 2 ? "inverter_history" : "local_measured",
	}));
}

/** Installs the plan, consumption, spot market and weather stubs; returns the mutable state. */
export async function installPlanMocks(page) {
	const day = stubDay();
	const plan = stubPlan();
	const state = {
		planStatus: "ok",
		planRequests: 0,
		consumption: { days: consumptionDays(day), recording_since: "2026-10-01T00:00:00+00:00" },
		consumptionGate: null,
	};
	await page.route("**/admin/api/energy/devices/*/dynamic-plan", (route) => {
		state.planRequests += 1;
		return route.fulfill({ json: state.planStatus === "ok" ? plan : { status: state.planStatus, slots: [], detail: null } });
	});
	await page.route("**/admin/api/energy/devices/*/consumption-history", async (route) => {
		if (state.consumptionGate) await state.consumptionGate;
		await route.fulfill({ json: state.consumption });
	});
	const hourly = (field, value) => Array.from({ length: 24 }, (_, hour) => ({
		start: new Date(day.getTime() + hour * 3600000).toISOString(), end: new Date(day.getTime() + (hour + 1) * 3600000).toISOString(),
		[field]: value(hour),
	}));
	await page.route("**/admin/api/energy/spot-market/preview", (route) => route.fulfill({
		json: {
			status: "connected", provider: "awattar", bidding_zone: "DE-LU", currency: "EUR", timezone: "UTC", stale: false,
			min_price_eur_per_kwh: 0.05, min_price_at: day.toISOString(), max_price_eur_per_kwh: 0.3, max_price_at: day.toISOString(),
			yesterday_intervals: [], today_intervals: hourly("price_eur_per_kwh", (hour) => 0.05 + hour * 0.01), next_day_intervals: [],
		},
	}));
	await page.route("**/admin/api/energy/weather/preview", (route) => route.fulfill({
		json: {
			status: "connected", source: "Open-Meteo", timezone: "UTC", stale: false, energy_next_24h_kwh: 12.3,
			covered_until: new Date(day.getTime() + 86400000).toISOString(),
			yesterday_intervals: [], today_intervals: hourly("power_kw", (hour) => Math.max(0, 4 - Math.abs(hour - 12) * 0.5)), next_day_intervals: [],
		},
	}));
	return state;
}

/** Number of Chart.js instances whose canvas sits inside `selector`. */
export const chartsIn = (page, selector) => page.evaluate((sel) => Object.values(window.Chart.instances).filter((chart) => chart.canvas.closest(sel)).length, selector);

/** The toggle cycles Auto -> Light -> Dark; click until the resolved theme matches. */
export async function clickThemeUntil(page, theme) {
	for (let i = 0; i < 4; i += 1) {
		if ((await page.locator("html").getAttribute("data-bs-theme")) === theme) return;
		await page.locator("#theme-toggle").click();
	}
}
