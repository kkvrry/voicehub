/* 浏览器驱动：CDP 直连 + 常用操作。给 `smoke.mjs` 与 `shot.mjs` 共用。
 *
 * 为什么不各自写一份：CDP 那一套（找浏览器、建标签、`evaluate` 带超时、
 * 等字样、关标签）有**五处**不显然的坑——重复一遍就是重复踩坑：
 *
 *   1. **导航不能等它返回。** ``Page.navigate`` 等的是"加载完成"，而本应用开着
 *      一条 SSE 长连接，那件事永远不发生（同理别用 ``--virtual-time-budget``，
 *      实测挂两分钟才被杀）。所以发出去就不等，改为自己轮询。
 *   2. **建标签的接口带查询参数不导航。** 那个查询参数实测被忽略，拿到的是
 *      about:blank；正确做法是新建空白标签再 ``Page.navigate``。
 *   3. **第一条 evaluate 之前要留落地时间。** 文档刚换、渲染进程还在接管时立刻
 *      evaluate 会等到超时。
 *   4. **等元素默认等"可见"。** 桌面视口下抽屉里的按钮是 ``display:none``，
 *      用"等可见"必然超时。要 ``state: "attached"`` 或直接轮询 innerText。
 *   5. **必须关掉自己开的标签。** 每个标签挂着一条 SSE 长连接，攒着不关会占满
 *      服务端连接，然后"第三次跑突然卡住"——实测就是这样查出来的。
 *   6. **无头模式下 ``el.focus()`` 不派发 focus 事件。** 页面上
 *      ``document.hasFocus()`` 是 ``false``，于是 ``focus()`` 只把
 *      ``activeElement`` 设过去、**不触发监听器**——而 ``@focus`` 正是"密钥框里
 *      的掩码让位"这类逻辑的挂钩点。症状是"代码明明写了 focus 处理器，点了没
 *      反应"。要真点击得走 ``Input.dispatchMouseEvent``（:func:`clickAt`）。
 *
 * 零第三方依赖：Node 自带 fetch 与 WebSocket。
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

//: `clickText` 的默认候选：**可点的文字**都在这些元素里。
//: 注意里面没有 `summary` —— <details> 折叠标题要显式给选择器。
const DEFAULT_CLICK_SELECTOR = ".step, button, .tab, .tag, label";

//: 常见位置的 Chrome/Edge。``CHROME_PATH`` 可覆盖。
const CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);

/** 找到本机浏览器；找不到就抛，并说清怎么指定。 */
export function findBrowser() {
  const bin = CANDIDATES.find((p) => existsSync(p));
  if (!bin) {
    throw new Error("找不到 Chrome/Edge。用 CHROME_PATH 指定一个。");
  }
  return bin;
}

/** 已开着的调试端口（`/json/version` 有响应就算数）。 */
async function portAlive(port) {
  try {
    await fetch(`http://127.0.0.1:${port}/json/version`);
    return true;
  } catch {
    return false;
  }
}

/**
 * 连上一个页面。
 *
 * :param url: 要打开的地址。
 * :param opts.launch: 为 true 就自己起一个 headless 浏览器。
 * :param opts.port: 调试端口（默认 9222）。
 * :param opts.viewport: ``{width, height}``，默认 1440x960。**桌面视口下抽屉里的
 *   按钮不可见**，要验抽屉内容就得给小视口。
 * :param opts.profile: 浏览器临时 profile 目录名。
 */
