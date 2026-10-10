//
// tests/e2e/listener_restart.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E for the background listener restart against the REAL entry point (`run.py serve`
// with a settings.env, no pre-bound socket): the port is changed in the Settings UI, the service
// re-executes itself, and the page follows it to the new origin. Companion of
// tests/test_restart_integration.py, which covers the same path without a browser.
// Usage: PLAYWRIGHT_DIR=<dir containing node_modules/playwright> node tests/e2e/listener_restart.mjs <artifact-dir>
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep } from './harness.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../..');
const PY = process.env.PYTHON || path.join(ROOT, '.venv/bin/python');
const SECRET = 'e2e-listener-' + 'x'.repeat(40);
const children = [];
process.once('exit', () => { for (const child of children) if (child.exitCode === null) child.kill('SIGKILL'); });

const answers = async (port) => {
  try { return (await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) })).ok; } catch { return false; }
};
const until = async (condition, what, ms = 30000) => {
  const start = Date.now();
  while (Date.now() - start < ms) { if (await condition()) return Date.now() - start; await sleep(100); }
  throw new Error(`timed out waiting for ${what}`);
};

// The real command line of the Docker image and the systemd unit, with an isolated data directory.
async function startService(tag, extraEnv = {}) {
  const dir = fs.mkdtempSync(path.join(OUT, `${tag}-`));
  const port = await freePort();
  const envFile = path.join(dir, 'settings.env');
  fs.writeFileSync(envFile, `BIND_PORT=${port}\nADMIN_DB_PATH=${path.join(dir, 'rct.db')}\nHMAC_SECRET=${SECRET}\n`, { mode: 0o600 });
  const log = path.join(dir, 'server.log');
  const env = { ...process.env, PYTHONUNBUFFERED: '1', ...extraEnv };
  for (const name of ['RCT_API_RESTARTED_AT', 'RCT_API_BOUND_FD']) delete env[name];
  if (!('RCT_API_CONTAINER' in extraEnv)) delete env.RCT_API_CONTAINER;
  const proc = spawn(PY, ['run.py', 'serve', '--env-file', envFile], { cwd: ROOT, env, detached: true });
  children.push(proc);
  let out = '';
  proc.stdout.on('data', (d) => { out += d; fs.appendFileSync(log, d); });
  proc.stderr.on('data', (d) => fs.appendFileSync(log, d));
  await until(() => answers(port), 'the service to start');
  const announced = /Password file:\s+(\S+)\s+\(0600\)/.exec(out)?.[1];
  if (!announced || !path.resolve(announced).startsWith(dir + path.sep)) throw new Error('first-start password file was not announced');
  return { proc, port, dir, output: () => out, password: fs.readFileSync(path.resolve(announced), 'utf8').trim() };
}
async function stop(service) {
  const { proc } = service;
  if (proc.exitCode !== null || proc.signalCode !== null) return;
  await new Promise((resolve) => {
    const timer = setTimeout(() => { proc.kill('SIGKILL'); resolve(); }, 15000);
    proc.once('exit', () => { clearTimeout(timer); resolve(); });
    proc.kill('SIGTERM');
  });
}

async function login(page, base, service) {
  await page.goto(`${base}/login`);
  await page.locator('#password').fill(service.password);
  await page.locator('#login-form button[type=submit]').click();
  await page.waitForURL('**/change-password');
  await page.locator('#current-password').fill(service.password);
  await page.locator('#new-password').fill(NEW_PASSWORD);
  await page.locator('#confirm-password').fill(NEW_PASSWORD);
  await page.locator('#change-password-form button[type=submit]').click();
  await page.waitForURL('**/ui/dashboard');
}
async function typePort(page, value) {
  await page.locator('#setting-bind_port').fill(String(value));
  await page.locator('#setting-bind_port').dispatchEvent('change');
  await page.waitForSelector('#confirm-modal.show');
}
const savedPort = (page) => page.evaluate(async () => (await (await fetch('/admin/api/settings')).json()).settings.bind_port);

