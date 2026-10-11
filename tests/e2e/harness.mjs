//
// tests/e2e/harness.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Shared setup of the browser E2E drivers: Playwright, artifact directory, result log and the
// Python helper processes. Importing it creates the artifact directory named by argv[2].
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(path.join(process.env.PLAYWRIGHT_DIR, 'noop.js'));
const playwright = require('playwright');
// BROWSER=chromium|webkit|firefox, VIEWPORT_WIDTH and COLOR_SCHEME=light|dark select the matrix cell.
export const engine = playwright[process.env.BROWSER || 'chromium'];
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
export const OUT = path.resolve(process.argv[2]);
fs.mkdirSync(OUT, { recursive: true });
const PY = process.env.PYTHON || path.join(ROOT, '.venv/bin/python');
export const NEW_PASSWORD = 'e2e-a-much-stronger-password';
export const results = [];

export const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const freePort = () => new Promise((resolve) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); }); });

const procs = [];
process.once('exit', () => { for (const proc of procs) if (proc.exitCode === null) proc.kill('SIGTERM'); });
export function spawnPy(args, log) {
  const proc = spawn(PY, ['-m', ...args], { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
  let out = '';
  proc.stdout.on('data', (d) => { out += d; fs.appendFileSync(log, d); });
  proc.stderr.on('data', (d) => fs.appendFileSync(log, d));
  procs.push(proc);
  return { proc, output: () => out };
}
