/* 生成页音频条：真跑一轮合成，再看行内波形、进度与点条定位。
 *
 * 用法（先起服务）：node scripts/shot-synth-bar.mjs http://127.0.0.1:17865/ <输出目录> [宽] [高]
 */
import { mkdirSync } from "node:fs";
import path from "node:path";
import process from "node:process";

import { openPage, sleep } from "./lib/cdp.mjs";

const url = process.argv[2] || "http://127.0.0.1:17865/";
const outDir = process.argv[3] || path.join(process.env.TEMP || "/tmp", "vh-synth");
const width = Number(process.argv[4] || 1440);
const height = Number(process.argv[5] || 980);
mkdirSync(outDir, { recursive: true });

const page = await openPage(url, { launch: true, port: 9333, viewport: { width, height } });

const rows = `(() => [...document.querySelectorAll(".engine-item")].map((el) => ({
  name: el.querySelector(".engine-pick-label")?.textContent.trim() || "",
  meta: el.querySelector(".engine-meta")?.textContent.replace(/\\s+/g, " ").trim() || "",
  bar: Boolean(el.querySelector(".engine-bar .vwave")),
  h: Math.round(el.getBoundingClientRect().height),
})))()`;

// 进生成页
for (let i = 0; i < 30; i += 1) {
  if (await page.evaluate("Boolean(document.querySelector('.sh-nav-item'))")) break;
  await sleep(300);
}
await page.evaluate(`(() => {
  const hit = [...document.querySelectorAll(".sh-nav-item")].find(
    (n) => (n.querySelector(".sh-nav-label")?.textContent || "").trim() === "生成"
  );
  hit?.click();
  return Boolean(hit);
})()`);
for (let i = 0; i < 20; i += 1) {
  if (await page.evaluate("Boolean(document.querySelector('.engine-item'))")) break;
  await sleep(300);
}

// 正文 + 音色
await page.evaluate(`(() => {
  const box = document.querySelector(".te-text");
  box.focus();
  box.textContent = "他推开门，屋里没有灯。桌角那盏油灯还亮着，火苗细得像一根线，风一过就歪。";
  box.dispatchEvent(new InputEvent("input", { bubbles: true }));
})()`);
await page.evaluate(`document.querySelector(".rail-list .rail-card")?.click()`);
await sleep(400);
await page.clickText("全选", ".engine-all");
await sleep(200);
console.log("生成前：", JSON.stringify(await page.evaluate(rows)));

await page.evaluate(`document.querySelector(".engine-head .btn.primary")?.click()`);
// 等所有这一轮跑完（不再有「生成中」）
for (let i = 0; i < 60; i += 1) {
  const busy = await page.evaluate(
    `[...document.querySelectorAll(".engine-meta")].some((el) => el.textContent.includes("生成中"))`
  );
  if (!busy) break;
  await sleep(1000);
}
await sleep(600);

const done = await page.evaluate(rows);
console.log("生成后：", JSON.stringify(done, null, 1));
await page.screenshot(path.join(outDir, "synth-rows.png"));

// 点第 60% 那一格
const click = await page.evaluate(`(() => {
  const bar = document.querySelector(".engine-item .engine-bar .vwave");
  if (!bar) return null;
  const r = bar.getBoundingClientRect();
  return { x: Math.round(r.x + r.width * 0.6), y: Math.round(r.y + r.height / 2), w: Math.round(r.width) };
})()`);
console.log("波形：", JSON.stringify(click));
if (click) {
  for (const type of ["mousePressed", "mouseReleased"]) {
    await page.send("Input.dispatchMouseEvent", {
      type,
      x: click.x,
      y: click.y,
      button: "left",
      clickCount: 1,
    });
  }
}
await sleep(900);
const after = await page.evaluate(`(() => ({
  time: document.querySelector("#vh-player")?.currentTime || 0,
  dur: document.querySelector("#vh-player")?.duration || 0,
  clock: document.querySelector(".engine-clock")?.textContent.replace(/\\s+/g, " ").trim() || "",
  progress: getComputedStyle(document.querySelector(".engine-bar .vwave") || document.body).opacity,
  playing: !(document.querySelector("#vh-player")?.paused ?? true),
  checked: [...document.querySelectorAll(".engine-check")].map((el) => el.checked),
}))()`);
console.log("点过之后：", JSON.stringify(after));
await page.screenshot(path.join(outDir, "synth-seek.png"));

// 换一条：点 IndexTTS2 那一行的 25%（应改播这一条，并从 25% 起）
const other = await page.evaluate(`(() => {
  const item = [...document.querySelectorAll(".engine-item")].find(
    (el) => el.querySelector(".engine-pick-label")?.textContent.trim() === "IndexTTS2"
  );
  const bar = item?.querySelector(".engine-bar .vwave");
  if (!bar) return null;
  const r = bar.getBoundingClientRect();
  return { x: Math.round(r.x + r.width * 0.25), y: Math.round(r.y + r.height / 2) };
})()`);
if (other) {
  for (const type of ["mousePressed", "mouseReleased"]) {
    await page.send("Input.dispatchMouseEvent", { type, x: other.x, y: other.y, button: "left", clickCount: 1 });
  }
}
await sleep(700);
console.log("换一条：", JSON.stringify(await page.evaluate(`(() => ({
  src: (document.querySelector("#vh-player")?.src || "").split("/").pop(),
  time: Math.round((document.querySelector("#vh-player")?.currentTime || 0) * 100) / 100,
  dur: Math.round((document.querySelector("#vh-player")?.duration || 0) * 100) / 100,
  clocks: [...document.querySelectorAll(".engine-clock")].map((el) => el.textContent.replace(/\\s+/g, " ").trim()),
}))()`)));

// 深色那一套也要看一眼
await page.evaluate(`document.querySelector(".sh-theme-toggle")?.click()`);
await sleep(600);
await page.screenshot(path.join(outDir, "synth-dark.png"));

if (page.problems.length) console.log("页面报错：\n" + page.problems.join("\n"));
await page.close();
