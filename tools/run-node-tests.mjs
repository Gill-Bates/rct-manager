//
// tools/run-node-tests.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Local `npm run test:local`: node --test over tests/js/*.test.js with a concurrency from the machine budget.
// Plain `npm test` runs the same files without a cap (as the CI gate does).
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { currentBudget } from "./test-budget.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dir = path.join(repo, "tests/js");

export function nodeTestConcurrency(budget, env = process.env) {
	const raw = env.RCT_LIMIT_NODE;
	if (raw === undefined) return budget.nodeConcurrency;
	if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
		throw new Error("RCT_LIMIT_NODE must be a positive integer");
	}
	return Math.min(budget.nodeConcurrency, Number(raw));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	const requested = process.argv.slice(2);
	const files = requested.length
		? requested
		: readdirSync(dir).filter((name) => name.endsWith(".test.js")).sort().map((name) => path.join("tests/js", name));
	const result = spawnSync(process.execPath, ["--test", `--test-concurrency=${nodeTestConcurrency(currentBudget())}`, ...files], { cwd: repo, stdio: "inherit" });
	if (result.error) throw result.error;
	process.exitCode = result.status ?? 2;
}
