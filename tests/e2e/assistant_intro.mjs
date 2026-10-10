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
let browser;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { ready = (await fetch(`${base}/health`)).ok; } catch { /* startup in progress */ }
    if (ready) break;
    await sleep(200);
  }
  if (!ready) throw new Error('isolated test server did not start');
  const announced = /Password file:\s+(\S+)\s+\(0600\)/.exec(server.output())?.[1];
  if (!announced || !path.resolve(announced).startsWith(dbDir + path.sep)) throw new Error('isolated password file was not announced');
  const initial = fs.readFileSync(path.resolve(announced), 'utf8').trim();

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
  const verify = page.getByRole('button', { name: 'Verify hardware' });
  await verify.waitFor({ state: 'visible', timeout: 20000 });
  await verify.click();
  const dialog = page.locator('.energy-assistant.show');
  await dialog.waitFor();
  const intro = await dialog.locator('.modal-body > div').innerText();
  check('short introduction retains test duration, restoration and save boundary',
    intro.trim().split(/\s+/).length <= 85 && /0 W/.test(intro) && /30 seconds/.test(intro)
    && /restores its previous state/.test(intro) && /Nothing is saved/.test(intro), intro);
  await page.screenshot({ path: path.join(OUT, 'assistant-intro.png') });

  const startRoute = '**/verification-assistant/start';
  await page.route(startRoute, async (route) => { await sleep(2200); await route.continue(); });
  const startClick = dialog.getByRole('button', { name: 'Start check' }).click();
  const progress = dialog.locator('.energy-assistant-progress');
  await progress.waitFor({ state: 'visible' });
  const spinner = progress.locator('.spinner-border');
  const active = await spinner.evaluate((node) => ({
    hidden: node.getAttribute('aria-hidden'), animation: getComputedStyle(node).animationName,
  }));
  check('busy state has a visible rotating spinner and text',
    active.hidden === 'true' && active.animation !== 'none' && /Working/.test(await progress.innerText()), JSON.stringify(active));
  await page.screenshot({ path: path.join(OUT, 'assistant-working.png') });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  check('reduced motion keeps the status but stops rotation',
    await spinner.evaluate((node) => getComputedStyle(node).animationName) === 'none'
    && /Working/.test(await progress.innerText()));
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await startClick;
  await page.unroute(startRoute);
  await progress.waitFor({ state: 'detached', timeout: 10000 });
  check('spinner disappears after the step finishes', true);
  check('dialog has no script errors', errors.length === 0, errors.join('; '));
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, errors }, null, 2));
  process.exitCode = results.some((result) => !result.ok) ? 1 : 0;
} finally {
  await browser?.close();
  server.proc.kill('SIGTERM');
  fake.proc.kill('SIGTERM');
  fs.rmSync(dbDir, { recursive: true, force: true });
}
