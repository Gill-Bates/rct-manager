//
// tests/e2e/admin-settings-form.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Settings page: card grid, live autosave, rejected autosave with revert, number field a11y, no restart notice.
// Replaces admin_flow.mjs lines 1263-1339. Own gateway: the story changes docs_public and the log level.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { expectedStatus, trackProblems } from "./support/problems.mjs";
test.describe("settings form @settings", () => {
	const fresh = freshGateway("default");
	test("settings grid, docs autosave and invalid autosave revert; number field a11y and no restart notice @settings", async ({ browser, browserName }) => {
		const { context, page } = await fresh.newPage(browser);
		try {
			const base = fresh.gateway.base;
			const tracked = trackProblems(page, browserName, base);
			const toastText = async () => (await page.locator("#toast-region").innerText()).trim();
			await page.goto("/ui/settings");
			await page.waitForSelector("#setting-docs_public");
			// Cards in the same .settings-grid row share one height.
			const rows = await page.evaluate(() => {
				const byTop = new Map();
				for (const card of document.querySelectorAll(".settings-grid > .card")) {
					const box = card.getBoundingClientRect();
					const key = Math.round(box.top);
					if (!byTop.has(key)) byTop.set(key, []);
					byTop.get(key).push(Math.round(box.height));
				}
				return [...byTop.values()];
			});
			expect(rows.some((r) => r.length > 1), `settings page renders more than one card per row ${JSON.stringify(rows)}`).toBe(true);
			for (const r of rows) expect(Math.max(...r) - Math.min(...r), `settings cards in one row share the same height ${JSON.stringify(rows)}`).toBeLessThanOrEqual(1);
			const before = await page.isChecked("#setting-docs_public");
			await page.locator("#setting-docs_public").setChecked(!before);
			await page.waitForFunction(() => document.getElementById("toast-region").textContent.includes("saved"), null, { timeout: 8000 });
			expect(await toastText(), "docs toggle autosaves live (saved toast)").toContain("saved");
			expect((await fetch(`${base}/docs`)).status === 200, "docs toggle active immediately").toBe(!before);
			// Trust settings ask for confirmation before they autosave.
			const invalidAutosaveStart = tracked.problems.length;
			await page.locator("#setting-trusted_proxies").fill("not-an-ip");
			await page.locator("#setting-trusted_proxies").dispatchEvent("change");
			await page.waitForSelector("#confirm-modal.show");
			await page.click("#confirm-accept");
			await page.waitForFunction(() => document.querySelector("#toast-region .alert-danger"), null, { timeout: 8000 });
			// fill() already fires a change event, so two saves may be in flight; wait for the final revert.
			await expect.poll(() => page.inputValue("#setting-trusted_proxies"), { message: "invalid autosave reverts the field", timeout: 8000 }).toBe("");
			// The toast is the visibility floor; the persistent alert is what survives it. No Retry here: the
			// per-key rollback already restored the committed value, so there is nothing left to resend.
			await page.waitForSelector("#save-error-general", { timeout: 8000 });
			expect(await page.locator("#save-error-general").innerText(), "a failed scalar save leaves a persistent alert at the section").toMatch(/was restored/);
			await expect(page.locator('#save-error-general button:has-text("Retry")'), "the general alert offers no Retry").toHaveCount(0);
			await expect(page.locator("#save-state"), '#save-state reads "Save failed" after a rejected save').toContainText("Save failed");
			// The rejected trusted_proxies save(s) answered 400 on purpose (one or two in flight).
			tracked.scrub(invalidAutosaveStart, expectedStatus(`${base}/admin/api/settings`, 400));
			// a11y: an out-of-range number field marks itself and references a non-empty error message. An invalid
			// value is never written to the draft, so nothing is saved beyond putting the original value back.
			const bindPortBefore = await page.inputValue("#setting-bind_port");
			await page.locator("#setting-bind_port").fill("10");
			await page.locator("#setting-bind_port").dispatchEvent("change");
			await expect(page.locator("#setting-bind_port"), "an out-of-range number field sets aria-invalid").toHaveAttribute("aria-invalid", "true");
			const fieldError = await page.evaluate(() => {
				const control = document.getElementById("setting-bind_port");
				const ids = (control.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
				return { ids, texts: ids.map((id) => document.getElementById(id)?.textContent.trim() || "") };
			});
			expect(fieldError.ids, `the field error is referenced ${JSON.stringify(fieldError)}`).toContain("setting-bind_port-error");
			expect(fieldError.texts.every(Boolean), `the field error is non-empty ${JSON.stringify(fieldError)}`).toBe(true);
			// A later valid value clears the marking again.
			await page.locator("#setting-bind_port").fill(bindPortBefore);
			await page.locator("#setting-bind_port").dispatchEvent("change");
			await expect.poll(() => page.getAttribute("#setting-bind_port", "aria-invalid"), { message: "a valid value clears aria-invalid again" }).toBeNull();
			await page.locator("#setting-log_level").selectOption("DEBUG");
			await expect.poll(async () => (await (await context.request.get(`${base}/admin/api/settings`)).json()).settings.log_level, { message: "the log level is saved", timeout: 8000 }).toBe("DEBUG");
			await expect(page.locator("#restart-notice"), "a settings change shows no restart notice").toHaveCount(0);
			expect(await page.locator("main").innerText(), "no restart wording anywhere on the page").not.toMatch(/restart/i);
			const settingsApi = await page.evaluate(async () => (await fetch("/admin/api/settings", { credentials: "same-origin" })).json());
			expect(settingsApi.settings.log_level, "the new log level is the saved one").toBe("DEBUG");
			expect("restart_required" in settingsApi, "no restart is pending").toBe(false);
			expect(tracked.problems, "no unexpected console errors, failed requests or dialogs").toEqual([]);
			expect([...tracked.external], "no request leaves the gateway").toEqual([]);
		} finally {
			await context.close();
		}
	});
});
