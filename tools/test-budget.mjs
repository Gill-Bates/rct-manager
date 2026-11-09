//
// tools/test-budget.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Single place that derives the local test resource budget (slots, cores per run, worker counts)
// from the machine: CPU affinity, cgroup limits, available RAM and the current load.
// Used by tools/run-limited.sh (CLI), tests/e2e/playwright.config.mjs (import) and the unit tests.
import { readFileSync } from "node:fs";
import os from "node:os";
import { fileURLToPath } from "node:url";

export function systemSources() {
	return {
		availableParallelism: () => os.availableParallelism(),
		loadavg: () => os.loadavg(),
		readFile: (file) => {
			try { return readFileSync(file, "utf8"); } catch { return null; }
		},
	};
}

const CGROUP_ROOT = "/sys/fs/cgroup";
const UNLIMITED_BYTES = 2 ** 60;

function cgroupV2Dirs(read) {
	const own = (read("/proc/self/cgroup") ?? "").split("\n").find((line) => line.startsWith("0::"));
	const dirs = [];
	let current = own ? own.slice(3).replace(/\/+$/, "") : "";
	for (;;) {
		dirs.push(`${CGROUP_ROOT}${current}`);
		if (!current) break;
		current = current.slice(0, current.lastIndexOf("/"));
	}
	return dirs;
}

// Smallest CPU limit (in cores) along the cgroup chain, or null when none is set.
function cgroupCpuLimit(read) {
	const limits = [];
	for (const dir of cgroupV2Dirs(read)) {
		const raw = read(`${dir}/cpu.max`);
		if (!raw) continue;
		const [quota, period] = raw.trim().split(/\s+/);
		if (quota !== "max" && Number(quota) > 0 && Number(period) > 0) limits.push(Number(quota) / Number(period));
	}
	const quotaV1 = Number(read(`${CGROUP_ROOT}/cpu/cpu.cfs_quota_us`));
	const periodV1 = Number(read(`${CGROUP_ROOT}/cpu/cpu.cfs_period_us`));
	if (quotaV1 > 0 && periodV1 > 0) limits.push(quotaV1 / periodV1);
	return limits.length ? Math.min(...limits) : null;
}

// Smallest free-memory figure in MB across /proc/meminfo and the cgroup chain, or null if unknown.
function availableMemoryMb(read) {
	const candidates = [];
	const meminfo = /^MemAvailable:\s+(\d+)\s+kB/m.exec(read("/proc/meminfo") ?? "");
	if (meminfo) candidates.push(Number(meminfo[1]) / 1024);
	for (const dir of cgroupV2Dirs(read)) {
		const max = Number(read(`${dir}/memory.max`));
		const used = Number(read(`${dir}/memory.current`));
		if (Number.isFinite(max) && max > 0 && max < UNLIMITED_BYTES) candidates.push((max - (Number.isFinite(used) ? used : 0)) / 1048576);
	}
	const limitV1 = Number(read(`${CGROUP_ROOT}/memory/memory.limit_in_bytes`));
	const usedV1 = Number(read(`${CGROUP_ROOT}/memory/memory.usage_in_bytes`));
	if (limitV1 > 0 && limitV1 < UNLIMITED_BYTES) candidates.push((limitV1 - (usedV1 || 0)) / 1048576);
	return candidates.length ? Math.max(0, Math.min(...candidates)) : null;
}

export function gatherInputs(sources = systemSources()) {
	const cpus = Math.max(1, Math.floor(sources.availableParallelism()));
	const cgroupCores = cgroupCpuLimit(sources.readFile);
	return {
		cpus,
		cgroupCores,
		cores: Math.max(1, Math.min(cpus, cgroupCores === null ? cpus : Math.floor(cgroupCores))),
		ramMb: availableMemoryMb(sources.readFile),
		load1: sources.loadavg()[0] ?? 0,
	};
}

function numberFromEnv(env, name, { min, max, integer, fallback }) {
	const raw = env[name];
	if (raw === undefined || raw === "") return fallback;
	const valid = /^[0-9]+(\.[0-9]+)?$/.test(raw) && (!integer || /^[0-9]+$/.test(raw));
	const value = Number(raw);
	if (!valid || !(value >= min) || (max !== undefined && value > max)) {
		const kind = integer ? "an integer" : "a number";
		const range = max === undefined ? `>= ${min}` : `between ${min} and ${max}`;
		throw new Error(`${name} must be ${kind} ${range}, got ${JSON.stringify(raw)}`);
	}
	return value;
}

/**
 * Pure budget calculation.
 * cores = effective cores (affinity and cgroup limit), load1 = 1-minute load average,
 * ramMb = available memory or null. Overrides come only from `env`.
 */
export function computeBudget(inputs, env = {}) {
	const { cores, ramMb, load1 } = inputs;
	const share = numberFromEnv(env, "RCT_TEST_CPU_SHARE", { min: 0.01, max: 1, fallback: 0.5 });
	const runShare = numberFromEnv(env, "RCT_TEST_RUN_SHARE", { min: 0.01, max: 1, fallback: 0.25 });
	const ramShare = numberFromEnv(env, "RCT_TEST_RAM_SHARE", { min: 0.01, max: 1, fallback: 0.5 });
	const ramPerWorkerMb = numberFromEnv(env, "RCT_TEST_RAM_PER_WORKER_MB", { min: 1, integer: true, fallback: 768 });
	const loadThreshold = numberFromEnv(env, "RCT_TEST_LOAD_THRESHOLD", { min: 0.1, fallback: 0.9 });
	const loadAware = numberFromEnv(env, "RCT_TEST_LOAD_AWARE", { min: 0, max: 1, integer: true, fallback: 1 }) === 1;
	const budgetCores = Math.max(1, Math.floor(cores * share));
	const coresPerRun = Math.min(
		budgetCores,
		numberFromEnv(env, "RCT_TEST_CORES_PER_RUN", { min: 1, integer: true, fallback: Math.max(1, Math.floor(cores * runShare)) }),
	);
	const slots = numberFromEnv(env, "RCT_TEST_SLOTS", { min: 1, integer: true, fallback: Math.max(1, Math.floor(budgetCores / coresPerRun)) });
	const freeCores = loadAware ? cores - load1 : cores;
	const runCores = Math.max(1, Math.min(coresPerRun, Math.floor(freeCores)));
	const ramWorkers = ramMb === null ? Infinity : Math.floor((ramMb * ramShare) / ramPerWorkerMb);
	return {
		cores,
		ramMb: ramMb === null ? null : Math.floor(ramMb),
		load1: Number(load1.toFixed(2)),
		budgetCores,
		coresPerRun,
		slots,
		runCores,
		playwrightWorkers: Math.max(1, Math.min(runCores, ramWorkers)),
		pytestWorkers: runCores,
		nodeConcurrency: runCores,
		busy: loadAware && load1 >= cores * loadThreshold,
	};
}

export function currentBudget(env = process.env, sources = systemSources()) {
	return computeBudget(gatherInputs(sources), env);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
	try {
		const budget = currentBudget();
		if (process.argv.includes("--json")) console.log(JSON.stringify(budget));
		else for (const [key, value] of Object.entries(budget)) console.log(`${key}=${value === null ? "" : value}`);
	} catch (error) {
		console.error(`Error: ${error.message}`);
		process.exitCode = 2;
	}
}
