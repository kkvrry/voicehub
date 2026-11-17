/* VoiceHub 界面自检 + 截图：真起一个无头浏览器，走一遍「看列表 → 点开音色 → 看样例」。
 *
 * 为什么要有它：``scripts/smoke.py`` 验的是**接口**通不通；「三栏排布对不对」
 * 「多样例才出现『设为主样例』」这类问题，静态检查与接口测试都抓不到，只能看渲染出来的页面。
 *
 * 用法（先起服务）：
 *
 *     python -m app.main --no-browser
 *     node scripts/shot.mjs http://127.0.0.1:17865/ %TEMP%/vh-shots
 *
 * 退出码非 0 = 有断言没过或有运行期报错。浏览器驱动见 ``scripts/lib/cdp.mjs``。
 */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

import { openPage } from "./lib/cdp.mjs";

// 端口只有 Python 那一处产地（``app.infra.config.DEFAULT_PORT``）：想改默认值就改那边，
// 这里只在环境变量没设时跟着那个数走。
const url = process.argv[2] || `http://127.0.0.1:${process.env.VOICEHUB_PORT || 17865}/`;
const outDir = process.argv[3] || path.join(tmpdir(), "vh-shots");
mkdirSync(outDir, { recursive: true });

/** 集合端点统一信封 → 数组；单个资源原样返回（没有 data 键时不动）。 */
const items = (payload) => (Array.isArray(payload) ? payload : payload?.data ?? payload);

const checks = [];
const expect = (label, cond, detail = "") => {
  console.log(`${cond ? "  ok " : " FAIL"} ${label}${detail ? "  — " + detail : ""}`);
  checks.push(!!cond);
};

// **先看现在有没有密钥**——有的话那是用户自己的真钥匙，读回来永远是掩码，
// 脚本不知道该填什么，也就**还不了**。所以"先清掉、再盖一把假的"这一套，
// 只在本来没有的时候才做（从前是照做不误，于是每跑一次界面自检，用户填的钥匙就
// 没了——"填一次没一次"就是这么来的）。自己造的才收得回来。
//
// 为什么必须在 openPage **之前**：引擎面板里那份设置的初值来自 store 里那份
// `/api/v1/engines`（它带着 `secrets_set`），进面板时**不再重新取一次**。页面加载后再
// PUT，应用那份缓存不知道，面板照样渲染成"从来没填过"。
const beforeKey = await fetch(new URL("/api/v1/engines/minimax/settings", url))
  .then((res) => res.json())
  .catch(() => null);
const hadKey = Boolean(beforeKey?.secrets_set?.includes("api_key"));
const seeded = hadKey
  ? beforeKey
  : await fetch(new URL("/api/v1/engines/minimax/settings", url), {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ api_key: "sk-shot-only" }),
    })
      .then((res) => res.json())
      .catch(() => null);
expect(
  hadKey
    ? "检测到你已经填过密钥——所有会抹掉它的动作用例这一轮都跳过"
    : "本来没有密钥，自检自己塞一把（这条链要从真有钥匙出发；收尾会清掉）",
  Array.isArray(seeded?.secrets_set) && seeded.secrets_set.includes("api_key"),
  seeded ? `secrets_set=${JSON.stringify(seeded.secrets_set)}` : "PUT 没成功"
);

// 发现一台插件不等于它在用：左栏、生成页与生产方候选只认**已启用**的。
// 这一轮的断言都按"两台都添加了"的现场验，先显式添加——全新配置里一台都没有，
// 而下面那句"填密钥"只顺带添加了 minimax。
for (const key of ["minimax", "gpt_sovits"]) {
  await fetch(new URL(`/api/v1/engines/${key}/enabled`, url), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ enabled: true }),
  });
}

const page = await openPage(url, {
  launch: true,
  port: 9333,
  viewport: { width: 1500, height: 940 },
});

const mount = await page.waitMount();
expect("应用挂载", Boolean(mount), mount ? `${mount.seconds.toFixed(1)}s` : "没挂上");

// 挂上了还不够——**得确认验的是现在这份产物**。这个无头浏览器复用同一个 profile，
// 缓存里那份 `index.html` 会指向一个**已经不在 dist 里的旧包**（`cdp.mjs` 里已经
// 关掉缓存，这条是"万一还是缓存"的兜底）。症状是最难受的一种：脚本跑完、断言全绿，
// 验的却是一段已经不存在的代码——真实撞到过：新加的界面元素在页面上怎么都找不到，
// 而它明明就在刚构建的包里。**对不上就当场说出来**，比"少了一个元素"好查得多。
const servedBundle = await (async () => {
  const html = await (await fetch(url)).text();
  return (html.match(/assets\/index-[A-Za-z0-9_-]+\.js/) || [""])[0];
})();
const loadedBundle = await page.evaluate(
  `[...document.querySelectorAll("script[src]")].map((s) => s.getAttribute("src")).join(" ")`
);
expect(
  "页面加载的就是现在这份构建产物（不是缓存里的旧包）",
  Boolean(servedBundle) && loadedBundle.includes(servedBundle),
  `页面 ${loadedBundle || "(没有 script)"} / 服务端 ${servedBundle || "(没读到)"}`
);

// 起始主题由脚本自己定成浅色——**不靠上一次运行留下的 localStorage**。
// 这个无头浏览器复用同一个 profile，而下面深色那一段会把主题写成 dark；
// 不显式复位的话，第二次跑起来整份"浅色"截图全是深色的，而且没有任何断言会发现。
await page.evaluate(
  `(() => { localStorage.setItem("vh-theme", "light"); document.documentElement.dataset.theme = "light"; })()`
);
// 同理，**筛选的起点也由脚本自己定**：筛选现在会存进 localStorage（关掉再打开还站在
// 上次那个筛选上），而这个浏览器复用 profile——上一次跑留下的筛选会被恢复出来，
// 于是"卡片数 = 树里计数"那一串断言全线变红，而红的原因跟这一轮改的东西毫无关系。
// 清掉之后要**重新导航**：页面已经带着旧筛选渲染完了，光清存储没用。
await page.evaluate(`(() => { localStorage.removeItem("vh-filters"); })()`);
await page.send("Page.navigate", { url });
await page.waitMount();
await new Promise((r) => setTimeout(r, 400));
const startFilters = await page.evaluate(`(() => {
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  return { stored: localStorage.getItem("vh-filters"), values: sels.map((s) => s.value) };
})()`);
expect(
  "起始没有筛选（起点由脚本自己清，不靠上次残留）",
  startFilters.stored === null && startFilters.values.every((v) => v === ""),
  `stored=${startFilters.stored} 下拉=${JSON.stringify(startFilters.values)}`
);
const startTheme = await page.evaluate(`(() => {
  const lum = (s) => {
    const [r, g, b] = s.match(/\\d+/g).map(Number).map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const body = getComputedStyle(document.body);
  return { attr: document.documentElement.dataset.theme, bgLum: lum(body.backgroundColor) };
})()`);
expect(
  "起始是浅色（起点由脚本自己复位，不靠上次残留）",
  startTheme.attr === "light" && startTheme.bgLum > 0.5,
  `theme=${startTheme.attr} 背景亮度=${startTheme.bgLum.toFixed(3)}`
);

expect("品牌名（左栏顶）", await page.waitText("VoiceHub"));
expect("左栏分类：原神", await page.waitText("原神"));
expect("左栏分类：明日方舟", await page.waitText("明日方舟"));
expect("音色卡片：胡桃", await page.waitText("胡桃"));
expect("音色卡片：钟离", await page.waitText("钟离"));

// 外壳：导航栏 + 分类栏 + 主区。改版后这三块是 `.app-shell / .sh-rail / .sh-content`，
// 所以断言跟着看"左栏在主区左边"——只看类名在不在，三栏塌成一栏也照样绿。
const layout = await page.evaluate(`(() => {
  const shell = document.querySelector(".app-shell");
  const rail = shell?.querySelector(".sh-rail");
  const main = shell?.querySelector(".sh-content");
  if (!shell || !rail || !main) return null;
  const r = rail.getBoundingClientRect();
  const m = main.getBoundingClientRect();
  return {
    railW: Math.round(r.width),
    mainW: Math.round(m.width),
    left: r.right <= m.left + 2,
    sameRow: Math.abs(r.top - m.top) < 40,
  };
})()`);
expect(
  "三栏骨架（分类栏在主区左边、同一行）",
  layout && layout.left && layout.sameRow && layout.railW > 60 && layout.mainW > layout.railW,
  layout ? `分类栏 ${layout.railW}px / 主区 ${layout.mainW}px` : "没有 .app-shell"
);

// 顶栏三个筛选下拉（性别 / 年龄感 / 语言）——**选项也来自后端词表**，
// 所以这里断言的是"下拉在不在 + 选项对不对"，不是"有没有那行中文"。
// 语言这一轮才加进来：它从前是一条标签，当字符串看可以，**筛不了**。
const filters = await page.evaluate(`(() => {
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  const pairs = (s) => [...s.options].map((o) => [o.value, o.textContent.trim()]);
  const age = sels.find((s) => [...s.options].some((o) => o.value === "young"));
  const gender = sels.find((s) => [...s.options].some((o) => o.value === "female"));
  const lang = sels.find((s) => [...s.options].some((o) => o.value === "yue"));
  return {
    count: sels.length,
    gender: gender ? pairs(gender) : null,
    age: age ? pairs(age) : null,
    lang: lang ? pairs(lang) : null,
  };
})()`);
expect(
  "顶栏有性别、年龄感、语言三个筛选",
  filters && filters.count >= 3 && filters.gender && filters.age && filters.lang,
  filters ? `${filters.count} 个下拉` : ""
);
expect(
  "年龄感筛选：不限 + 五档（后端词表）",
  filters &&
    filters.age &&
    filters.age.map((pair) => pair[1]).join("/") === "年龄不限/儿童/少年/青年/中年/老年",
  filters?.age ? JSON.stringify(filters.age) : "没有年龄感筛选"
);
expect(
  "语言筛选：不限 + 粤语（跟性别/年龄并排的第三个维度）",
  filters && filters.lang && filters.lang[0][1] === "语言不限" && filters.lang.some((p) => p[1] === "粤语"),
  filters?.lang ? `首项 ${filters.lang[0][1]}，共 ${filters.lang.length} 项` : "没有语言筛选"
);

// 语言当**筛选**用：选粤语之后剩下的音色必须每一个都是粤语（不是"列表变短了"）。
await page.evaluate(`(() => {
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  const lang = sels.find((s) => [...s.options].some((o) => o.value === "yue"));
  lang.value = "yue";
  lang.dispatchEvent(new Event("change"));
})()`);
let langFiltered = null;
for (let i = 0; i < 25; i += 1) {
  langFiltered = await page.evaluate(`(() => {
    const cards = document.querySelectorAll(".card").length;
    const text = document.querySelector(".result-count")?.textContent || "";
    const loading = document.querySelector(".grid-wrap .empty")?.innerText.includes("加载中");
    return { cards, text, loading: Boolean(loading) };
  })()`);
  if (langFiltered && !langFiltered.loading && langFiltered.cards > 0) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "筛「粤语」之后列表变小，且个数写在标题旁边",
  langFiltered && langFiltered.cards > 0 && langFiltered.cards < 20 && langFiltered.text.includes(String(langFiltered.cards)),
  langFiltered ? `${langFiltered.cards} 张卡片 / “${langFiltered.text.trim()}”` : ""
);
// 清掉，别让它影响后面那些"卡片数 = 树里计数"的断言
await page.evaluate(`(() => {
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  const lang = sels.find((s) => [...s.options].some((o) => o.value === "yue"));
  lang.value = "";
  lang.dispatchEvent(new Event("change"));
})()`);
await new Promise((r) => setTimeout(r, 400));