const services = [];
let browser;
try {
  browser = await engine.launch();

  // 1. Outside a container: port change, reconnect, persistence, same process.
  const plain = await startService('plain');
  services.push(plain);
  const oldBase = `http://127.0.0.1:${plain.port}`;
  const pid = plain.proc.pid;
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-GB' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page, oldBase, plain);
  await page.goto(`${oldBase}/ui/settings`);
  await page.waitForSelector('#setting-bind_port');

  // Occupied port: held by this process for the whole step, so nothing can race for it.
  const holder = net.createServer();
  await new Promise((resolve) => holder.listen(0, '127.0.0.1', resolve));
  const occupied = holder.address().port;
  await typePort(page, occupied);
  await page.click('#confirm-accept');
  await until(async () => (await savedPort(page)) !== occupied || (await page.locator('.is-reconnecting').count()) > 0, 'the refused save to settle', 5000).catch(() => {});
  await sleep(2500); // longer than the 1.5 s debounce
  check('an occupied port is refused: nothing persisted, no reconnect, old port still serves',
    (await savedPort(page)) === plain.port && (await page.locator('body.is-reconnecting').count()) === 0 && await answers(plain.port));
  check('an occupied port triggers no restart', !/Restarting in the background/.test(plain.output()));
  await new Promise((resolve) => holder.close(resolve));
  await page.reload();
  await page.waitForSelector('#setting-bind_port');

  // A free second port, reserved as late as possible.
  const newPort = await freePort();
  const newBase = `http://127.0.0.1:${newPort}`;
  await typePort(page, newPort);
  const confirmText = await page.locator('#confirm-modal .modal-body').innerText();
  check('outside a container the confirmation says nothing about Docker', !/docker/i.test(confirmText), confirmText);
  await page.click('#confirm-accept');
  const started = Date.now();
  await page.waitForSelector('#reconnect-modal.show');
  const modalAt = Date.now() - started;
  const title = await page.locator('#reconnect-title').innerText();
  const modalText = await page.locator('#reconnect-modal').innerText();
  check('the "Applying settings" modal appears', /Applying settings/.test(title), title);
  check('no restart wording in the modal', !/restart/i.test(modalText), modalText);
  await until(async () => !(await answers(plain.port)), 'the old port to stop answering');
  const oldDownAt = Date.now() - started;
  await until(() => answers(newPort), 'the new port to answer');
  const newUpAt = Date.now() - started;
  check('old port stops, then the new port answers', oldDownAt <= newUpAt, `down ${oldDownAt} ms, up ${newUpAt} ms, modal ${modalAt} ms`);
  await page.waitForURL(`${newBase}/ui/settings`, { timeout: 30000 }).catch(async (error) => {
    await page.screenshot({ path: path.join(OUT, 'listener-restart-stuck.png') });
    throw new Error(`${error.message} (page is at ${page.url()}: ${(await page.locator('#reconnect-modal').innerText()).replace(/\s+/g, ' ')})`);
  });
  await page.waitForSelector('#setting-bind_port');
  check('the page followed the new origin without a login', !page.url().includes('/login'), page.url());
  check('the new port is shown after the reload', (await page.inputValue('#setting-bind_port')) === String(newPort));
  check('the new port is persisted server-side', (await savedPort(page)) === newPort);
  check('the old port is gone', !(await answers(plain.port)));
  check('same process: the PID is unchanged and still running', plain.proc.exitCode === null
    && (plain.output().match(new RegExp(`starting \\(.*\\), pid=${pid}\\b`, 'g')) || []).length === 2);
  check('the restart logged a graceful hand-over', /Shutdown finished/.test(plain.output()) && !/Traceback|Re-exec failed/.test(plain.output()));
  check('no restart wording anywhere on the page', !/restart/i.test(await page.locator('main').innerText()));
  check('no script errors', errors.length === 0, errors.join('; '));
  await page.screenshot({ path: path.join(OUT, 'listener-restart-new-origin.png') });

  // 2. Inside a container the confirmation names the Docker consequences and a cancel changes nothing.
  const boxed = await startService('container', { RCT_API_CONTAINER: '1' });
  services.push(boxed);
  const boxedBase = `http://127.0.0.1:${boxed.port}`;
  const boxedPage = await (await browser.newContext({ locale: 'en-GB' })).newPage();
  await login(boxedPage, boxedBase, boxed);
  await boxedPage.goto(`${boxedBase}/ui/settings`);
  await boxedPage.waitForSelector('#setting-bind_port');
  const help = await boxedPage.locator('#setting-bind_port-help').innerText();
  check('the port help names the Docker mapping', /port mapping/.test(help) && /BIND_PORT/.test(help), help);
  await typePort(boxedPage, await freePort());
  const warning = await boxedPage.locator('#confirm-modal .modal-body').innerText();
  check('in a container the confirmation warns about the port mapping and the health check',
    /published Docker port mapping/.test(warning) && /BIND_PORT/.test(warning) && /unhealthy/.test(warning), warning);
  await boxedPage.click('#confirm-cancel');
  await boxedPage.waitForSelector('#confirm-modal', { state: 'hidden' });
  await sleep(2200);
  check('declining the confirmation changes nothing', (await savedPort(boxedPage)) === boxed.port && await answers(boxed.port));

  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, errors }, null, 2));
  process.exitCode = results.some((result) => !result.ok) ? 1 : 0;
} finally {
  await browser?.close();
  for (const service of services) await stop(service);
}
