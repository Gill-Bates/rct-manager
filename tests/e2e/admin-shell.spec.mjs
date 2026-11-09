//
// tests/e2e/admin-shell.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Shell of the admin GUI: security headers, per-page overflow, theme toggle and navbar.
// Replaces admin_flow.mjs line 806 (CSP) and lines 959-1028 (layouts, theme, navbar).
import { expect, test } from "./support/fixtures.mjs";
import { trackProblems } from "./support/problems.mjs";
import { settle } from "./support/settle.mjs";
// Must match the key written by toggleTheme() in app/admin/static/js/admin-core.js.
const THEME_KEY = "rct-admin-theme";
const PAGES = ["/ui/dashboard", "/ui/inverters", "/ui/tsdb", "/ui/prometheus", "/ui/tokens", "/ui/settings", "/ui/about"];
const VIEWPORTS = [[1280, 800], [390, 844], [320, 640]];
// The toggle cycles Auto -> Light -> Dark; click until the resolved theme matches.
async function clickThemeUntil(page, theme) {
	for (let i = 0; i < 4; i += 1) {
		if ((await page.locator("html").getAttribute("data-bs-theme")) === theme) return;
		await page.locator("#theme-toggle").click();
	}
}
test.describe("admin shell @shell", () => {
	test("CSP header is present @shell", async ({ adminPage: page }) => {
		const response = await page.context().request.get("/ui/dashboard");
		expect(response.status(), "the dashboard answers 200").toBe(200);
		const csp = response.headers()["content-security-policy"];
		expect(csp, "CSP header present").toBeTruthy();
		expect(csp, "CSP header restricts scripts to the own origin").toContain("script-src 'self'");
	});
	for (const scheme of ["light", "dark"]) {
		test(`no horizontal overflow on every page and viewport (${scheme}) @shell @layout`, async ({ adminPage: page, gateway, browserName }) => {
			const tracked = trackProblems(page, browserName, gateway.base);
			await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [THEME_KEY, scheme]);
			for (const [width, height] of VIEWPORTS) {
				await page.setViewportSize({ width, height });
				for (const url of PAGES) {
					await page.goto(url);
					await settle(page, `${url} settles at ${width}px`);
					if (scheme === "dark") {
						expect(await page.locator("html").getAttribute("data-bs-theme"), `dark mode active ${url} @${width}`).toBe("dark");
					}
					const extra = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
					expect(extra, `no horizontal overflow ${scheme} ${url} @${width} (overflow=${extra})`).toBeLessThanOrEqual(0);
				}
			}
			expect(tracked.problems, "no console errors, page errors, failed requests or native dialogs while walking the pages").toEqual([]);
			expect([...tracked.external], "no request leaves the gateway").toEqual([]);
		});
	}
	test("theme toggle cycles; dark mode active; header stays sticky @shell", async ({ adminPage: page }) => {
		await page.goto("/ui/dashboard");
		await page.evaluate(([key, value]) => localStorage.setItem(key, value), [THEME_KEY, "dark"]);
		await page.goto("/ui/dashboard");
		const themePref = () => page.evaluate(() => document.documentElement.getAttribute("data-theme-pref"));
		const themeBefore = await themePref();
		await page.click("#theme-toggle");
		const themeAfter = await themePref();
		expect([themeBefore, themeAfter], `theme toggle cycles dark -> auto (${themeBefore} -> ${themeAfter})`).toEqual(["dark", "auto"]);
		await clickThemeUntil(page, "dark");
		expect(await page.evaluate(() => document.documentElement.getAttribute("data-bs-theme")), "dark mode active").toBe("dark");
		await page.click("#theme-toggle");
		expect(await page.evaluate(() => getComputedStyle(document.querySelector(".admin-navbar")).position), "header stays sticky").toBe("sticky");
	});
	test("navbar label, logo size and header button touch targets @shell", async ({ adminPage: page }) => {
		await page.goto("/ui/dashboard");
		const navbar = () => page.evaluate(() => {
			const box = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { w: r.width, h: r.height }; };
			return {
				logo: box(".nav-logo"),
				brand: document.querySelector(".navbar-brand span").textContent.trim(),
				title: document.title,
				header: box(".admin-navbar"),
				theme: box("#theme-toggle"),
				logout: box("#logout-button"),
				menu: box("#nav-toggle"),
			};
		});
		await settle(page, "desktop navbar settles");
		let nav = await navbar();
		expect(nav.brand, "navbar label shortened to Administration").toBe("Administration");
		expect(nav.title, "browser tab title still says RCT Administration").toMatch(/RCT Administration/);
		// 3.1rem x 1.8rem before, 5.1rem x 2.78rem now (1.64x) at the SVG's own 599:326 ratio.
		expect(Math.abs(nav.logo.w - 81.6), `logo width enlarged by about 1.5-1.8x ${JSON.stringify(nav.logo)}`).toBeLessThan(2);
		expect(Math.abs(nav.logo.h - 44.5), `logo height enlarged by about 1.5-1.8x ${JSON.stringify(nav.logo)}`).toBeLessThan(2);
		expect(Math.abs(nav.logo.w / nav.logo.h - 599 / 326), "logo keeps the SVG aspect ratio").toBeLessThan(0.05);
		expect(nav.logo.h, "enlarged logo fits inside the header").toBeLessThanOrEqual(nav.header.h);
		// 2.8rem (44.8px) before; 2.3rem (36.8px) now, still above the 2.2rem floor.
		for (const [name, button] of [["theme toggle", nav.theme], ["logout", nav.logout]]) {
			expect(button.h, `${name}: header buttons shrunk but keep a 2.2rem touch target (>= 35)`).toBeGreaterThanOrEqual(35);
			expect(button.h, `${name}: header buttons shrunk (< 40)`).toBeLessThan(40);
		}
		expect(Math.abs(nav.theme.w - nav.theme.h), "theme toggle is square at the smaller size").toBeLessThan(1);
		await page.setViewportSize({ width: 390, height: 844 });
		await settle(page, "mobile navbar settles");
		nav = await navbar();
		expect(nav.menu, "hamburger exists at mobile width").toBeTruthy();
		expect(Math.abs(nav.menu.h - nav.theme.h), "hamburger shares the smaller header button height").toBeLessThan(1);
		expect(nav.logo.h, "logo still fits the header at mobile width").toBeLessThanOrEqual(nav.header.h);
	});
});
