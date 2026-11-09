//
// tests/e2e/support/problems.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Console/page-error collector shared by the ported drivers. WebKit-only environment noise (also seen
// on /login) is filtered exactly as in the former drivers; everything else is kept.
const WEBKIT_NOISE = /^Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'|\/health due to access control checks|Blocked by Web Inspector/;
/** Collects console errors/warnings and page errors of `page`; `browserName` selects the WebKit filter. */
export function collectProblems(page, browserName) {
	const problems = [];
	const noise = (text) => browserName === "webkit" && WEBKIT_NOISE.test(text);
	page.on("console", (m) => { if (!noise(m.text()) && ["error", "warning"].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
	page.on("pageerror", (e) => { if (!noise(e.message)) problems.push(`pageerror: ${e.message}`); });
	return problems;
}
/** Routes the planning/energy device list so the simulated inverter reports `mode()` (default manual). */
export async function reportInverterAs(page, mode = () => "manual") {
	await page.route(/\/admin\/api\/energy\/(planning-)?devices$/, async (route) => {
		const response = await route.fetch();
		const body = await response.json().catch(() => null);
		if (Array.isArray(body)) for (const device of body) device.mode = mode();
		await route.fulfill({ response, json: body ?? undefined });
	});
}
/**
 * Whole-page problem collector of the former admin_flow `trackProblems`: console errors/warnings, page
 * errors, failed requests (minus navigation aborts), HTTP >= 400, unexpected native dialogs, and requests
 * to hosts other than the gateway. `scrub(start, matches)` removes only problems from index `start` on
 * that a deliberate fault produced; `problems.length` marks that start.
 */
export function trackProblems(page, browserName, base) {
	const problems = [];
	const external = new Set();
	const noise = (text) => browserName === "webkit" && WEBKIT_NOISE.test(text);
	page.on("console", (m) => { if (!noise(m.text()) && ["error", "warning"].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()} @ ${m.location().url}`); });
	page.on("pageerror", (e) => { if (!noise(e.message)) problems.push(`pageerror: ${e.message}`); });
	// Chromium aborts in-flight health probes, the SSE stream and the energy status poll when a navigation or reload replaces the page.
	const navigationAbort = (r) => r.failure()?.errorText === "net::ERR_ABORTED" && r.method() === "GET"
		&& /\/(health|admin\/api\/events|admin\/api\/energy\/devices)$/.test(new URL(r.url()).pathname);
	page.on("requestfailed", (r) => {
		const text = r.failure()?.errorText || "";
		// WebKit reports in-flight loads dropped by a navigation as failed; Chromium does not, so real aborts still show.
		if (text === "Load request cancelled" || navigationAbort(r) || noise(text)) return;
		problems.push(`requestfailed: ${r.method()} ${r.url()} (${text || "unknown"})`);
	});
	page.on("response", (r) => { if (r.status() >= 400) problems.push(`http ${r.status()}: ${r.request().method()} ${r.url()}`); });
	page.on("request", (r) => { if (!r.url().startsWith(base) && !r.url().startsWith("data:")) external.add(r.url()); });
	// The GUI uses themed modals, so any native dialog is unexpected: dismiss it and record it.
	page.on("dialog", (d) => { problems.push(`unexpected native ${d.type()} dialog: ${d.message()}`); return d.dismiss(); });
	const scrub = (start, matches) => {
		const kept = problems.slice(start).filter((problem) => !matches(problem));
		problems.splice(start, problems.length - start, ...kept);
	};
	return { problems, external, scrub };
}
/** A deliberate HTTP error shows up twice: as the response and as the browser's console line. */
export const expectedStatus = (url, ...statuses) => (p) => statuses.some((st) =>
	(p.startsWith(`http ${st}: `) || (p.startsWith("console ") && p.includes(`status of ${st}`))) && p.includes(url));