// ---- 筛选会被记住：重新打开还站在上次那个筛选上 ----
// 改下拉之后列表要**等后端那次查询回来**才变：等"卡片数与标题里的个数对上"，
// 而不是睡一个固定时长——睡出来的绿是赛跑赢的，机器忙一点就红。
const countBefore = await page.evaluate(`document.querySelectorAll(".card").length`);
async function settleGrid() {
  let state = null;
  for (let i = 0; i < 25; i += 1) {
    state = await page.evaluate(`(() => {
      const cards = document.querySelectorAll(".card").length;
      const text = document.querySelector(".result-count")?.textContent || "";
      const loading = Boolean(document.querySelector(".grid-wrap .empty")?.innerText.includes("加载中"));
      return { cards, text, loading, settled: !loading && String(text).includes(String(cards)) };
    })()`);
    if (state?.settled) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  return state;
}
await page.evaluate(`(() => {
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  const g = sels.find((s) => [...s.options].some((o) => o.value === "female"));
  g.value = "female";
  g.dispatchEvent(new Event("change"));
})()`);
let beforeNav = null;
for (let i = 0; i < 25; i += 1) {
  beforeNav = await settleGrid();
  // 起点是"不筛选"那一档：数字没变小说明这次查询还没回来
  if (beforeNav && beforeNav.cards > 0 && beforeNav.cards < countBefore) break;
  await new Promise((r) => setTimeout(r, 200));
}
await page.send("Page.navigate", { url });
await page.waitMount();
let afterNav = null;
for (let i = 0; i < 25; i += 1) {
  const probe = await page.evaluate(`(() => {
    const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
    const g = sels.find((s) => [...s.options].some((o) => o.value === "female"));
    return { value: g ? g.value : "(没有这个下拉)", stored: localStorage.getItem("vh-filters") };
  })()`);
  const grid = await settleGrid();
  afterNav = { ...probe, cards: grid?.cards ?? 0 };
  if (afterNav.value === "female" && afterNav.cards > 0 && afterNav.cards < countBefore) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "重新打开页面，筛选停在原来那一档（下拉 + 列表都对得上）",
  afterNav &&
    afterNav.value === "female" &&
    beforeNav &&
    afterNav.cards === beforeNav.cards &&
    String(afterNav.stored || "").includes("female"),
  afterNav
    ? `下拉=${afterNav.value} 卡片 ${beforeNav?.cards}→${afterNav.cards}（起点 ${countBefore}）stored=${afterNav.stored}`
    : ""
);
// 自己收尾：把筛选清掉并重载，后面那些断言要在"不筛选"的起点上跑
await page.evaluate(`(() => {
  localStorage.removeItem("vh-filters");
  const sels = [...document.querySelectorAll(".lib-head select.lib-select")];
  const g = sels.find((s) => [...s.options].some((o) => o.value === "female"));
  g.value = "";
  g.dispatchEvent(new Event("change"));
})()`);
await new Promise((r) => setTimeout(r, 400));

// ---- 音色特点：一排可点的标签 ----
const chips = await page.evaluate(`(() => {
  const bar = document.querySelector(".lib-tagbar");
  if (!bar) return null;
  const all = bar.querySelector(".tag-chip.is-all");
  const list = [...bar.querySelectorAll(".tag-chip:not(.is-all)")];
  return {
    allText: all ? all.textContent.trim() : "",
    count: list.length,
    first: list[0] ? list[0].textContent.trim() : "",
    hasCount: Boolean(list[0]?.querySelector(".tag-count")),
  };
})()`);
expect(
  "标签区就是拿来点的（一颗「全部」+ 每颗写着还剩几条）",
  chips && chips.count > 0 && chips.hasCount && chips.allText === "全部",
  chips ? `${chips.count} 颗，第一颗「${chips.first}」` : "没有 .lib-tagbar"
);
const chipPick = await page.evaluate(`(() => {
  const chip = document.querySelector(".lib-tagbar .tag-chip:not(.is-all)");
  if (!chip) return null;
  const tag = chip.textContent.replace(/\\d+$/, "").trim();
  chip.click();
  return tag;
})()`);
let chipState = null;
for (let i = 0; i < 25; i += 1) {
  chipState = await page.evaluate(`(() => {
    const active = document.querySelector(".lib-tagbar .tag-chip.active");
    const cards = document.querySelectorAll(".card").length;
    const loading = document.querySelector(".grid-wrap .empty")?.innerText.includes("加载中");
    return { active: active ? active.textContent.trim() : "", cards, loading: Boolean(loading) };
  })()`);
  if (chipState && !chipState.loading && chipState.active) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "点一颗标签：它变选中，且列表只剩带这颗标签的音色",
  chipState && chipState.active.includes(chipPick) && chipState.cards > 0,
  chipState ? `选中「${chipState.active}」→ ${chipState.cards} 张` : ""
);
await page.evaluate(`document.querySelector(".lib-tagbar .tag-chip.active")?.click()`);
await new Promise((r) => setTimeout(r, 400));

await page.screenshot(path.join(outDir, "01-grid.png"));

// ---- 点开「胡桃」（3 条样例）----
const clicked = await page.evaluate(`(() => {
  const card = [...document.querySelectorAll(".card")].find((el) => el.innerText.includes("胡桃"));
  if (!card) return false;
  card.click();
  return true;
})()`);
expect("点开音色详情", clicked);
expect("详情里出现样例区标题", await page.waitText("音频样例"));

const hutao = await page.evaluate(`(() => {
  const d = document.querySelector(".vh-modal");
  if (!d) return null;
  const rows = [...d.querySelectorAll(".sample")];
  // 主样例 = **一颗按钮两副长相**：亮着（.is-on）的那颗是状态，灰的那几颗是操作。
  const toggles = [...d.querySelectorAll(".sample-main")];
  return {
    title: d.querySelector(".vh-title")?.textContent?.trim(),
    // 说话内容现在就是那条长条的名字（无框输入，input 或 textarea）——文件名不再显示
    texts: rows.map((r) => r.querySelector(".sample-text")?.value ?? ""),
    n: rows.length,
    // 情绪是一颗 chip（ChipSelect）：没标时那颗写的是占位词「＋ 情绪」。
    emotions: rows.map((r) => {
      const text = (r.querySelector(".sample-meta .vchip-text")?.textContent || "").trim();
      return text.startsWith("＋") ? "" : text;
    }),
    idle: toggles.filter((b) => !b.classList.contains("is-on")).length,
    lit: toggles.filter((b) => b.classList.contains("is-on")).length,
    // 亮着那颗写的是状态（主样例），不是操作；灰的那几颗写的是操作。
    litText: toggles.find((b) => b.classList.contains("is-on"))?.textContent.trim() || "",
    idleText: toggles.find((b) => !b.classList.contains("is-on"))?.textContent.trim() || "",
  };
})()`);
expect("详情标题 = 音色名", hutao && hutao.title === "胡桃", hutao ? hutao.title : "没打开详情");
expect("样例 3 条", hutao && hutao.n === 3, hutao ? `共 ${hutao.n} 条` : "");
expect(
  "样例行带说话内容（长条上直接写着那句话，不显示文件名）",
  hutao && hutao.texts.every((t) => t.length > 0) && hutao.texts.some((t) => t.includes("往生堂")),
  hutao ? hutao.texts.map((t) => t.slice(0, 6)).join(" / ") : ""
);
expect(
  "情绪来自文件名并回填（「默认」是主样例的磁盘标记，不是情绪——那一条该空着）",
  hutao &&
    hutao.emotions.filter(Boolean).length === 2 &&
    hutao.emotions.every((e) => e !== "默认"),
  hutao ? hutao.emotions.map((e) => e || "（空）").join(", ") : ""
);
expect(
  "主样例只有一颗按钮：3 条各一颗，其中 1 颗亮着、2 颗是灰的「设为主样例」",
  hutao && hutao.lit === 1 && hutao.idle === 2 && hutao.litText === "主样例" && hutao.idleText === "设为主样例",
  hutao ? `亮着的 ${hutao.lit} 颗（${hutao.litText || "无"}）、灰的 ${hutao.idle} 颗（${hutao.idleText || "无"}）` : ""
);

// ---- 那一条本身就是播放键：点条就播，点文字只进编辑 ----
const barClick = await page.evaluate(`(async () => {
  const row = document.querySelector(".vh-modal .sample");
  if (!row) return null;
  const bar = () => document.querySelector(".pb-label")?.textContent.trim() || "";
  const before = bar();
  row.click();
  await new Promise((r) => setTimeout(r, 500));
  const played = bar();
  // 再点一次停下，别把后面几条断言拖进"正在放音"的状态
  row.click();
  await new Promise((r) => setTimeout(r, 350));
  return { before, played, changed: played !== before && played.length > 0 };
})()`);
expect(
  "整条可点即播（点长条就放这一段，不用去找那颗圆钮）",
  barClick?.changed,
  barClick ? `底栏“${barClick.played}”` : "没有 .sample"
);

const textClick = await page.evaluate(`(async () => {
  const rows = [...document.querySelectorAll(".vh-modal .sample")];
  const row = rows[1];
  const text = row?.querySelector(".sample-text");
  if (!text) return null;
  const bar = () => document.querySelector(".pb-label")?.textContent.trim() || "";
  const before = bar();
  text.click();
  await new Promise((r) => setTimeout(r, 400));
  return { before, after: bar(), focused: document.activeElement === text };
})()`);
expect(
  "点说话内容只进编辑，不会顺手开始放音",
  textClick && textClick.before === textClick.after,
  textClick ? `底栏：${textClick.before || "（空）"} → ${textClick.after || "（空）"}` : "没有第二条样例"
);

// ---- 详情里的文案：一格只写名字，说明与待办都不该出现在界面上 ----
const detailCopy = await page.evaluate(`(() => {
  const d = document.querySelector(".vh-modal");
  if (!d) return null;
  return {
    // 就地编辑的那几段没有可见标签，说法挂在 aria-label 上（读屏照样念得出来）
    aria: [...d.querySelectorAll(".vinline")].map((el) => el.getAttribute("aria-label") || ""),
    // 属性是名字后面那颗圆标 + 一排只写字的胶囊，说法同样只在 aria-label 里
    chips: [...d.querySelectorAll(".vh-title-row .vgender-btn, .vh-traits .vchip")].map(
      (el) => el.getAttribute("aria-label") || ""
    ),
    // 名称不再单独占一格：它就是标题，点一下才变成输入框
    title: d.querySelector(".vh-title")?.textContent?.trim() || "",
    titleEditable: Boolean(d.querySelector(".vh-title.is-editable")),
    text: d.innerText,
    // 位置不再单占一行：文件夹的入口是右上角那颗「在资源管理器里打开」
    hasReveal: Boolean(d.querySelector('.vh-actions button[aria-label="在资源管理器里打开"]')),
  };
})()`);
expect(
  "「描述」就叫「描述」；名称就是标题本身（没有单独的「显示名」那一格）",
  detailCopy &&
    detailCopy.aria.includes("描述") &&
    !detailCopy.aria.includes("显示名") &&
    detailCopy.titleEditable,
  detailCopy ? `aria=${detailCopy.aria.join("、")}｜标题可点=${detailCopy.titleEditable}` : ""
);
expect(
  "语言 / 性别 / 年龄感都在（**语言**是筛选栏的一维，从前界面上看不到也改不了）",
  detailCopy &&
    ["语言", "性别", "年龄感"].every((name) =>
      detailCopy.chips.some((label) => label.startsWith(name))
    ),
  detailCopy ? detailCopy.chips.join("、") : ""
);
expect(
  "详情里没有说明性与待办口吻的文字（那是注释与文档的事）",
  detailCopy &&
    !["适合念什么", "参考文本随样例", "它需要知道自己", "还没接", "试听功能"].some((w) =>
      detailCopy.text.includes(w)
    ),
  detailCopy ? `${detailCopy.text.length} 字` : ""
);
expect(
  "本地音色给出文件夹入口（右上角那颗「在资源管理器里打开」，不再占正文一行）",
  detailCopy && detailCopy.hasReveal,
  detailCopy ? `有入口=${detailCopy.hasReveal}` : ""
);

// ---- 一条音频自己的标签：加了要真写进库（sidecar + 索引），不是只画在屏幕上 ----
const sampleTagUI = await page.evaluate(`(async () => {
  const row = document.querySelector(".vh-modal .sample");
  if (!row) return { stage: "没有样例行" };
  // 加标签那颗是 TagChips 里的虚线 chip（写着「＋」），不是另外一颗 .chip-add。
  const add = row.querySelector(".sample-meta .vtag-field .vchip.is-blank");
  if (!add) return { stage: "没有加标签那颗 chip" };
  add.click();
  await new Promise((r) => setTimeout(r, 200));
  // 弹层挂在 body 上（固定定位），不在这一行里面——所以从 document 上找。
  const pop = document.querySelector(".vpop");
  const input = pop?.querySelector("input");
  if (!input) return { stage: "弹层没开或没有输入框", pops: document.querySelectorAll(".vpop").length };
  input.value = "笑声";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  // 合成出来的 Enter 不会触发原生提交（不可信事件没有默认动作），直接派发 submit。
  pop.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 600));
  return {
    stage: "ok",
    tags: [...row.querySelectorAll(".sample-meta .vtag-field .vchip:not(.is-blank)")].map((c) =>
      c.textContent.replace("✕", "").trim()
    ),
  };
})()`);
expect(
  "样例行有自己的标签栏（与音色的标签是两处，各管一段）",
  sampleTagUI?.tags?.includes("笑声"),
  sampleTagUI ? `${sampleTagUI.stage}｜${JSON.stringify(sampleTagUI.tags || [])}` : "没有结果"
);

/** 绕过界面直接问后端：**界面上画出来不等于存住了**——本地 ref 改了而请求没发的话，
 *  屏幕上照样有一颗芯片，只有重新取详情才看得出来。 */
async function hutaoSampleTags() {
  const list = await fetch(new URL("/api/v1/voices?q=%E8%83%A1%E6%A1%83", url))
    .then((r) => r.json())
    .then(items)
    .catch(() => []);
  const hit = (list || []).find((v) => (v.shown_name || "").includes("胡桃"));
  if (!hit) return null;
  const detail = await fetch(new URL("/api/v1/voices/" + encodeURIComponent(hit.id), url)).then((r) =>
    r.json()
  );
  return (detail.samples || []).map((s) => s.tags || []);
}
const tagPersisted = await hutaoSampleTags();
expect(
  "那颗标签真的落了库（重取详情还在）",
  tagPersisted && tagPersisted.some((tags) => tags.includes("笑声")),
  JSON.stringify(tagPersisted)
);
// 自己收尾：删掉这一颗，别留在演示库里
await page.evaluate(`(async () => {
  // 上面加完标签之后弹层还开着：先把弹层收掉，否则下面那一下点的是"关上它"。
  document.querySelector(".vpop-scrim")?.click();
  await new Promise((r) => setTimeout(r, 200));
  const row = document.querySelector(".vh-modal .sample");
  const chip = [...row.querySelectorAll(".sample-meta .vtag-field .vchip:not(.is-blank)")].find((c) =>
    c.textContent.includes("笑声")
  );
  chip?.click();
  await new Promise((r) => setTimeout(r, 250));
  // 点开的是"点一下移除"的弹层：再点那一颗才算删掉
  [...document.querySelectorAll(".vpop .vchip")].find((c) => c.textContent.includes("笑声"))?.click();
  await new Promise((r) => setTimeout(r, 500));
})()`);
const tagCleared = await hutaoSampleTags();
expect(
  "删掉那一颗，库里也跟着没了",
  tagCleared && !tagCleared.some((tags) => tags.includes("笑声")),
  JSON.stringify(tagCleared)
);

// ---- 属性：选项**来自后端词表**，前端不写第二份中文 ----
// 性别是名字后面那颗圆标，语言与年龄感是抬头里那两颗胶囊（都靠 aria-label 认）。
// 断言因此改看"点开之后弹出的那些词"，那才是用户看到的东西。
async function optionsOf(label) {
  return page.evaluate(`(async () => {
    const name = ${JSON.stringify(label)};
    const trigger =
      name === "性别"
        ? document.querySelector(".vh-modal .vh-title-row .vgender-btn")
        : [...document.querySelectorAll(".vh-modal .vh-traits .vchip")].find((el) =>
            (el.getAttribute("aria-label") || "").startsWith(name)
          );
    if (!trigger) return null;
    trigger.click();
    await new Promise((r) => setTimeout(r, 200));
    const pop = document.querySelector(".vpop");
    const list = pop
      ? [...pop.querySelectorAll(".vgender-opt, .vpop-grid .vchip")].map((b) => b.textContent.trim())
      : null;
    const clear = pop?.querySelector(".vpop-clear")?.textContent.trim() || "";
    document.querySelector(".vpop-scrim")?.click();
    await new Promise((r) => setTimeout(r, 150));
    return { list, clear };
  })()`);
}
const tagForm = {
  gender: await optionsOf("性别"),
  age: await optionsOf("年龄感"),
};
expect(
  "性别是名字后面那颗圆标，点开是词表里的三档（含「清空」）",
  tagForm.gender && tagForm.gender.list.join("/") === "女声/男声/其他" && tagForm.gender.clear === "清空性别",
  tagForm.gender ? JSON.stringify(tagForm.gender) : "没有性别圆标"
);
expect(
  "年龄感是胶囊弹层而不是自由文本，五档 + 清空",
  tagForm.age &&
    tagForm.age.list.join("/") === "儿童/少年/青年/中年/老年" &&
    tagForm.age.clear === "清空年龄感",
  tagForm.age ? JSON.stringify(tagForm.age) : "没有年龄感那颗胶囊"
);

// 详情里**改完就存**，没有「保存修改」按钮——所以"这一格真的连着后端"不能再看
// 按钮亮不亮，得**问后端**：改一档，重取详情，值必须是新的。
//
// 起点值不假定为空——冒烟脚本会往同一个演示库里写 age，所以"改成另一档"而不是
// "改成某个写死的档"才是稳的：比的是**后端从旧值变成另一个值、又变回来**，
// 不依赖"青年"对应哪个键。
const autoSave = await page.evaluate(`(async () => {
  // 年龄感那颗胶囊在抬头那一排里，靠 aria-label 认（界面上不写"年龄感"三个字）。
  const chip = [...document.querySelectorAll(".vh-modal .vh-traits .vchip")].find((el) =>
    (el.getAttribute("aria-label") || "").startsWith("年龄感")
  );
  // **这一颗没了就直接返回 null**（而不是让 evaluate 抛异常）：
  // 抛的话整个脚本当场退出，后面几十条断言一条都跑不到，最后看到的只有一个堆栈。
  if (!chip) return null;
  const chipText = () => (chip.querySelector(".vchip-text")?.textContent || "").trim();
  const original = chipText();
  const readBackend = async () => {
    const payload = await fetch("/api/v1/voices?q=%E8%83%A1%E6%A1%83").then((r) => r.json());
    const rows = Array.isArray(payload) ? payload : payload?.data || [];
    const hit = (rows || []).find((v) => (v.shown_name || "").includes("胡桃"));
    return hit ? hit.age : "(没找到这条)";
  };
  const openPop = async () => {
    chip.click();
    await new Promise((r) => setTimeout(r, 180));
    return document.querySelector(".vpop");
  };
  const closePop = async () => {
    document.querySelector(".vpop-scrim")?.click();
    await new Promise((r) => setTimeout(r, 120));
  };
  const options = async () => {
    await openPop();
    const list = [...document.querySelectorAll(".vpop .vpop-grid .vchip")].map((b) => b.textContent.trim());
    await closePop();
    return list;
  };
  // **等到后端真的变了**再往下走：改完就存是"发一次请求"，什么时候回来不由界面决定。
  const settle = async (want) => {
    for (let i = 0; i < 40; i += 1) {
      const value = await readBackend();
      if (want === null ? value !== before : value === want) return value;
      await new Promise((r) => setTimeout(r, 100));
    }
    return "(没等到)";
  };
  const before = await readBackend();
  const other = (await options()).find((t) => t && t !== original);
  if (!other) return null;
  await openPop();
  [...document.querySelectorAll(".vpop .vpop-grid .vchip")]
    .find((b) => b.textContent.trim() === other)
    ?.click();
  const after = await settle(null);
  // 改回去：原来那一档，或者（原本没标时）「清空」
  await openPop();
  if (original) {
    [...document.querySelectorAll(".vpop .vpop-grid .vchip")]
      .find((b) => b.textContent.trim() === original)
      ?.click();
  } else {
    document.querySelector(".vpop-clear")?.click();
  }
  const reverted = await settle(before);
  return { original, other, before, after, reverted };
})()`);
const noSaveButton = await page.evaluate(
  `![...document.querySelectorAll(".vh-modal button")].some((b) => b.textContent.includes("保存修改"))`
);
expect("没有「保存修改」按钮了（改完即存）", noSaveButton === true, "");
expect(
  "改一格就存进去了（**问后端**，不是看按钮）：改 → 后端变；改回去 → 后端也跟着回来",
  autoSave && autoSave.after !== autoSave.before && autoSave.reverted === autoSave.before,
  autoSave ? JSON.stringify(autoSave) : ""
);

// ---- 名称不再单独占一格：点标题就地改 ----
// **只验"点了会变成输入框、点走又变回标题"**，不真改名字——演示库里那条名字是
// 拿来比对的，改坏了后面那些"找胡桃那张卡"的用例会一起迷路。
const titleEdit = await page.evaluate(`(async () => {
  const title = document.querySelector(".vh-modal .vh-title.is-editable");
  if (!title) return { stage: "标题不可点" };
  title.click();
  await new Promise((r) => setTimeout(r, 260));
  const input = document.querySelector(".vh-modal .vh-title-input");
  if (!input) return { stage: "点了没出现输入框" };
  const value = input.value;
  // 失焦（合成事件：无头浏览器里主动 blur 不派发事件）
  input.dispatchEvent(new FocusEvent("blur"));
  await new Promise((r) => setTimeout(r, 400));
  return {
    stage: "ok",
    value,
    back: Boolean(document.querySelector(".vh-modal .vh-title")),
    stillInput: Boolean(document.querySelector(".vh-modal .vh-title-input")),
  };
})()`);
expect(
  "点标题就能就地改名（点走之后回到标题；没有另开一格「显示名」）",
  titleEdit?.stage === "ok" && titleEdit.back && !titleEdit.stillInput,
  JSON.stringify(titleEdit)
);

// 本地音色也要真能移除（从前只有一句"把文件夹移出扫描目录后重扫"的文案）。
// **不点它**——点了就真把演示库的文件夹送进回收站了；这一条只验"按钮在、
// 并且说清了它要动什么"。
// 现在管理动作收在抬头右上角那一排图标里（打开目录 / 隐藏 / 移除 ｜ 收藏 / 关闭），
// 说明挂在 title 上，所以这里按 aria-label 找，不按文字找。
const actions = await page.evaluate(`(() => {
  const list = [...document.querySelectorAll(".vh-modal .vh-actions button")];
  const byLabel = (name) => list.find((x) => (x.getAttribute("aria-label") || "") === name);
  const remove = byLabel("移除音色");
  return {
    n: list.length,
    labels: list.map((x) => x.getAttribute("aria-label") || ""),
    removeTitle: remove ? remove.title || "" : "",
    hasFavorite: Boolean(byLabel("收藏") || byLabel("取消收藏")),
  };
})()`);
expect(
  "抬头右上角那一排：打开目录 / 隐藏 / 移除 ｜ 收藏 / 关闭（收藏没被挤掉）",
  actions && actions.n === 5 && actions.hasFavorite && actions.labels.includes("在资源管理器里打开"),
  actions ? `${actions.n} 颗：${actions.labels.join("、")}` : "没有 .vh-actions"
);
expect(
  "本地音色的「移除」是一颗真按钮（title 里说清文件会进回收站）",
  actions && actions.removeTitle.includes("回收站"),
  actions ? `title=“${actions.removeTitle}”` : "没有这颗按钮"
);

await page.screenshot(path.join(outDir, "02-detail-hutao.png"));

// ---- 关掉，点开「阿米娅」（1 条样例）→ 不该有「设为主样例」----
await page.evaluate(`(() => {
  [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
})()`);
let singleClosed = false;
for (let i = 0; i < 20; i += 1) {
  singleClosed = await page.evaluate(`!document.querySelector(".vh-modal")`);
  if (singleClosed) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("抽屉可关闭", singleClosed);

await page.evaluate(`(() => {
  const card = [...document.querySelectorAll(".card")].find((el) => el.innerText.includes("阿米娅"));
  if (!card) return false;
  card.click();
  return true;
})()`);
let amiya = null;
for (let i = 0; i < 20; i += 1) {
  amiya = await page.evaluate(`(() => {
    const d = document.querySelector(".vh-modal");
    if (!d) return null;
    return {
      title: d.querySelector(".vh-title")?.textContent?.trim(),
      n: d.querySelectorAll(".sample").length,
      setMain: [...d.querySelectorAll(".set-main")].filter((b) => b.textContent.includes("主样例")).length,
    };
  })()`);
  if (amiya && amiya.n > 0) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("单样例：抽屉有 1 条", amiya && amiya.n === 1, amiya ? `title=${amiya.title}` : "");
expect("单样例：不出现「设为主样例」", amiya && amiya.setMain === 0, amiya ? `找到 ${amiya.setMain} 个` : "");

await page.screenshot(path.join(outDir, "03-detail-single.png"));

// ---- 引擎（插件）：左栏那一条 → 右侧引擎面板 → 克隆页签 ----
// 这一段验的是"**插件说了算**"：表单照着 settings_schema 渲染、能力照着
// capability_notes 渲染、映射照着 mapping 渲染——前端不认识 MiniMax 任何一个键。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
})()`);
await new Promise((r) => setTimeout(r, 250));

const row = await page.evaluate(`(() => {
  const el = [...document.querySelectorAll(".tree-item.engine")].find((n) =>
    n.innerText.includes("MiniMax")
  );
  if (!el) return null;
  return {
    text: el.innerText.replace(/\\s+/g, " ").trim(),
    kind: el.querySelector(".badge.platform")?.textContent?.trim() || "",
    title: el.getAttribute("title") || "",
    icon: el.querySelector("img.engine-icon")?.getAttribute("src") || "",
    warn: !!el.querySelector(".engine-warn"),
    titles: [...el.querySelectorAll("button")].map((b) => b.title),
    count: el.querySelector(".count")?.textContent?.trim() || "",
  };
})()`);
expect(
  "左栏「引擎」那一段有 MiniMax 这一条（**管理入口**，不是音色集合）",
  Boolean(row),
  row ? row.text : "没找到"
);
expect("形态由插件声明（云端）", row && row.kind === "云端", row ? `kind=${row.kind}` : "");
// 左栏这一列说的是"**我有哪些音色**"：用户自己的扫描根 + 一段「引擎」。
// 引擎那一段点进去是**面板**（配置 / 通道 / 克隆），音色本身统一在「全部音色」里看。
expect(
  "左栏有「引擎」段标题，且不再有「本地音色 / 平台音色」两个分组",
  await page.evaluate(`(() => {
    const body = document.querySelector(".sider-body");
    if (!body) return false;
    const text = body.innerText;
    return text.includes("引擎") && !text.includes("本地音色") && !text.includes("平台音色");
  })()`),
  ""
);
expect(
  "引擎那一行**不挂音色计数**（计数在它的面板里说，这里只当管理入口）",
  row && row.count === "",
  row ? `count=${row.count}` : ""
);
expect(
  "引擎那一行的 title 写着它的音色保存目录（生成的东西存在哪）",
  row && row.title.includes("音色保存目录"),
  row ? row.title : ""
);
expect(
  "图标来自插件目录（/api/v1/engines/minimax/icon）",
  row && row.icon === "/api/v1/engines/minimax/icon",
  row ? row.icon : ""
);
expect(
  "行上**一颗按钮都没有**（⟳ 拉清单在它的面板里，不在左栏这一行）",
  row && row.titles.length === 0,
  row ? row.titles.join(" / ") || "（无按钮）" : ""
);

// ---- 同一套渲染，装第二台引擎（本机推理那种）不用改前端 ----
// GPT-SoVITS 与 MiniMax 的形态、能力**处处不同**：不要密钥、没有清单、没有克隆、
// 音色来自你自己的本地音色。这一段验的正是"这些差异全都由插件说，界面对着渲染"。
const localRow = await page.evaluate(`(() => {
  const el = [...document.querySelectorAll(".tree-item.engine")].find((n) =>
    n.innerText.includes("GPT-SoVITS")
  );
  if (!el) return null;
  return {
    text: el.innerText.replace(/\\s+/g, " ").trim(),
    kind: el.querySelector(".badge.platform")?.textContent?.trim() || "",
    icon: el.querySelector("img.engine-icon")?.getAttribute("src") || "",
    warn: !!el.querySelector(".engine-warn"),
    titles: [...el.querySelectorAll("button")].map((b) => b.title),
  };
})()`);
expect("左栏「引擎」那一段同时列出本机引擎 GPT-SoVITS", Boolean(localRow), localRow ? localRow.text : "没找到");
expect("形态由插件声明（本地）", localRow && localRow.kind === "本地", localRow ? `kind=${localRow.kind}` : "");
expect(
  "本机引擎不需要凭据 → 不打「未就绪」的感叹号",
  localRow && !localRow.warn,
  localRow ? `warn=${localRow.warn}` : ""
);
expect(
  "没有清单可拉 → 行上不给 ⟳（别摆一颗点了才报错的按钮）",
  localRow && localRow.titles.length === 0,
  localRow ? localRow.titles.join(" / ") : ""
);
expect(
  "第二台引擎的图标同样来自它自己的目录",
  localRow && localRow.icon === "/api/v1/engines/gpt_sovits/icon",
  localRow ? localRow.icon : ""
);

// ---- 「引擎卸载了、音色还留在库里」那种残留条目 ----
// 它是**另一种行**：没有配置可配、没有清单可拉、没有面板可开，唯一有意义的动作是
// "看它留下的那些音色"。真实踩到的坑：这种行也挂在「引擎」段里、点开也是面板 →
// **右侧一整块空白**（没有面板、没有提示、也没有任何报错），用户只会以为页面坏了。
// 所以它单列一段、点一下是筛音色。数据由 scripts/make_demo.py 在隔离库里造。
const treePayload = await (await fetch(new URL("/api/v1/tree", url))).json();
const orphanRows = (treePayload.engines || []).filter((row) => row.orphan);
const leftoverRows = await page.evaluate(`(() => {
  const all = [...document.querySelectorAll(".tree-item.orphan")];
  if (!all.length) return null;
  const el = all[0];
  const body = document.querySelector(".sider-body");
  return {
    n: all.length,
    text: el.innerText.replace(/\\s+/g, " ").trim(),
    badge: el.querySelector(".badge.platform")?.textContent?.trim() || "",
    note: el.getAttribute("title") || "",
    // **它不能挂 engine 这个类**：那个类是"点开是面板"的意思。
    isEngineClass: el.classList.contains("engine"),
    sectionBefore: (body?.innerText || "").includes("已卸载的引擎"),
  };
})()`);
if (!orphanRows.length || !leftoverRows) {
  // 库里没有这种行 → 这一段验不到。**明说跳过**，别假装通过。
  console.log(
    "  ..  跳过「引擎卸载后的残留条目」这一段：库里没有这种行" +
      "（跑一次 scripts/make_demo.py 就会造出来，见它的 seed_leftovers）"
  );
} else {
  expect(
    "「引擎」那一段**只列真引擎**：残留的源单列成「已卸载的引擎」",
    leftoverRows.sectionBefore && leftoverRows.isEngineClass === false,
    `段标题=${leftoverRows.sectionBefore} 挂了 engine 类=${leftoverRows.isEngineClass}`
  );
  expect(
    "残留那一条的称呼来自后端（已卸载），不是前端自己编的词",
    leftoverRows.badge === "已卸载",
    `badge=${leftoverRows.badge}`
  );
  expect(
    "残留那一条的悬停说明来自后端（kind_note），说清点它会怎样",
    leftoverRows.note.includes("插件已经不在了") && leftoverRows.note.includes("只在音色库里看"),
    leftoverRows.note
  );

  const leftoversApi = items(
    await (
      await fetch(new URL(`/api/v1/voices?source=${encodeURIComponent(orphanRows[0].source)}`, url))
    ).json()
  );
  await page.evaluate(`(() => {
    document.querySelector(".tree-item.orphan")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  const afterLeftover = await page.evaluate(`(() => {
    const main = document.querySelector(".main");
    const head = document.querySelector(".lib-head h1");
    return {
      // **主区不能是空的**——那一整块空白正是这条断言的来历。
      blank: !main || !main.innerText.trim(),
      inLibrary: Boolean(document.querySelector(".grid")),
      panel: Boolean(document.querySelector(".engine-panel")),
      cards: document.querySelectorAll(".card").length,
      text: main ? main.innerText.replace(/\\s+/g, " ").trim().slice(0, 120) : "",
      // 标题写的就是**选中了谁**（与本地分类同一套：标题 = 那一行的名字）。
      // 从前这里写着「只看「legacy_engine」」，前面还并摆一颗「只看：… ✕」，
      // 读出来"只看"说两遍——两处都删了。
      title: head ? head.innerText.replace(/\\s+/g, " ").trim() : "",
    };
  })()`);
  expect(
    "点残留那一条：**主区不是空白**（从前这里整块空掉，还没有任何报错）",
    !afterLeftover.blank,
    afterLeftover.blank ? "主区什么都没有" : afterLeftover.text
  );
  expect(
    "点残留那一条：**不进面板**（它没有面板可开），而是留在音色库",
    afterLeftover.inLibrary && !afterLeftover.panel,
    `在音色库=${afterLeftover.inLibrary} 开了面板=${afterLeftover.panel}`
  );
  expect(
    "点残留那一条＝只看它留下的音色（卡片数对着接口数）",
    afterLeftover.cards === leftoversApi.length && afterLeftover.cards > 0,
    `界面上 ${afterLeftover.cards} 张 / 接口 ${leftoversApi.length} 条`
  );
  expect(
    "标题写的就是选中了谁（`legacy_engine`），**不带「只看「」」那一圈**",
    afterLeftover.title.startsWith("legacy_engine") && !afterLeftover.title.includes("只看"),
    afterLeftover.title || "标题里没写"
  );
  // 收尾：把筛选撤掉（左栏「全部音色」那一行就是"看全部"），免得下面那些
  // "看全部音色"的断言被它带偏。
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".sider-body .tree-item")]
      .find((el) => el.innerText.includes("全部音色"))
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 300));
}

// ---- 「全部音色」里的**引擎层**（MiniMax 那一层）：一层看全它的音色，有生成样例的在前 ----
// 数据由 scripts/make_demo.py 在隔离库里造（seed_generated）：两条平台音色，其中一条
// 生成过一段——而它磁盘上的文件夹名（`demo_gen`）与显示名（「演示生成音色」）**故意
// 不一样**，那正是当初把生成样例弄丢的那种数据：从前读回时按显示名去找文件夹，
// 于是这条音色的生成样例整段消失（"两个半边"只剩一个）。
const levelRow = await page.evaluate(`(() => {
  const el = document.querySelector(".tree-item.engine-voices");
  if (!el) return null;
  return {
    text: el.innerText.replace(/\\s+/g, " ").trim(),
    icon: el.querySelector("img.engine-icon")?.getAttribute("src") || "",
    badge: el.querySelector(".badge.platform")?.textContent.trim() || "",
    note: el.getAttribute("title") || "",
    count: el.querySelector(".count")?.textContent.trim() || "",
    isEngineRow:
      el.className.includes("tree-item engine") && !el.className.includes("engine-voices"),
  };
})()`);
const sourceCount = items(await (await fetch(new URL("/api/v1/voices?source=minimax", url))).json())
  .length;