export async function openPage(url, opts = {}) {
  const { launch = false, port = 9222, viewport, profile = "cdp-profile" } = opts;
  const browserBin = findBrowser();
  let child = null;

  if (launch || !(await portAlive(port))) {
    child = spawn(
      browserBin,
      [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        // **自动播放不设门槛**：`el.play()` 在没有用户手势时会被拦下（NotAllowedError），
        // 而这里所有"点击"都是 `el.click()` 派发的、不算手势——不关掉它，播放条
        // 那几条断言永远停在 00:00，看起来像"播放条没接上"。
        "--autoplay-policy=no-user-gesture-required",
        `--user-data-dir=${path.join(tmpdir(), profile)}`,
        `--remote-debugging-port=${port}`,
        ...(viewport ? [`--window-size=${viewport.width},${viewport.height}`] : []),
        "about:blank",
      ],
      { stdio: "ignore" }
    );
    for (let i = 0; i < 20; i += 1) {
      if (await portAlive(port)) break;
      await sleep(500);
    }
  }

  const tab = await (
    await fetch(`http://127.0.0.1:${port}/json/new`, { method: "PUT" })
  ).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = rej;
  });

  let seq = 0;
  const waiting = new Map();
  /** 收集到的运行期报错（未捕获异常 / console.error / error 级日志）。 */
  const problems = [];
  /** CDP 事件订阅（`Page.screencastFrame` 等）。 */
  const eventListeners = new Set();

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && waiting.has(msg.id)) {
      const { resolve, reject } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      return;
    }
    if (msg.method) {
      for (const listener of eventListeners) {
        try {
          listener(msg);
        } catch {
          // 订阅者自己的错误不能把驱动打停
        }
      }
    }
    const d = msg.params?.exceptionDetails;
    if (msg.method === "Runtime.exceptionThrown") {
      problems.push(`未捕获异常：${d.exception?.description ?? d.text}`);
    }
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      problems.push(
        `console.error：${msg.params.args.map((a) => a.value ?? a.description ?? a.type).join(" ")}`
      );
    }
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      // 带上 url：只看得懂"400 (Bad Request)"就没法把**故意跑出来的**那条报错
      // 与真的问题分开，于是自检只能要么全放行、要么全部算失败。
      const where = msg.params.entry.url ? ` ${msg.params.entry.url}` : "";
      problems.push(`日志错误：${msg.params.entry.text}${where}`);
    }
  };

  //: 每一步都有超时。CDP 偶尔会因为渲染进程忙而迟迟不回；挂住与失败一样没用，
  //: 只是更浪费你的时间。
  const send = (method, params = {}, timeoutMs = 15000) =>
    new Promise((resolve, reject) => {
      const id = ++seq;
      const timer = setTimeout(() => {
        waiting.delete(id);
        reject(new Error(`CDP 没回：${method}（等了 ${timeoutMs / 1000}s）`));
      }, timeoutMs);
      waiting.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject,
      });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const evaluate = async (expression) => {
    const r = await send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      throw new Error(
        r.exceptionDetails.exception?.description ?? r.exceptionDetails.text
      );
    }
    return r.result.value;
  };

  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  // **缓存关掉**（在下面 navigate 之前）。这个无头浏览器用的是固定 profile
  // （`cdp-profile`，见上面 `--user-data-dir`），所以它**留着上一次运行的 HTTP 缓存**：
  // 重建前端之后（`npm run build` 换成新的 `index-<hash>.js`），它仍然可能拿缓存里
  // 那份 `index.html`，于是**加载的是上一版产物**。症状是最难受的一种——
  // 脚本照常跑完、断言照常全绿，验的却是**一段已经不存在的代码**（真实撞到过：
  // 新加的那段界面元素在页面上怎么都找不到，而它明明就在刚构建的包里）。
  // 和"主题/筛选不靠上次残留"是同一条纪律，只是这一条在 HTTP 层。
  await send("Network.enable");
  await send("Network.setCacheDisabled", { cacheDisabled: true });
  if (viewport) {
    await send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
  }
  send("Page.navigate", { url }).catch(() => {});
  await sleep(2500);

  const api = {
    send,
    evaluate,
    problems,

    /** 订阅 CDP 事件；返回取消订阅函数。 */
    onEvent(listener) {
      eventListeners.add(listener);
      return () => eventListeners.delete(listener);
    },

    /** 页面上有没有这段文字。 */
    async has(text) {
      return evaluate(
        `!!document.querySelector("#app") && document.querySelector("#app").innerText.includes(${JSON.stringify(text)})`
      );
    },

    /**
     * 轮询等某段文字出现。数据是异步拉来的，所以要轮询而不是"等固定时长再判死刑"——
     * 后者会把"慢一点"误报成"坏了"。
     */
    async waitText(text, { tries = 20, gap = 400 } = {}) {
      for (let i = 0; i < tries; i += 1) {
        if (await api.has(text)) return true;
        await sleep(gap);
      }
      return false;
    },

    /**
     * 点一个含指定文字的元素，找不到就返回 false（**不抛**）。
     *
     * 命中的元素可能不可见（比如桌面视口下抽屉里的按钮），所以这里
     * **不做可见性判断**——"点了但没反应"由调用方的 `waitText` 去判断，
     * 这比在这里抛"元素不可见"有用得多。
     *
     * `selector` 只在**一个元素都没匹配上**时才退回默认名单：调用方可能是
     * 把正文里的 `@`（邮箱之类）误当成了选择器后缀。但要是选择器匹配到了
     * 元素、只是没有那条文字，就老实返回 false——悄悄去别处点一下，
     * "点了但没反应"就再也说不清了。
     */
    async clickText(text, selector = DEFAULT_CLICK_SELECTOR) {
      return evaluate(`(() => {
        const want = ${JSON.stringify(text)};
        const hitIn = (sel) => [...document.querySelectorAll(sel)]
          .find((el) => (el.textContent || "").includes(want));
        let sel = ${JSON.stringify(selector)};
        let hit = hitIn(sel);
        if (!hit && !document.querySelectorAll(sel).length) {
          sel = ${JSON.stringify(DEFAULT_CLICK_SELECTOR)};
          hit = hitIn(sel);
        }
        if (!hit) return false;
        hit.click();
        return true;
      })()`);
    },

    /**
     * 真点击某个元素（按选择器），走 ``Input.dispatchMouseEvent``。
     *
     * **为什么不用 ``el.click()`` 也不用 ``el.focus()``**：两者都不派发
     * ``focus`` 事件（见本文件顶部第六条坑），于是任何挂在 ``@focus`` 上的逻辑
     * 在这里验不出来——而"点进密钥框时掩码让位"恰好就是那种逻辑。第一条真实点击
     * 验出来的让位，与 dispatch 出来的 ``FocusEvent`` 结果一致。
     *
     * 返回 false 表示**选择器没命中**（元素不存在或不可见）。
     */
    async clickAt(selector) {
      const box = await evaluate(`(() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      })()`);
      if (!box) return false;
      for (const type of ["mousePressed", "mouseReleased"]) {
        await send("Input.dispatchMouseEvent", {
          type,
          x: box.x,
          y: box.y,
          button: "left",
          clickCount: 1,
        });
      }
      return true;
    },

    /** 等应用挂上，返回顶栏那段文字。 */
    async waitMount({ tries = 40, gap = 500 } = {}) {
      const started = Date.now();
      for (let i = 0; i < tries; i += 1) {
        await sleep(gap);
        const text = await evaluate(
          `(() => { const el = document.querySelector("#app"); return el ? (el.innerText || "").replace(/\\s+/g," ").trim() : ""; })()`
        );
        if (text) return { text, seconds: (Date.now() - started) / 1000 };
      }
      return null;
    },

    /** 截图落盘，返回文件路径。 */
    async screenshot(file) {
      const shot = await send("Page.captureScreenshot", { format: "png" }, 20000);
      const { writeFileSync } = await import("node:fs");
      writeFileSync(file, Buffer.from(shot.data, "base64"));
      return file;
    },

    /** 关掉自己开的标签并断开。**必须调用**，否则 SSE 连接会堆着。 */
    async close() {
      await send("Page.close").catch(() => {});
      ws.close();
      if (child) child.kill();
    },
  };

  return api;
}
