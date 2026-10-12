//
// tests/e2e/assistant_intro.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E for the hardware-verification introduction and busy indicator.
// Usage: PLAYWRIGHT_DIR=<dir containing node_modules/playwright> node tests/e2e/assistant_intro.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const devicePort = await freePort();
const httpPort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1000);
const server = spawnPy(['tests.e2e.run_server', path.join(dbDir, 'e2e.db'), String(httpPort), String(devicePort), 'energy'], path.join(OUT, 'server.log'));
const stop = async (proc) => {
  if (proc.exitCode !== null || proc.signalCode !== null) return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { proc.kill('SIGKILL'); reject(new Error(`test process ${proc.pid} did not stop`)); }, 5000);
    proc.once('exit', () => { clearTimeout(timeout); resolve(); });
    proc.kill('SIGTERM');
  });
};
let browser;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { ready = (await fetch(`${base}/health`)).ok; } catch { /* startup in progress */ }
    if (ready) break;
    await sleep(200);
  }
  if (!ready) throw new Error('isolated test server did not start');
  const initial = /One-time password:\s+(\S+)/.exec(server.output())?.[1];
  if (!initial) throw new Error('first-start password was not printed in the banner');

  browser = await engine.launch();
  const context = await browser.newContext({
    viewport: { width: Number(process.env.VIEWPORT_WIDTH || 1280), height: 900 },
    colorScheme: process.env.COLOR_SCHEME || 'light', locale: 'en-GB',
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`${base}/login`);
  await page.locator('#password').fill(initial);
  await page.locator('#login-form button[type=submit]').click();
  await page.waitForURL('**/change-password');
  await page.locator('#current-password').fill(initial);
  await page.locator('#new-password').fill(NEW_PASSWORD);
  await page.locator('#confirm-password').fill(NEW_PASSWORD);
  await page.locator('#change-password-form button[type=submit]').click();
  await page.waitForURL('**/ui/dashboard');
  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-panel');
  const manualResponse = page.waitForResponse((response) => response.request().method() === 'PUT' && response.url().endsWith('/mode'));
  await page.locator('.energy-mode-switch [role=radio]').nth(1).click();
  if (!(await manualResponse).ok()) throw new Error('isolated Manual mode setup failed');
  const offResponse = page.waitForResponse((response) => response.request().method() === 'PUT' && response.url().endsWith('/mode'));
  await page.locator('.energy-mode-switch [role=radio]').first().click();
  if (!(await offResponse).ok()) throw new Error('isolated Off mode reset failed');
  const verify = page.getByRole('button', { name: 'Verify hardware' });
  await verify.waitFor({ state: 'visible', timeout: 20000 });
  await verify.click();
  const dialog = page.locator('.energy-assistant.show');
  await dialog.waitFor();
  const intro = await dialog.locator('.modal-body > div').innerText();
  check('short introduction explains the automatic check and its bounded control test',
    intro.trim().split(/\s+/).length <= 55 && /0 W/.test(intro) && /30 seconds/.test(intro)
    && /restores it/.test(intro) && /saves the result/.test(intro), intro);
  check('one start action covers the automatic flow without a stepper',
    (await dialog.getByRole('button', { name: 'Start verification' }).count()) === 1
    && (await dialog.locator('.energy-assistant-steps').count()) === 0);
  await page.screenshot({ path: path.join(OUT, 'assistant-intro.png') });

  await page.route('**/verification-assistant/auto-check', (route) => route.fulfill({
    status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }),
  }));
  const startRoute = '**/verification-assistant/start';
  await page.route(startRoute, async (route) => {
    const response = await route.fetch();
    await sleep(2200);
    await route.fulfill({ response });
  });
  const startResponse = page.waitForResponse((response) => response.url().endsWith('/verification-assistant/start'));
  const startClick = dialog.getByRole('button', { name: 'Start verification' }).click();
  const progress = dialog.locator('.energy-assistant-progress');
  await progress.waitFor({ state: 'visible' });
  const spinner = progress.locator('.spinner-border');
  const active = await spinner.evaluate((node) => ({
    hidden: node.getAttribute('aria-hidden'), animation: getComputedStyle(node).animationName,
  }));
  check('busy state has a visible rotating spinner and text',
    active.hidden === 'true' && active.animation !== 'none' && /Checking/.test(await progress.innerText()), JSON.stringify(active));
  await page.screenshot({ path: path.join(OUT, 'assistant-working.png') });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  check('reduced motion keeps the status but stops rotation',
    await spinner.evaluate((node) => getComputedStyle(node).animationName) === 'none'
    && /Checking/.test(await progress.innerText()));
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  if (!(await startResponse).ok()) throw new Error('verification start request failed');
  await startClick;
  await page.unroute(startRoute);
  const missingRoute = dialog.locator('.alert-danger');
  await missingRoute.waitFor({ state: 'visible' });
  const missingText = await missingRoute.innerText();
  check('a missing assistant route does not claim the inverter is missing',
    /verification service is unavailable/.test(missingText) && /inverter may still be online/.test(missingText)
    && missingText.trim() !== 'Not Found', missingText);
  check('a failed automatic check offers retry without test or save actions',
    (await dialog.getByRole('button', { name: 'Retry' }).count()) === 1
    && (await dialog.getByRole('button', { name: /Run test|Save verification|Yes, continue/ }).count()) === 0);
  await page.unroute('**/verification-assistant/auto-check');
  check('dialog has no script errors', errors.length === 0, errors.join('; '));
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, errors }, null, 2));
  process.exitCode = results.some((result) => !result.ok) ? 1 : 0;
} finally {
  await browser?.close();
  await stop(server.proc);
  await stop(fake.proc);
  fs.rmSync(dbDir, { recursive: true, force: true });
}