expect(
  "「全部音色」里有 MiniMax 这一层（这台引擎自己的那一组音色）",
  Boolean(levelRow),
  levelRow ? levelRow.text : "没找到 .tree-item.engine-voices"
);
expect(
  "这一层带引擎图标与形态徽标（不是 📁 文件夹——它是引擎给的清单）",
  levelRow && levelRow.icon === "/api/v1/engines/minimax/icon" && levelRow.badge === "云端",
  levelRow ? `${levelRow.icon} / ${levelRow.badge}` : ""
);
expect(
  "这一层右边写着**它自己有多少条**（左栏唯一能看出「清单还在」的地方）",
  levelRow && Number(levelRow.count) === sourceCount && sourceCount >= 2,
  `层上「${levelRow && levelRow.count}」 / 接口 ${sourceCount} 条`
);
expect(
  "这一层的悬停说明它是这台引擎给的清单",
  levelRow && levelRow.note.includes("MiniMax 的全部音色"),
  (levelRow && levelRow.note) || ""
);
expect(
  "这一层**不是**「引擎」段里那种行（点它是筛音色，不是开面板）",
  levelRow && levelRow.isEngineRow === false,
  `className=${levelRow && levelRow.isEngineRow}`
);

// 「全部音色」那个数 = **库里一共多少条**（含平台清单），不是各扫描根相加。
const treeCounts = await page.evaluate(`(() => {
  const rows = [...document.querySelectorAll(".sider-body .tree-item")];
  const all = rows.find((el) => el.innerText.includes("全部音色"));
  // **只数扫描根那一层**：引擎层（engine-voices）与分类节点（sub）都排除掉——
  // 它们也是"某一层有多少条"，但把它们加起来正好等于总数，那一比就假了。
  const roots = rows.filter((el) => {
    if (!el.querySelector(".count")) return false;
    if (el.className.includes("engine")) return false;
    if (el.className.includes("tree-item sub")) return false;
    return !el.innerText.includes("全部音色");
  });
  return {
    all: all ? Number(all.querySelector(".count")?.textContent.trim() || 0) : -1,
    roots: roots.map((el) => Number(el.querySelector(".count")?.textContent.trim() || 0)),
    // **同一个引擎在「全部音色」这一组里只该有一行**：从前是「引擎层」+「保存目录」
    // 两行（一个写着全部条数、一个写着生成过的那几条，哪个都不是全部）。
    // 判据用 classList.contains("engine")（**精确的那个类名**）：用字符串 includes 会把
    // "engine-voices" / "engine-dir" 也一并算进去——而那两样正是这一层要数的东西。
    minimax: rows.filter(
      (el) => !el.classList.contains("engine") && el.innerText.includes("MiniMax")
    ).length,
  };
})()`);
const mmRowsInTree = treeCounts.minimax;
const everything = items(await (await fetch(new URL("/api/v1/voices", url))).json()).length;
expect(
  "左栏「全部音色」的数＝库里一共多少条（从前只累加扫描根：写着 577、点开 880）",
  treeCounts.all === everything,
  `左栏 ${treeCounts.all} / 接口 ${everything} 条`
);
expect(
  "光靠扫描根那几个数是**得不出**它的（说明它确实不是「相加」来的）",
  treeCounts.all > treeCounts.roots.reduce((a, b) => a + b, 0),
  `全部音色 ${treeCounts.all} > 各根相加 ${treeCounts.roots.reduce((a, b) => a + b, 0)}`
);

// 点这一层 → 只看它自己的音色，而且**有生成样例的排在前面**。
await page.evaluate(`(() => { document.querySelector(".tree-item.engine-voices")?.click(); })()`);
await new Promise((r) => setTimeout(r, 450));
const levelView = await page.evaluate(`(() => {
  const cards = [...document.querySelectorAll(".card")];
  const head = document.querySelector(".lib-head h1");
  const first = cards[0];
  return {
    cards: cards.length,
    first: first?.querySelector(".card-name")?.textContent.trim() || "",
    // 圆标**与本地音色同一套**（首字母色块）——平台音色不换成那台引擎的 logo。
    firstIsInitial: Boolean(first?.querySelector("span.cover")) &&
      !first?.querySelector("img.cover"),
    title: head ? head.innerText.replace(/\\s+/g, " ").trim() : "",
  };
})()`);
expect(
  "点这一层 = 只看这台引擎的音色（卡片数 = 接口给的条数）",
  levelView.cards === sourceCount,
  `卡片 ${levelView.cards} / 接口 ${sourceCount}`
);
expect(
  "标题**就是这台引擎的名字**（与本地分类同一套；不带「只看「」」那一圈）",
  levelView.title.startsWith("MiniMax") && !levelView.title.includes("只看"),
  `标题「${levelView.title}」`
);
expect(
  "**有生成样例的排在前面**（这类清单动辄几百条，那几条才是你要的）",
  levelView.first === "演示生成音色",
  `第一张是「${levelView.first}」`
);
expect(
  "圆标**与本地音色同一套**（首字母色块，不是那台引擎的 logo）",
  levelView.firstIsInitial,
  levelView.firstIsInitial ? "首字母色块" : "画成了别的（img.cover？）"
);
expect(
  "**这台引擎在「全部音色」里只有一行**（不再同时有「引擎层」与「保存目录」两个 MiniMax）",
  mmRowsInTree === 1,
  `「全部音色」里提到 MiniMax 的行有 ${mmRowsInTree} 行`
);
await page.evaluate(`(() => { document.querySelector(".card")?.click(); })()`);
await new Promise((r) => setTimeout(r, 600));
const levelDrawer = await page.evaluate(`(() => {
  const d = document.querySelector(".vh-modal");
  if (!d) return null;
  return {
    sections: [...d.querySelectorAll(".section-title")].map((t) =>
      t.innerText.replace(/\\s+/g, " ").trim()
    ),
    head: d.innerText.replace(/\\s+/g, " ").trim().slice(0, 90),
  };
})()`);
expect(
  "平台音色的右侧**有「生成样例」**（与它未重构时同一套：它本来就是那台引擎生成的）",
  Boolean(levelDrawer?.sections?.some((t) => t.includes("生成样例"))),
  levelDrawer ? levelDrawer.sections.join(" / ") : "没打开抽屉"
);
expect(
  "打开的是那条**显示名与文件夹名不一样**的音色（认归属认的是 id）",
  Boolean(levelDrawer?.head?.includes("演示生成音色")),
  levelDrawer?.head || ""
);
await page.evaluate(`(() => {
  [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
})()`);
await new Promise((r) => setTimeout(r, 250));

// 那条平台音色自己：**生成样例必须挂在它身上**——只有"按归属 id 认"这一条路能认回
// 来（名字那条必然错过：文件夹叫 demo_gen、显示名叫「演示生成音色」）。
const demoVoice = await (await fetch(new URL("/api/v1/voices/minimax:demo_gen", url))).json();
expect(
  "平台音色也认回了它自己的那一段（**按归属 id，不是按显示名**）",
  (demoVoice.samples || []).length === 1 && Boolean(demoVoice.folder),
  `样例 ${(demoVoice.samples || []).length} 条 · folder=${demoVoice.folder || "（空）"}`
);
expect(
  "那条平台音色的**显示名仍然是用户改过的那个**（认归属不该把元信息带回去）",
  demoVoice.shown_name === "演示生成音色",
  `shown_name=${demoVoice.shown_name}`
);
// 收尾：把筛选撤掉（左栏「全部音色」那一行），免得下面那些"看全部音色"的断言被带偏。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".sider-body .tree-item")]
    .find((el) => el.innerText.includes("全部音色"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 300));

// ---- 引擎保存目录不占左栏一行（无论开不开旧开关）----

// ---- 引擎指定的「音色保存目录」那一行（**只有库里真有这种数据时才有**）----
// 它**只为"给不出音色清单"的那几家**存在（库桥那几家：产物按本地音色落盘，没有同名
// 条目可挂，不扫就成了磁盘上看得见、库里看不见的东西）。能给清单的引擎不给这一行
// ——它有自己那一层（`engine-voices`），再来一行就是同一个引擎的两行。
// 隔离演示库里两台引擎都还带不出这种目录 → **明说跳过**，不假装验过。
const engineDirRow = await page.evaluate(`(() => {
  const row = document.querySelector(".tree-item.engine-dir");
  if (!row) return null;
  return {
    text: row.innerText.replace(/\\s+/g, " ").trim(),
    icon: row.querySelector("img.engine-icon")?.getAttribute("src") || "",
    badge: row.querySelector(".badge.platform")?.textContent.trim() || "",
    note: row.getAttribute("title") || "",
    isEngineRow: row.className.includes("tree-item engine"),
  };
})()`);
if (!engineDirRow) {
  console.log(
    "  ..  跳过「引擎保存目录那一行」：库里没有这种数据" +
      "（它只在引擎给不出音色清单时出现）"
  );
} else {
  expect(
    "引擎指定的保存目录那一行**带引擎图标**（不是 📁——它属于那台引擎）",
    engineDirRow.icon.startsWith("/api/v1/engines/") && engineDirRow.icon.endsWith("/icon"),
    `icon=${engineDirRow.icon}`
  );
  expect(
    "那一行写着**引擎的名字与形态**（不是光秃秃的文件夹名）",
    Boolean(engineDirRow.text) && Boolean(engineDirRow.badge),
    `${engineDirRow.text} / ${engineDirRow.badge}`
  );
  expect(
    "那一行的悬停说清它是这台引擎的保存目录（光看名字会以为是用户自己加的扫描根）",
    engineDirRow.note.includes("的音色保存目录"),
    engineDirRow.note
  );
  expect(
    "那一行**不是**「引擎」段里那种行（点它是筛音色，不是开面板）",
    engineDirRow.isEngineRow === false,
    `className=${engineDirRow.isEngineRow}`
  );
}


// ---- 设置 · 目录分栏：**它是右侧主区那一页**（左栏底下那颗齿轮进来），不是弹窗 ----
// 两段：扫描根目录 / 全局生成结果目录。每台引擎的音色保存目录**不在这里**——它跟着
// 那台引擎，住引擎面板的「音色」分区。分成两三处的话，用户改完一格还得去别处找另一格。
//
// 这一段守的是**"它是一页"**这件事：齿轮点下去之后主区换了内容、DOM 里一个弹窗都没有、
// 从左栏真能回到列表。从前它是 460px 的弹窗，扫描根那种一行一个目录的列表
// 在那么窄的地方只剩两三个字看得见（见 SettingsView 顶部那段说明）。
const enginesPayload = items(await (await fetch(new URL("/api/v1/engines", url))).json());
const engineLabels = enginesPayload.filter((e) => e.key).map((e) => e.label);

const foldersOpened = await page.evaluate(`(() => {
  const btn = [...document.querySelectorAll(".sider-foot button")].find(
    (b) => b.getAttribute("title") === "设置"
  );
  if (!btn) return false;
  btn.click();
  return true;
})()`);
await new Promise((r) => setTimeout(r, 300));
const foldersPage = await page.evaluate(`(() => {
  const m = document.querySelector(".settings-panel");
  if (!m) return null;
  return {
    inMain: Boolean(m.closest(".main")),
    modals: document.querySelectorAll(".modal").length,
    tabs: [...m.querySelectorAll(".tab")].map((b) => b.textContent.trim()),
    activeTab: m.querySelector(".tab.active")?.textContent.trim() || "",
    titles: [...m.querySelectorAll(".section-title")].map((n) => n.textContent.trim()),
    inputs: m.querySelectorAll(".folder-line input").length,
    flex: getComputedStyle(m).display === "flex",
    rows: m.querySelectorAll(".asr-row").length,
    foot: (m.querySelector(".panel-foot .muted")?.textContent || "").trim(),
  };
})()`);
expect(
  "左栏底下那颗齿轮进的是「设置」页：主区里那一页，**DOM 里没有弹窗**",
  foldersOpened && foldersPage && foldersPage.inMain && foldersPage.modals === 0,
  foldersPage
    ? `在主区=${foldersPage.inMain} / 弹窗 ${foldersPage.modals} 个`
    : "没开"
);
expect(
  "落的是「目录」分栏",
  foldersPage && foldersPage.activeTab === "目录",
  foldersPage ? `分栏=${foldersPage.tabs.join(" / ")}（当前 ${foldersPage.activeTab}）` : ""
);
expect(
  "这一页只有两栏：目录 / 语音识别（**语音识别不再单占一颗按钮**）",
  foldersPage && foldersPage.tabs.length === 2 && foldersPage.tabs[1] === "语音识别",
  foldersPage ? foldersPage.tabs.join(" / ") : ""
);
expect(
  "目录分栏里**不出现**语音识别那张表（两栏不是一个长页面）",
  foldersPage && foldersPage.rows === 0,
  foldersPage ? `${foldersPage.rows} 行模型` : ""
);
expect(
  "两段都在：扫描根目录 / 生成结果目录",
  foldersPage &&
    foldersPage.titles.length === 2 &&
    ["扫描根目录", "生成结果目录"].every((t) => foldersPage.titles.includes(t)),
  foldersPage ? foldersPage.titles.join(" / ") : ""
);
expect(
  "只有两个输入框（扫描根那一行 + 生成结果目录）——各引擎的音色保存目录不在这页",
  foldersPage && foldersPage.inputs === 2,
  foldersPage ? `${foldersPage.inputs} 个输入框` : ""
);
expect(
  "版式与引擎面板同一套（纵向 flex：抬头/页签/底栏常驻，中间那一段自己滚）",
  foldersPage && foldersPage.flex,
  foldersPage ? `display=${foldersPage.flex ? "flex" : "不是 flex"}` : ""
);
expect(
  "离开不保存改动这件事写在底栏上（页面上没有「取消」这颗按钮了）",
  foldersPage && foldersPage.foot.includes("不会保存") && foldersPage.foot.includes("保存并扫描"),
  foldersPage ? foldersPage.foot : ""
);
await page.screenshot(path.join(outDir, "05-folder-settings.png"));
// 收尾：**从左栏回列表页**。各页右上那颗「← 音色库」已经撤了——左栏一直都在，
// 每页再摆一颗是多余的。这一步同时是一条断言：设置页消失、列表回来。
const settingsBack = await page.evaluate(`(() => {
  const el = [...document.querySelectorAll(".sider-body .tree-item")].find((n) =>
    n.innerText.includes("全部音色")
  );
  if (!el) return false;
  el.click();
  return true;
})()`);
await new Promise((r) => setTimeout(r, 400));
const afterBack = await page.evaluate(`(() => ({
  page: Boolean(document.querySelector(".settings-panel")),
  grid: Boolean(document.querySelector(".grid-wrap")),
}))()`);
expect(
  "从左栏「全部音色」回到列表页（设置页消失、列表回来）",
  settingsBack && !afterBack.page && afterBack.grid,
  `设置页还在=${afterBack.page} / 列表回来=${afterBack.grid}`
);

// 另一处入口：生成页下方那块「语音识别」里那句"去装一档"。它得把用户**直接送到
// 语音识别那一栏**——齿轮那颗落在「目录」，照抄过去就等于让用户进去再自己切一下。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".synth-entry")].find((n) => n.textContent.includes("生成"))?.click();
})()`);
await new Promise((r) => setTimeout(r, 900));
const jumpedTo = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll(".synth-asr button")].find((x) =>
    x.textContent.includes("装一档")
  );
  if (!b) return null;
  b.click();
  return b.textContent.trim();
})()`);
await new Promise((r) => setTimeout(r, 900));
const jumpState = await page.evaluate(`(() => {
  const m = document.querySelector(".settings-panel");
  return {
    page: Boolean(m),
    tab: (m?.querySelector(".tab.active")?.textContent || "").trim(),
  };
})()`);
expect(
  "生成页那句「去装一档」直接落在语音识别那一栏（不是落回「目录」）",
  Boolean(jumpedTo) && jumpState.page && jumpState.tab === "语音识别",
  `按钮=${jumpedTo || "没找到"} / 页=${jumpState.page} 分栏=${jumpState.tab || "(空)"}`
);
// 收回列表页：后面那段要照旧点左栏的引擎行。
await page.evaluate(`document.querySelector(".settings-panel .panel-head .btn")?.click()`);
await new Promise((r) => setTimeout(r, 400));

