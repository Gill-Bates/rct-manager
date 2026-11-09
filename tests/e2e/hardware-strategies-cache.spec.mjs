//
// tests/e2e/hardware-strategies-cache.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Cache-only contract of /admin/api/energy/hardware-strategies (no stub): the endpoint is served from
// the gateway's in-memory metric cache and never starts inverter I/O on a page load, so repeated polls
// over ~16 s answer 200 with a well-formed device entry (device_id, boolean stale flag), never a crash
// or a broken payload.
//
// NOT covered: the 10 s cache TTL and the stale flip. The strategy register only enters the cache
// through an explicit mode/action path that needs a hardware-verified Energy Manager setup, which the
// simulated inverter does not provide, so `code` stays null here.
import { expect, test } from "./support/fixtures.mjs";

// energy mode: write support on and the dispatch store isolated, so the Energy page's Operate poll
// reads power_mng_soc_strategy into the cache.
test.use({ gatewayMode: "energy" });

const SAMPLES = 9;
const CADENCE_MS = 2000;

test("hardware-strategies endpoint is cache-only and well-formed over repeated polls @cache @slow", async ({ adminPage: page }) => {
	await page.goto("/ui/energy");
	await expect(page.locator(".energy-tabs .nav-link").first()).toBeVisible();
	const read = () => page.evaluate(async () => {
		const res = await fetch("/admin/api/energy/hardware-strategies", { headers: { "Cache-Control": "no-cache" }, cache: "no-store" });
		return { status: res.status, body: await res.json() };
	});
	// Precondition (replaces a fixed 3 s sleep): the Operate poll has registered the device. Samples
	// are taken and checked only after this; no sample is discarded.
	await expect.poll(async () => (await read()).body[0]?.device_id, {
		message: "Operate poll registered the simulated device", timeout: 15_000,
	}).toBe("sim");
	// Sample across ~16 s (longer than the 10 s TTL) at a 2 s cadence; every call is a REAL request.
	// The cadence is a deliberate measurement window, driven by the clock instead of a sleep.
	const samples = [];
	const startedAt = Date.now();
	for (let i = 0; i < SAMPLES; i++) {
		const due = startedAt + i * CADENCE_MS;
		await expect.poll(() => Date.now() >= due, { intervals: [100], timeout: CADENCE_MS + 5000 }).toBe(true);
		const r = await read();
		samples.push({ t: i * 2, status: r.status, device: r.body[0] });
	}
	const spanSeconds = samples[samples.length - 1].t - samples[0].t;
	const summary = JSON.stringify(samples.map((s) => s.device && { code: s.device.code, stale: s.device.stale }));
	await test.info().attach("hardware-strategies-samples.json", { body: JSON.stringify(samples, null, 2), contentType: "application/json" });
	expect(spanSeconds, "sampling spans more than the 10 s cache TTL").toBeGreaterThan(10);
	expect(samples.map((s) => s.status),
		"/hardware-strategies answers 200 on every real poll over the sampling span").toEqual(Array(SAMPLES).fill(200));
	for (const s of samples) {
		expect(s.device?.device_id, `every response carries the device entry (t=${s.t}s): ${summary}`).toBe("sim");
		expect(typeof s.device?.stale, `the stale flag is boolean, never a broken payload (t=${s.t}s)`).toBe("boolean");
	}
});
