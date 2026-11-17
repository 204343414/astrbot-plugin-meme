/* 在 Node 里把 app.js 真跑一遍（假 DOM + 假桥接层）。
 *
 * 目的不是"渲染得好看"，而是抓运行期崩溃：元素取不到、函数没定义、
 * payload 结构对不上时抛 TypeError —— 这些在浏览器里只表现为"按钮没反应"。
 *
 * 用法：node tests/page_harness.mjs <页面目录> <state.json 路径>
 * 退出码非 0 表示页面脚本抛错。
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";

const [pageDir, statePath] = process.argv.slice(2);
const html = readFileSync(path.join(pageDir, "index.html"), "utf8");
const source = readFileSync(path.join(pageDir, "app.js"), "utf8");
const state = JSON.parse(readFileSync(statePath, "utf8"));

const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

function makeNode(id) {
  const classes = new Set();
  const node = {
    id,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    title: "",
    dataset: {},
    style: {},
    listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? (classes.delete(c), false) : (classes.add(c), true)),
    },
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    },
    dispatch(type, event = {}) {
      const handlers = this.listeners[type] || [];
      if (!handlers.length) throw new Error(`#${id} 上没有 ${type} 事件处理器`);
      for (const handler of handlers) handler({ target: this, ...event });
    },
    querySelectorAll: () => [],
    querySelector: () => null,
    closest: () => null,
    scrollIntoView: () => {},
    click: () => {},
    appendChild: () => {},
    remove: () => {},
  };
  if (id === "settings" || id === "bans" || id === "modal" || id === "confirm") {
    classes.add("hidden");
  }
  return node;
}

const nodes = new Map([...ids].map((id) => [id, makeNode(id)]));
const errors = [];

const bridge = {
  ready: async () => {},
  apiGet: async (endpoint) => {
    if (endpoint === "ui/state") return { status: "ok", data: state };
    if (endpoint === "ui/options") return { status: "ok", data: { skipReview: true } };
    if (endpoint === "ui/config") {
      return { status: "ok", data: { fields: [], values: {}, flags: [], readonly: {} } };
    }
    return { status: "ok", data: {} };
  },
  apiPost: async () => ({ status: "ok", data: {} }),
  upload: async () => ({ status: "ok", data: {} }),
  download: async () => {},
};

const sandbox = {
  window: {
    AstrBotPluginPage: bridge,
    AstrBotPluginView: bridge,
    location: { origin: "http://x" },
    addEventListener: () => {},
    removeEventListener: () => {},
    parent: null,
  },
  document: {
    getElementById: (id) => nodes.get(id) || null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { innerHTML: "" },
  },
  console: {
    log: () => {},
    warn: (msg) => errors.push(`warn: ${msg}`),
    error: (...args) =>
      errors.push(
        "error: " +
          args
            .map((arg) => (arg instanceof Error ? `${arg.message}\n${arg.stack}` : String(arg)))
            .join(" "),
      ),
  },
  setTimeout,
  clearTimeout,
  Date,
  Math,
  JSON,
  URL,
  fetch: async () => ({ ok: true, json: async () => ({}), blob: async () => new Blob() }),
};
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox);
try {
  new vm.Script(source, { filename: "app.js" }).runInContext(context);
} catch (error) {
  console.error(`页面脚本一加载就报错：${error.message}`);
  process.exit(1);
}

// 页面里 boot() 是自动跑的；等它把 promise 链走完
await new Promise((resolve) => setTimeout(resolve, 300));

const fatal = errors.filter((line) => line.startsWith("error"));
if (fatal.length) {
  console.error("页面运行期报错：");
  for (const line of fatal) console.error("  " + line);
  process.exit(1);
}

const report = {
  statAuto: nodes.get("statAuto")?.textContent ?? "",
  autoSummary: nodes.get("autoSummary")?.innerHTML ?? "",
  autoGroups: nodes.get("autoGroups")?.innerHTML ?? "",
  subtitle: nodes.get("subtitle")?.textContent ?? "",
  statMode: nodes.get("statMode")?.textContent ?? "",
};
console.log(JSON.stringify(report, null, 2));

if (!report.autoSummary.includes("个群订阅中")) {
  console.error("自动推送摘要没渲染出来");
  process.exit(1);
}
if (state.auto.subscribed.length && !report.autoGroups.includes(state.auto.subscribed[0].group)) {
  console.error("订阅的群没出现在列表里");
  process.exit(1);
}
// 调参按钮必须真的挂上了处理器（这次线上事故就是它没挂上）
try {
  nodes.get("toggleSettings").dispatch("click");
} catch (error) {
  console.error(`调参按钮没反应：${error.message}`);
  process.exit(1);
}
if (nodes.get("settings").classList.contains("hidden")) {
  console.error("点了调参，参数区还是隐藏的");
  process.exit(1);
}
