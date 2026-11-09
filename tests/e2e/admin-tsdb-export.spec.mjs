//
// tests/e2e/admin-tsdb-export.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// TSDB export settings: master toggle, backend field cards, retention fields, Apply-only sending, withheld
// incomplete groups, secrets typed during an apply, small-screen grid.
// Replaces admin_flow.mjs lines 1707-1912. One serial story on its own gateway (export on, questdb saved,
// hostname set, then switched to influxdb_v2).
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
import { elapse } from "./support/timing.mjs";
import { settle } from "./support/settle.mjs";
const THEME_KEY = "rct-admin-theme";
test.describe("tsdb export settings @tsdb", () => {
	test.describe.configure({ mode: "serial" });
	const fresh = freshGateway("default");
	let context;
	let page;
	let tracked;
	// Bodies of every settings PUT; "nothing is sent" is asserted against this.
	const tsdbPuts = [];
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser));
		tracked = trackProblems(page, browserName, fresh.gateway.base);
		page.on("request", (r) => { if (r.method() === "PUT" && r.url().endsWith("/admin/api/settings")) tsdbPuts.push(r.postData()); });
	});
	test.afterAll(async () => { await context?.close(); });
	const settingsNow = async () => (await (await context.request.get(`${fresh.gateway.base}/admin/api/settings`)).json()).settings;
	const settingsPut = () => page.waitForResponse((r) => r.request().method() === "PUT" && r.url().endsWith("/admin/api/settings"));
	// A success toast sits bottom right, over the Apply button, and a pointer resting on it pauses its countdown;
	// so release the pointer and let earlier toasts leave before clicking, as a user would.
	const clickExportApply = async () => {
		await page.mouse.move(0, 0);
		await page.waitForFunction(() => document.querySelectorAll("#toast-region .alert").length === 0, null, { timeout: 15000 });
		await page.click("#export-apply");
	};
	const saved = () => page.waitForFunction(() => document.getElementById("save-state").dataset.state === "saved", null, { timeout: 8000 });
	const change = async (selector, value) => {
		await page.locator(selector).fill(value);
		await page.locator(selector).dispatchEvent("change");
	};

	test("export toggle and backend field cards @tsdb", async () => {
		await page.goto("/ui/tsdb");
		// Export is off by default and the dependent fields are hidden then; switch it on first.
		await page.waitForSelector("#setting-metrics_export_enabled");
		if (!(await page.locator("#setting-metrics_export_enabled").isChecked())) {
			const enabled = settingsPut();
			await page.locator("#setting-metrics_export_enabled").click({ force: true });
			await enabled;
		}
		await page.waitForSelector("#setting-db_type");
		let answer = settingsPut();
		await page.locator("#setting-metrics_export_enabled").click({ force: true });
		let response = await answer;
		expect(await page.locator(".export-card").count(), "disabled export keeps only the toggle card").toBe(1);
		expect(await page.locator("#setting-db_type, #setting-metrics_export_interval_seconds, #tsdb-mini-console").count(), "disabled export hides every dependent TSDB section").toBe(0);
		expect((await settingsNow()).metrics_export_enabled, "disabling export is saved immediately").toBe(false);
		expect(await page.isDisabled("#export-apply"), "disabling export leaves nothing to apply").toBe(true);
		expect(response.request().postDataJSON(), "disabling export sends only the toggle").toEqual({ metrics_export_enabled: false });
		answer = settingsPut();
		await page.locator("#setting-metrics_export_enabled").click({ force: true });
		response = await answer;
		await page.waitForSelector("#setting-db_type");
		expect((await settingsNow()).metrics_export_enabled, "enabling export is saved immediately").toBe(true);
		expect(await page.isDisabled("#export-apply"), "enabling export leaves nothing to apply").toBe(true);
		expect(response.request().postDataJSON(), "enabling export sends only the toggle").toEqual({ metrics_export_enabled: true });
		await page.locator("#setting-db_type").selectOption("questdb");
		expect(await page.locator("#setting-questdb_hostname").count(), "tsdb page shows backend fields").toBe(1);
		expect(await page.locator("#setting-docs_public").count(), "docs toggle lives on settings only").toBe(0);
		// All cards visible in the shared grid; changes are sent by Apply only. The toggle PUTs above are not
		// part of that contract (the former script started recording here), so the recording restarts now.
		tsdbPuts.length = 0;
		await expect(page.locator("#settings-sections > .export-card"), "questdb backend shows all five export cards").toHaveCount(5);
		expect(await page.locator(".export-card-export #setting-questdb_downsampling").count(), "retention lives in Export settings").toBe(1);
		expect(await page.locator("#tsdb-mini-console").count(), "TSDB page has a runtime mini console").toBe(1);
		expect((await page.locator("#tsdb-mini-console").innerText()).trim().length, "the Status & Log console shows the TSDB status").toBeGreaterThan(0);
		expect(await page.locator("#tsdb-mini-console").getAttribute("role"), "the Status & Log console announces politely (role=log)").toBe("log");
		expect(await page.locator("#tsdb-status-badge").count(), "no separate TSDB status badge").toBe(0);
		expect(await page.locator('.export-card button, #settings-sections button:has-text("Save")').count(), "no explicit Save button on the TSDB page").toBe(0);
		// The cards sit in the shared settings grid: cards of one row end at the same height.
		const exportRows = await page.evaluate(() => [...document.querySelectorAll("#settings-sections > .export-card")].map((card) => {
			const box = card.getBoundingClientRect();
			return { top: Math.round(box.top), bottom: Math.round(box.bottom) };
		}));
		expect(exportRows.length, "five export cards are measured").toBe(5);
		for (const row of exportRows) {
			if (row.top === exportRows[0].top) expect(row.bottom, `export cards of one grid row share top and bottom edge ${JSON.stringify(exportRows)}`).toBe(exportRows[0].bottom);
		}
	});

	test("retention fields and Apply-only sending @tsdb", async () => {
		await page.locator("#setting-questdb_downsampling").selectOption("manual");
		await expect(page.locator("#setting-questdb_raw_retention_days"), "manual retention shows raw days").toHaveCount(1);
		await expect(page.locator("#setting-questdb_retention_days"), "manual retention hides total days").toHaveCount(0);
		await page.locator("#setting-questdb_downsampling").selectOption("off");
		await expect(page.locator("#setting-questdb_retention_days"), "off retention shows total days").toHaveCount(1);
		await expect(page.locator("#setting-questdb_raw_retention_days"), "off retention hides raw days").toHaveCount(0);
		// Nothing is sent while typing; Apply is disabled until the backend's required fields are complete
		// (questdb needs only the hostname). Measurement window: the 1.5 s save debounce has not fired by 500 ms.
		await elapse(500, "nothing is sent while the form is incomplete");
		expect((await settingsNow()).db_type, "incomplete backend config is not saved (db_type)").not.toBe("questdb");
		expect(tsdbPuts.length, "incomplete backend config is not sent").toBe(0);
		expect(await page.isDisabled("#export-apply"), "Apply stays disabled while a required field is missing").toBe(true);
		await change("#setting-questdb_hostname", "questdb.example.org");
		await elapse(900, "a complete form is not saved without Apply");
		let now = await settingsNow();
		expect(now.questdb_hostname, "a complete form is still not saved automatically (server value)").not.toBe("questdb.example.org");
		expect(tsdbPuts.length, "a complete form is still not saved automatically (no PUT)").toBe(0);
		expect(await page.isEnabled("#export-apply"), "Apply is enabled").toBe(true);
		expect(await page.locator("#export-actions button").count(), "Apply is the only button of the action bar").toBe(1);
		expect((await page.locator("#export-apply").innerText()).trim(), "Apply label").toBe("Apply changes");
		expect(await page.locator("#export-actions .badge, #export-actions p, #export-discard").count(), "the action bar holds nothing else").toBe(0);
		await clickExportApply();
		await saved();
		now = await settingsNow();
		expect(tsdbPuts.length, "Apply sends the whole group exactly once").toBe(1);
		expect(now.db_type, "Apply saved the backend").toBe("questdb");
		expect(now.questdb_hostname, "Apply saved the hostname").toBe("questdb.example.org");
		await expect(page.locator("#export-apply"), "Apply is disabled again once the draft equals the server state").toBeDisabled();
		await page.locator("#setting-questdb_tls_enabled").click({ force: true });
		await elapse(900, "a toggle is not saved without Apply");
		now = await settingsNow();
		expect(now.questdb_tls_enabled, "a toggle is not saved until Apply (server value)").not.toBe(true);
		expect(tsdbPuts.length, "a toggle is not saved until Apply (no PUT)").toBe(1);
		await clickExportApply();
		await saved();
		expect((await settingsNow()).questdb_tls_enabled, "Apply saves the toggle").toBe(true);
		await page.locator("#setting-questdb_downsampling").selectOption("low");
		await clickExportApply();
		await saved();
		await page.evaluate((key) => localStorage.setItem(key, "dark"), THEME_KEY);
		await page.reload();
		await page.waitForSelector("#setting-db_type");
		expect(await page.locator("html").getAttribute("data-bs-theme"), "dark mode active for the fit check").toBe("dark");
		for (const [width, height] of [[1920, 1080], [1536, 864]]) {
			await page.setViewportSize({ width, height });
			await settle(page, `TSDB page settles at ${width}x${height}`);
			const fit = await page.evaluate(() => ({
				vertical: document.documentElement.scrollHeight - window.innerHeight,
				horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
			}));
			expect(fit.vertical, `TSDB configuration fits ${width}x${height} without vertical scroll ${JSON.stringify(fit)}`).toBeLessThanOrEqual(2);
			expect(fit.horizontal, `TSDB configuration fits ${width}x${height} without horizontal scroll ${JSON.stringify(fit)}`).toBeLessThanOrEqual(0);
		}
		await page.setViewportSize({ width: 1280, height: 800 });
		await page.evaluate((key) => localStorage.setItem(key, "light"), THEME_KEY);
	});

	test("incomplete export config is withheld, not sent @tsdb", async () => {
		await page.reload();
		await page.waitForSelector("#setting-questdb_hostname");
		// A configuration that becomes incomplete again can neither be applied nor leak out: Apply is disabled,
		// the hint names what is missing, and the saved value stays untouched.
		const dbTypeBefore = (await settingsNow()).db_type;
		const putsBeforeIncomplete = tsdbPuts.length;
		await change("#setting-questdb_hostname", "");
		await expect(page.locator("#toast-region .alert-warning"), "a warning toast names what is missing, once").toHaveCount(1);
		await elapse(900, "an incomplete export group is not sent");
		let exportState = await settingsNow();
		expect(exportState.questdb_hostname, "an export config that became incomplete again is not saved").toBe("questdb.example.org");
		expect(tsdbPuts.length, "an export config that became incomplete again is not sent").toBe(putsBeforeIncomplete);
		expect(await page.isDisabled("#export-apply"), "Apply is disabled for an incomplete export group").toBe(true);
		expect(await page.locator("#save-state").innerText(), "#save-state reports the incomplete export group").toContain("Incomplete");
		expect(await page.locator("#toast-region").innerText(), "the warning toast names what is missing").toMatch(/Not saved yet: Hostname or URL is required/);
		expect(await page.getAttribute("#setting-questdb_hostname", "aria-invalid"), "the emptied required field is flagged on the field").toBe("true");
		expect(await page.locator("#save-hint-export").count(), "no separate hint element").toBe(0);
		// The pairing branch: every required key is present, so the message has to name the pair instead.
		await change("#setting-questdb_hostname", "questdb.example.org");
		await change("#setting-questdb_username", "metrics");
		await expect(page.locator("#toast-region"), "the incomplete hint names the pair, not a missing required field").toContainText(/must be set together or left empty/);
		await elapse(900, "an unpaired username is not sent");
		exportState = await settingsNow();
		expect(exportState.questdb_username, "an unpaired username is withheld instead of producing a server 400 (server value)").toBeFalsy();
		expect(await page.isDisabled("#export-apply"), "an unpaired username keeps Apply disabled").toBe(true);
		await change("#setting-questdb_username", "");
		await expect(page.locator("#export-apply"), "Apply has nothing to send once the draft is back at the saved state").toBeDisabled();
		expect((await settingsNow()).db_type, `db_type survived the withheld export changes (${dbTypeBefore})`).toBe(dbTypeBefore);
		await page.locator("#setting-db_type").selectOption("influxdb_v2");
		await expect(page.locator("#settings-sections > .export-card"), "influxdb backend shows all five export cards").toHaveCount(5);
		expect(await page.locator(".export-card button").count(), "still no Save button after switching backend").toBe(0);
	});

	test("a token typed during an apply survives the older response; small screens use one column @tsdb", async () => {
		// A value typed while the apply is in flight must survive the response, which clears only the
		// secret revision it actually confirmed.
		await page.fill("#setting-influxdb_hostname", "influx.example.org");
		await page.locator("#setting-influxdb_hostname").dispatchEvent("change");
		await page.fill("#setting-influxdb_organization", "rct");
		await page.locator("#setting-influxdb_organization").dispatchEvent("change");
		await page.fill("#setting-influxdb_bucket", "metrics");
		await page.locator("#setting-influxdb_bucket").dispatchEvent("change");
		// Settings._check_export() counts an InfluxDB token as credentials that are always present, so
		// _guard_plaintext() rejects the entire save for a non-loopback host without TLS unless plaintext
		// credentials are explicitly allowed.
		await page.locator("#setting-influxdb_allow_plaintext_credentials").check();
		await page.waitForSelector("#confirm-modal.show");
		await page.click("#confirm-accept");
		// Only the first PUT is held until the second value was typed while its response is outstanding.
		let settingsPuts = 0;
		let release;
		const gate = new Promise((resolve) => { release = resolve; });
		await page.route("**/admin/api/settings", async (route) => {
			if (route.request().method() !== "PUT") return route.continue();
			settingsPuts += 1;
			if (settingsPuts === 1) await gate;
			return route.continue();
		});
		await page.fill("#setting-influxdb_token", "token-one");
		await page.locator("#setting-influxdb_token").dispatchEvent("change");
		expect(await page.isEnabled("#export-apply"), "an incomplete-free group with a token can be applied").toBe(true);
		await clickExportApply();
		await expect.poll(() => settingsPuts, { message: "the first apply request is in flight and held" }).toBe(1);
		await page.fill("#setting-influxdb_token", "token-two");
		await page.locator("#setting-influxdb_token").dispatchEvent("change");
		release();
		await page.waitForFunction(() => document.getElementById("export-apply").getAttribute("aria-busy") === "false", null, { timeout: 15000 });
		expect(await page.inputValue("#setting-influxdb_token"), "a token typed during the apply is not cleared by the older response").toBe("token-two");
		expect(await page.isEnabled("#export-apply"), "the newer token can still be applied").toBe(true);
		await clickExportApply();
		await page.waitForFunction(() => {
			const field = document.getElementById("setting-influxdb_token");
			return field.value === "" && field.placeholder.endsWith("(stored)");
		}, null, { timeout: 8000 });
		expect(settingsPuts, "the newer token is sent by the second apply").toBe(2);
		expect(await page.getAttribute("#setting-influxdb_token", "placeholder"), "the newer token is then cleared and shown as stored").toMatch(/\(stored\)$/);
		expect(await page.isDisabled("#export-apply"), "a blank token keeps the stored one: nothing left to apply").toBe(true);
		await page.unroute("**/admin/api/settings");
		// Measured geometry: on a phone the shared settings grid collapses to one column and every card
		// is as wide as the grid.
		for (const width of [390, 320]) {
			await page.setViewportSize({ width, height: 844 });
			await settle(page, `TSDB page settles at ${width}px`);
			const mobileGrid = await page.evaluate(() => {
				const grid = document.querySelector("#settings-sections");
				const gridBox = grid.getBoundingClientRect();
				const cards = [...grid.querySelectorAll(":scope > .export-card")].map((card) => card.getBoundingClientRect());
				return {
					cardCount: cards.length,
					columns: new Set(cards.map((card) => Math.round(card.left))).size,
					widestDelta: Math.max(...cards.map((card) => Math.abs(card.width - gridBox.width))),
				};
			});
			const note = JSON.stringify(mobileGrid);
			expect(mobileGrid.cardCount, `export cards are present at ${width}px ${note}`).toBeGreaterThanOrEqual(2);
			expect(mobileGrid.columns, `export cards collapse to one column at ${width}px ${note}`).toBe(1);
			expect(mobileGrid.widestDelta, `export cards are as wide as the grid at ${width}px ${note}`).toBeLessThanOrEqual(1);
		}
		await page.setViewportSize({ width: 1280, height: 800 });
		expect(tracked.problems, "no console errors, failed requests or dialogs").toEqual([]);
		expect([...tracked.external], "no request leaves the gateway").toEqual([]);
	});
});
