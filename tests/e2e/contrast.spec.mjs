//
// tests/e2e/contrast.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// WCAG contrast of the Energy tab links and the Expert mode switch, measured in a REAL browser from
// getComputedStyle in whichever theme the project selects (@theme-matrix). The admin.css tokens are only
// trusted once a rendered page proves their ratios. Targets (WCAG 2.2): tab-link text 1.4.3 >= 4.5:1;
// switch outline and knob 1.4.11 >= 3:1. A disabled control is exempt (WCAG 1.4.11 excludes inactive
// components) and only reported. axe-core is covered by axe.spec.mjs; this spec keeps the measured
// ratios. Replaces contrast_audit.mjs.
import { expect, test } from "./support/fixtures.mjs";
import { reportInverterAs } from "./support/problems.mjs";
import { settle } from "./support/settle.mjs";
test.use({ gatewayMode: "planning" });
// WCAG ratio math, ported verbatim from tests/test_admin_design_rules_badges.py: the browser hands over
// resolved rgb()/rgba() strings, so only those (and hex) are parsed.
function rgba(value) {
	const hex = value.trim().match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
	if (hex) {
		let h = hex[1];
		if (h.length === 3) h = h.split("").map((c) => c + c).join("");
		return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
	}
	const numbers = (value.match(/[\d.]+/g) || []).map(Number);
	return [numbers[0], numbers[1], numbers[2], numbers.length > 3 ? numbers[3] : 1];
}
function luminance([r, g, b]) {
	const lin = [r, g, b].map((c) => {
		const s = c / 255;
		return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}
// fg composited over bg, then the standard (L1+0.05)/(L2+0.05).
function contrast(fg, bg) {
	const [r, g, b, a] = rgba(fg);
	const back = rgba(bg).slice(0, 3);
	const mixed = [r, g, b].map((c, i) => c * a + back[i] * (1 - a));
	const [l1, l2] = [luminance(mixed), luminance(back)].sort((x, y) => y - x);
	return (l1 + 0.05) / (l2 + 0.05);
}
// Effective background behind an element: walk up to the first non-transparent background-color
// (how a sighted user perceives the colour behind the text). Defined as a function passed to evaluate
// because the CSP forbids injecting an inline <script>.
const installEffectiveBg = (page) => page.evaluate(() => {
	window.__effectiveBg = (el) => {
		for (let node = el; node; node = node.parentElement) {
			const bg = getComputedStyle(node).backgroundColor;
			const m = (bg.match(/[\d.]+/g) || []).map(Number);
			const alpha = m.length > 3 ? m[3] : 1;
			if (bg && bg !== "transparent" && alpha > 0) return bg;
		}
		return getComputedStyle(document.body).backgroundColor;
	};
});
async function openEnergy(page) {
	// Spot Market, Weather and Charging plan stay hidden until an inverter is Manual (product contract, F3);
	// the idle tab links measured below must be visible, so the simulated inverter is reported as Manual.
	await reportInverterAs(page);
	await page.goto("/ui/energy");
	await page.waitForSelector(".energy-tabs .nav-link");
	await page.waitForSelector("#energy-expert-mode");
	await installEffectiveBg(page);
}
const theme = () => (test.info().project.use.colorScheme === "dark" ? "dark" : "light");
test.describe("contrast audit @contrast @theme-matrix", () => {
	test("page renders in the selected theme @contrast", async ({ adminPage: page }) => {
		await openEnergy(page);
		// A token read below only counts if the theme we claim to measure actually rendered.
		const rendered = await page.evaluate(() => document.documentElement.getAttribute("data-bs-theme"));
		expect(rendered, `page renders in the ${theme()} theme (data-bs-theme=${rendered})`).toBe(theme());
	});
	test("energy tab link text contrast per state @contrast", async ({ adminPage: page }) => {
		await openEnergy(page);
		const idleSel = ".energy-tabs .nav-link:not(.active)";
		const activeSel = ".energy-tabs .nav-link.active";
		const read = (selector) => page.evaluate((sel) => {
			const el = document.querySelector(sel);
			return { color: getComputedStyle(el).color, bg: window.__effectiveBg(el) };
		}, selector);
		const findings = {};
		findings.idle = await read(idleSel);
		await page.locator(idleSel).first().hover();
		await settle(page, "tab hover transition finished");
		findings.hover = await read(idleSel);
		await page.mouse.move(0, 0);
		findings.active = await read(activeSel);
		await page.locator(idleSel).first().focus();
		await settle(page, "tab focus transition finished");
		findings.focus = await read(idleSel);
		await page.locator(idleSel).first().blur();
		await test.info().attach(`contrast-tab-links-${theme()}.json`, { body: JSON.stringify(findings, null, 2), contentType: "application/json" });
		for (const [state, data] of Object.entries(findings)) {
			const ratio = contrast(data.color, data.bg);
			expect.soft(ratio, `tab link ${state} text >= 4.5:1 (${theme()}): ${ratio.toFixed(2)}:1 (${data.color} on ${data.bg})`).toBeGreaterThanOrEqual(4.5);
		}
	});
	test("Expert switch outline and knob contrast per state @contrast", async ({ adminPage: page }) => {
		await openEnergy(page);
		const SWITCH = "#energy-expert-mode";
		// The knob is a background-image SVG; the fill can be a hex (url-encoded %23) or an rgba() with
		// alpha (Bootstrap's low-contrast default knob). A translucent fill is composited by contrast().
		const knobFillFrom = (candidates) => {
			for (const raw of candidates) {
				const hit = decodeURIComponent(raw).match(/fill=['"]?(#[0-9a-fA-F]{3,8}|rgba?\([^'")]+\))/);
				if (hit) return hit[1];
			}
			return null;
		};
		// Bootstrap exposes the knob through --bs-form-switch-bg, but the resolved shorthand is what paints;
		// read both and keep whichever carries the data URI, so the measurement never depends on the engine.
		const readSwitch = () => page.evaluate((sel) => {
			const el = document.querySelector(sel);
			const cs = getComputedStyle(el);
			return {
				border: cs.borderTopColor, track: cs.backgroundColor,
				knobCandidates: [cs.getPropertyValue("--bs-form-switch-bg"), cs.backgroundImage].filter((v) => v && v.includes("svg")),
				pageBg: window.__effectiveBg(el.closest(".card, .energy-panel, body") || el),
				checked: el.checked, disabled: el.disabled,
			};
		}, SWITCH);
		const switchState = async (name) => {
			// The knob SVG background resolves lazily: poll the measured value instead of sleeping.
			await expect.poll(async () => knobFillFrom((await readSwitch()).knobCandidates), { message: `switch ${name} knob fill resolved (${theme()})` }).not.toBeNull();
			await settle(page, `switch ${name} transition finished`);
			const s = await readSwitch();
			const knobFill = knobFillFrom(s.knobCandidates);
			const transparentTrack = !s.track || s.track === "rgba(0, 0, 0, 0)";
			const outline = { vsPage: contrast(s.border, s.pageBg), vsTrack: transparentTrack ? null : contrast(s.border, s.track) };
			// The knob sits on the track; where the track is transparent (off state) the page shows through.
			const knobBg = transparentTrack ? s.pageBg : s.track;
			return { name, checked: s.checked, disabled: s.disabled, border: s.border, track: s.track, pageBg: s.pageBg, knobFill, knobBg, outline, knobRatio: knobFill ? contrast(knobFill, knobBg) : null };
		};
		const findings = {};
		await page.locator(SWITCH).scrollIntoViewIfNeeded();
		findings.off = await switchState("off");
		await page.locator(SWITCH).focus();
		findings.focus = await switchState("focus");
		await page.locator(SWITCH).check();
		findings.on = await switchState("on");
		await page.locator(SWITCH).uncheck();
		await settle(page, "switch unchecked");
		// Disabled is set programmatically so its rendering can be captured (reported, exempt).
		await page.evaluate((sel) => { document.querySelector(sel).disabled = true; }, SWITCH);
		findings.disabled = await switchState("disabled");
		await page.evaluate((sel) => { document.querySelector(sel).disabled = false; }, SWITCH);
		await test.info().attach(`contrast-switch-${theme()}.json`, { body: JSON.stringify(findings, null, 2), contentType: "application/json" });
		await test.info().attach(`energy-${theme()}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
		// off/focus are item 1's own tokens (--rct-switch-off outline, --rct-switch-knob-off knob): outline
		// (vs page and vs the track's effective background) and knob are gated at >= 3:1. The checked state
		// keeps Bootstrap's filled brand look (--rct-brand, out of scope): only its white knob against the brand
		// track is gated; the track against the page is informational. Disabled is exempt and reported.
		for (const state of ["off", "focus"]) {
			const d = findings[state];
			const detail = `vsPage ${d.outline.vsPage.toFixed(2)}:1${d.outline.vsTrack !== null ? `, vsTrack ${d.outline.vsTrack.toFixed(2)}:1` : ""} (${d.border})`;
			expect.soft(d.outline.vsPage, `switch ${state} outline vs page >= 3:1 (${theme()}): ${detail}`).toBeGreaterThanOrEqual(3);
			if (d.outline.vsTrack !== null) expect.soft(d.outline.vsTrack, `switch ${state} outline vs track >= 3:1 (${theme()}): ${detail}`).toBeGreaterThanOrEqual(3);
			expect.soft(d.knobRatio, `switch ${state} knob fill resolved (${theme()})`).not.toBeNull();
			expect.soft(d.knobRatio, `switch ${state} knob >= 3:1 (${theme()}): ${d.knobRatio?.toFixed(2)}:1 (${d.knobFill} on ${d.knobBg})`).toBeGreaterThanOrEqual(3);
		}
		const on = findings.on;
		expect.soft(on.knobRatio, `switch on knob >= 3:1 (${theme()}): ${on.knobRatio?.toFixed(2)}:1 (${on.knobFill} on ${on.knobBg})`).toBeGreaterThanOrEqual(3);
		const dis = findings.disabled;
		test.info().annotations.push({ type: "info", description: `switch on brand track vs page (informational, out of scope): ${on.outline.vsPage.toFixed(2)}:1; disabled (exempt): outline vsPage ${dis.outline.vsPage.toFixed(2)}:1, knob ${dis.knobRatio ? dis.knobRatio.toFixed(2) + ":1" : "n/a"}` });
	});
});
