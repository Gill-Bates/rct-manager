//
// tests/e2e/support/settle.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// State-based replacements for the fixed sleeps of the former audit drivers ("let the layout settle").
import { expect } from "@playwright/test";
/** Resolve after two animation frames (a style change has been applied and laid out). */
export const frames = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
/**
 * Wait until the page is visually settled: web fonts loaded, no finite CSS transition/animation running and
 * the layout signature (document size and element count) is identical on two consecutive reads. The
 * SSE stream keeps the network busy for good, so network idleness is not usable here.
 */
export async function settle(page, reason = "page layout settles") {
	await page.evaluate(() => document.fonts.ready);
	let previous = null;
	await expect.poll(async () => {
		const signature = await page.evaluate(() => {
			// Infinite animations (the energy flow dashes) never finish and do not move layout: only finite ones count.
			const running = document.getAnimations().filter((a) => a.playState === "running" && a.effect?.getComputedTiming().endTime !== Infinity).length;
			const doc = document.documentElement;
			return `${running}|${doc.scrollWidth}x${doc.scrollHeight}|${document.body.getElementsByTagName("*").length}`;
		});
		const stable = signature === previous && signature.startsWith("0|");
		previous = signature;
		return stable;
	}, { message: reason, intervals: [100, 150, 250], timeout: 15_000 }).toBe(true);
	await frames(page);
}
