//
// tests/e2e/support/timing.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Clock-driven measurement windows. A window is NOT a wait for a state: it is used where the former
// driver slept on purpose (soak, expiry age, "nothing happens" after a debounce). Each call site
// states its reason. Implemented with expect.poll so no fixed sleep or waitForTimeout is used.
import { expect } from "@playwright/test";

/** Resolve once `ms` milliseconds have passed since `startedAt` (default: now). */
export async function elapse(ms, reason, startedAt = Date.now()) {
	const due = startedAt + ms;
	await expect.poll(() => Date.now() >= due, {
		message: `measurement window: ${reason}`, intervals: [100, 250, 500], timeout: Math.max(0, due - Date.now()) + 10_000,
	}).toBe(true);
}
