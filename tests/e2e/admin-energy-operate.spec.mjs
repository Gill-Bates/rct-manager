//
// tests/e2e/admin-energy-operate.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Energy page of a hardware-verified inverter: lite flow, mode control (Off / Manual / External), commands,
// PAT versus GUI access, Expert mode and power limits. Replaces admin_flow.mjs lines 2128-2360.
// One story on an own energy gateway. The former driver relied on the setup story having recorded Off;
// this spec records Off itself in its first test and approves the required write registers as an operator
// does on the Inverters page (the product now requires it before the panel is ready). Former `if (...)` gated checks are hard assertions.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { ATTESTATION, installEnergyMocks } from "./support/energy-mocks.mjs";
import { approveRequiredWrites } from "./support/energy-setup.mjs";
import { trackProblems } from "./support/problems.mjs";
test.describe.configure({ mode: "serial" });
test.describe("energy operate and expert @energy", () => {
	const fresh = freshGateway("energy");
	let context;
	let page;
	let watch;
	let mocks;
	let secret = "";
	const commands = [];
	const modeCalls = [];
	const limitPuts = [];
	const modeTimings = [];
	const radios = () => page.locator(".energy-mode-switch [role=radio]");
	const buttons = () => ["Charge battery", "Keep battery idle", "Discharge battery"]
		.map((name) => page.locator(".energy-actions button", { hasText: new RegExp(`^${name}$`) }));
	const states = async () => Promise.all(buttons().map((button) => button.isDisabled()));
	const checkedMode = () => page.evaluate(() => document.querySelector(".energy-mode-switch [aria-checked=true]")?.textContent.trim());
	const operateText = () => page.locator(".energy-panel").first().innerText();
	const csrf = () => page.evaluate(async () => (await (await fetch("/admin/api/session")).json()).csrf_token);
	// Synchronise on the PUT /mode answer, then on the rendered state. Off after an active command answers
	// only once the device restore finished, so that call passes a longer budget.
	const switchMode = async (act, label, responseTimeout = 15_000) => {
		const answered = page.waitForResponse((r) => r.request().method() === "PUT" && r.url().endsWith("/mode"), { timeout: responseTimeout });
		const startedAt = Date.now();
		await act();
		await answered;
		modeTimings.push(`${label}:${Date.now() - startedAt}ms`);
		await page.waitForFunction((l) => (document.querySelector(".energy-mode-switch [aria-checked=true]")?.textContent.trim() || "").endsWith(l), label, { timeout: 15_000 });
	};
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser, { viewport: { width: 1280, height: 900 } }));
		watch = trackProblems(page, browserName, fresh.gateway.base);
		mocks = await installEnergyMocks(page);
		mocks.strategy = { ...mocks.strategy, code: 1, label: "Constant" }; // as left by the setup story
		page.on("request", (r) => {
			if (r.method() === "POST" && r.url().endsWith("/command")) commands.push(r.postDataJSON());
			if (r.method() === "PUT" && r.url().endsWith("/mode")) modeCalls.push(r.postDataJSON());
			if (r.method() === "PUT" && /\/dispatch\/devices\//.test(r.url())) limitPuts.push(r.postDataJSON());
		});
	});
	test.afterAll(async () => { await context?.close(); });

	test("Off is recorded and the hardware verification endpoint accepts the evidence @energy", async () => {
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-panel");
		// The device ships without a recorded mode; record Off as an operator would.
		const [off] = await Promise.all([
			page.waitForResponse((r) => r.request().method() === "PUT" && r.url().endsWith("/mode"), { timeout: 15_000 }),
			radios().first().click(),
		]);
		expect(off.ok(), `Off is recorded (HTTP ${off.status()})`).toBe(true);
		modeCalls.length = 0;
		// Manual control needs the required write registers approved on the Inverters page (the former driver
		// never did this and no longer reached a ready panel; see report finding F7).
		expect(await approveRequiredWrites(page.request), "required write registers approved").toBe(200);
		// The simulator ships unverified hardware; verify it first, as an operator would in Expert.
		const verification = await page.request.put(`${fresh.gateway.base}/admin/api/energy/devices/sim/hardware-verification`, {
			headers: { "X-CSRF-Token": await csrf(), Origin: fresh.gateway.base },
			data: { ...ATTESTATION, note: "e2e simulator" },
		});
		expect(verification.ok(), `hardware verification endpoint accepts the evidence (HTTP ${verification.status()})`).toBe(true);
	});

	test("the verified panel shows the lite flow beside the controls and stacked on mobile @energy", async () => {
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-panel");
		await expect(page.locator(".energy-panel"), "energy page renders a device panel").not.toHaveCount(0);
		// The Energy panel reuses the live readings in a compact grid-house-battery view.
		await expect(page.locator(".energy-panel .energy-flow-lite svg"), "the lite flow is rendered once the verified state is known").toBeVisible({ timeout: 15_000 });
		const lite = await page.locator(".energy-panel .energy-body").evaluate((layout) => {
			const graphic = layout.querySelector(".energy-flow-lite");
			const control = layout.querySelector(".energy-operate-main");
			const svg = graphic?.querySelector("svg");
			const graphicRect = graphic?.getBoundingClientRect();
			const controlRect = control?.getBoundingClientRect();
			return {
				visible: Boolean(graphic && !graphic.hidden && svg),
				sideBySide: Boolean(graphicRect && controlRect && graphicRect.left >= controlRect.right - 1),
				nodes: graphic?.querySelectorAll(".flow-node").length,
				lines: graphic?.querySelectorAll(".flow-line").length,
				label: svg?.getAttribute("aria-label") || "",
			};
		});
		expect(lite.visible, `lite energy flow is visible ${JSON.stringify(lite)}`).toBe(true);
		expect(lite.sideBySide, `lite energy flow sits beside the controls ${JSON.stringify(lite)}`).toBe(true);
		expect(lite.nodes, "lite energy flow shows grid, house and battery nodes").toBe(3);
		expect(lite.lines, "lite energy flow has two lines").toBe(2);
		expect(lite.label, "lite energy flow is named after grid, house or battery").toMatch(/grid|house|battery/i);
		expect(lite.label, "lite energy flow has no PV").not.toMatch(/PV/);
		await page.setViewportSize({ width: 390, height: 844 });
		await expect.poll(() => page.locator(".energy-panel .energy-body").evaluate((layout) => {
			const graphic = layout.querySelector(".energy-flow-lite").getBoundingClientRect();
			const control = layout.querySelector(".energy-operate-main").getBoundingClientRect();
			return graphic.top >= control.bottom - 1 && document.documentElement.scrollWidth <= window.innerWidth + 1;
		}), { message: "Lite energy flow stacks below controls at 390 px without horizontal overflow" }).toBe(true);
		await page.setViewportSize({ width: 1280, height: 900 });
		// The relabelled, Operate-layer action buttons (presentation only; REST names unchanged).
		expect(await page.locator(".energy-actions button").evaluateAll((nodes) => nodes.length === 3
			&& nodes.every((button) => button.classList.contains("btn-primary")
				&& ![...button.classList].some((name) => name.startsWith("btn-outline-")))), "all Energy Manager actions use filled buttons").toBe(true);
	});

	test("the mode control is an accessible radio group; Off disables the actions @energy", async () => {
		await page.reload();
		await page.waitForSelector(".energy-panel");
		await expect(page.locator(".energy-mode-switch[role=radiogroup]"), "the mode control is one radio group").toHaveCount(1);
		const labels = (await radios().allInnerTexts()).map((text) => text.replace(/\s+/g, " ").trim().replace(/^\S+ /, "")).join("|");
		expect(labels, "the mode control is a radio group with Off, Manual and External").toBe("Off|Manual|External");
		// The first test recorded Off, so Off is the selected radio and the tab stop.
		expect(await checkedMode(), "the recorded Off mode is the selected radio").toMatch(/Off$/);
		await expect(page.locator('.energy-mode-switch [role=radio][tabindex="0"]'), "roving tabindex: exactly one radio is tabbable").toHaveCount(1);
		expect(await radios().evaluateAll((nodes) => nodes.every((n) => n.getAttribute("aria-label").startsWith(n.lastElementChild.textContent.trim()))),
			"every radio has an aria-label that starts with its visible text").toBe(true);
		const initialStates = await states();
		expect(initialStates.every(Boolean), `Off: the operate buttons are disabled ${JSON.stringify(initialStates)}`).toBe(true);
		expect(await buttons()[0].getAttribute("title"), "Off: a disabled button names the reason").toMatch(/Energy Manager is off/);
	});

	test("keyboard: arrows move the focus, Space selects Manual @energy", async () => {
		await radios().first().focus();
		await page.keyboard.press("ArrowRight");
		expect(await page.evaluate(() => document.activeElement?.textContent.trim().endsWith("Manual")), "ArrowRight moves the focus to Manual").toBe(true);
		expect(modeCalls, "ArrowRight selects nothing").toHaveLength(0);
		expect(await checkedMode(), "ArrowRight leaves Off selected").toMatch(/Off$/);
		await switchMode(() => page.keyboard.press("Space"), "Manual");
		expect(await checkedMode(), "Space selects Manual").toMatch(/Manual$/);
		expect(modeCalls.map((call) => call.mode), `Space sends exactly one mode request ${JSON.stringify(modeCalls)}`).toEqual(["manual"]);
		expect(await page.evaluate(() => document.activeElement?.closest(".energy-mode-switch") !== null), "the focus stays on the control").toBe(true);
		await expect.poll(async () => (await states()).some((disabled) => !disabled), { message: "Manual enables the available actions", timeout: 15_000 }).toBe(true);
		const liteEdges = await page.locator(".energy-panel .energy-operate").evaluate((operation) => {
			const operateMain = operation.querySelector(".energy-operate-main").getBoundingClientRect();
			const graphic = operation.querySelector(".energy-flow-lite").getBoundingClientRect();
			const freshness = operation.querySelector(".energy-mode")?.textContent || "";
			return {
				top: graphic.top - operateMain.top,
				hasManagerStatus: /Manager status:|No Energy Manager command is active/.test(freshness),
				hasSocTextRow: Boolean(operation.querySelector(".energy-soc")),
			};
		});
		expect(liteEdges.hasManagerStatus, "the mode text row is absent").toBe(false);
		expect(liteEdges.hasSocTextRow, "the battery-SoC text row is absent").toBe(false);
		expect(Math.abs(liteEdges.top), `the lite flow aligns with the action area ${JSON.stringify(liteEdges)}`).toBeLessThanOrEqual(1);
	});

	test("a PAT cannot command a Manual inverter @energy", async () => {
		// The PAT is created via the API and reused by the External test below.
		const patBody = await page.evaluate(async () => (await fetch("/admin/api/tokens", {
			method: "POST",
			headers: { "Content-Type": "application/json", "X-CSRF-Token": (await (await fetch("/admin/api/session")).json()).csrf_token },
			body: JSON.stringify({ name: "e2e-mode", role: "read/write" }),
		})).json());
		secret = patBody.token || patBody.secret || "";
		expect(secret.length, `a PAT could be created for the Manual-mode check ${JSON.stringify(patBody).slice(0, 80)}`).toBeGreaterThan(0);
		const viaPat = await page.request.post(`${fresh.gateway.base}/api/v1/devices/sim/energy/command`, { headers: { Authorization: `Bearer ${secret}` }, data: { action: "hold" } });
		expect(viaPat.status(), "a PAT command on a Manual inverter is refused").toBe(409);
		expect((await viaPat.json()).code, "a PAT command on a Manual inverter answers energy_manager_not_external").toBe("energy_manager_not_external");
	});

	test("Manual: Charge, Keep battery idle and Off hand control back @energy", async () => {
		const sent = async (action) => {
			const before = commands.length;
			await action();
			await expect.poll(() => commands.length, { message: "the command request is sent", timeout: 8000, intervals: [100] }).toBeGreaterThan(before);
			return commands[before];
		};
		expect((await states())[0], "Charge is available in Manual").toBe(false);
		await buttons()[0].click();
		await expect(page.locator(".energy-target"), "Charge opens the target SoC area").toBeVisible();
		const charge = await sent(() => page.locator(".energy-target button").click());
		expect(charge?.action, `Charge sends a charge command ${JSON.stringify(charge)}`).toBe("charge");
		expect(Number.isFinite(charge?.target_soc_percent), `Charge sends a target SoC ${JSON.stringify(charge)}`).toBe(true);
		// The simulator needs ~10 s to apply a charge, and the buttons stay busy until then.
		await expect(buttons()[1], "Keep battery idle becomes available after the charge was applied").toBeEnabled({ timeout: 40_000 });
		const hold = await sent(() => buttons()[1].click());
		expect(hold?.action, `Keep battery idle sends a hold command ${JSON.stringify(hold)}`).toBe("hold");
		expect("target_soc_percent" in hold, "Keep battery idle sends no target SoC").toBe(false);
		mocks.strategy = { ...mocks.strategy, code: 4, label: "Internal" };
		await page.reload();
		await page.waitForSelector(".energy-panel");
		const commandCount = commands.length;
		await switchMode(() => radios().nth(0).click(), "Off", 60_000);
		expect(commands.length, `Off hands control back without a separate auto command ${JSON.stringify(modeCalls)} ${modeTimings.join(",")}`).toBe(commandCount);
		expect(await checkedMode(), "Off is the selected mode").toMatch(/Off$/);
		expect((await states()).every(Boolean), "Off disables the actions again").toBe(true);
		mocks.strategy = { ...mocks.strategy, code: 1, label: "Constant" };
		await page.reload();
		await expect(page.locator(".energy-mode-evidence"), "an external change to Constant raises the mismatch warning")
			.toContainText(/Off is recorded, but the inverter reports SOC strategy/);
		await expect(page.locator('.energy-mode-option[aria-checked="true"]'), "Off stays recorded while the mismatch warning shows").toHaveCount(1);
		mocks.strategy = { ...mocks.strategy, code: 4, label: "Internal" };
		await page.reload();
		await expect(page.locator(".energy-mode-option[aria-checked=\"true\"]"), "the recorded mode is selected again").toHaveCount(1);
	});

	test("External: the GUI is refused, a PAT may command @energy", async () => {
		await switchMode(() => radios().nth(2).click(), "External");
		await expect.poll(async () => (await states()).every(Boolean), { message: "External: the controls are disabled", timeout: 5000 }).toBe(true);
		const externalText = await operateText();
		expect(externalText, "External: the panel says an external app is in control").toMatch(/controlled by an external app through the API \(PAT required\)\./);
		expect(await buttons()[0].getAttribute("title"), "External: a disabled control names the reason").toMatch(/external app/);
		const viaGui = await page.request.post(`${fresh.gateway.base}/admin/api/energy/devices/sim/command`, { headers: { "X-CSRF-Token": await csrf(), Origin: fresh.gateway.base }, data: { action: "hold" } });
		expect(viaGui.status(), "External: a GUI command is refused").toBe(409);
		expect((await viaGui.json()).code, "External: a GUI command answers energy_manager_external").toBe("energy_manager_external");
		const viaPat = await page.request.post(`${fresh.gateway.base}/api/v1/devices/sim/energy/command`, { headers: { Authorization: `Bearer ${secret}` }, data: { action: "hold" } });
		expect(viaPat.status(), "External: a PAT command is accepted").toBe(200);
		// Back to Manual so the remaining checks see the operable controls.
		await switchMode(() => radios().nth(1).click(), "Manual");
	});

	test("Operate shows the relabelled actions; Expert mode is a pure display switch @energy", async () => {
		const operate = await operateText();
		expect(operate, "Operate shows the Manual mode").toMatch(/Manual/);
		expect(operate, "Operate shows the External mode").toMatch(/External/);
		expect(operate, "Operate shows Charge battery").toMatch(/Charge battery/);
		expect(operate, "Operate shows Keep battery idle").toMatch(/Keep battery idle/);
		expect(operate, "Operate shows Discharge battery").toMatch(/Discharge battery/);
		// Normal mode stays compact: no raw capability names, reject detail or expert controls.
		expect(operate, "Normal mode shows no raw capability names, engineering mode or reject detail")
			.not.toMatch(/write_path_convention|reject|Engineering mode|SoC target policy|Revoke verification|Strategy code/i);
		await expect(page.locator(".energy-setup:visible"), "Normal mode shows no setup block").toHaveCount(0);
		// Expert mode is a pure display switch: toggling must not send any write request.
		const writes = [];
		const onRequest = (r) => { if (r.method() !== "GET") writes.push(`${r.method()} ${r.url()}`); };
		page.on("request", onRequest);
		await page.locator("#energy-expert-mode").check();
		await page.locator(".energy-expert").first().waitFor();
		const expertBox = await page.locator(".energy-expert").first().innerText();
		expect(expertBox, "Expert mode ON shows Revoke verification").toMatch(/Revoke verification/);
		expect(expertBox, "Expert mode ON shows engineering mode").toMatch(/Engineering mode/);
		expect(expertBox, "Expert mode ON shows the SoC policy").toMatch(/SoC target policy/);
		await page.locator(".energy-diagnostics summary").first().click();
		const diagText = await page.locator(".energy-diagnostics").first().innerText();
		expect(diagText, "Diagnostics still exposes write_path_convention").toMatch(/write_path_convention/);
		expect(diagText, "Diagnostics still exposes battery_power_sign_convention").toMatch(/battery_power_sign_convention/);
		const expertText = await page.locator(".energy-expert").first().innerText();
		expect(expertText, "Expert shows the charging power limit in kW").toMatch(/Maximum charging power \(kW\)/);
		expect(expertText, "Expert shows the discharging power limit in kW").toMatch(/Maximum discharging power \(kW\)/);
		await page.locator("#energy-expert-mode").uncheck();
		await expect(page.locator(".energy-expert"), "Expert mode OFF hides the expert content again").toHaveCount(0);
		await expect(page.getByText("Revoke verification"), "Expert mode OFF hides Revoke verification again").toHaveCount(0);
		page.off("request", onRequest);
		expect(writes, "toggling Expert mode sent no write request").toEqual([]);
	});

	test("saving the power limits keeps the engineering mode; missing limits open the limits step @energy", async () => {
		const putAnswered = () => page.waitForResponse((r) => r.request().method() === "PUT" && /\/dispatch\/devices\//.test(r.url()), { timeout: 15_000 });
		await page.locator("#energy-expert-mode").check();
		const expertBox = page.locator(".energy-expert").first();
		await expertBox.getByLabel("Engineering mode").check();
		await Promise.all([putAnswered(), expertBox.getByRole("button", { name: "Save engineering mode" }).click()]);
		await Promise.all([putAnswered(), expertBox.getByRole("button", { name: "Save limits" }).click()]);
		expect(limitPuts.length, `engineering mode and limits were both saved ${JSON.stringify(limitPuts)}`).toBeGreaterThanOrEqual(2);
		expect(limitPuts.at(-1).engineering_mode, `saving the power limits keeps the existing engineering_mode ${JSON.stringify(limitPuts)}`).toBe(true);
		await expertBox.getByLabel("Engineering mode").uncheck(); // restore the simulator state
		await Promise.all([putAnswered(), expertBox.getByRole("button", { name: "Save engineering mode" }).click()]);
		await page.locator("#energy-expert-mode").uncheck();
		// Missing power limits open the limits step, without the engineering-mode control.
		mocks.noLimits = true;
		await page.reload();
		await expect(page.locator(".energy-setup-step").first(), "the limits step is shown").toBeVisible({ timeout: 15_000 });
		const noLimitsText = await page.locator(".energy-panel").first().innerText();
		expect(noLimitsText, "missing power limits show the limits step").toMatch(/Maximum charging power/);
		expect(noLimitsText, "the limits step has no engineering mode").not.toMatch(/Engineering mode/);
		mocks.noLimits = false;
	});

	test("the operate story raised no console, network or dialog problems @energy", async () => {
		expect(watch.problems, "no console errors, page errors, failed requests, HTTP >= 400 or native dialogs").toEqual([]);
		expect([...watch.external], "no request leaves the gateway").toEqual([]);
	});
});
