//
// tests/e2e/admin-inverters-modal.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Inverter editor modal on the dashboard: structure, inline validation, add, server refusals, double submit,
// delete with confirmation, modal geometry and focus.
// Replaces admin_flow.mjs lines 1388-1626. One serial story on its own gateway: the inverter list carries
// from test to test (add 192.0.2.10:18899, add 192.0.2.77:18877 via double submit, delete both).
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { trackProblems } from "./support/problems.mjs";
import { elapse } from "./support/timing.mjs";
import { settle } from "./support/settle.mjs";
test.describe("inverter editor modal @inverters", () => {
	test.describe.configure({ mode: "serial" });
	const fresh = freshGateway("default");
	let context;
	let page;
	let base;
	let tracked;
	// Every request that sends the device list; the add and delete contract is asserted against this.
	const devicePuts = [];
	let injectedStart = 0;
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser));
		base = fresh.gateway.base;
		tracked = trackProblems(page, browserName, base);
		page.on("request", (r) => {
			if (r.method() !== "PUT" || !r.url().endsWith("/admin/api/settings")) return;
			try {
				const body = JSON.parse(r.postData() || "{}");
				if (Object.hasOwn(body, "devices")) devicePuts.push(body);
			} catch { /* not JSON */ }
		});
	});
	test.afterAll(async () => { await context?.close(); });
	const deviceRows = () => page.locator(".device-settings-item").count();
	const savedRows = () => page.locator("#device-list .device-settings-item").count();
	const newField = (field) => page.locator(`#device-add-form input[data-field="${field}"]`);
	const modalOpen = () => page.locator("#inverters-modal.show").count();
	// A toast can sit over the bottom-right controls; closing it first keeps the click deterministic.
	const clearToasts = () => page.evaluate(() => document.querySelectorAll("#toast-region .alert").forEach((node) => node.remove()));
	const serverDevices = async () => (await (await context.request.get(`${base}/admin/api/settings`)).json()).settings.devices;
	const formState = () => page.evaluate(() => {
		const error = document.getElementById("device-error");
		return {
			invalid: [...document.querySelectorAll("#device-add-form input.is-invalid")].map((input) => input.dataset.field),
			described: [...document.querySelectorAll('#device-add-form input[aria-invalid="true"]')].every((input) => input.getAttribute("aria-describedby") === "device-error"),
			error: error.hidden ? "" : error.textContent,
			status: document.getElementById("device-status").textContent,
		};
	});
	const trashFor = (host) => page.locator(`#device-list button[aria-label^="Remove inverter ${host}:"]`);
	const focusedLabel = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label") || document.activeElement?.id || "");
	/** Dashboard -> plus button -> modal with its inverter list rendered. */
	async function openModal(url = "/ui/dashboard") {
		if (url) await page.goto(url);
		await page.click("#add-inverter");
		await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
		await page.waitForSelector("#device-list .device-settings-item");
		// The Bootstrap fade transform must be finished before the dialog position is measured.
		await expect.poll(() => page.evaluate(() => getComputedStyle(document.querySelector("#inverters-modal .modal-dialog")).transform), { message: "modal fade transform finished" }).toMatch(/^(none|matrix\(1, 0, 0, 1, 0, 0\))$/);
	}
	const onlyDeviceStatus = (text) => page.waitForFunction((t) => document.getElementById("device-status").textContent.includes(t), text, { timeout: 8000 });
	const holdPuts = async (status, detail) => page.route("**/admin/api/settings", (route) => (route.request().method() === "PUT"
		? route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ detail }) })
		: route.continue()));

	test("inverter editor modal structure @inverters", async () => {
		await page.goto("/ui/dashboard");
		await page.click("#add-inverter");
		await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
		expect(await page.locator("#inverters-modal.show").count(), "plus opens the inverter editor modal").toBe(1);
		await page.waitForFunction(() => document.getElementById("inverters-modal").contains(document.activeElement));
		expect(await page.evaluate(() => document.getElementById("inverters-modal").contains(document.activeElement)), "focus stays in the modal").toBe(true);
		expect([await savedRows(), await deviceRows()], "the dialog lists the saved inverter and offers one empty add row").toEqual([1, 2]);
		expect(await page.locator('#device-list button[aria-label^="Remove inverter "] .material-icons').first().innerText(), "trash icon is the delete_outline glyph").toBe("delete_outline");
		expect(await page.locator("#device-list button").first().getAttribute("aria-label"), "trash icon has an accessible label with the address").toMatch(/^Remove inverter \S+:\d+$/);
		expect(await page.locator("#device-list input").count(), "saved inverters are shown as text, not editable").toBe(0);
		expect(await page.locator('.device-settings-item input[data-field="device_id"], .device-settings-item input[data-field="display_name"]').count(), "no id or name fields").toBe(0);
		// The dialog has three functions only: add, delete (trash per saved row) and close.
		const modalButtons = await page.$$eval("#inverters-modal button", (nodes) => nodes.map((node) => node.id || node.className));
		expect(modalButtons.length, `the only buttons are close, add and one trash per saved inverter ${JSON.stringify(modalButtons)}`).toBe(2 + (await savedRows()));
		expect(modalButtons.filter((name) => name.includes("btn-close")).length, "exactly one close button").toBe(1);
		expect(modalButtons, "the add button is present").toContain("device-add");
		const modalText = await page.evaluate(() => document.getElementById("inverters-modal").textContent);
		expect(/apply/i.test(modalText), "no Apply anywhere in the modal text").toBe(false);
		expect(await page.locator("#device-apply, #inverters-modal .apply-bar, #inverters-modal .modal-footer").count(), "no Apply bar or footer in the modal").toBe(0);
		expect(modalText, "the subtitle says each change takes effect immediately").toContain("Each change takes effect immediately");
	});

	test("inline validation sends nothing @inverters", async () => {
		const addStyle = await page.evaluate(() => {
			const button = document.getElementById("device-add");
			const style = getComputedStyle(button);
			return { classes: button.className, bg: style.backgroundColor, text: button.textContent.trim(), disabled: button.disabled };
		});
		const styleNote = JSON.stringify(addStyle);
		expect(addStyle.classes.split(" "), `Add inverter is a btn-primary button ${styleNote}`).toContain("btn-primary");
		expect(addStyle.classes, `Add inverter is not an outline button ${styleNote}`).not.toContain("outline");
		expect(["rgba(0, 0, 0, 0)", "transparent"], `Add inverter is filled ${styleNote}`).not.toContain(addStyle.bg);
		expect(addStyle.text, "Add inverter label").toBe("Add inverter");
		expect(addStyle.disabled, "Add inverter is enabled").toBe(false);
		// Invalid input is reported inline on the offending field and nothing is sent.
		const putsBefore = devicePuts.length;
		await page.click("#device-add");
		let state = await formState();
		expect(state.invalid, `an empty host is reported on the host field ${JSON.stringify(state)}`).toEqual(["host"]);
		expect(state.error, "the empty-host message asks for an IP address").toContain("Enter an IP address");
		expect(state.described, "the invalid field is described by the error").toBe(true);
		expect(await page.evaluate(() => document.activeElement?.dataset.field), "the host field is focused").toBe("host");
		await newField("host").fill("http://nope/path");
		expect((await formState()).error, "typing clears the previous error").toBe("");
		await page.click("#device-add");
		state = await formState();
		expect(state.invalid, `a host with a scheme is reported on the host field ${JSON.stringify(state)}`).toEqual(["host"]);
		expect(state.error, "the scheme message").toContain("without scheme");
		await newField("host").fill("192.0.2.10");
		await newField("port").fill("70000");
		await page.click("#device-add");
		state = await formState();
		expect(state.invalid, `an out-of-range port is reported on the port field, not the host ${JSON.stringify(state)}`).toEqual(["port"]);
		expect(state.error, "the port message names the limit").toContain("65535");
		const savedHost = (await page.locator("#device-list .device-saved-host").first().innerText()).trim();
		const savedPort = (await page.locator("#device-list .device-settings-item").first().innerText()).match(/Port (\d+)/)[1];
		await newField("host").fill(savedHost);
		await newField("port").fill(savedPort);
		await page.click("#device-add");
		state = await formState();
		expect(state.invalid, `a duplicate inverter is reported on the host field ${JSON.stringify(state)}`).toEqual(["host"]);
		expect(state.error, "the duplicate message").toContain("already listed");
		// Measurement window: an erroneous request would be sent within the 1.5 s save debounce at the latest.
		await elapse(900, "no request is sent for invalid input");
		expect(devicePuts.length, "invalid input sent no request").toBe(putsBefore);
		expect(await modalOpen(), "invalid input kept the dialog open").toBe(1);
	});

	test("add inverter: one PUT, persistence, refusal, 409, double submit @inverters", async () => {
		const putsBefore = devicePuts.length;
		await newField("host").fill("192.0.2.10");
		await newField("port").fill("18899");
		await clearToasts();
		await newField("host").press("Enter");
		await onlyDeviceStatus("added");
		const sent = devicePuts.slice(putsBefore);
		expect(sent.length, `adding sends exactly one PUT ${JSON.stringify(sent)}`).toBe(1);
		expect(sent[0].devices.length, "the PUT carries the full new list").toBe(2);
		expect(sent[0].devices[1].host, "the new host is in the list").toBe("192.0.2.10");
		expect(sent[0].devices[1].port, "the new port is in the list").toBe(18899);
		expect(Object.keys(sent[0]), "nothing but devices is sent").toEqual(["devices"]);
		// In the dashboard modal a successful add finishes the interaction and closes the modal.
		await page.waitForSelector("#inverters-modal", { state: "hidden" });
		expect(await modalOpen(), "the dialog closes after the add").toBe(0);
		expect((await serverDevices()).some((d) => d.host === "192.0.2.10" && Number(d.port) === 18899), "the server has the new inverter").toBe(true);
		// The add dismissed the modal, so the on-close success toast is awaited instead of clicking Close.
		await page.waitForFunction(() => [...document.querySelectorAll("#toast-region .alert")].some((node) => node.dataset.kind === "success" && /inverter added/i.test(node.dataset.message)), null, { timeout: 3000 });
		expect(await page.locator("#toast-region .alert-success").count(), "closing after a successful add shows a success toast").toBeGreaterThanOrEqual(1);
		await page.reload();
		await openModal(null);
		expect(await savedRows(), "the added inverter persists after a reload (two saved rows)").toBe(2);
		expect(await trashFor("192.0.2.10").count(), "the added inverter persists after a reload (its trash icon)").toBe(1);
		await newField("host").fill("192.0.2.10");
		await newField("port").fill("18899");
		await page.click("#device-add");
		expect((await formState()).error, "adding the same address again is refused inline").toContain("already listed");
		// A server rejection stays inline, keeps the input and focuses the add button.
		injectedStart = tracked.problems.length;
		await newField("host").fill("192.0.2.77");
		await newField("port").fill("18877");
		await holdPuts(500, "Injected failure");
		await page.click("#device-add");
		await page.waitForFunction(() => document.getElementById("device-error").textContent.includes("Injected failure"), null, { timeout: 8000 });
		expect(await newField("host").inputValue(), "a failed add keeps the host input").toBe("192.0.2.77");
		expect(await newField("port").inputValue(), "a failed add keeps the port input").toBe("18877");
		expect(await savedRows(), "a failed add names the reason inline and keeps two saved rows").toBe(2);
		expect(await page.locator('#device-error[role="alert"]').count(), "the inline reason is an alert").toBe(1);
		expect(await page.evaluate(() => document.activeElement?.id === "device-add" && !document.activeElement.disabled), "focus returns to the add button and it is usable again").toBe(true);
		await page.unroute("**/admin/api/settings");
		await holdPuts(409, "Device graph build failed");
		await page.click("#device-add");
		await page.waitForFunction(() => document.getElementById("device-error").textContent.includes("rolled back"), null, { timeout: 8000 });
		expect(await page.locator("#device-error").innerText(), "a 409 reports the rollback and the reason").toContain("Device graph build failed");
		await page.unroute("**/admin/api/settings");
		// Double submit: the button is locked while the request runs. The PUT is held open by the test until the
		// lock has been observed and a second (forced) click was attempted, then released.
		const putsAtLock = devicePuts.length;
		let release;
		const gate = new Promise((resolve) => { release = resolve; });
		await page.route("**/admin/api/settings", async (route) => {
			if (route.request().method() === "PUT") await gate;
			await route.continue();
		});
		await page.click("#device-add");
		await page.waitForFunction(() => document.getElementById("device-add").disabled, null, { timeout: 3000 });
		const locked = await page.evaluate(() => document.getElementById("device-add").getAttribute("aria-busy") === "true");
		// A forced click on the disabled button may time out; that is the expected outcome, not a failure.
		await page.locator("#device-add").click({ force: true, timeout: 500 }).catch(() => { });
		release();
		await onlyDeviceStatus("added");
		await page.unroute("**/admin/api/settings");
		expect(locked, "Add is busy (disabled, aria-busy) during the request").toBe(true);
		expect(devicePuts.length - putsAtLock, "Add sends exactly one PUT").toBe(1);
		tracked.scrub(injectedStart, (p) => p.includes(`${base}/admin/api/settings`)
			&& (p.startsWith("http 500: PUT ") || p.startsWith("http 409: PUT ") || (p.startsWith("console ") && /\b(500|409)\b/.test(p))));
	});

	test("delete inverter: confirm, cancel, refusal, persistence, close toast @inverters", async () => {
		// The successful double-submit add closed the dashboard modal; reopen it for the delete steps.
		await page.waitForSelector("#inverters-modal", { state: "hidden" });
		await openModal(null);
		const putsAtDelete = devicePuts.length;
		await clearToasts();
		await trashFor("192.0.2.77").click();
		await page.waitForSelector("#confirm-modal.show", { timeout: 5000 });
		const confirmText = await page.locator("#confirm-modal .modal-body").innerText();
		expect(confirmText, "the confirmation names the inverter").toContain("192.0.2.77:18877");
		expect(confirmText, "the confirmation names the reset").toMatch(/Engineering Mode/);
		await page.click("#confirm-cancel");
		await page.waitForSelector("#confirm-modal", { state: "hidden", timeout: 5000 });
		await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Remove inverter"), null, { timeout: 3000 });
		expect(devicePuts.length - putsAtDelete, "cancelling sends nothing").toBe(0);
		expect(await trashFor("192.0.2.77").count(), "cancelling keeps the row").toBe(1);
		expect(await focusedLabel(), "cancelling returns focus to its trash icon").toBe("Remove inverter 192.0.2.77:18877");
		const refusedStart = tracked.problems.length;
		await holdPuts(409, "Active battery dispatch");
		await trashFor("192.0.2.77").click();
		await page.waitForSelector("#confirm-modal.show", { timeout: 5000 });
		await page.click("#confirm-accept");
		await page.waitForFunction(() => document.getElementById("device-error").textContent.includes("Active battery dispatch"), null, { timeout: 8000 });
		expect(await trashFor("192.0.2.77").count(), "a refused delete keeps the row").toBe(1);
		expect((await serverDevices()).some((d) => d.host === "192.0.2.77"), "a refused delete keeps the server entry").toBe(true);
		await page.unroute("**/admin/api/settings");
		tracked.scrub(refusedStart, (p) => p.includes(`${base}/admin/api/settings`) && (p.startsWith("http 409: PUT ") || (p.startsWith("console ") && /\b409\b/.test(p))));
		await trashFor("192.0.2.77").click();
		await page.waitForSelector("#confirm-modal.show", { timeout: 5000 });
		await page.click("#confirm-accept");
		await onlyDeviceStatus("192.0.2.77:18877 removed");
		expect(await trashFor("192.0.2.77").count(), "deleting removes the row").toBe(0);
		expect((await serverDevices()).every((d) => d.host !== "192.0.2.77"), "deleting removes the server entry").toBe(true);
		expect(devicePuts.at(-1).devices.length, `deleting sends one PUT with the remaining list ${JSON.stringify(devicePuts.at(-1))}`).toBe(2);
		await trashFor("192.0.2.10").click();
		await page.waitForSelector("#confirm-modal.show", { timeout: 5000 });
		await page.click("#confirm-accept");
		await onlyDeviceStatus("192.0.2.10:18899 removed");
		expect(await savedRows(), "the second delete leaves only the original inverter (rows)").toBe(1);
		expect((await serverDevices()).length, "the second delete leaves only the original inverter (server)").toBe(1);
		expect(devicePuts.at(-1).devices.length, "the second delete PUT carries one device").toBe(1);
		expect(await page.evaluate(() => document.getElementById("inverters-modal").contains(document.activeElement)), "focus lands on a usable control after a delete").toBe(true);
		await page.reload();
		await openModal(null);
		expect(await savedRows(), "the deletion persists after a reload (rows)").toBe(1);
		expect(await trashFor("192.0.2.10").count(), "the deletion persists after a reload (trash icon)").toBe(0);
		// This open did no add (only a reload), so closing it must not toast.
		await clearToasts();
		await page.click("#inverters-modal .btn-close");
		await page.waitForSelector("#inverters-modal", { state: "hidden" });
		await elapse(300, "no toast appears after closing without an add");
		expect(await page.locator("#toast-region .alert").count(), "closing without an add shows no toast").toBe(0);
	});

	test("modal geometry: no stray Close, centering, short viewport, focus return @inverters", async () => {
		await page.click("#add-inverter");
		await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
		await expect.poll(() => page.evaluate(() => getComputedStyle(document.querySelector("#inverters-modal .modal-dialog")).transform), { message: "modal fade transform finished" }).toMatch(/^(none|matrix\(1, 0, 0, 1, 0, 0\))$/);
		expect(await page.locator("#inverters-modal .modal-footer .btn-secondary").count(), "the redundant modal-footer Close button is gone").toBe(0);
		// The outer #inverters-modal is itself a scroll container; reset it before measuring so the check
		// reflects layout, not a leftover scroll position.
		const centering = () => page.evaluate(() => {
			document.getElementById("inverters-modal").scrollTop = 0;
			const rect = document.querySelector("#inverters-modal .modal-dialog").getBoundingClientRect();
			return { left: rect.left, right: window.innerWidth - rect.right, top: rect.top, bottom: window.innerHeight - rect.bottom };
		});
		let center = await centering();
		expect(Math.abs(center.left - center.right), `the modal is horizontally centered at the default viewport ${JSON.stringify(center)}`).toBeLessThanOrEqual(2);
		expect(Math.abs(center.top - center.bottom), `the modal is vertically centered at the default viewport ${JSON.stringify(center)}`).toBeLessThanOrEqual(2);
		// At a short viewport .modal-body scrolls internally and the page does not grow past its default height.
		const pageHeightAtDefaultViewport = await page.evaluate(() => document.documentElement.scrollHeight);
		await page.setViewportSize({ width: 1280, height: 400 });
		await expect.poll(async () => Math.abs((center = await centering()).left - center.right), { message: "the modal stays horizontally centered at a short viewport" }).toBeLessThanOrEqual(2);
		const shortViewport = await page.evaluate(() => {
			const body = document.querySelector("#inverters-modal .modal-body");
			return { bodyScrolls: body.scrollHeight > body.clientHeight, pageHeight: document.documentElement.scrollHeight };
		});
		expect(shortViewport.bodyScrolls, `.modal-body scrolls internally at a short viewport ${JSON.stringify(shortViewport)}`).toBe(true);
		expect(shortViewport.pageHeight, `shrinking the viewport does not grow the page past its default-viewport height ${JSON.stringify({ ...shortViewport, pageHeightAtDefaultViewport })}`).toBeLessThanOrEqual(pageHeightAtDefaultViewport);
		await page.setViewportSize({ width: 1280, height: 800 });
		await settle(page, "layout settles after restoring the viewport");
		await page.click("#inverters-modal .btn-close");
		await page.waitForSelector("#inverters-modal", { state: "hidden" });
		await page.click("#add-inverter");
		await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
		await page.waitForFunction(() => document.getElementById("inverters-modal").contains(document.activeElement));
		await page.keyboard.press("Escape");
		await page.waitForSelector("#inverters-modal", { state: "hidden" });
		await page.waitForFunction(() => document.activeElement?.id === "add-inverter");
		expect(await page.evaluate(() => document.activeElement?.id), "closing returns focus to the plus button").toBe("add-inverter");
		expect(tracked.problems, "no unexpected console errors, failed requests or dialogs").toEqual([]);
		expect([...tracked.external], "no request leaves the gateway").toEqual([]);
	});
});