// 点本机引擎那一行 → **它自己的面板**：音色那一块说的是"用音色库的音色"，
// **不能**照搬云端那句"点 ⟳ 拉清单"（入口在这台引擎上行上根本不存在）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item.engine")]
    .find((n) => n.innerText.includes("GPT-SoVITS"))
    ?.click();
})()`);
let localPanel = null;
for (let i = 0; i < 25; i += 1) {
  localPanel = await page.evaluate(`(() => {
    const panel = document.querySelector(".engine-panel");
    if (!panel) return null;
    return {
      inMain: Boolean(panel.closest(".main")),
      title: (panel.querySelector(".engine-title")?.innerText || "").replace(/\\s+/g, " ").trim(),
      tabs: [...panel.querySelectorAll(".tab")].map((b) => b.textContent.trim()),
    };
  })()`);
  if (localPanel) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "点本机引擎那一行：开的是**右侧面板**，抬头带引擎名与形态",
  localPanel && localPanel.inMain && localPanel.title.includes("本地"),
  localPanel ? localPanel.title : "没开"
);
// 「音色」那一块：本机引擎说的是"用音色库的全部音色"（通道由后端给）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")]
    .find((b) => b.textContent.includes("音色"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 260));
const localVoices = await page.evaluate(`(() => {
  const panel = document.querySelector(".engine-panel");
  return panel ? panel.innerText.replace(/\\s+/g, " ").trim() : "";
})()`);
expect(
  "本机引擎的「音色」分区说的是「用音色库的全部音色」，且不给「拉清单」",
  localVoices.includes("音色库的全部音色") && !localVoices.includes("拉取音色清单"),
  localVoices.slice(0, 120)
);
// 音色通道：**这台引擎的音色从哪来、怎么供**。逻辑在配置里，所以这一块得在界面上
// 看得见、改得动——而它长什么样由后端下发（前端不写任何一台引擎的专有词）。
const localChannel = await page.evaluate(`(() => {
  const card = document.querySelector(".engine-panel .channel-card");
  if (!card) return null;
  return {
    headings: [...document.querySelectorAll(".engine-panel .section-title")].map((t) =>
      t.textContent.trim()
    ),
    badge: card.querySelector(".badge")?.textContent.trim() || "",
    note: (card.querySelector(".channel-note")?.innerText || "").replace(/\\s+/g, " ").trim(),
    buttons: [...card.querySelectorAll("button")].map((b) => b.textContent.trim()),
    selects: card.querySelectorAll("select").length,
    promote: card.querySelectorAll("input[type=checkbox]").length,
  };
})()`);
const localChannelApi = items(
  await (await fetch(new URL("/api/v1/engines", url))).json()
).find((e) => e.key === "gpt_sovits").channel;
expect(
  "通道是「音色」分区里的一等区块（小标题 + 形态徽章 + 「保存通道」）",
  localChannel &&
    localChannel.headings.includes("音色通道") &&
    localChannel.buttons.includes("保存通道"),
  localChannel ? localChannel.headings.join(" / ") : "没有 .channel-card"
);
expect(
  "通道的形态说法由后端给（前端不写「音色库供给」这几个字）",
  localChannel &&
    localChannel.badge === localChannelApi.mode_label &&
    localChannel.note.includes(localChannelApi.mode_note),
  localChannel ? `徽章「${localChannel.badge}」` : ""
);
expect(
  "只有一个候选来源时不摆下拉（没得选的东西不摆一个控件）",
  localChannel && localChannel.selects === 0 && localChannelApi.allowed_modes.length === 1,
  `下拉 ${localChannel?.selects} 个 / 后端候选 ${localChannelApi.allowed_modes.length} 个`
);
await page.screenshot(path.join(outDir, "04-engine-panel-local.png"));

// **平台音色清单只能从平台拉**，而自检不花额度——所以先数一数库里有几条。
// 一条都没有（没填密钥 / 从没点过刷新）时，这一整段跳过并说明原因，
// 而不是让一处空清单把后面那些与平台无关的断言一起带走。
const platformCount = items(await (await fetch(new URL("/api/v1/voices?source=minimax", url))).json())
  .length;
if (platformCount > 0) {
  // **兜底**：这一段中间把"MiniMax 的音色保存目录"临时改到系统临时目录去了。要是中间
  // 哪一步抛了异常，那一格就留在那儿——下一次自检会看到"demo_gen 的样例怎么不见了"
  // （它的音频在默认目录里，而服务现在看的是临时目录），而报出来的错与真正的原因
  // 隔着十万八千里。所以无论走哪条路出去，都把它放回默认。
  try {
  // ---- 平台音色的「生成结果」**就是它自己的样例**（一套逻辑、一套画法）----
  // 这一段自己造状态、自己收尾：把 MiniMax 的音色保存目录指到一个临时目录，往里放一条
  // 与某条**平台音色同名**的音频——那条音色当场就该多出一条样例，而且画成与本地音色
  // 一样的那种卡片（同一个 SampleList）。用完把那一格恢复成默认。
  // 为什么造这条状态：这是"我明明生成了，它却没有"那个症状的正身——生成出来的东西必须
  // 挂在**发起生成的那条音色**上，而不是另立一条同名的本地音色、或者谁也挂不上。
  const outTmp = mkdtempSync(path.join(tmpdir(), "vh-engine-out-"));
  await fetch(new URL("/api/v1/engines/minimax/output", url), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ dir: outTmp }),
  });

  // 挑一条**还没生成过、且目录名安全**的平台音色：文件夹按它的**稳定名**
  // （`voice.name`）建，不是显示名——显示名是给人看的、会改，改完这段音频就再也
  // 认不回来（见 infra/archive.write_audio）。**挑没样例的那条**：这一段要验的是
  // "生成出来的音频正好让它多出 1 条样例"，本来就有样例的话那个 1 是数不出来的。
  const platformVoices = items(await (await fetch(new URL("/api/v1/voices?source=minimax", url))).json());
  const chosen = platformVoices.find(
    (v) => /^[A-Za-z0-9 _-]+$/.test(v.name || "") && !(v.sample_count > 0)
  );
  mkdirSync(path.join(outTmp, chosen.name), { recursive: true });
  writeFileSync(path.join(outTmp, chosen.name, "20260101-000000-01.mp3"), Buffer.alloc(2048));
  // 顺带把"这一段念的是什么、拿什么参数生成的"也放进 sidecar —— 生成时本来就会写
  // （见 archive.record_sample），这里只是照那个形状造一份。那张样例卡片该把两样都显示出来：
  // 只显示"1 条样例 + 播放"的话，同一条音色生成五次之后还是谁也说不清哪一段是哪一句。
  writeFileSync(
    path.join(outTmp, chosen.name, "voice.json"),
    JSON.stringify(
      {
        version: 1,
        samples: {
          "20260101-000000-01.mp3": {
            text: "茉莉花开",
            emotion: "开心",
            params: { speed: "1.2", pitch: "1.3", format: "mp3" },
          },
        },
      },
      null,
      2
    ),
    "utf8"
  );

  // 重进那一组（列表与卡片上的"几条样例"都跟着变）
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".tree-item.engine")]
      .find((n) => n.innerText.includes("MiniMax"))
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  // **必须显式切到「音色」分区**：面板每次打开都落在第一个分区（设置），而
  // 「在全部音色中查看」那一颗只在「音色」里——靠上一次留在哪个分区是靠不住的。
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".engine-panel .tab")]
      .find((b) => b.textContent.trim() === "音色")
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 260));
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".engine-panel .btn")]
      .find((b) => b.textContent.includes("在全部音色中查看"))
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  // 卡片列表是**异步拉的**（点那颗按钮只是改了筛选）——所以要等它到，不能点完就读。
  // 等不到时把**当时真有哪些卡片**一起报出来：不然"没找到那张卡片"没法查。
  let generatedCard = null;
  let cardNames = [];
  for (let i = 0; i < 25; i += 1) {
    const seen = await page.evaluate(`(() => {
      const cards = [...document.querySelectorAll(".card")];
      const card = cards.find((c) =>
        c.innerText.includes(${JSON.stringify(chosen.shown_name)})
      );
      return {
        names: cards.map((c) => c.querySelector(".card-name")?.textContent.trim() || ""),
        hit: card
          ? {
              text: card.innerText.replace(/\\s+/g, " ").trim(),
              hasPlay: Boolean(card.querySelector(".btn.mini")),
            }
          : null,
      };
    })()`);
    cardNames = seen.names;
    if (seen.hit) {
      generatedCard = seen.hit;
      await page.evaluate(`(() => {
        [...document.querySelectorAll(".card")]
          .find((c) => c.innerText.includes(${JSON.stringify(chosen.shown_name)}))
          ?.click();
      })()`);
      break;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  expect(
    "生成过的那条平台音色，卡片上就有「1 条样例」",
    generatedCard && generatedCard.text.includes("1 条样例"),
    generatedCard ? generatedCard.text.slice(0, 60) : `没找到「${chosen.shown_name}」；卡片有：${cardNames.join("、") || "（一张都没有）"}`
  );
  expect(
    "卡片上那颗 ▶ 也出现了（与本地音色同一颗：判据是「有没有音频」）",
    generatedCard && generatedCard.hasPlay,
    generatedCard ? `hasPlay=${generatedCard.hasPlay}` : ""
  );

  let generatedDrawer = null;
  for (let i = 0; i < 25; i += 1) {
    generatedDrawer = await page.evaluate(`(() => {
      const d = document.querySelector(".vh-modal");
      if (!d) return null;
      const rows = [...d.querySelectorAll(".sample")];
      return {
        sections: [...d.querySelectorAll(".vh-sec-title, .section-title")].map((t) =>
          t.textContent.replace(/\\s+/g, "").trim()
        ),
        texts: rows.map((r) => r.querySelector(".sample-text")?.value || ""),
        params: rows.map((r) => r.querySelector(".sample-params")?.textContent.trim() || ""),
        // 只有一条样例时「设为主样例」不出现（设它等于没设），状态改由这一条自己的 is-main 说。
        main: rows.filter((r) => r.classList.contains("is-main")).length,
        toggle: rows.filter((r) => r.querySelector(".sample-main")).length,
        n: rows.length,
        // 文件名不再写在条上，但留在 title 里（要认文件的时候还认得出来）
        file: rows[0]?.getAttribute("title") || "",
        text: d.innerText,
        hasReveal: Boolean(
          d.querySelector('.vh-actions button[aria-label="在资源管理器里打开"]')
        ),
      };
    })()`);
    if (generatedDrawer && generatedDrawer.n > 0) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  expect(
    "详情里是**同一份样例长条**（音频样例段落 + 一条 .sample + 主样例标记）",
    generatedDrawer &&
      generatedDrawer.sections.includes("音频样例（1）") &&
      generatedDrawer.n === 1 &&
      generatedDrawer.main === 1 &&
      // 只有一条时不给「设为主样例」这颗按钮：设它等于没设。
      generatedDrawer.toggle === 0,
    generatedDrawer
      ? `${generatedDrawer.sections.join(" / ")}；样例 ${generatedDrawer.n} 条、主样例标记 ${generatedDrawer.main}、按钮 ${generatedDrawer.toggle}`
      : "没开详情"
  );
  expect(
    "文件名不摆在条上，只留在 title 里",
    generatedDrawer && generatedDrawer.file === "20260101-000000-01.mp3",
    generatedDrawer ? `title=“${generatedDrawer.file}”` : ""
  );
  expect(
    "样例卡片上写着**它念的是什么**（生成时登记的正文，与本地样例同一格）",
    generatedDrawer && generatedDrawer.texts[0] === "茉莉花开",
    generatedDrawer ? `正文=${JSON.stringify(generatedDrawer.texts[0])}` : ""
  );
  expect(
    "也写着**当时用的参数**（中文名由后端照规范翻好再下发，前端不造词）",
    generatedDrawer && generatedDrawer.params[0] === "语速 1.2 · 音高 1.3 · 格式 mp3",
    generatedDrawer ? `参数=${JSON.stringify(generatedDrawer.params[0])}` : ""
  );
  expect(
    "磁盘路径不再摆进正文（位置只剩右上角那颗「在资源管理器里打开」）",
    generatedDrawer && !generatedDrawer.text.includes(outTmp),
    generatedDrawer ? `hasReveal=${generatedDrawer.hasReveal}` : ""
  );
  await page.screenshot(path.join(outDir, "08-platform-generated.png"));

  // 收尾：关抽屉、把那一格恢复成默认（临时目录留在系统临时区，由系统去清）。
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 200));
  await fetch(new URL("/api/v1/engines/minimax/output", url), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ dir: "" }),
  });

  // 只看这一台引擎（入口在它的面板里：引擎那一段不再是音色集合）
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".tree-item.engine")]
      .find((n) => n.innerText.includes("MiniMax"))
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".engine-panel .tab")]
      .find((b) => b.textContent.trim() === "音色")
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 260));
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".engine-panel .btn")]
      .find((b) => b.textContent.includes("在全部音色中查看"))
      ?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  let openedPlatform = false;
  for (let i = 0; i < 25; i += 1) {
    openedPlatform = await page.evaluate(`(() => {
      const card = document.querySelector(".card");
      if (!card) return false;
      card.click();
      return true;
    })()`);
    if (openedPlatform) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  expect("点开一台平台音色", openedPlatform);

  // ---- 平台音色的抬头：名字来自平台，id 不摆出来（它是接口字段；MiMo 这类
  //      音色的 id 就是名字本身，摆在抬头只是多一行看不懂的字）----
  // 抽屉是点开卡片之后才取回详情的，所以要等它真画出来（等不到就返回 null，
  // 让下面那条断言报出来，而不是抛异常把整个脚本带走）。
  let platformDrawer = null;
  for (let i = 0; i < 25; i += 1) {
    platformDrawer = await page.evaluate(`(() => {
      const d = document.querySelector(".vh-modal");
      if (!d || !d.querySelector(".audition")) return null;
      return {
        // 就地编辑的那几段没有可见标签，说法挂在 aria-label 上
        fields: [...d.querySelectorAll(".vinline[aria-label]")].map((el) =>
          el.getAttribute("aria-label")
        ),
        ids: [...d.querySelectorAll(".vh-id")].map((b) => b.textContent.trim()),
        sections: [...d.querySelectorAll(".vh-card-title, .section-title")].map((t) =>
          t.textContent.replace(/\\s+/g, "").trim()
        ),
        sub: d.querySelector(".vh-sub")?.textContent.trim() || "",
        readonly: d.querySelectorAll(".readonly-line input").length,
        hasSamples: [...d.querySelectorAll(".section-title")].some((t) =>
          t.textContent.includes("音频样例")
        ),
      };
    })()`);
    if (platformDrawer) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  expect(
    "平台音色没有「显示名」那一格（那一格写的其实是它的 id，与抬头重复）",
    platformDrawer && !platformDrawer.fields.includes("显示名"),
    platformDrawer ? platformDrawer.fields.join("、") : ""
  );
  expect(
    "平台音色抬头不摆 id 那一行（接口字段，不是给人读的）",
    platformDrawer &&
      platformDrawer.ids.length === 0 &&
      // 路径那一行**跟着"有没有生成结果"走**：生成过（上面那段造的那种）就有，
      // 没生成过就没有——本地音色那一格不是给平台音色摆样子的。
      platformDrawer.readonly === (platformDrawer.hasSamples ? 1 : 0) &&
      !platformDrawer.sections.includes("平台音色 id"),
    platformDrawer
      ? `抬头「${platformDrawer.sub}」；段落：${platformDrawer.sections.join(" / ")}`
      : ""
  );

  let audition = null;
  for (let i = 0; i < 25; i += 1) {
    audition = await page.evaluate(`(() => {
      const box = document.querySelector(".vh-modal .audition");
      if (!box) return null;
      const pick = box.querySelector(".aud-pick");
      const run = box.querySelector(".btn.primary");
      const title = box.closest(".section")?.querySelector(".section-title");
      const gear = title?.querySelector("button.icon-btn");
      const pickBox = pick?.getBoundingClientRect();
      const runBox = run?.getBoundingClientRect();
      return {
        value: pick ? pick.value : "",
        tag: pick ? pick.tagName : "",
        // 关着的时候菜单不在 DOM 里（"点开有几条"是下面另一段的事）。
        menuClosed: box.querySelectorAll(".aud-menu button").length,
        // 「生成」与那一格**在同一行**（写完就念，本来就是一件事）。
        sameRow: Boolean(pickBox && runBox && Math.abs(pickBox.top - runBox.top) < 6),
        // 参数不在这一格里（在齿轮打开的窗口里）。
        fields: box.querySelectorAll(".field").length,
        // 齿轮挂在「生成样例」标题那一行的右端，而且是**图标**（不是一颗写着字的按钮）。
        gearText: gear ? gear.textContent.trim() : "",
        onTitle: Boolean(gear && title && title.textContent.trim().startsWith("生成样例")),
        runDisabled: Boolean(run?.disabled),
      };
    })()`);
    if (audition && audition.value.length > 4) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  expect(
    "平台音色的详情里有「生成样例」（它在磁盘上一段音频都没有，只能现合成）",
    audition && audition.value.length > 4 && audition.menuClosed === 0,
    audition ? `框里是「${audition.value.slice(0, 12)}…」，菜单关着` : "没有 .audition"
  );
  expect(
    "正文是一格**能自己打字的输入框**（清单只是候选，不是白名单）",
    audition && audition.tag === "INPUT",
    audition ? audition.tag : ""
  );

  // **点开下拉要看得到全部句子**。这条盯的是修过的一个坑：从前用 `<input list="…">`，
  // 浏览器会**按框里现在的值过滤候选**，而框里预填着一整句——点开就只剩那一条
  // （看起来像"清单只剩一句了"）。这里数菜单里真摆出来的按钮，并且要求**其中有
  // 不是当前那句的**：只数条数的话，"清单里只有一条"与"过滤后剩一条"分不开。
  await page.evaluate(
    `(() => document.querySelector(".vh-modal .audition .aud-caret")?.click())()`
  );
  await new Promise((r) => setTimeout(r, 200));
  const menu = await page.evaluate(`(() => {
    const box = document.querySelector(".vh-modal .audition");
    const rows = [...box.querySelectorAll(".aud-menu button")].map((b) => b.textContent.trim());
    const current = box.querySelector(".aud-pick")?.value || "";
    return { count: rows.length, others: rows.filter((t) => t !== current).length };
  })()`);
  expect(
    "点开下拉看到的是**全部**句子（不是只有框里那一条）",
    menu && menu.count >= 5 && menu.others >= 4,
    menu ? `${menu.count} 句，其中 ${menu.others} 句与框里那句不同` : "没点开"
  );

  // 点一句 → 填进框里，菜单收起来（挑一句是省事，不是限制）。
  await page.evaluate(`(() => {
    const box = document.querySelector(".vh-modal .audition");
    const current = box.querySelector(".aud-pick")?.value;
    const target = [...box.querySelectorAll(".aud-menu button")].find(
      (b) => b.textContent.trim() !== current
    );
    target?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  })()`);
  await new Promise((r) => setTimeout(r, 200));
  const picked = await page.evaluate(`(() => {
    const box = document.querySelector(".vh-modal .audition");
    return {
      value: box.querySelector(".aud-pick")?.value || "",
      closed: box.querySelectorAll(".aud-menu button").length === 0,
    };
  })()`);
  expect(
    "点一句就填进框里（不用自己想该念什么），选完菜单收起",
    picked && picked.value.length > 4 && picked.closed,
    picked ? `「${picked.value.slice(0, 14)}…」菜单收起=${picked.closed}` : ""
  );
  expect(
    "「生成」与正文那一格在同一行（写完就念，是一件事）",
    audition && audition.sameRow,
    audition ? `同一行=${audition.sameRow}` : ""
  );
  expect(
    "参数不在这一格里就地展开（它在齿轮打开的窗口里）",
    audition && audition.fields === 0,
    audition ? `${audition.fields} 格` : ""
  );
  expect(
    "「参数设置」是一颗**图标**，挂在「生成样例」标题那一行的右端",
    audition && audition.onTitle && audition.gearText.length <= 2 && audition.gearText.length > 0,
    audition ? `标题行里那颗写着「${audition.gearText}」` : ""
  );

  // **自定义输入**：清单之外自己写一句也要能用（挑一句是省事，不是限制）。
  await page.evaluate(`(() => {
    const pick = document.querySelector(".vh-modal .audition .aud-pick");
    pick.value = "我自己写的一句话，用来试听。";
    pick.dispatchEvent(new Event("input", { bubbles: true }));
  })()`);
  await new Promise((r) => setTimeout(r, 150));
  const custom = await page.evaluate(`(() => {
    const pick = document.querySelector(".vh-modal .audition .aud-pick");
    return {
      value: pick.value,
      disabled: Boolean(document.querySelector(".vh-modal .audition .btn.primary")?.disabled),
    };
  })()`);
  expect(
    "自己写一句也算数（值留在框里，「生成」可用）",
    custom.value === "我自己写的一句话，用来试听。" && custom.disabled === false,
    `「${custom.value}」disabled=${custom.disabled}`
  );
  // 收尾：换回清单第一句——后面那张截图与"改一格"那一段都按起点状态走。
  await page.evaluate(
    `(() => document.querySelector(".vh-modal .audition .aud-caret")?.click())()`
  );
  await new Promise((r) => setTimeout(r, 150));
  await page.evaluate(`(() => {
    document
      .querySelector(".vh-modal .audition .aud-menu button")
      ?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  })()`);
  await new Promise((r) => setTimeout(r, 200));
  // 这一张拍的是**没调过参数**的样子（后面那个"调过几项"的角标是另一回事，
  // 截图起点自己定——不靠上一段跑剩下的状态）。
  await page.screenshot(path.join(outDir, "08-platform-audition.png"));

  // 齿轮 → 参数在自己的窗口里。**窗口里的每一格都要真能被选中**：
  // 从前「情绪」在那一格里是个空白框（一项都没选中），所以这条盯的是"下拉里
  // 有一项真的被选上"，不只是"下拉在不在"。
  await page.evaluate(
    `(() => document.querySelector(".vh-modal .section-title button.icon-btn")?.click())()`
  );
  await new Promise((r) => setTimeout(r, 250));
  const paramModal = await page.evaluate(`(() => {
    const m = document.querySelector(".modal");
    if (!m) return null;
    const labelOf = (el) =>
      (el.querySelector(".field-label")?.textContent || "")
        .replace(/默认|倍率|秒|Hz|bps/g, "").replace(/\\s+/g, "").trim();
    const rows = [...m.querySelectorAll(".field")];
    return {
      title: m.querySelector("h3")?.textContent.trim() || "",
      inline: document.querySelectorAll(".vh-modal .audition .field").length,
      count: rows.length,
      labels: rows.map(labelOf),
      selects: rows
        .filter((r) => r.querySelector("select"))
        .map((r) => {
          const s = r.querySelector("select");
          return { label: labelOf(r), index: s.selectedIndex, text: s.options[s.selectedIndex]?.text || "" };
        }),
      // **组合框（候选 + 手输）**：情绪与音色这类"候选不是白名单"的格子现在走它，
      // 于是它们**不再是原生 select**——只数 select 的话，这一格会凭空消失，
      // "非数值格 = 下拉数"这条式子就永远差一格。
      combos: rows
        .filter((r) => r.querySelector(".combo"))
        .map((r) => ({
          label: labelOf(r),
          candidates: r.querySelector(".combo-caret")?.getAttribute("title") || "",
        })),
      sliders: m.querySelectorAll(".field-range").length,
      badges: m.querySelectorAll(".field-badge").length,
      // **生成处能做的，这个窗口里一样能做**：正文那一格与语气词标签那几组也在。
      textarea: Boolean(m.querySelector("textarea.te-text")),
      marks: m.querySelectorAll(".mark-bar .mark-btn").length,
      text: m.querySelector("textarea.te-text")?.value || "",
    };
  })()`);
  expect(
    "点那颗齿轮弹出参数窗口（不再就地展开）",
    paramModal && paramModal.title === "参数设置" && paramModal.count > 0 && paramModal.inline === 0,
    paramModal
      ? `「${paramModal.title}」${paramModal.count} 格，抽屉里还剩 ${paramModal.inline} 格`
      : "没有 .modal"
  );
  expect(
    "窗口里也有正文与那几组语气词标签（在生成处能做的，这里一样能做）",
    paramModal && paramModal.textarea && paramModal.marks >= 10 && paramModal.text.length > 4,
    paramModal
      ? `正文「${paramModal.text.slice(0, 10)}…」、标签 ${paramModal.marks} 个`
      : ""
  );
  expect(
    "参数那一列里没有「正文」（它是窗口里单独的一格，不混在参数里）",
    // **精确匹配**：「正文语言」里也有「正文」两个字，用 includes 的话这条断言
    // 在"正文那一格真的混进去了"与"没混进去"两种情况下都会红（分不开两种情形）。
    paramModal && !paramModal.labels.includes("正文") && !paramModal.labels.includes("输入文本"),
    paramModal ? paramModal.labels.join("、") : ""
  );
  expect(
    "参数**只画这台引擎认得的那几格**（MiniMax 没有参考音频、也没有参考语言）",
    paramModal &&
      ["语速", "音量", "音高", "情绪", "格式"].every((n) => paramModal.labels.includes(n)) &&
      !paramModal.labels.some((n) => n.includes("参考") || n.includes("静默")),
    paramModal ? paramModal.labels.join("、") : ""
  );
  expect(
    "每一格都有控件：下拉与组合框都真的能挑选（**情绪是候选＋手输**，不再是空白框）、数值都是滑块＋默认角标",
    paramModal &&
      // 「非数值格的数量 = 下拉 + 组合框」——不是"下拉够不够几个"：后者在"情绪那格
      // 退化成一个输入框"时照样绿（剩下的下拉仍然是 3 个），两种情形分不开。
      paramModal.selects.length + paramModal.combos.length ===
        paramModal.count - paramModal.sliders &&
      paramModal.selects.every((s) => s.index >= 0 && s.text !== "") &&
      // 组合框那一格要有候选可挑（"能自己打字"不等于"有候选"——只剩手输的话，
      // 用户得把情绪名背下来，这与加候选的初衷正好相反）。
      paramModal.combos.every((c) => /候选（[1-9]/.test(c.candidates)) &&
      paramModal.sliders >= 4 &&
      paramModal.badges === paramModal.sliders,
    paramModal
      ? `下拉 ${paramModal.selects.map((s) => `${s.label}=${s.text || "空白"}`).join("、")}；组合框 ${paramModal.combos.map((c) => `${c.label}${c.candidates}`).join("、")}；滑块 ${paramModal.sliders} 个，挂默认角标 ${paramModal.badges} 个`
      : ""
  );
  // **字与条的比例**：一整列紫条抢注意力是返工过的那版，所以量两头——
  // 名字的字重（要强化）与滑块自己的盒子高（要收小），不看颜色（那是另一回事）。
  const proportions = await page.evaluate(`(() => {
    const row = [...document.querySelectorAll(".modal .field")].find((f) => f.querySelector(".field-range"));
    if (!row) return null;
    const label = row.querySelector(".field-label");
    const range = row.querySelector(".field-range");
    return {
      weight: Number(getComputedStyle(label).fontWeight),
      labelSize: parseFloat(getComputedStyle(label).fontSize),
      rangeH: range.getBoundingClientRect().height,
    };
  })()`);
  expect(
    "参数名比那根滑块显眼：字重 ≥ 600，滑块盒子矮（≤ 16px）",
    proportions && proportions.weight >= 600 && proportions.rangeH <= 16,
    proportions
      ? `字重 ${proportions.weight}、${proportions.labelSize}px；滑块盒子高 ${proportions.rangeH}px`
      : ""
  );
  await page.screenshot(path.join(outDir, "09-param-modal.png"));

  // 改一格 → 关窗 → 再打开：值要还在（说明那一格真的连着 store，不是一个画着玩的滑块）。
  const moved = await page.evaluate(`(() => {
    const row = [...document.querySelectorAll(".modal .field")]
      .find((f) => (f.querySelector(".field-label")?.textContent || "").includes("语速"));
    const slider = row?.querySelector(".field-range");
    if (!slider) return null;
    slider.value = slider.max;
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    return { after: slider.value };
  })()`);
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".modal button")].find((b) => b.textContent.trim() === "完成")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 250));
  const closedState = await page.evaluate(`(() => {
    const gear = document.querySelector(".vh-modal .section-title button.icon-btn");
    return {
      label: gear ? gear.textContent.replace(/\\s+/g, "") : "",
      fields: document.querySelectorAll(".vh-modal .audition .field").length,
      modal: Boolean(document.querySelector(".modal")),
    };
  })()`);
  expect(
    "关掉窗口＝参数不再占着这一格；调过几项挂在齿轮上（收进窗口后，这是唯一能看出这次不是全默认的地方）",
    moved && !closedState.modal && closedState.fields === 0 && closedState.label === "⚙1",
    `齿轮「${closedState.label}」· 抽屉里 ${closedState.fields} 格 · 窗口还在=${closedState.modal}`
  );

  await page.evaluate(
    `(() => document.querySelector(".vh-modal .section-title button.icon-btn")?.click())()`
  );
  await new Promise((r) => setTimeout(r, 250));
  const kept = await page.evaluate(`(() => {
    const row = [...document.querySelectorAll(".modal .field")]
      .find((f) => (f.querySelector(".field-label")?.textContent || "").includes("语速"));
    if (!row) return null;
    return {
      readout: row.querySelector(".field-readout")?.textContent.trim() || "",
      reset: Boolean(row.querySelector(".field-reset")),
      badge: Boolean(row.querySelector(".field-badge")),
    };
  })()`);
  expect(
    "重开窗口，改过的值还在（读数＝刚才拖到的位置，默认角标换成了归位键）",
    moved && kept && kept.readout === moved.after && kept.reset && !kept.badge,
    kept ? `读数 ${kept.readout}、归位键=${kept.reset}、默认角标=${kept.badge}` : ""
  );

  // 自己收尾：归位 → 参数回到全默认（后面还要拿抽屉里那些东西量亮度，
  // 别带着一个改过的滑块）。
  await page.evaluate(`(() => {
    const row = [...document.querySelectorAll(".modal .field")]
      .find((f) => (f.querySelector(".field-label")?.textContent || "").includes("语速"));
    row?.querySelector(".field-reset")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 150));
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".modal button")].find((b) => b.textContent.trim() === "完成")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 200));
  const backToDefault = await page.evaluate(`(() => {
    const gear = document.querySelector(".vh-modal .section-title button.icon-btn");
    return gear ? gear.textContent.replace(/\\s+/g, "") : "";
  })()`);
  expect("归位之后齿轮上那个数没了（回到全默认）", backToDefault === "⚙", `齿轮「${backToDefault}」`);
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 250));
  } finally {
    await fetch(new URL("/api/v1/engines/minimax/output", url), {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dir: "" }),
    }).catch(() => {});
  }
} else {
  console.log("  ..  跳过「平台音色的生成结果 / 详情 / 现合成」这一段：库里没有平台音色（自检不拉清单、不花额度）");
}

