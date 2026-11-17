/* 把静态设计稿渲染成 PNG，供自查。用法：node scripts/shot-html.mjs <html> <out.png> [w] [h] */
import { pathToFileURL } from "node:url";
import path from "node:path";
import process from "node:process";

import { openPage, sleep } from "./lib/cdp.mjs";

const file = path.resolve(process.argv[2]);
const out = path.resolve(process.argv[3]);
const width = Number(process.argv[4] || 1500);
const height = Number(process.argv[5] || 1700);

const page = await openPage(pathToFileURL(file).href, {
  launch: true,
  port: 9333,
  viewport: { width, height },
});
await sleep(800);
const box = await page.evaluate(
  "({ h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth })"
);
console.log("页面尺寸", box);
if (box.h > height) {
  await page.send("Emulation.setDeviceMetricsOverride", {
    width,
    height: Math.ceil(box.h),
    deviceScaleFactor: 1,
    mobile: false,
  });
  await sleep(400);
}
const clipSelector = process.argv[6];
if (clipSelector) {
  const rect = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(clipSelector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height };
  })()`);
  if (!rect) throw new Error(`没有匹配到 ${clipSelector}`);
  const shot = await page.send("Page.captureScreenshot", {
    format: "png",
    clip: { ...rect, scale: 2 },
  });
  const { writeFileSync } = await import("node:fs");
  writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log(out);
} else {
  console.log(await page.screenshot(out));
}
if (page.problems.length) console.log(page.problems.join("\n"));
await page.close();
