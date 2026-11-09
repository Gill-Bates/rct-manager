//
// tests/e2e/admin-tokens.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Personal access tokens: create, show once, use against the API, copy fallback inside the modal, revoke.
// Replaces admin_flow.mjs lines 1193-1262. One story on its own gateway (the token exists between the steps).
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
test.describe("personal access tokens @tokens", () => {
	const fresh = freshGateway("default");
	test("PAT form, create, show once, use, copy fallback in modal, revoke, empty state @tokens", async ({ browser, browserName }) => {
		const { context, page } = await fresh.newPage(browser);
		try {
			const tracked = trackProblems(page, browserName, fresh.gateway.base);
			const call = async (token) => (await fetch(`${fresh.gateway.base}/api/v1/devices/sim/metrics/grid_power`, { headers: { Authorization: `Bearer ${token}` } })).status;
			await page.goto("/ui/tokens");
			await page.waitForSelector("#tokens-list tr");
			expect(await page.locator(".token-table thead th").evaluateAll((n) => n.map((e) => e.textContent.trim())), "token table has the seven columns")
				.toEqual(["Name", "Permission", "Token", "Created", "Last used", "Expires", "Actions"]);
			expect(await page.locator("#open-add-token").isVisible(), "Add token button is shown").toBe(true);
			expect(await page.locator("#token-form").isVisible(), "the form is not permanently on the page").toBe(false);
			await page.click("#open-add-token");
			await page.waitForSelector("#add-token-modal.show #token-name", { state: "visible" });
			await expect(page.locator("#token-expires"), "expiry defaults to 90 days").toHaveValue("90d");
			await page.fill("#token-name", "e2e-monitor");
			await page.click("#token-form button[type=submit]");
			await page.waitForSelector("#new-token-result:not([hidden])");
			const token = (await page.locator("#new-token-value").innerText()).trim();
			expect(token.length, "result state shows a PAT").toBeGreaterThan(0);
			expect(await page.locator("#new-token-result").innerText(), "result state says the PAT is shown once").toContain("will not be shown again");
			await expect(page.locator("#tokens-list tr", { hasText: "e2e-monitor" }), "new row appears without reload").toHaveCount(1);
			expect(await call(token), "new PAT is accepted by the API").toBe(200);
			const copyGeometry = await page.locator("#copy-token").evaluate((button) => {
				const icon = button.querySelector(".material-icons");
				const box = button.getBoundingClientRect();
				const glyph = icon?.getBoundingClientRect();
				return glyph && {
					horizontalOffset: (glyph.left + glyph.width / 2) - (box.left + box.width / 2),
					verticalOffset: (glyph.top + glyph.height / 2) - (box.top + box.height / 2),
				};
			});
			expect(copyGeometry, "token copy icon exists").toBeTruthy();
			expect(Math.abs(copyGeometry.horizontalOffset), `token copy icon is centred horizontally ${JSON.stringify(copyGeometry)}`).toBeLessThanOrEqual(0.5);
			expect(Math.abs(copyGeometry.verticalOffset), `token copy icon is centred vertically ${JSON.stringify(copyGeometry)}`).toBeLessThanOrEqual(0.5);
			// Chromium treats loopback HTTP as secure and normally takes navigator.clipboard. Force its
			// rejection so the test exercises the real LAN/HTTP fallback inside Bootstrap's focus trap.
			await page.evaluate(() => {
				Object.defineProperty(navigator, "clipboard", {
					configurable: true,
					value: { writeText: () => Promise.reject(new DOMException("Clipboard denied", "NotAllowedError")) },
				});
				const originalExecCommand = document.execCommand.bind(document);
				window.__tokenCopyFallback = null;
				document.execCommand = (command) => {
					if (command !== "copy") return originalExecCommand(command);
					const textarea = document.activeElement;
					window.__tokenCopyFallback = { value: textarea?.value, insideOpenModal: Boolean(textarea?.closest(".modal.show")) };
					return true;
				};
			});
			await page.click("#copy-token");
			await page.waitForFunction(() => window.__tokenCopyFallback !== null);
			await page.waitForFunction(() => document.getElementById("toast-region").textContent.includes("Token copied."));
			const copied = await page.evaluate(() => window.__tokenCopyFallback);
			expect(copied?.value, "copying the PAT falls back with the token value").toBe(token);
			expect(copied.insideOpenModal, "the copy fallback runs inside the open modal").toBe(true);
			expect(await page.locator("#token-done").isDisabled(), "Done is enabled after copying").toBe(false);
			expect((await page.locator("#toast-region").innerText()).trim(), "copy toast is shown").toContain("Token copied.");
			await page.click("#token-done");
			await page.waitForSelector("#add-token-modal", { state: "hidden" });
			await page.waitForSelector(".modal-backdrop", { state: "detached" });
			expect(await page.evaluate((t) => !document.documentElement.outerHTML.includes(t) && !document.body.innerText.includes(t), token), "closing the modal removes the PAT from the DOM").toBe(true);
			await page.click('button[aria-label="Revoke token e2e-monitor"]');
			await page.waitForSelector("#confirm-modal.show");
			await page.click("#confirm-accept");
			await page.waitForFunction(() => !document.getElementById("tokens-list").textContent.includes("e2e-monitor"));
			expect(await call(token), "revoked PAT is rejected").toBe(401);
			await expect(page.locator('#tokens-list td[colspan="7"]'), "empty state returns after the last token is revoked").toHaveCount(1);
			expect(tracked.problems, "no console errors, failed requests or dialogs").toEqual([]);
			expect([...tracked.external], "no request leaves the gateway").toEqual([]);
		} finally {
			await context.close();
		}
	});
});