// 面板里应当有几格，**以后端那份 schema 为准**，不写死一个数字：写死的话，每加一格
// 设置这条就要回来改一次（"13" 就是这么过期的——字幕那两格加进来之后它一直红着），
// 而这条要抓的错是"**漏渲染了一格**"，不是"有人加了设置项"。
const declaredFields = Object.fromEntries(
  items(await fetch(new URL("/api/v1/engines", url)).then((r) => r.json())).map((e) => [
    e.key,
    Object.keys(e.settings_schema?.properties || {}).length,
  ])
);
// 下拉的候选**也以后端那份 schema 为准**（同一理由：写死条数的话，插件加/减一档模型
// 这条就红——"4"和"8"都是这么过期的；这条要抓的错是"下拉里的候选与插件给的对不上"）。
const declaredChoices = Object.fromEntries(
  items(await fetch(new URL("/api/v1/engines", url)).then((r) => r.json())).map((e) => [
    e.key,
    Object.fromEntries(
      Object.entries(e.settings_schema?.properties || {}).map(([key, schema]) => [
        key,
        (schema.choices || []).map((pair) => String(pair[1])),
      ])
    ),
  ])
);

// 点引擎那一行 → **右侧主区里的面板**（不是弹窗）
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item.engine")].find((n) =>
    n.innerText.includes("MiniMax")
  )?.click();
})()`);
let modal = null;
for (let i = 0; i < 20; i += 1) {
  modal = await page.evaluate(`(() => {
    const m = document.querySelector(".engine-panel");
    if (!m) return null;
    const fields = [...m.querySelectorAll(".field")];
    const byLabel = (text) =>
      fields.find((f) => (f.querySelector("label")?.innerText || "").includes(text));
    const opts = (t) => {
      const box = byLabel(t)?.querySelector("select");
      return box ? [...box.options].map((o) => o.textContent.trim()) : [];
    };
    const box = m.getBoundingClientRect();
    const body = m.querySelector(".panel-body");
    const foot = m.querySelector(".panel-foot")?.getBoundingClientRect();
    return {
      title: (m.querySelector(".engine-title")?.innerText || "").replace(/\\s+/g, " ").trim(),
      // **它得在主区里**：面板不是盖在别的东西上面的一层（弹窗才是）。
      inMain: Boolean(m.closest(".main")),
      noModal: !document.querySelector(".modal.wide"),
      bigIcon: !!m.querySelector("img.engine-icon.lg"),
      // 图标"看得见"不等于"长得对"：SVG 没有尺寸约束时会撑满整块主区，
      // 所以这里量的是**渲染出来的盒子**，不是"元素在不在"。
      iconBox: (() => {
        const b = m.querySelector("img.engine-icon.lg")?.getBoundingClientRect();
        return b ? { w: Math.round(b.width), h: Math.round(b.height) } : null;
      })(),
      fitsViewport: box.height <= window.innerHeight,
      fieldCount: fields.length,
      labels: fields.map((f) => (f.querySelector("label")?.innerText || "").replace(/\\s+/g, " ").trim()),
      keyType: byLabel("API Key")?.querySelector("input")?.type || "",
      keyTagged: !!byLabel("API Key")?.querySelector(".secret-tag"),
      regionOptions: opts("站点"),
      modelOptions: opts("合成模型"),
      tabs: [...m.querySelectorAll(".tab")].map((b) => b.textContent.trim()),
      saveDisabled: [...m.querySelectorAll(".panel-foot button")].find((b) =>
        b.textContent.includes("保存")
      )?.disabled,
      // 说明文字那一段：**不在这里**——长文（强调/小标题/条目）住在「能力与参数」
      // 那一页，量尺也跟过去了（见下面 about 那一段）。
      bodyText: m.innerText.replace(/\\s+/g, " ").trim(),
      // 底栏要**一直看得见**：量的是它渲染出来的盒子在不在视口里。
      footBox: foot ? { top: Math.round(foot.top), bottom: Math.round(foot.bottom) } : null,
      bodyScrollable: body ? body.scrollHeight > body.clientHeight + 4 : null,
      panelBottom: Math.round(box.bottom),
    };
  })()`);
  if (modal) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "点引擎那一行开出**引擎面板**（在右侧主区里，不是弹窗）",
  modal && modal.inMain && modal.noModal,
  modal ? modal.title : "没开"
);
expect(
  "面板抬头带引擎名与形态",
  modal && modal.title.includes("MiniMax") && modal.title.includes("云端"),
  modal ? modal.title : ""
);
expect("面板头显示插件图标", modal && modal.bigIcon);
expect(
  "面板头的图标尺寸受控（不是撑满主区的那一大块）",
  modal && modal.iconBox && modal.iconBox.w <= 48 && modal.iconBox.h <= 48,
  modal ? JSON.stringify(modal.iconBox) : ""
);
expect(
  "面板高度不超出视口（内容多时内部滚动）",
  modal && modal.fitsViewport,
  modal ? `fits=${modal.fitsViewport}` : ""
);
expect(
  `设置项照 schema 渲染（${declaredFields.minimax} 个字段全在）`,
  modal && modal.fieldCount === declaredFields.minimax,
  modal ? `字段 ${modal.fieldCount} 个：${modal.labels.slice(0, 4).join(" / ")}……` : ""
);
expect(
  "机密字段渲染成密码框且标了「机密」",
  modal && modal.keyType === "password" && modal.keyTagged,
  modal ? `type=${modal.keyType} tag=${modal.keyTagged}` : ""
);
expect(
  "站点是下拉且两个候选都在",
  modal && modal.regionOptions.length === 2 && modal.regionOptions.join("").includes("国内站"),
  modal ? modal.regionOptions.join(" / ") : ""
);
expect(
  "模型下拉的候选与插件给的那份**逐个对得上**（不写死条数：插件加/减一档不该让它变红）",
  modal &&
    JSON.stringify(modal.modelOptions) ===
      JSON.stringify(declaredChoices.minimax.model || []),
  modal
    ? `下拉 ${modal.modelOptions.length} 个 / 接口 ${(declaredChoices.minimax.model || []).length} 个`
    : ""
);
expect(
  "页签由后端 page.sections 给（设置 / 音色 / 音色克隆 / 能力与参数）",
  modal &&
    modal.tabs.length === 4 &&
    modal.tabs.includes("音色") &&
    modal.tabs.includes("音色克隆"),
  modal ? modal.tabs.join(" / ") : ""
);
// 说明文字那一段（插件写的 `**强调**` 与小标题）**现在住在「能力与参数」页签**——
// 从前它挤在设置页签的下面，所以那两条断言长在这里；搬了家就把量尺也搬过去
// （见下面 about 那一段），留在这里量的是一个没有长文的设置页签：只会量到 0。
// 底栏固定：**量渲染出来的盒子**，不看 CSS 写了什么。内容长到要滚的时候，
// 底栏跟着滚出去的话，用户得先滚到底才按得到保存——而那时又看不见自己改的是哪一格。
expect(
  "长内容确实要滚（否则下面那条断言验不出东西）",
  modal && modal.bodyScrollable === true,
  modal ? `scrollable=${modal.bodyScrollable}` : ""
);
expect(
  "底栏常驻在面板下方（没滚的时候也在视口里）",
  modal && modal.footBox && modal.footBox.bottom <= modal.panelBottom + 1,
  modal && modal.footBox
    ? `底栏 ${modal.footBox.top}–${modal.footBox.bottom}，面板底 ${modal.panelBottom}`
    : "量不到底栏"
);
const footAfterScroll = await page.evaluate(`(() => {
  const body = document.querySelector(".engine-panel .panel-body");
  if (body) body.scrollTop = body.scrollHeight;
  const box = document.querySelector(".engine-panel .panel-foot")?.getBoundingClientRect();
  return box ? { bottom: Math.round(box.bottom) } : null;
})()`);
expect(
  "正文滚到底之后底栏仍在原处（固定住了，不是跟着滚）",
  footAfterScroll && modal && footAfterScroll.bottom <= modal.panelBottom + 1,
  footAfterScroll ? `滚到底后底栏 bottom=${footAfterScroll.bottom}，面板底 ${modal.panelBottom}` : ""
);
// 把滚动条放回顶部，好让截图拍的是**面板的开头**（字段与说明的第一段）。
await page.evaluate(`(() => {
  const body = document.querySelector(".engine-panel .panel-body");
  if (body) body.scrollTop = 0;
})()`);

await page.screenshot(path.join(outDir, "05-engine-settings.png"));

// ---- 「能力与参数」分区：能力清单 + 参数映射 + 引擎要求 ----
// 三样同住一个页签（**名字由后端给**）。映射表的说明住在每一行里，
// 表下只留逐条的重点说明；插件默认配置不再单列（设置表单本身就是默认值）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")]
    .find((b) => b.textContent.includes("能力"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 260));
const about = await page.evaluate(`(() => {
  const m = document.querySelector(".engine-panel");
  if (!m) return null;
  const headers = [...m.querySelectorAll(".param-map thead th")].map((th) => th.textContent.trim());
  return {
    capLabels: [...m.querySelectorAll(".cap-label")].map((c) => c.textContent.trim()),
    headers,
    mapRows: [...(m.querySelectorAll(".param-map tbody tr") || [])].map((tr) =>
      [...tr.querySelectorAll("td")].map((td) =>
        td.textContent
          .replace("必填", "")
          .replace(/\\s+/g, " ")
          .trim()
      )
    ),
    bodyText: m.innerText.replace(/\\s+/g, " ").trim(),
    cautions: [...m.querySelectorAll(".map-cautions li")].map((li) =>
      li.textContent.replace(/\\s+/g, " ").trim()
    ),
    cautionIcons: m.querySelectorAll(".map-cautions li svg").length,
    reqTags: [...m.querySelectorAll(".req-tag-item")].map((item) =>
      item.textContent.replace(/\\s+/g, " ").trim()
    ),
  };
})()`);
expect(
  "能力清单由插件给出（含音色克隆与文本上限）",
  about && about.capLabels.includes("音色克隆") && about.capLabels.includes("文本上限"),
  about ? about.capLabels.join(" / ") : ""
);
expect(
  "参数映射表的表头是「项目规范 / 映射位置 / 换算 / 说明」",
  about && about.headers.join("|") === "项目规范|映射位置|换算|说明",
  about ? about.headers.join(" / ") : ""
);
expect(
  "映射表按项目规范逐项列全（voice_id 落到 voice_setting.voice_id）",
  about &&
    about.mapRows.length >= 18 &&
    about.mapRows.some(
      (r) =>
        r[0].includes("voice_id") &&
        r[1] === "voice_setting.voice_id" &&
        r[2] === "原样"
    ) &&
    about.mapRows.some((r) => r[0].includes("speed") && r[2] === "数值") &&
    // 音高是这一家里**唯一要换算**的一项：倍率 → 半音（不是原样透传）。
    about.mapRows.some((r) => r[0].includes("pitch") && r[2] === "倍率 → 半音"),
  about ? `${about.mapRows.length} 行` : ""
);
expect(
  "每一行都带参数说明",
  about && about.mapRows.every((r) => r[3] && r[3] !== "—"),
  about ? `${about.mapRows.filter((r) => !r[3]).length} 行没有说明` : ""
);
expect(
  "重点说明按点换行并带标记",
  about && about.cautions.length >= 2 && about.cautionIcons === about.cautions.length,
  about ? `${about.cautions.length} 条 / ${about.cautionIcons} 个图标` : ""
);
expect(
  "引擎要求以关键词标签表达（参考音频 / 逐字稿）",
  about &&
    about.reqTags.some((t) => t.includes("参考音频")) &&
    about.reqTags.some((t) => t.includes("逐字稿")),
  about ? about.reqTags.join(" / ") : ""
);
expect(
  "插件默认配置不再单列（设置表单已经体现了默认值）",
  about && !about.bodyText.includes("插件里写着的默认配置"),
  about ? "" : ""
);


// 音色克隆页签
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")]
    .find((b) => b.textContent.includes("克隆"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 260));
const cloneTab = await page.evaluate(`(() => {
  const m = document.querySelector(".engine-panel");
  if (!m) return null;
  const file = m.querySelector('input[type=file]');
  const submit = [...m.querySelectorAll(".panel-foot button")].find((b) =>
    b.textContent.includes("克隆")
  );
  return {
    accept: file?.getAttribute("accept") || "",
    labels: [...m.querySelectorAll(".field label")].map((l) =>
      l.innerText.replace(/\\s+/g, " ").trim()
    ),
    submitDisabled: submit?.disabled,
    // 「这一下会花钱」必须写在界面上——但**那句话归形态说明**（云端那条自己写着
    // 「按量计费」），不在克隆页签里再写第二遍。同一件事说两处，一处迟早会说错。
    saysKindBilling: m.innerText.includes("按量计费"),
  };
})()`);
expect("克隆页签：有文件选择且只收音频", cloneTab && cloneTab.accept === "audio/*", cloneTab?.accept);
expect(
  "克隆页签：必填项为空时按钮禁用",
  cloneTab && cloneTab.submitDisabled === true,
  cloneTab ? `disabled=${cloneTab.submitDisabled}` : ""
);
expect(
  "克隆页签：计费与否由「形态说明」给出（云端 = 按量计费）",
  cloneTab && cloneTab.saysKindBilling,
  cloneTab ? cloneTab.labels.join(" / ") : ""
);

await page.screenshot(path.join(outDir, "06-engine-clone.png"));

// ---- 清空密钥这条路：空串=不改，所以「清除」必须真的有去处 ----
// 前置的那把假钥匙是脚本在 openPage 之前自己塞的（见文件开头）；走完这里会被清掉。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")].find((b) => b.textContent.includes("设置"))?.click();
})()`);
await new Promise((r) => setTimeout(r, 200));
const cleared = await page.evaluate(`(() => {
  const m = document.querySelector(".engine-panel");
  const fields = [...m.querySelectorAll(".field")];
  const key = fields.find((f) => (f.querySelector("label")?.innerText || "").includes("API Key"));
  const btn = key?.querySelector(".secret-row button");
  const had = Boolean(key?.querySelector(".secret-row"));
  const label = btn?.textContent?.trim() || "";
  btn?.click();
  return { hadRow: had, clicked: label };
})()`);
// 点完要等一次渲染——Vue 的 DOM 更新在下一个 tick，读同一拍读到的是旧界面
await new Promise((r) => setTimeout(r, 250));
const afterClearClick = await page.evaluate(`(() => {
  const m = document.querySelector(".engine-panel");
  const key = [...m.querySelectorAll(".field")].find((f) =>
    (f.querySelector("label")?.innerText || "").includes("API Key")
  );
  return {
    after: key?.querySelector("input")?.placeholder || "",
    canUndo: key?.querySelector(".secret-row button")?.textContent?.trim() || "",
  };
})()`);
Object.assign(cleared, afterClearClick);
expect(
  "已存密钥旁边有「清除」（空串=不改，所以得有个明说的去处）",
  cleared && cleared.hadRow && cleared.clicked === "清除",
  JSON.stringify(cleared)
);
expect(
  "按了「清除」当场改口为「保存后清空」，并给了撤销",
  cleared && cleared.after === "保存后清空" && cleared.canUndo === "撤销",
  JSON.stringify(cleared)
);
if (hadKey) {
  // **撤销回去，绝不按「保存」**——这一下按下去就是真的把用户那把钥匙抹了。
  // 「清除 → 改口 → 撤销」这套界面动作本身照样验：它一次请求都没发。
  await page.evaluate(`(() => {
    const key = [...document.querySelectorAll(".engine-panel .field")].find((f) =>
      (f.querySelector("label")?.innerText || "").includes("API Key")
    );
    key?.querySelector(".secret-row button")?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 250));
  const undone = await page.evaluate(`(() => {
    const key = [...document.querySelectorAll(".engine-panel .field")].find((f) =>
      (f.querySelector("label")?.innerText || "").includes("API Key")
    );
    return {
      placeholder: key?.querySelector("input")?.placeholder || "",
      button: key?.querySelector(".secret-row button")?.textContent?.trim() || "",
    };
  })()`);
  expect(
    "按「撤销」改回「留空即不改」，并给回「清除」那颗按钮",
    undone.placeholder === "留空则不修改" && undone.button === "清除",
    JSON.stringify(undone)
  );
  expect(
    "你那把钥匙毫发无损（读回来仍是掩码，secrets_set 里还在）",
    Boolean(beforeKey?.secrets_set?.includes("api_key")),
    `secrets_set=${JSON.stringify(beforeKey?.secrets_set)}`
  );
  await page.screenshot(path.join(outDir, "07-engine-undo-clear.png"));
} else {
  // 这把钥匙是上面自己塞的，所以"清掉它"可以走完 —— 起点与终点都是"没钥匙"。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .panel-foot button")]
    .find((b) => b.textContent.includes("保存"))
    ?.click();
})()`);
let afterClear = null;
for (let i = 0; i < 25; i += 1) {
  afterClear = await page.evaluate(`(() => {
    const m = document.querySelector(".engine-panel");
    const el = [...document.querySelectorAll(".tree-item.engine")].find((n) =>
      n.innerText.includes("MiniMax")
    );
    return {
      badge: m ? (m.innerText.includes("未就绪") ? "未就绪" : "已就绪") : "面板没了",
      warn: !!el?.querySelector(".engine-warn"),
      note: el?.getAttribute("title") || "",
      keyPlaceholder:
        [...(m?.querySelectorAll(".field") || [])]
          .find((f) => (f.querySelector("label")?.innerText || "").includes("API Key"))
          ?.querySelector("input")?.placeholder || "",
    };
  })()`);
  // 退出条件要盖住**接下来要断言的那几样**：左栏那条的 `warn` 与面板里的
  // `badge` / `keyPlaceholder` 不是同一份数据，落定有先后。只等 `warn` 就 break
  // 的话，服务刚冷启动那一次会读到还没刷新的面板（偶发红，踩过一次）。
  if (
    afterClear &&
    afterClear.warn &&
    afterClear.badge === "未就绪" &&
    afterClear.keyPlaceholder === "未填写"
  ) {
    break;
  }
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "清空密钥后面板当场变「未就绪」",
  afterClear && afterClear.badge === "未就绪",
  afterClear ? afterClear.badge : ""
);
expect(
  "清空密钥后左栏那一条立刻出现「!」（不用等全量刷新）",
  afterClear && afterClear.warn,
  afterClear ? `warn=${afterClear.warn} title=${afterClear.note}` : ""
);
expect(
  "未就绪的理由写在 title 里",
  afterClear && afterClear.note.includes("API Key"),
  afterClear ? afterClear.note : ""
);
expect(
  "清空之后密码框提示改回「未填写」（界面上那句话就是它，别再写成旧文案）",
  afterClear && afterClear.keyPlaceholder === "未填写",
  afterClear ? afterClear.keyPlaceholder : ""
);

}

await page.screenshot(path.join(outDir, "07-engine-cleared.png"));

await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .btn")]
    .find((b) => b.textContent.includes("音色库"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 300));


// ---- 同一个面板组件渲染本机引擎：没有密钥、没有克隆、不出现"按量计费" ----
// 面板不认识任何一台引擎：格数照 schema、页签照 page.sections、能力照 capability_notes。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item.engine")].find((n) =>
    n.innerText.includes("GPT-SoVITS")
  )?.click();
})()`);
let localModal = null;
for (let i = 0; i < 25; i += 1) {
  localModal = await page.evaluate(`(() => {
    const m = document.querySelector(".engine-panel");
    if (!m) return null;
    const fields = [...m.querySelectorAll(".field")];
    return {
      title: (m.querySelector(".engine-title")?.innerText || "").replace(/\\s+/g, " ").trim(),
      fieldCount: fields.length,
      labels: fields.map((f) =>
        (f.querySelector("label")?.innerText || "").replace(/\\s+/g, " ").trim()
      ),
      passwordInputs: m.querySelectorAll('input[type=password]').length,
      secretTags: m.querySelectorAll(".secret-tag").length,
      tabs: [...m.querySelectorAll(".tab")].map((b) => b.textContent.trim()),
      bodyText: m.innerText.replace(/\\s+/g, " ").trim(),
    };
  })()`);
  if (localModal && localModal.fieldCount > 0) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "本机引擎的配置也照同一份 schema 渲染（格数与后端声明一致）",
  localModal && localModal.fieldCount === declaredFields.gpt_sovits,
  localModal
    ? `字段 ${localModal.fieldCount} 个 / 后端声明 ${declaredFields.gpt_sovits} 个：${localModal.labels.slice(0, 3).join(" / ")}……`
    : "没开"
);
expect(
  "没有凭据的引擎不出现密码框，也不出现「机密」角标",
  localModal && localModal.passwordInputs === 0 && localModal.secretTags === 0,
  localModal ? `password=${localModal.passwordInputs} secret-tag=${localModal.secretTags}` : ""
);
expect(
  "「按量计费」只属于云端那条形态说明——本机引擎不出现它",
  localModal && !localModal.bodyText.includes("按量计费") && localModal.title.includes("本地"),
  localModal ? localModal.title : ""
);
expect(
  "页签由后端给：没有克隆那一下 → 设置 / 音色 / 能力与参数（不摆点了才报错的页签）",
  localModal &&
    localModal.tabs.length === 3 &&
    localModal.tabs.includes("音色") &&
    !localModal.tabs.includes("音色克隆"),
  localModal ? localModal.tabs.join(" / ") : ""
);

// 能力与映射在它们自己的页签里（从前挤在设置页签下面）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")]
    .find((b) => b.textContent.includes("能力"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 250));
const localAbout = await page.evaluate(`(() => {
  const m = document.querySelector(".engine-panel");
  if (!m) return null;
  return {
    capLabels: [...m.querySelectorAll(".cap-label")].map((c) => c.textContent.trim()),
    mapRows: [...(m.querySelectorAll(".param-map tbody tr") || [])].map((tr) =>
      [...tr.querySelectorAll("td")].map((td) =>
        td.textContent.replace("必填", "").replace(/\\s+/g, " ").trim()
      )
    ),
  };
})()`);
expect(
  "能力清单只说它真有的（参考音频 / 试听合成），不提音色克隆",
  localAbout &&
    localAbout.capLabels.includes("参考音频") &&
    localAbout.capLabels.includes("试听合成") &&
    !localAbout.capLabels.includes("音色克隆"),
  localAbout ? localAbout.capLabels.join(" / ") : ""
);
expect(
  "映射表按项目规范列全（reference_audio → ref_audio_path）",
  localAbout &&
    localAbout.mapRows.length >= 18 &&
    localAbout.mapRows.some(
      (r) => r[0].includes("reference_audio") && r[1] === "ref_audio_path"
    ) &&
    localAbout.mapRows.some(
      (r) => r[0].includes("reference_text") && r[1] === "prompt_text"
    ),
  localAbout ? `${localAbout.mapRows.length} 行` : ""
);

// 等上一条提示自己消失（3.2 秒后自动关）。截图是给人看的，让上一段的
// 「已保存——还没填 API Key……」浮在这张"本机引擎设置"上，会读成这张的提示。
// 轮询而不是死等一个固定秒数：提示的存在时长是 UI 的实现细节，不该写进自检。
for (let i = 0; i < 30; i += 1) {
  const hasToast = await page.evaluate(`Boolean(document.querySelector(".toast"))`);
  if (!hasToast) break;
  await new Promise((r) => setTimeout(r, 250));
}
await page.screenshot(path.join(outDir, "08-engine-local-settings.png"));

// 回音色库（面板没有"关闭"：它不是一个盖在别处的东西；回列表走**左栏**那行，
// 面板右上那颗「← 音色库」已撤）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".sider-body .tree-item")]
    .find((el) => el.innerText.includes("全部音色"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 300));


// ---- 面板里的「在全部音色中查看」：回列表并**只看这一台** ----
// 引擎那一段不再是"点一下筛一遍音色"的音色集合，所以过滤入口搬进了面板。
// 看哪一台**由标题说**（标题 = 那一行的名字，与本地分类同一套）——它有且只有这一处：
// 从前标题写着「只看「MiniMax」」、旁边还并摆一颗「只看：… ✕」，同一个状态说了两遍，
// 两处都已经删掉。
// 入口住在面板里，所以先把它打开（上一段收尾时已经把面板关掉了）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item.engine")]
    .find((n) => n.innerText.includes("MiniMax"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 400));
// 那颗按钮住在「音色」分区里，而页签是**跟着上次停在哪**的（上一段停在「能力」）——
// 不点回「音色」，按钮根本不在 DOM 里，后面读到的会是一个空标记。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .tab")]
    .find((b) => b.textContent.includes("音色"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 260));
// 「自有音色」这条通道的界面：**音色在统一音色库里**（不是在面板里另列一份），
// 面板只给计数与入口。这是 minimax 与全部音色**同一套 UI** 的前半句；
// 后半句（平台音色的卡片/详情/生成样例与本地音色同款）在上一段里验。
const ownChannel = await page.evaluate(`(() => {
  const panel = document.querySelector(".engine-panel");
  const card = panel?.querySelector(".channel-card");
  // 计数那一格是那句话说**最后**一个加粗：前面还有一个加粗是"统一音色库"
  // 这个词本身（同一个句子里，读第一个会 read 成那四个字）。
  const hint = [...(panel?.querySelectorAll(".section .hint") || [])].find((h) =>
    h.innerText.includes("统一音色库")
  );
  const marks = hint ? [...hint.querySelectorAll("b")] : [];
  return {
    text: panel ? panel.innerText.replace(/\\s+/g, " ").trim() : "",
    badge: card?.querySelector(".badge")?.textContent.trim() || "",
    promote: card ? card.querySelectorAll("input[type=checkbox]").length : -1,
    ownCount: marks.length ? marks[marks.length - 1].textContent.trim() : "",
  };
})()`);
const ownChannelApi = items(await (await fetch(new URL("/api/v1/engines", url))).json()).find(
  (e) => e.key === "minimax"
).channel;
const mmInLibrary = items(await (await fetch(new URL("/api/v1/voices?source=minimax", url))).json());
expect(
  "「自有音色」这条通道的说明由后端给：音色在统一音色库里，生成物就是那条音色自己的样例",
  ownChannel &&
    ownChannel.badge === ownChannelApi.mode_label &&
    ownChannel.text.includes("统一音色库") &&
    ownChannel.text.includes("样例"),
  ownChannel ? `徽章「${ownChannel.badge}」` : ""
);
expect(
  "面板里的音色计数 = 这一台在统一音色库里的条数（不写死、也不在面板里另数一份）",
  ownChannel && ownChannel.ownCount === String(mmInLibrary.length),
  ownChannel ? `面板 ${ownChannel.ownCount} / 接口 ${mmInLibrary.length}` : ""
);
await page.screenshot(path.join(outDir, "04b-engine-panel-channel.png"));
await page.evaluate(`(() => {
  [...document.querySelectorAll(".engine-panel .btn")]
    .find((b) => b.textContent.includes("在全部音色中查看"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 400));
// 同一台引擎在库里的音色数：上面那次已经取过了，直接复用（少一次请求，
// 也免得"两次取值不一样"时看不出是哪一次错了）。
const engineVoices = mmInLibrary;
let platformView = null;
for (let i = 0; i < 25; i += 1) {
  platformView = await page.evaluate(`(() => {
    const empty = document.querySelector(".grid-wrap .empty");
    const head = document.querySelector(".lib-head h1");
    return {
      title: head ? head.innerText.replace(/\\s+/g, " ").trim() : "",
      cards: document.querySelectorAll(".card").length,
      empty: empty ? empty.innerText.replace(/\\s+/g, " ").trim() : "",
    };
  })()`);
  // 「加载中……」也长在 .empty 上：等它过去，再看卡片数对不对得上
  if (
    platformView &&
    platformView.title.includes("MiniMax") &&
    platformView.cards === engineVoices.length &&
    !platformView.empty.includes("加载中")
  ) {
    break;
  }
  await new Promise((r) => setTimeout(r, 200));
}
expect(
  "「在全部音色中查看」= 回列表并只看这一台（标题就是这台引擎的名字）",
  platformView && platformView.title.startsWith("MiniMax") && !platformView.title.includes("只看"),
  platformView ? platformView.title : ""
);
expect(
  "卡片数 = 这一台在库里的音色数（不写死数字，按接口算）",
  platformView && platformView.cards === engineVoices.length,
  platformView ? `卡片 ${platformView.cards} / 接口 ${engineVoices.length}` : ""
);


// 回到「全部音色」——后面还要看卡片与深色主题
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item")].find((n) =>
    n.innerText.includes("全部音色")
  )?.click();
})()`);
let restored = false;
for (let i = 0; i < 20; i += 1) {
  restored = await page.evaluate(`document.querySelectorAll(".card").length > 0`);
  if (restored) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("点回「全部音色」列表恢复", restored);

// ---- 左栏的「生成」：按规范采集参数，一个音色扇出给多台引擎 ----
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item")].find((n) => n.innerText.includes("生成"))?.click();
})()`);
let synthUp = false;
for (let i = 0; i < 25; i += 1) {
  synthUp = await page.evaluate(`Boolean(document.querySelector(".synth"))`);
  if (synthUp) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("左栏那条「生成」换到了生成页", synthUp);

// ---- 「通常不用动」的那几组默认收起来 ----
// 收起来是**规范说的**（`Group.folded`），界面只是照办。所以这里两样都查：开关在不在，
// 以及收着的那些格子**真的没渲染**（`v-if` 而不是 `v-show`——留着十几个隐藏的输入框，
// 键盘 Tab 会一个个跳进去）。
const synthFolds = await page.evaluate(`(() => {
  const toggles = [...document.querySelectorAll(".synth-side .fold-toggle")];
  const labels = [...document.querySelectorAll(".field-label")].map((el) => el.textContent.trim());
  return {
    names: toggles.map((b) => b.textContent.trim().replace(/^[▸▾]\\s*/, "")),
    collapsed: toggles.map((b) => b.getAttribute("aria-expanded") === "false"),
    // 这三组收着时，它们的格子一个都不该在页面上
    leaked: ["前静默", "后静默", "正文语言", "参考语言", "格式", "采样率", "码率", "声道"]
      .filter((name) => labels.some((l) => l.startsWith(name))),
  };
})()`);
expect(
  "语言 / 静默 / 输出三组默认收起来（收着的格子不渲染）",
  synthFolds &&
    synthFolds.names.join("、") === "静默、语言、输出" &&
    synthFolds.collapsed.every(Boolean) &&
    synthFolds.leaked.length === 0,
  synthFolds
    ? `开关：${synthFolds.names.join("、")}（收起 ${synthFolds.collapsed.filter(Boolean).length}/3）；漏出来的格子：${synthFolds.leaked.join("、") || "无"}`
    : ""
);

// 点开它们——后面那几条断言（每一格都在、滑块的起点）要在**全展开**的样子上跑，
// 收着的那几格不在 DOM 里，不展开就断言，红的是断言而不是页面。
await page.evaluate(`(() => {
  for (const b of document.querySelectorAll(".synth-side .fold-toggle")) b.click();
})()`);
await new Promise((r) => setTimeout(r, 250));

const synthForm = await page.evaluate(`(() => {
  const labels = [...document.querySelectorAll(".field-label")].map((el) =>
    el.textContent.trim().replace(/\\s+/g, " ")
  );
  const main = document.querySelector(".synth-col-main");
  return {
    title: document.querySelector(".synth-head h1")?.textContent.trim() || "",
    headings: [...document.querySelectorAll(".synth h1")].map((el) => el.textContent.trim()),
    textarea: Boolean(document.querySelector(".te-text")),
    voiceSelect: Boolean(document.querySelector(".synth-side select.filter-select")),
    labels,
    engines: document.querySelectorAll(".engine-pick input[type=checkbox]").length,
    enginesInMain: main ? main.querySelectorAll(".engine-pick input[type=checkbox]").length : -1,
  };
})()`);
expect(
  "规范里的每一格都在页面上（**加一项参数不用改前端**）",
  synthForm &&
    ["参考音频", "情绪", "语速", "音量", "音高", "前静默", "后静默", "正文语言", "参考语言", "格式", "采样率", "码率", "声道", "语音引导", "CFG", "种子"].every(
      (name) => synthForm.labels.some((l) => l.startsWith(name))
    ),
  synthForm ? `${synthForm.labels.length} 格：${synthForm.labels.join("、")}` : ""
);
expect("正文与音色是单独摆的两块（不是挤在那张格里）", synthForm && synthForm.textarea && synthForm.voiceSelect);
expect(
  "每台已装引擎一个勾选框",
  synthForm && synthForm.engines >= 2,
  synthForm ? `${synthForm.engines} 台` : ""
);

// 标题就是左边那一格的名字——这一页不再另起一个「生成」压在上面。
expect(
  "标题是「输入文本」（不再有「生成」那一行）",
  synthForm && synthForm.title === "输入文本" && !synthForm.headings.includes("生成"),
  synthForm ? synthForm.headings.join(" / ") : ""
);
// 引擎选择**在左栏**（正文那一列的下方），不是挤在右边那堆参数里。
expect(
  "引擎选择在左栏（与正文同一列）",
  synthForm && synthForm.enginesInMain >= 2 && synthForm.enginesInMain === synthForm.engines,
  synthForm ? `${synthForm.enginesInMain}/${synthForm.engines} 个在左栏` : ""
);

// 「两个引擎的图标占满整个屏幕」出过——SVG 自己没有宽高，落在没写过尺寸的位置就按
// 容器撑开。所以这里**量渲染出来的盒子**，而不是查那个元素在不在（§自检脚本的纪律）。
const engineIcons = await page.evaluate(`(() => {
  return [...document.querySelectorAll(".engine-pick img.engine-icon")].map((img) => {
    const box = img.getBoundingClientRect();
    return { w: Math.round(box.width), h: Math.round(box.height) };
  });
})()`);
expect(
  "引擎勾选行里的图标就是图标大小（没有撑满屏幕）",
  engineIcons.length >= 2 && engineIcons.every((b) => b.w >= 8 && b.w <= 24 && b.h <= 24),
  engineIcons.map((b) => `${b.w}×${b.h}`).join("、") || "行里没有图标"
);

// 数值参数是滑块：**不必先手打一个数字**就能微调，而且起点写的是**这一项自己的
// 默认值**（语速 1.0 = 原速、静默 0 = 不留空、采样率 24000）。
//
// 有一条走过又退回的路记在这里：为了让几个滑块"排成一条线"，起点曾被换成**区间
// 中点**（1.25 / 5 / 28000……）。那是错的——滑块上写着 1.25，人会以为"这台引擎的
// 默认语速是 1.25 倍"。位置参差是这个事实的诚实呈现，不是缺陷。
const sliders = await page.evaluate(`(() => {
  return [...document.querySelectorAll(".field-range-row")].map((row) => {
    const el = row.querySelector("input[type=range]");
    return {
      value: el?.value || "",
      readout: row.querySelector(".field-readout")?.textContent.trim() || "",
      badge: row.querySelector(".field-badge")?.textContent.trim() || "",
      label: (row.parentElement?.querySelector(".field-label")?.textContent || "")
        .replace(/\\s+/g, " ").trim(),
    };
  });
})()`);
expect(
  "每个数值参数都是滑块，读数就是它自己的默认值（起点落在刻度上，不差半档）",
  sliders.length >= 5 && sliders.every((s) => s.readout !== "" && s.readout === s.value),
  sliders.map((s) => `${s.readout}`).join("、") || "没有滑块"
);
// 再钉住几个代表值：**"默认值被换掉"要能被抓到**——换成一堆中点时读数仍然自洽，
// 只有对着值本身看才发现变了（这条就是那么加上来的）。
const SLIDER_STARTS = [
  ["语速", 1],
  ["音量", 1],
  ["音高", 1],
  ["前静默", 0],
  ["后静默", 0],
  ["采样率", 24000],
  ["码率", 128000],
];
const started = SLIDER_STARTS.map(([label, want]) => {
  const hit = sliders.find((s) => s.label.startsWith(label));
  return { label, want, got: hit ? Number(hit.value) : NaN };
});
expect(
  "滑块起点就是这个参数的默认值（1.0 = 原速、0 = 不留空、采样率 24000）",
  sliders.length >= 5 && started.every((s) => s.got === s.want),
  started.map((s) => `${s.label}=${Number.isNaN(s.got) ? "?" : s.got}`).join("、")
);
expect(
  "没动过的数值格标着「默认」（那一格此时不参与请求）",
  sliders.length >= 5 && sliders.every((s) => s.badge === "默认"),
  sliders.map((s) => s.badge || "—").join("、")
);

// ---- 种子那一格：**不是滑块**（填一个数，或者要随机） ----
//
// 它单独一种控件是刻意的，不是画法偏好：0 到 21 亿的行程上滑块停不到想要的那个数，
// 而这一格需要的动作只有三个（填 / 换 / 随机）。所以这一条量的是"它长得不像滑块"，
// 以及那颗「随机」真能把它设成 **-1**——-1 是这个项目里"随机"那一个约定，
// 而下面那一层不收负数，由插件在发出去之前抽一个真的种子（见 request_spec 那一格）。
const seedView = `(() => {
  const row = [...document.querySelectorAll(".field")].find((el) =>
    (el.querySelector(".field-label")?.textContent || "").trim().startsWith("种子")
  );
  if (!row) return null;
  return {
    sliders: row.querySelectorAll("input[type=range]").length,
    box: Boolean(row.querySelector(".field-seed")),
    input: row.querySelector("input[type=number]")?.value ?? null,
    hasRandom: [...row.querySelectorAll("button")].some((b) => b.textContent.trim() === "随机"),
    badge: row.querySelector(".field-badge")?.textContent.trim() || "",
  };
})()`;
const seedBefore = await page.evaluate(seedView);
expect(
  "种子是输入框 + 一颗「随机」，不是滑块",
  seedBefore && seedBefore.box && seedBefore.sliders === 0 && seedBefore.hasRandom && seedBefore.input === "",
  seedBefore ? `滑块=${seedBefore.sliders} 框=${JSON.stringify(seedBefore.input)} 有随机=${seedBefore.hasRandom}` : "没有种子那一格"
);
expect(
  "没填过的种子标着「默认」（= 这一格不参与请求，由那台引擎配置里那一格决定）",
  seedBefore && seedBefore.badge === "默认",
  seedBefore ? seedBefore.badge || "（没有角标）" : ""
);

await page.evaluate(`(() => {
  const row = [...document.querySelectorAll(".field")].find((el) =>
    (el.querySelector(".field-label")?.textContent || "").trim().startsWith("种子")
  );
  [...row.querySelectorAll("button")].find((b) => b.textContent.trim() === "随机")?.click();
})()`);
await new Promise((r) => setTimeout(r, 200));
const seedAfter = await page.evaluate(seedView);
expect(
  "点「随机」→ 填进 -1 并标着「随机」（约定确实落在这一格上）",
  seedAfter && seedAfter.input === "-1" && seedAfter.badge === "随机",
  seedAfter ? `值=${JSON.stringify(seedAfter.input)} 角标=${seedAfter.badge || "—"}` : ""
);

// 归位：后面那些断言不该带着一个改过的参数跑。
await page.evaluate(`(() => {
  const row = [...document.querySelectorAll(".field")].find((el) =>
    (el.querySelector(".field-label")?.textContent || "").trim().startsWith("种子")
  );
  row?.querySelector(".field-reset")?.click();
})()`);
await new Promise((r) => setTimeout(r, 200));
const seedReset = await page.evaluate(seedView);
expect(
  "归位按钮把那格清回「默认」（清空 ≠ 填 0）",
  seedReset && seedReset.input === "" && seedReset.badge === "默认",
  seedReset ? `值=${JSON.stringify(seedReset.input)} 角标=${seedReset.badge || "—"}` : ""
);

// ---- 生成页的识别入口：语音输入那颗 + 样例行「更多」里的识别 ----
//
// 识别从设置页搬进「引擎」页之后，生成页只剩"用"的入口，不再单占一块：
// 麦克风按钮跟在标题后面（就绪时 title 说"本机语音识别"），样例的逐字稿识别收在
// 行的「更多」菜单里——**没配识别引擎时那颗根本不出现**（与"不能试听的音色不出现
// ▶"同一条纪律）。指路的话术在麦克风的 disabled title 里，写的是「引擎 → 识别」。
const synthRecognize = await page.evaluate(`(() => {
  const mic = [...document.querySelectorAll(".mic-btn")].find(
    (b) => (b.getAttribute("aria-label") || "") === "语音输入"
  );
  return {
    mic: Boolean(mic),
    micTitle: mic?.getAttribute("title") || "",
    // 「参考音频」不再单占一段：挑法在音色卡详情里（样例行那颗「使用」），
    // 页面只留卡片列头上那张冻结卡，录音入口收到同一列的列头。
    refBlock: [...document.querySelectorAll(".synth-col-main .synth-block .block-title")].some(
      (el) => el.textContent.trim() === "参考音频"
    ),
    railMic: Boolean(document.querySelector(".rail-acts .mic-btn")),
    railActs: document.querySelectorAll(".rail-acts .rail-act").length,
    railTitle: document.querySelector(".rail-title")?.textContent.trim() || "",
  };
})()`);
expect(
  "生成页有语音输入按钮；就绪时 title 说的是本机语音识别",
  synthRecognize.mic && /本机语音识别/.test(synthRecognize.micTitle),
  synthRecognize.micTitle || "没有语音输入按钮"
);
expect(
  "生成页不单占一块「参考音频」：改到卡片列头上的功能条（录音 / 播放 / 设计音色）",
  !synthRecognize.refBlock &&
    synthRecognize.railTitle === "参考音频" &&
    synthRecognize.railMic &&
    synthRecognize.railActs === 3,
  `段=${synthRecognize.refBlock} / 标题=${synthRecognize.railTitle} / 录音=${synthRecognize.railMic} / 按钮=${synthRecognize.railActs}`
);

// 下拉框**必须真的选中一项**：绑到 undefined 时浏览器会"一项都没选中"，
// 框里一片空白——连首项「不指定」都不显示。这一条量的是"选中的是哪一个"。
const selects = await page.evaluate(`(() => {
  return [...document.querySelectorAll(".synth-side select")].map((el) => ({
    label: el.getAttribute("aria-label") || el.closest(".field")?.querySelector(".field-label")?.textContent.trim() || "",
    index: el.selectedIndex,
    text: el.options[el.selectedIndex]?.text || "",
  }));
})()`);
expect(
  "每个下拉都有一项被选中（不出现空白框）",
  selects.length >= 5 && selects.every((s) => s.index >= 0 && s.text !== ""),
  selects.map((s) => `${s.label}=${s.text || "空白"}`).join("、")
);

// 「情绪」是**候选 + 手输的组合框**：候选是那八个词（外加「不指定」），而框里照样
// 能自己打——规范 §4.5 那条"候选不是白名单"就是这么落地的（认不出的值由那台引擎
// 剔掉并说明，而不是"打了也发不出去"）。所以这一条量的是**两半都在**：
// 框是能打字的 input，点开右边那颗箭头有九个候选。
const emotionCombo = await page.evaluate(`(async () => {
  const combo = [...document.querySelectorAll(".synth-side .combo")]
    .find((b) => (b.querySelector("input")?.getAttribute("aria-label") || "") === "情绪");
  if (!combo) return null;
  const caret = combo.querySelector(".combo-caret");
  caret.click();
  await new Promise((r) => setTimeout(r, 150));
  // 菜单挂在 .combo 下面（不是 .combo-box 里）——它要能朝上开、要能盖住下面的格子。
  const menu = combo.querySelector(".combo-menu");
  const out = {
    tag: combo.querySelector("input").tagName,
    rows: menu ? [...menu.querySelectorAll("button")].map((b) => b.textContent.trim()) : [],
  };
  caret.click();
  await new Promise((r) => setTimeout(r, 150));
  return out;
})()`);
expect(
  "情绪是「候选 + 手输」的组合框（九个候选，框里也能自己打）",
  emotionCombo && emotionCombo.tag === "INPUT" && emotionCombo.rows.length === 9 && emotionCombo.rows[0] === "不指定",
  emotionCombo ? `${emotionCombo.rows.length} 个候选：${emotionCombo.rows.slice(0, 3).join("、")}……` : "没有情绪组合框"
);

// ---- 正文里的语气词标签：**一组一行**，通用在前、各引擎自己那一组在后 ----
// 两条要钉的口径（都是"加引擎不用改脚本"的前提）：
//   1. 「通用」= **支持语气词的引擎的交集**——一台都不认的那类引擎不参与（它在的话
//      交集恒为空，而"通用"的意思明明是"凡是认这东西的引擎都认"）；
//   2. 分组**与勾了哪台引擎无关**：不勾也摆着（勾了多台才给某一条打叉）。
const markGroups = await page.evaluate(`(() => {
  const groups = [...document.querySelectorAll(".mark-bar .mark-group")];
  return groups.map((g) => {
    const box = g.getBoundingClientRect();
    return {
      label: g.querySelector(".mark-group-label")?.textContent.trim() || "",
      names: [...g.querySelectorAll(".mark-btn")].map((b) => b.textContent.trim()),
      top: Math.round(box.top),
    };
  });
})()`);
expect(
  "语气词标签：通用那组在前，各引擎自己那组在后，**一组一行**",
  markGroups.length >= 2 &&
    markGroups[0].label === "通用" &&
    // 换行是**量出来的**：后一组的上边界要在前一组下面。挤在一行里的话两组 top
    // 相同——那是"靠一道竖线分组"的老样子，二十来条标签时那道线根本找不到。
    markGroups[1].top > markGroups[0].top &&
    markGroups.slice(1).every((g) => g.label && g.label !== "通用"),
  markGroups.map((g) => `${g.label}(${g.names.length} 条@${g.top})`).join(" / ")
);
expect(
  "「通用」= 所有支持语气词的引擎都认的那几条（笑声、叹气）",
  markGroups[0].names.includes("笑声") && markGroups[0].names.includes("叹气"),
  markGroups[0] ? markGroups[0].names.slice(0, 6).join("、") : "没有通用那一组"
);
expect(
  "同一概念不因写法不同变成两条（BreezeTTS 的 (gasp) 就是喘气，界面上不出 gasp）",
  markGroups.some((g) => g.names.includes("喘气")) &&
    !markGroups.some((g) => g.names.includes("gasp")),
  markGroups.map((g) => g.names.join("、")).join(" / ")
);
expect(
  "不支持语气词的引擎不进交集，也不单列一组（GPT-SoVITS 一条都不认）",
  markGroups.length > 1 && !markGroups.some((g) => g.label.includes("GPT-SoVITS")),
  markGroups.map((g) => g.label).join(" / ")
);

// **不勾引擎时也照样分组显示**：那是"这次要发给谁"以外的另一件事
// （"谁认哪几条"一直成立），所以取消勾选不该让那一排消失。
const unpicked = await page.evaluate(`(async () => {
  const boxes = () => [...document.querySelectorAll(".engine-pick input[type=checkbox]")];
  const before = boxes().filter((b) => b.checked).map((b) => b.closest(".engine-pick").textContent.trim());
  for (const b of boxes()) if (b.checked) b.click();
  await new Promise((r) => setTimeout(r, 200));
  const out = {
    groups: document.querySelectorAll(".mark-bar .mark-group").length,
    off: document.querySelectorAll(".mark-bar .mark-btn.off").length,
  };
  for (const b of boxes()) {
    const want = before.includes(b.closest(".engine-pick").textContent.trim());
    if (b.checked !== want) b.click();
  }
  await new Promise((r) => setTimeout(r, 200));
  return out;
})()`);
expect(
  "不勾引擎时那几组也在（分组与勾选无关），且一条都不灰（还没得发，不置评）",
  unpicked.groups >= 2 && unpicked.off === 0,
  `${unpicked.groups} 组 / ${unpicked.off} 灰`
);

// 点一下插进**光标处**；再切到只勾本机引擎（它一条都不认）→ 那些按钮要变灰
// （点了也会被从正文里删掉，留着会被念出来）。
const inserted = await page.evaluate(`(async () => {
  const box = document.querySelector(".te-text");
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
  [...document.querySelectorAll(".mark-btn")].find((b) => b.textContent.trim() === "笑声")?.click();
  await new Promise((r) => setTimeout(r, 120));
  return document.querySelector(".te-text").value;
})()`);
expect(
  "点一下就把标签插进正文（统一写法，不是某一家那串）",
  inserted.includes("[[笑]]"),
  inserted.slice(-24)
);
const onlyLocal = await page.evaluate(`(async () => {
  for (const box of document.querySelectorAll(".engine-pick input[type=checkbox]")) {
    const row = box.closest(".engine-pick");
    const want = row.textContent.includes("GPT-SoVITS");
    if (box.checked !== want) box.click();
  }
  await new Promise((r) => setTimeout(r, 200));
  const bar = document.querySelector(".mark-bar");
  return { total: bar.querySelectorAll(".mark-btn").length, off: bar.querySelectorAll(".mark-btn.off").length };
})()`);
expect(
  "勾中的引擎一条都不认时，标签按钮全变灰（**不认的会从正文里删掉**）",
  onlyLocal.total > 0 && onlyLocal.off === onlyLocal.total,
  `${onlyLocal.off}/${onlyLocal.total} 灰`
);
const backToCloud = await page.evaluate(`(async () => {
  for (const box of document.querySelectorAll(".engine-pick input[type=checkbox]")) {
    const row = box.closest(".engine-pick");
    const want = row.textContent.includes("MiniMax");
    if (box.checked !== want) box.click();
  }
  await new Promise((r) => setTimeout(r, 200));
  const groups = [...document.querySelectorAll(".mark-bar .mark-group")];
  return groups.map((g) => ({
    label: g.querySelector(".mark-group-label")?.textContent.trim() || "",
    total: g.querySelectorAll(".mark-btn").length,
    off: g.querySelectorAll(".mark-btn.off").length,
  }));
})()`);
expect(
  "换回认它们的引擎：通用那一组全亮（别台那一组照旧标着「它不认」）",
  backToCloud.length >= 2 &&
    backToCloud[0].label === "通用" &&
    backToCloud[0].total > 0 &&
    backToCloud[0].off === 0 &&
    // 别的引擎那几组里，MiniMax 认的（它自己那一组）不灰，别家的灰。
    backToCloud.slice(1).some((g) => g.label.includes("MiniMax") ? g.off === 0 : g.off > 0),
  backToCloud.map((g) => `${g.label} ${g.off}/${g.total} 灰`).join(" | ")
);
// 自己收尾：**正文清干净 + 引擎勾回本机那台**——后面那几条（按钮灰态、底栏没有
// 提示文字）是按"只勾了本机引擎、正文为空"这个起点写的。
await page.evaluate(`(async () => {
  const box = document.querySelector(".te-text");
  box.value = box.value.replace("[[笑]]", "");
  box.dispatchEvent(new Event("input", { bubbles: true }));
  for (const b of document.querySelectorAll(".engine-pick input[type=checkbox]")) {
    const want = b.closest(".engine-pick").textContent.includes("GPT-SoVITS");
    if (b.checked !== want) b.click();
  }
  await new Promise((r) => setTimeout(r, 200));
})()`);

// 界面上不该出现说明文字与待办口吻——那是注释的事，不是界面的事。
const synthCopy = await page.evaluate(`(() => {
  const page = document.querySelector(".synth");
  return {
    text: page ? page.innerText : "",
    notes: document.querySelectorAll(".field-note").length,
  };
})()`);
expect("参数下面不再挂说明文字", synthCopy.notes === 0, `${synthCopy.notes} 处`);
expect(
  "旧的幼稚文案已清干净（含被删掉的「说话人」）",
  ["要念的话", "谁在说", "怎么念", "挑一个", "勾几台", "说话人"].every(
    (word) => !synthCopy.text.includes(word)
  ),
  ["要念的话", "谁在说", "怎么念", "挑一个", "勾几台", "说话人"]
    .filter((word) => synthCopy.text.includes(word))
    .join("、") || "都没有"
);

// 左右结构：正文在左、参数在右，两栏**并排**（而不是上下堆成一条长流）。
const columns = await page.evaluate(`(() => {
  const text = document.querySelector(".synth-col-main")?.getBoundingClientRect();
  const side = document.querySelector(".synth-side")?.getBoundingClientRect();
  if (!text || !side) return null;
  return { textRight: Math.round(text.right), sideLeft: Math.round(side.left), sideW: Math.round(side.width) };
})()`);
expect(
  "正文与参数是左右两栏",
  columns && columns.sideLeft >= columns.textRight - 2 && columns.sideW > 200,
  columns ? `正文右边界 ${columns.textRight}，参数栏左边界 ${columns.sideLeft}` : "没有两栏"
);

const gated = await page.evaluate(`(() => {
  const btn = document.querySelector(".synth-foot .btn.primary");
  return {
    disabled: Boolean(btn?.disabled),
    reason: btn?.title || "",
    footHints: document.querySelectorAll(".synth-foot .hint").length,
  };
})()`);
expect(
  "什么都没填时按钮是灰的，**并且说了为什么**（理由写在 title 里——底栏不再挂一行提示文字）",
  gated && gated.disabled && gated.reason.length > 0 && gated.footHints === 0,
  gated ? `“${gated.reason}” / 底栏提示 ${gated.footHints} 行` : ""
);

// 音色那一格是**可搜索的挑选器**（不再是"几百条平铺进一个原生下拉"——那东西音色一多
// 就没人找得到东西）。名单是进这一页时才取的（生成页没有筛选的概念），所以**要等它
// 到齐**再挑：不等的话下面那句会拿到一个空名单，而报错的样子像个脚本 bug，
// 不像"还没加载完"。箭头那颗按钮的 title 上写着条数，就地量它。
let voiceCount = 0;
for (let i = 0; i < 25; i += 1) {
  voiceCount = await page.evaluate(`(() => {
    const caret = document.querySelector(".vpick .combo-caret");
    // 括号是**全角**的（界面上写的是「音色（12 条）」）——照半角去匹配的话
    // 永远匹配不到，于是这条会一直等到超时、报"0 条"，看起来像名单没到齐。
    const m = (caret?.title || "").match(/[（(](\\d+) 条[)）]/);
    return m ? Number(m[1]) : 0;
  })()`);
  if (voiceCount > 1) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("进生成页后音色名单到齐（挑选器上写着条数）", voiceCount > 1, `${voiceCount} 条`);

// 填正文 + 用挑选器挑「胡桃」+ 只勾 GPT-SoVITS（它连不上本机那台服务，
// 于是这一趟会**真的跑一次失败路径**——而这正是要验的东西：那台引擎自己报的话。
const pickedName = await page.evaluate(`(async () => {
  const set = (el, value) => {
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  set(document.querySelector(".te-text"), "你好，世界。这是按规范发出去的一次合成。");

  // 打开菜单用**那颗箭头**（caret 的 @click）：无头模式下 focus() 不派发 focus 事件，
  // 而输入框是挂在 @focus 上的（见 cdp.mjs 顶部第 6 条）。
  // 选一条则要在那一行上派发 **mousedown**（条目用的就是 @mousedown.prevent）。
  const box = document.querySelector(".vpick");
  box.querySelector(".combo-caret").click();
  await new Promise((r) => setTimeout(r, 150));
  const input = box.querySelector(".vpick-input");
  input.value = "胡桃";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  const row = [...box.querySelectorAll(".vpick-row")].find((b) => b.textContent.includes("胡桃"));
  row.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));

  for (const check of document.querySelectorAll(".engine-pick input[type=checkbox]")) {
    const line = check.closest(".engine-pick");
    const want = line.textContent.includes("GPT-SoVITS");
    if (check.checked !== want) check.click();
  }
  return box.querySelector(".vpick-input").value;
})()`);
await new Promise((r) => setTimeout(r, 300));
expect("挑选器选中之后就写着那一条的名字", pickedName === "胡桃", `框里=${pickedName || "（空）"}`);

// 库里那条记录的 id 从接口拿：界面上那一格存的是 id，而这里要知道的是"挑中的是谁"。
const pickedVoiceId = items(await (await fetch(new URL("/api/v1/voices?q=胡桃", url))).json())[0].id;

// 「参考音频」那一格的名单是**进这一页时才拉的**（跟着音色走），所以要等它到位。
// 这一格列的就是这个音色挂着的那些音频：挑一条非主样例的＝换一段嗓子（连它自带的
// 语气一起换），而它与「情绪」那一格**不是二选一**——两个都给了就都发出去。
let refAudio = null;
for (let i = 0; i < 25; i += 1) {
  refAudio = await page.evaluate(`(() => {
    const sel = document.querySelector('.synth-side select[aria-label="参考音频"]');
    return sel ? [...sel.options].map((o) => o.textContent.trim()) : null;
  })()`);
  if (refAudio && refAudio.length > 1) break;
  await new Promise((r) => setTimeout(r, 200));
}
// 首项**直接写主样例的文件名**（"现在用的是哪一个"一眼看得出来），
// 所以拿服务端那条记录对一下——不是对着"主样例"三个字断言。
const pickedDetail = await (
  await fetch(new URL(`/api/v1/voices/${encodeURIComponent(pickedVoiceId)}`, url))
).json();
expect(
  "参考音频首项直接写主样例的文件名（没有「主样例」这种字眼）",
  refAudio && refAudio[0] === pickedDetail.main_sample && !refAudio.some((t) => t.includes("主样例")),
  refAudio ? `首项=${refAudio[0]} / 主样例=${pickedDetail.main_sample}` : "没有这一格"
);
expect(
  "其余音频另列，且不与首项重复",
  refAudio && refAudio.length >= 2 && new Set(refAudio).size === refAudio.length,
  refAudio ? refAudio.join(" / ") : ""
);

const ready = await page.evaluate(`(() => {
  const btn = document.querySelector(".synth-foot .btn.primary");
  return { disabled: Boolean(btn?.disabled), text: btn?.textContent.trim() || "" };
})()`);
expect("填齐之后按钮能点，且写着发给几台", ready && !ready.disabled && ready.text.includes("1"), ready?.text);

await page.evaluate(`document.querySelector(".synth-foot .btn.primary").click()`);
let job = null;
for (let i = 0; i < 40; i += 1) {
  job = await page.evaluate(`(() => {
    const row = document.querySelector(".job");
    if (!row) return null;
    return {
      name: row.querySelector(".job-name")?.textContent.trim() || "",
      badge: row.querySelector(".badge")?.textContent.trim() || "",
      error: row.querySelector(".job-error")?.textContent.trim() || "",
    };
  })()`);
  if (job && job.badge !== "生成中") break;
  await new Promise((r) => setTimeout(r, 250));
}
expect(
  "每台引擎一行结果（这台失败，就把它自己报的那句话原样摆出来）",
  job && job.badge === "失败" && job.error.includes("推理服务"),
  job ? `${job.name} / ${job.badge} / “${job.error}”` : "没有 .job"
);

// 这一条 400 是**我们故意跑出来的**（上面那条断言要的就是一次失败），
// 它留在报错清单里会把一次成功的自检判成失败。所以把它挑出来——
// 顺手也把它变成一条断言：失败的那一次在控制台里留下的是**一条 400**，不是崩溃。
const synthNoise = page.problems.filter((text) => text.includes("/api/v1/synthesize"));
expect(
  "跑失败的那一次在控制台里是一条 400（不是未捕获异常）",
  synthNoise.length === 1 && synthNoise[0].includes("400") && !synthNoise.some((t) => t.includes("未捕获异常")),
  synthNoise.join(" | ") || "控制台没有留下这次请求的痕迹"
);
for (const text of synthNoise) page.problems.splice(page.problems.indexOf(text), 1);

await page.screenshot(path.join(outDir, "05-synth.png"));

// 回到音色库——后面深色那一段要在列表上量亮度（走**左栏**，生成页右上那颗
// 「返回音色库」已撤）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".sider-body .tree-item")]
    .find((el) => el.innerText.includes("全部音色"))
    ?.click();
})()`);
let backToLibrary = false;
for (let i = 0; i < 25; i += 1) {
  backToLibrary = await page.evaluate(
    `Boolean(document.querySelector(".lib-head")) && document.querySelectorAll(".card").length > 0`
  );
  if (backToLibrary) break;
  await new Promise((r) => setTimeout(r, 200));
}
expect("生成页能从左栏回到音色库", backToLibrary);

// ---- 语音识别：三个档位、默认一个都不下 ----
//
// 这一段的断言**不假定"这台机器上装没装"**——它验的是"界面说的与后端那份状态
// 一致"：按钮文案（下载 ↔ 移除）、单选框能不能选、样例行里有没有那颗「识别」。
// 装不装是用户自己的事，界面照着后端说的画；两处各说各话才是要拦下的那种错。
//
// "真的去下载几 GB"那一路没法在这里跑（权重不是自检该下的东西），所以后半段
// **自己造一份账本**把"装了之后长什么样"也照出来——脚本自己造状态、自己收尾。
// 安装与识别那两条路（pip 怎么调、账怎么记、只吃库里的样例）由 tests/test_asr.py
// 用假的下载器钉住。
/** 打开「设置」页并切到语音识别那一栏。
 *
 *  **两步之间要留一拍**：齿轮点下去之后主区是 Vue 下一个 tick 才换页的，
 *  同一拍里去找那个页签必然找不到（那会让整段断言看起来像"页面打不开"）。
 *  **已经在这一页时不再点齿轮**：那颗齿轮落在「目录」栏，重复点只会把分栏拨回去。 */
// ---- 语音识别那一栏 ----------------------------------------------------------
//
// **这一段从前按旧界面写**（``.settings-panel`` 里的 ``.asr-row`` 表、下载/移除
// 两种按钮、自造一份 ``installed.json`` 账本），而那套界面与"下载一档模型"这件事
// 都已经不存在了：识别现在是**一类插件**（``app/engines/asr_base``），设置页里
// 是"引擎卡片 + 由 settings_schema 渲染的表单"，没有安装与移除。
//
// 契约（请求体形状、错误翻译、掩码/恢复默认、当前引擎的回落）由 ``tests/test_asr.py``
// 钉；这里只留一条**界面确实渲染了表单**的烟测，避免两处各写一遍规则。

const openAsrSection = async () => {
  await page.evaluate(`(() => {
    [...document.querySelectorAll(".sh-nav-item")].find((n) => n.innerText.includes("设置"))?.click();
  })()`);
  await new Promise((r) => setTimeout(r, 600));
  await page.evaluate(`(() => {
    document.querySelector("#sec-asr")?.scrollIntoView({ block: "start" });
  })()`);
  await new Promise((r) => setTimeout(r, 400));
};

await openAsrSection();
const asrView = await page.evaluate(`(() => {
  const host = document.querySelector("#sec-asr");
  const rows = [...(host?.querySelectorAll(".setting-row") || [])];
  return {
    present: Boolean(host),
    rows: rows.map((r) => (r.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 80)),
    fields: [...(host?.querySelectorAll("input, textarea, select") || [])].length,
    probe: [...(host?.querySelectorAll("button") || [])].some((b) => b.textContent.includes("测试连接")),
  };
})()`);
const asrServer = await fetch(new URL("/api/v1/asr", url)).then((res) => res.json());
expect(
  "设置页有一栏「语音识别」，列出后端给的识别引擎",
  asrView.present && asrView.rows.some((line) => line.includes(asrServer.engines[0]?.label || "识别")),
  `行：${asrView.rows.join(" | ") || "（空）"}`
);
expect(
  "它的设置表单是按 schema 渲染的（有输入框，不是一句说明）",
  asrView.fields >= 2,
  `${asrView.fields} 个输入框`
);
expect(
  "有一颗「测试连接」（探测是单独的动作，不夹在打开设置里）",
  asrView.probe,
  `probe=${asrView.probe}`
);
await page.screenshot(path.join(outDir, "10-asr.png"));

// ---- 同一块「语音识别」，**装了模型之后的样子** ----
//
// 装了才有"挑一段音频 + 识别 + 结果框 + 写回『说话内容』"这一整套。这一块与上面
// 那条链是同一件事的两端：引擎要逐字稿时中转站会自己听一遍（见 usecase/reference），
// 而这里是**给人看、给人改、给人存**的那一份——听出来的一整句常常要改两个字，
// 而它一旦写回，那条样例的来路就变了，那种事不该自动发生。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item")].find((n) => n.innerText.includes("生成"))?.click();
})()`);
for (let i = 0; i < 25; i += 1) {
  if (await page.evaluate(`Boolean(document.querySelector(".synth"))`)) break;
  await new Promise((r) => setTimeout(r, 200));
}
const listenReady = await page.evaluate(`(() => {
  const block = document.querySelector(".synth-asr");
  if (!block) return null;
  const row = block.querySelector(".listen-row");
  return {
    pick: Boolean(row?.querySelector("select")),
    button: row?.querySelector("button")?.textContent.trim() || "",
    area: Boolean(block.querySelector(".listen-text")),
    save: [...block.querySelectorAll("button")].some((b) => b.textContent.includes("写入")),
    // 还没挑音频时按钮是灰的——**灰态本身就是"还差一步"的说明**
    //（这一页此时还没选音色，所以那段选择框是空的）。
    idle: row?.querySelector("button")?.disabled === true,
  };
})()`);
expect(
  "装了模型之后：挑音频 + 识别 + 可改的结果框 + 写回「说话内容」",
  listenReady &&
    listenReady.pick &&
    listenReady.button === "识别" &&
    listenReady.area &&
    listenReady.save &&
    listenReady.idle,
  listenReady
    ? `挑音频=${listenReady.pick} 按钮=${listenReady.button} 结果框=${listenReady.area} 写回=${listenReady.save} 灰=${listenReady.idle}`
    : "没有 .synth-asr"
);

// ---- 日志：**左栏一个入口，正文在右侧主区** ----
//
// 从前正文是在左栏里展开的，而左栏只有 236px：时间 / 级别 / 来源 / 正文四段挤在那里，
// 正文会被压成一字一行（正文恰恰是唯一要读的东西）。现在左栏只留一个入口（名称 +
// 角标），点它 → 主区那一页。这一块钉的正是这条分界，以及那几个"看着能用、其实没接上"
// 的点：角标数的是**收到的全部**（不受勾选影响）、来源候选**由收到的行生成**、
// 四段并排、清屏**只清视图**。
//
// 先自己造两条**必然产生、且不触发 4xx** 的日志：浏览器对任何 4xx 都会在控制台记一条
// error，而这份自检把控制台报错也算失败（所以不能靠"拿一台不存在的引擎去合成"来造
// warning）。改用扫描这条路：
//   · info —— 一次扫描就是一条汇总（source=library）；
//   · warning —— 把扫描根**临时**加上一个不存在的目录再扫一次（那条 warning 说的正是
//     "怎么一条都没有"最常见的病因），扫完立刻把根改回去。
await page.evaluate(`(async () => {
  const cfg = await fetch("/api/v1/folders").then((r) => r.json());
  const keep = cfg.folders || [];
  const put = (folders) =>
    fetch("/api/v1/folders", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders }),
    });
  await put([...keep, "C:/__自检用的不存在目录__"]);
  await fetch("/api/v1/scan", { method: "POST" });
  await put(keep);
})()`);
await new Promise((r) => setTimeout(r, 900));

// **左栏只有入口**：名称 + 角标，没有正文、没有列表——这条就是这次改动的那条分界线。
const entry = await page.evaluate(`(() => {
  const sider = document.querySelector(".sider");
  const row = sider?.querySelector(".log-entry");
  if (!row) return null;
  return {
    text: row.textContent.replace(/\\s+/g, " ").trim(),
    badges: [...row.querySelectorAll(".badge")].map((b) => b.textContent.trim()),
    // 正文不在这一列里：日志页、行、表头都不该出现在 .sider 之内。
    viewInSider: Boolean(sider.querySelector(".logview")),
    rowsInSider: sider.querySelectorAll(".ln").length,
    next: row.nextElementSibling?.className || "",
    active: row.classList.contains("active"),
  };
})()`);
expect(
  "左栏那一行**只是入口**：写着「日志」+ 角标，没有正文也没有列表",
  Boolean(entry) &&
    // 行里还有一个图标字形（☰），所以量的是"含『日志』且整行很短"，不是前缀。
    entry.text.includes("日志") &&
    entry.text.length <= 12 &&
    !entry.viewInSider &&
    entry.rowsInSider === 0,
  entry ? `文字="${entry.text}" 左栏里的日志行=${entry.rowsInSider}` : "没有 .log-entry"
);
expect(
  "入口在音色树与左栏脚注之间（不随音色树滚动）",
  Boolean(entry && entry.next.includes("sider-foot")),
  entry ? `下一个兄弟=${entry.next || "—"}` : ""
);
expect(
  "角标在入口上（错误不允许被折叠隐藏）",
  Boolean(entry && entry.badges.length),
  entry ? `角标=${entry.badges.join("、")}` : ""
);

// 点入口 → 主区那一页。**判据是宽度**：那正是改它的原因，所以要量出来。
await page.evaluate(`document.querySelector(".sider .log-entry").click()`);
await new Promise((r) => setTimeout(r, 500));

const view = await page.evaluate(`(() => {
  const page_ = document.querySelector(".main .logview");
  if (!page_) return null;
  const list = page_.querySelector(".logview-list");
  const rows = [...list.querySelectorAll(".ln:not(.ln-head)")];
  const head = list.querySelector(".ln-head");
  const first = rows[0];
  const rect = (el) => (el ? el.getBoundingClientRect() : null);
  const at = rect(first?.querySelector(".at"));
  const msg = rect(first?.querySelector(".msg"));
  const src = rect(first?.querySelector(".src"));
  return {
    siderWidth: Math.round(document.querySelector(".sider").getBoundingClientRect().width),
    listWidth: Math.round(list.getBoundingClientRect().width),
    rows: rows.length,
    headCols: head ? [...head.children].map((c) => c.textContent.trim()) : [],
    // 四段**并排**：正文与时间在同一水平线上（一行一条），不是上下两行。
    sideBySide: Boolean(at && msg) && Math.abs(msg.top - at.top) <= 8,
    // 正文拿得到地方：占这一页一半以上（左栏那版只有四成出头）。
    msgShare: msg ? Math.round((msg.width / list.getBoundingClientRect().width) * 100) : 0,
    srcBeforeMsg: Boolean(src && msg) && src.right <= msg.left + 1,
    levels: [...page_.querySelectorAll(".levels .chk")].map((l) => ({
      label: l.textContent.trim(),
      on: l.querySelector("input").checked,
    })),
    opts: [...page_.querySelectorAll(".logview-tools select option")]
      .map((o) => o.value)
      .filter((v) => v !== ""),
    srcs: [...new Set(rows.map((r) => r.querySelector(".src")?.textContent.trim() || "unknown"))],
    buttons: [...page_.querySelectorAll(".logview-tools button")].map((b) => b.textContent.trim()),
  };
})()`);
expect(
  "点左栏那一行 → 右侧主区出现日志页，且**比左栏宽得多**",
  Boolean(view) && view.rows >= 1 && view.listWidth >= view.siderWidth * 3,
  view ? `列表 ${view.listWidth}px / 左栏 ${view.siderWidth}px，行数=${view.rows}` : "没有 .main .logview"
);
expect(
  "四段并排（时间 | 级别 | 来源 | 正文），正文占这一页一半以上",
  Boolean(view) &&
    view.headCols.join("|") === "时间|级别|来源|正文" &&
    view.sideBySide &&
    view.srcBeforeMsg &&
    view.msgShare >= 50,
  view ? `表头=${view.headCols.join(" | ")} 同在一行=${view.sideBySide} 正文占 ${view.msgShare}%` : ""
);
expect(
  "四档级别齐全，且**调试默认关**（它是诊断细节，开着会把要看的行冲掉）",
  Boolean(view) &&
    view.levels.length === 4 &&
    view.levels[0].label === "调试" &&
    !view.levels[0].on &&
    view.levels.slice(1).every((l) => l.on),
  view ? view.levels.map((l) => l.label + (l.on ? "☑" : "☐")).join(" ") : ""
);
expect(
  "来源候选由**收到的行**生成（新来源自动出现，零登记手续）",
  Boolean(view) && view.opts.length > 0 && JSON.stringify(view.opts) === JSON.stringify([...view.srcs].sort()),
  view ? `下拉=${view.opts.join("、")} / 行里的来源=${view.srcs.join("、")}` : ""
);
expect(
  "清屏与导出都在（清屏只清视图；导出按当前过滤）",
  Boolean(view) && view.buttons.includes("清屏") && view.buttons.includes("导出"),
  view ? view.buttons.join("、") : ""
);

await page.screenshot(path.join(outDir, "12-logview.png"));

// **角标数的是收到的全部**：它回答的是"攒了多少条要看的"。跟着过滤器走的话，
// 勾掉「警告」就等于把事故藏起来——所以取消勾选之后左栏那个角标必须**一动不动**。
const badgeFreedom = await page.evaluate(`(async () => {
  const read = () => [...document.querySelectorAll(".sider .log-entry .badge")].map((b) => b.textContent.trim());
  const rowsOf = () => document.querySelectorAll(".logview-list .ln:not(.ln-head)").length;
  const warnOf = () => document.querySelectorAll(".logview-list .ln.warning").length;
  const chk = [...document.querySelectorAll(".logview .levels .chk")]
    .find((l) => l.textContent.trim() === "警告").querySelector("input");
  const before = { badges: read(), warn: warnOf(), rows: rowsOf() };
  chk.click();
  await new Promise((r) => setTimeout(r, 300));
  const off = { badges: read(), warn: warnOf(), rows: rowsOf() };
  chk.click();
  await new Promise((r) => setTimeout(r, 300));
  return { before, off, back: rowsOf() };
})()`);
expect(
  "取消勾选「警告」：行少了，但左栏角标一动不动（角标数的是收到的全部）",
  badgeFreedom.before.warn >= 1 &&
    badgeFreedom.off.warn === 0 &&
    badgeFreedom.off.badges.join("|") === badgeFreedom.before.badges.join("|"),
  `警告行 ${badgeFreedom.before.warn}→${badgeFreedom.off.warn}，角标 ${badgeFreedom.before.badges.join("、")} → ${badgeFreedom.off.badges.join("、")}`
);
expect(
  "勾回来行数复原（过滤是视图上的事，不是把行删了）",
  badgeFreedom.back === badgeFreedom.before.rows,
  `${badgeFreedom.before.rows} → ${badgeFreedom.back} 行`
);

// 搜索只搜正文。用一个**必然搜不到**的串把空态逼出来，再清空看行回来——
// 这样量的是"搜索真的接在数据上"，不是"框里能打字"。
const searched = await page.evaluate(`(async () => {
  const list = document.querySelector(".logview-list");
  const box = document.querySelector(".logview .needle");
  const set = (v) => {
    box.value = v;
    box.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const rowsOf = () => list.querySelectorAll(".ln:not(.ln-head)").length;
  const before = rowsOf();
  set("zzz绝对搜不到的一串");
  await new Promise((r) => setTimeout(r, 300));
  const empty = { rows: rowsOf(), note: list.querySelector(".pad")?.textContent.trim() || "" };
  set("");
  await new Promise((r) => setTimeout(r, 300));
  return { before, empty, after: rowsOf() };
})()`);
expect(
  "搜索接在真数据上：搜不到时给空态那句话，清空后行回来",
  searched.empty.rows === 0 && searched.empty.note.includes("没有日志") && searched.after === searched.before,
  `${searched.before} 行 → 搜不到 ${searched.empty.rows} 行（${searched.empty.note}）→ 清空 ${searched.after} 行`
);

// 清屏：**只清视图**。判据是"屏幕上没了、后端那份还在"——把"我暂时不想看"做成
// "删掉历史"，是这一整块里最容易犯、也最没法挽回的一种误删。
const screenCleared = await page.evaluate(`(async () => {
  [...document.querySelectorAll(".logview-tools button")]
    .find((b) => b.textContent.trim() === "清屏").click();
  await new Promise((r) => setTimeout(r, 300));
  const rows = document.querySelectorAll(".logview-list .ln:not(.ln-head)").length;
  const kept = await fetch("/api/v1/logs?since=0")
    .then((r) => r.json())
    .then((j) => (j.lines || []).length)
    .catch(() => -1);
  return { rows, kept };
})()`);
expect(
  "清屏只清视图：屏幕上空了，后端那份仍是全量",
  screenCleared.rows === 0 && screenCleared.kept > 0,
  `屏上 ${screenCleared.rows} 行 / 后端仍有 ${screenCleared.kept} 行`
);

// 回音色库：日志是主区里的一页，退出去要回到卡片那一页（而不是留一片空白）。
// 走**左栏**（日志页右上那颗「返回音色库」已撤）。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".sider-body .tree-item")]
    .find((el) => el.innerText.includes("全部音色"))
    ?.click();
})()`);
await new Promise((r) => setTimeout(r, 700));
const leftLogview = await page.evaluate(`(() => ({
  gone: !document.querySelector(".logview"),
  cards: document.querySelectorAll(".card").length,
  rowsInSider: document.querySelectorAll(".sider .ln").length,
}))()`);
expect(
  "从左栏真的回到卡片那一页（日志页退场，左栏仍是入口）",
  leftLogview.gone && leftLogview.cards > 0 && leftLogview.rowsInSider === 0,
  `日志页还在=${!leftLogview.gone} 卡片=${leftLogview.cards} 左栏日志行=${leftLogview.rowsInSider}`
);

// 后面深色那一段要"有东西可看"：清屏清掉的是视图，不是来路，再造一条。
await page.evaluate(`fetch("/api/v1/scan", { method: "POST" })`);
await new Promise((r) => setTimeout(r, 700));

// ---- 播放条：**声音归那颗圆钮，不归某一页** ----
//
// 这一段守两件事：
//  1. 试听走的是右下角那颗圆钮：收起时只占一个角、内容不让位，移上去展开成一条坞才让
//     出末尾那一截；「全部试听」把一串音频排成队列、放完自动接下一段；
//  2. **再点一次「生成」不能把正在放的声音掐掉**——用户报的就是这一条：从前播放器
//     是结果行里的 `<audio controls>`，整排结果被重写时那个元素一起没了。
//     现在 `<audio>` 挂在 body 上、状态在 store 里，所以这里量的是"元素还在、src 没变"。
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
// 圆钮与坞分开量：坞收起时是 `visibility: hidden`，**布局盒还在**，
// 所以"看不看得见"读 visibility，不能读 rect 宽窄。
const barState = `(() => {
  const fold = document.querySelector(".player-fold");
  const chip = document.querySelector(".pb-chip");
  const dock = document.querySelector(".pb-dock");
  const el = document.querySelector("#vh-player");
  const scroller = document.querySelector(
    ".grid-wrap, .rows-wrap, .rail-list, .rail-chapters, .synth-col-main, .synth-side, .logview-list"
  );
  const chipRect = chip ? chip.getBoundingClientRect() : null;
  return {
    fold: Boolean(fold),
    open: Boolean(fold?.classList.contains("is-open")),
    pinned: Boolean(fold?.classList.contains("is-pinned")),
    label: (document.querySelector(".pb-label")?.textContent || "").trim(),
    count: (document.querySelector(".pb-count")?.textContent || "").trim(),
    buttons: [...document.querySelectorAll(".pb-dock .pb-btn")].map((b) => b.getAttribute("aria-label") || ""),
    chip: chipRect ? [Math.round(chipRect.width), Math.round(chipRect.height)] : [],
    bottom: chipRect ? Math.round(window.innerHeight - chipRect.bottom) : null,
    right: chipRect ? Math.round(window.innerWidth - chipRect.right) : null,
    corner: chipRect ? chipRect.right > window.innerWidth / 2 && chipRect.bottom < window.innerHeight : false,
    dockW: dock ? Math.round(dock.getBoundingClientRect().width) : null,
    dockVisible: dock ? getComputedStyle(dock).visibility === "visible" : false,
    pad: scroller ? Math.round(Number.parseFloat(getComputedStyle(scroller).paddingBottom)) : null,
    siderH: Math.round(document.querySelector(".sider")?.getBoundingClientRect().height || 0),
    vh: window.innerHeight,
    // 有没有悬停可用决定展开怎么触发：能悬停就真移鼠标，不能就点圆钮。
    hover: window.matchMedia("(hover: hover)").matches,
    paused: el ? el.paused : null,
    src: el ? el.getAttribute("src") || "" : "",
    dur: el ? Number((el.duration || 0).toFixed(2)) : 0,
  };
})()`;

const cardPlayed = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll(".card button")].find((x) => x.textContent.includes("试听"));
  if (!b) return false;
  b.click();
  return true;
})()`);
let bar1 = null;
for (let i = 0; i < 20; i += 1) {
  bar1 = await page.evaluate(barState);
  if (bar1.fold && bar1.dur > 0) break;
  await wait(100);
}
expect(
  "点卡片上的「试听」→ 右下角出现一颗圆钮（48×48），坞还收着",
  cardPlayed &&
    bar1 &&
    bar1.fold &&
    bar1.chip.join("×") === "48×48" &&
    bar1.bottom === 22 &&
    bar1.corner &&
    !bar1.dockVisible,
  bar1 ? `圆钮=${bar1.chip.join("×")} 距底=${bar1.bottom} 距右=${bar1.right} 坞可见=${bar1.dockVisible}` : "没有播放条"
);
expect(
  "收起时内容不为它让位（末尾那一截是各页滚动容器自己的）",
  bar1 && bar1.siderH === bar1.vh,
  bar1 ? `左栏高=${bar1.siderH} / 视口=${bar1.vh} 让位=${bar1.pad}` : ""
);
expect(
  "它真的在放：<audio> 拿得到时长、坞上写着放的是哪一段",
  bar1 && bar1.dur > 0 && bar1.label.length > 0 && bar1.paused === false,
  bar1 ? `时长=${bar1.dur}s 标题="${bar1.label}" 暂停=${bar1.paused}` : ""
);

// ---- 展开与收起：悬停驱动的（没有悬停可用时点圆钮） ----
const chipAt = await page.evaluate(`(() => {
  const r = document.querySelector(".pb-chip")?.getBoundingClientRect();
  return r ? { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) } : null;
})()`);
const hoverIn = async () => {
  if (!chipAt) return;
  if (bar1.hover) {
    await page.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: chipAt.x, y: chipAt.y });
  } else {
    await page.clickAt(".pb-chip");
  }
};
const hoverOut = async () => {
  if (!chipAt) return;
  if (bar1.hover) {
    await page.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 120, y: 120 });
  } else {
    await page.clickAt(".pb-chip");
  }
};
await hoverIn();
let barOpen = null;
for (let i = 0; i < 15; i += 1) {
  barOpen = await page.evaluate(barState);
  if (barOpen.dockVisible) break;
  await wait(80);
}
expect(
  "移到圆钮上就展开成一条坞（528 宽），末尾那一截跟着让出来",
  barOpen && barOpen.open && barOpen.dockW === 528 && barOpen.pad === 96,
  barOpen ? `展开=${barOpen.open} 坞宽=${barOpen.dockW} 让位=${bar1.pad}→${barOpen.pad}（--player-space=96；悬停可用=${barOpen.hover}）` : "没展开"
);
await hoverOut();
let barClosed = null;
for (let i = 0; i < 15; i += 1) {
  barClosed = await page.evaluate(barState);
  if (!barClosed.dockVisible) break;
  await wait(80);
}
expect(
  "移开就收回：260ms 后坞收掉，让位那一截还回去",
  barClosed && !barClosed.open && !barClosed.pinned && barClosed.pad === bar1.pad,
  barClosed ? `展开=${barClosed.open} 让位=${barClosed.pad}` : "没收回"
);

// 全部试听：一条音色的几段排成队列（demo 库里「胡桃」有 3 段）。
// 「全部」就摆在**卡片上**（试听旁边）：点它不打开详情抽屉，直接在播放条上放。
const allClicked = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll(".card button")].find((x) =>
    x.textContent.includes("全部")
  );
  if (!b) return false;
  b.click();
  return true;
})()`);
let bar2 = null;
for (let i = 0; i < 20; i += 1) {
  bar2 = await page.evaluate(barState);
  if (bar2.fold && bar2.count) break;
  await wait(100);
}
expect(
  "「全部试听」把这几段排成一条队列：坞上写着 1 / N，上一段/下一段跟着出现",
  allClicked &&
    bar2 &&
    /^1 \/ [2-9]$/.test(bar2.count) &&
    bar2.buttons.includes("上一段") &&
    bar2.buttons.includes("下一段"),
  bar2 ? `队列=${bar2.count} 按钮=${bar2.buttons.join("/")}` : "没点着"
);
// 放完一段要**自己接下一段**（这就是"全部试听"的全部实现）。demo 的样例只有 1.2 秒。
let advanced = "";
for (let i = 0; i < 40; i += 1) {
  advanced = (await page.evaluate(barState)).count;
  if (advanced && !/^1 \/ /.test(advanced)) break;
  await wait(150);
}
expect(
  "第一段放完自动接下一段（队列位置从 1/N 走到 2/N）",
  /^2 \/ /.test(advanced),
  `队列=${advanced || "(空)"}`
);

// 收尾前先给那个 `<audio>` 盖个记号：一会儿比"还是不是同一个节点"。
// **不要比 src**——队列正一段一段往前放，src 本来就会变（那是对的）。
await page.evaluate(`(() => {
  const el = document.querySelector("#vh-player");
  if (el) el.dataset.probe = "keep";
})()`);

// 去生成页，真点一次「生成」——**列表会被整排重写**，声音不该跟着没。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item")].find((n) => n.innerText.includes("生成"))?.click();
})()`);
for (let i = 0; i < 25; i += 1) {
  if (await page.evaluate(`Boolean(document.querySelector(".synth"))`)) break;
  await wait(200);
}
// 参数格（.vpick）要等**规范取回来**才渲染——从前这一步被详情抽屉顺带预热过
// （openDetail 顺手拉 spec），现在抽屉不开了，这里就得自己等。
for (let i = 0; i < 25; i += 1) {
  if (await page.evaluate(`Boolean(document.querySelector(".vpick"))`)) break;
  await wait(200);
}
// 正文 + 音色 + 勾一台引擎（按钮才点得动）。三样都按界面自己的方式来：
// 正文走 v-model（派发 input）、音色要**点那颗箭头再在条目上派发 mousedown**
// （`@mousedown.prevent`，其中 focus 在无头模式下不派发——见 cdp.mjs 顶部第 6 条）。
const genReady = await page.evaluate(`(async () => {
  const set = (el, value) => {
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const box = document.querySelector(".vpick");
  box.querySelector(".combo-caret").click();
  await new Promise((r) => setTimeout(r, 150));
  const row = [...box.querySelectorAll(".vpick-row")][0];
  row?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  const text = document.querySelector(".te-text");
  if (!text.value.trim()) set(text, "播放条不该被生成打断。");
  const pick = [...document.querySelectorAll(".engine-pick input[type=checkbox]")][0];
  if (pick && !pick.checked) pick.click();
  await new Promise((r) => setTimeout(r, 200));
  const btn = document.querySelector(".synth-foot .btn.primary");
  return { voice: box.querySelector(".vpick-input").value, disabled: Boolean(btn?.disabled) };
})()`);
const genClicked = await page.evaluate(`(() => {
  const b = document.querySelector(".synth-foot .btn.primary");
  if (!b || b.disabled) return false;
  b.click();
  return true;
})()`);
// 等结果区真的出来（= 那一排结果确实被重写了）。失败也是结果，这里要的就是"重写"。
// **等到它跑完**（徽标不再是「生成中」）而不是"出现了就算"：那一句 400 是响应回来
// 之后才进控制台的，先滤掉再等，它会在滤过之后才落进来（自检照样红）。
let jobsShown = 0;
for (let i = 0; i < 60; i += 1) {
  const state = await page.evaluate(`(() => {
    const rows = [...document.querySelectorAll(".job")];
    return {
      count: rows.length,
      running: rows.filter((r) => (r.querySelector(".badge")?.textContent || "").includes("生成中"))
        .length,
    };
  })()`);
  jobsShown = state.count;
  if (state.count > 0 && state.running === 0) break;
  await wait(200);
}
const afterGen = await page.evaluate(`(() => {
  const el = document.querySelector("#vh-player");
  const bar = document.querySelector(".player-fold");
  return {
    bar: Boolean(bar),
    probe: el?.dataset.probe || "",
    src: el?.getAttribute("src") || "",
    count: (document.querySelector(".pb-count")?.textContent || "").trim(),
  };
})()`);
expect(
  "再点一次「生成」不会掐掉正在放的声音（结果区被重写了，那个 <audio> 还是原来那个节点）",
  genClicked && jobsShown > 0 && afterGen.bar && afterGen.probe === "keep" && afterGen.src,
  afterGen
    ? `结果 ${jobsShown} 条（音色=${genReady.voice || "没选上"}，按钮灰=${genReady.disabled}）/ ` +
      `同一个节点=${afterGen.probe === "keep"} / 条还在=${afterGen.bar}`
    : ""
);
// 这一回**又是我们故意跑出来的**合成（这台机器上那几台引擎都跑不通）：它留下的
// 那条 400 与上面那一段是同一件事，留着会把一次成功的自检判成失败。
for (const text of page.problems.filter((t) => t.includes("/api/v1/synthesize"))) {
  page.problems.splice(page.problems.indexOf(text), 1);
}

// 关掉它：圆钮与坞一起没了、声音停下、让位那一截还回去。
const padBefore = (await page.evaluate(barState)).pad;
const closed = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll(".pb-dock .pb-btn")].find(
    (x) => (x.getAttribute("aria-label") || "") === "关掉播放条"
  );
  if (!b) return false;
  b.click();
  return true;
})()`);
await wait(300);
const bar3 = await page.evaluate(barState);
expect(
  "「关掉播放条」：圆钮没了、让位那一截还回去、声音停下",
  closed && !bar3.fold && bar3.paused === true && bar3.pad <= padBefore,
  `按到了=${closed} 条还在=${bar3.fold} 让位=${padBefore}→${bar3.pad} 暂停=${bar3.paused}`
);

// 回音色库：后面深色那一段要看卡片。
await page.evaluate(`(() => {
  [...document.querySelectorAll(".tree-item")].find((n) => n.innerText.includes("全部音色"))?.click();
})()`);
await wait(500);

// ---- 深色主题：可读性用亮度算，不靠"看着挺暗" ----
await page.evaluate(`(() => {
  [...document.querySelectorAll(".vh-modal .vh-icon")].find((b) => b.title === "关闭")?.click();
})()`);
await new Promise((r) => setTimeout(r, 300));
await page.evaluate(
  `(() => { localStorage.setItem("vh-theme", "dark"); document.documentElement.dataset.theme = "dark"; })()`
);
await new Promise((r) => setTimeout(r, 300));

const theme = await page.evaluate(`(() => {
  const lum = (s) => {
    const [r, g, b] = s.match(/\\d+/g).map(Number).map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const body = getComputedStyle(document.body);
  const card = document.querySelector(".card") ? getComputedStyle(document.querySelector(".card")) : null;
  return {
    attr: document.documentElement.dataset.theme,
    bg: body.backgroundColor,
    fg: body.color,
    bgLum: lum(body.backgroundColor),
    fgLum: lum(body.color),
    cardBg: card ? card.backgroundColor : "",
  };
})()`);
expect("深色主题属性生效", theme.attr === "dark", theme.attr);
expect("深色：背景是暗的", theme.bgLum < 0.12, `bg=${theme.bg} (亮度 ${theme.bgLum.toFixed(3)})`);
expect("深色：文字是亮的", theme.fgLum > 0.5, `text=${theme.fg} (亮度 ${theme.fgLum.toFixed(3)})`);
expect("深色：卡片与背景可区分", theme.cardBg && theme.cardBg !== theme.bg, `card=${theme.cardBg}`);

await page.screenshot(path.join(outDir, "04-dark.png"));

// ---- 深色下的日志页：sticky 表头那一层**必须跟着主题走** ----
//
// 钉的是"颜色有没有被写死"：表头用 `var(--bg)`、正文用 `var(--text-soft)`，换主题时
// 它们得跟着变。若哪天有人给表头写了白底黑字，浅色下看着挺好、深色下就是一块白板，
// 而别的断言全是绿的——所以这一条单独量。
await page.evaluate(`document.querySelector(".sider .log-entry").click()`);
await new Promise((r) => setTimeout(r, 600));

const darkLog = await page.evaluate(`(() => {
  const lum = (s) => {
    const [r, g, b] = s.match(/\\d+/g).map(Number).map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const body = getComputedStyle(document.body);
  const head = document.querySelector(".logview-list .ln-head");
  const msg = document.querySelector(".logview-list .ln:not(.ln-head) .msg");
  return {
    headBg: head ? getComputedStyle(head).backgroundColor : "",
    bodyBg: body.backgroundColor,
    msgLum: msg ? lum(getComputedStyle(msg).color) : 0,
    rows: document.querySelectorAll(".logview-list .ln:not(.ln-head)").length,
  };
})()`);
expect(
  "深色下的日志页：表头那一层跟着主题（不是写死的白底），正文仍是亮的",
  darkLog.headBg === darkLog.bodyBg && darkLog.msgLum > 0.5 && darkLog.rows >= 1,
  `表头底=${darkLog.headBg} / 页面底=${darkLog.bodyBg} / 正文亮度=${darkLog.msgLum.toFixed(3)} / ${darkLog.rows} 行`
);
await page.screenshot(path.join(outDir, "13-logview-dark.png"));

if (page.problems.length) {
  console.log("\n运行期报错：");
  for (const p of page.problems) console.log("  - " + p);
}

await page.close();

const failed = checks.filter((c) => !c).length;
console.log(`\n截图输出：${outDir}`);
console.log(
  failed || page.problems.length
    ? `界面自检未通过：${failed} 项断言失败，${page.problems.length} 条报错。`
    : "界面自检全通过。"
);
process.exit(failed || page.problems.length ? 1 : 0);
