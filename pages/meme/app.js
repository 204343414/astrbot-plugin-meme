/* 梗图库页面。
 *
 * 一切后端访问都走桥接层（window.AstrBotPluginPage）：页面是嵌在面板里的
 * iframe，直连后端会被令牌拦掉，桥接层帮我们把请求带上作用域令牌。
 */
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
const el = (id) => document.getElementById(id);

/* 挂事件必须走这里。
 *
 * 以前这里是一串「el(某 id).addEventListener」：只要有一个 id 在 HTML 里
 * 不存在（比如「可见范围」下拉框删了、代码还留着），第一行就抛 TypeError，
 * **后面所有按钮全都挂不上** —— 症状是「调参点了没反应、统计永远显示 —」，
 * 而且控制台只看得到一句 TypeError，极难往「前面某个元素没了」上想。
 * 现在：缺元素只跳过它自己，并且明确进 console.warn。
 */
function on(id, type, handler) {
  const node = el(id);
  if (!node) {
    console.warn(`[meme page] 跳过 ${id} 的 ${type}：页面上没有这个元素`);
    return null;
  }
  node.addEventListener(type, handler);
  return node;
}

const state = {
  origin: "all",
  q: "",
  sort: "new",
  status: "all",
  limit: 24,
  offset: 0,
  cards: [],
  hasMore: false,
  settings: null,
};

const ORIGIN_ORDER = ["all", "submitted", "api", "generated"];
const ORIGIN_FALLBACK = { all: "全部", submitted: "投稿", api: "接口", generated: "AI 现画" };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/* 桥接层不同版本会直接返回 JSON、包一层 data，或包两层 —— 统一拆开。 */
function unwrap(response) {
  let value = response;
  for (let depth = 0; depth < 3; depth += 1) {
    if (!value || typeof value !== "object") break;
    if (value.status === "error") throw new Error(value.message || "接口返回错误");
    if (value.ok === false) throw new Error(value.message || "接口返回错误");
    if (Object.prototype.hasOwnProperty.call(value, "data") && Object.keys(value).length <= 2) {
      value = value.data;
      continue;
    }
    break;
  }
  return value;
}

async function apiGet(name, params) {
  if (!bridge?.apiGet) throw new Error("桥接层不可用（AstrBotPluginPage.apiGet 缺失）");
  return unwrap(await bridge.apiGet(name, params || {}));
}

async function apiPost(name, body) {
  if (!bridge?.apiPost) throw new Error("桥接层不可用（AstrBotPluginPage.apiPost 缺失）");
  return unwrap(await bridge.apiPost(name, body || {}));
}

function fmtTime(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

/* ---------------- 数据加载 ---------------- */

async function loadState({ append = false } = {}) {
  const offset = append ? state.offset + state.limit : 0;
  const payload = await apiGet("ui/state", {
    origin: state.origin === "all" ? "" : state.origin,
    q: state.q,
    sort: state.sort,
    status: state.status === "all" ? "" : state.status,
    limit: state.limit,
    offset,
  });

  state.offset = payload.offset;
  state.hasMore = Boolean(payload.hasMore);
  state.cards = append ? state.cards.concat(payload.cards) : payload.cards;

  el("statActive").textContent = payload.activeTotal;
  el("statTotal").textContent = payload.libraryTotal;
  el("statRejected").textContent = payload.rejected;
  // 顶栏按钮上的数字：不点进去也知道有多少条等着人工过
  if (el("rejectedCount")) {
    el("rejectedCount").textContent = payload.rejected ? String(payload.rejected) : "";
  }
  if (el("statHosted")) {
    // 图床覆盖：已搬 / 全库。数不上来说明图床没配或 Hub 太老，显示 —。
    const hosted = payload.hosted || null;
    el("statHosted").textContent = hosted
      ? `${hosted.covered ?? 0}/${hosted.active ?? 0}`
      : "—";
    el("statHosted").title = hosted
      ? `已搬到图床 ${hosted.covered ?? 0} 张，全库 ${hosted.active ?? 0} 张`
      : "图床未启用";
    if (el("hostCount")) {
      const left = hosted ? (hosted.active ?? 0) - (hosted.covered ?? 0) : 0;
      el("hostCount").textContent = left > 0 ? String(left) : "";
    }
  }
  if (el("statAuto")) {
    const auto = payload.auto || {};
    const pcts = auto.pcts || {};
    el("statAuto").textContent = auto.enabled
      ? `${auto.groups || 0} 群 · ${pcts.low ?? 10}/${pcts.mid ?? 25}/${pcts.high ?? 50}%`
      : "已关（总开关）";
    // 悬停能看到**是哪几个群**订阅了、什么档、今天发了几条 —— 只看一个数字
    // 查不出「为什么这个群不推」（比如它其实被自动关掉了）。
    const subs = Array.isArray(auto.subscribed) ? auto.subscribed : [];
    const offed = Array.isArray(auto.offed) ? auto.offed : [];
    const lines = [
      `每小时概率：低 ${pcts.low ?? 10}% / 中 ${pcts.mid ?? 25}% / 高 ${pcts.high ?? 50}%`,
      `每群每天上限 ${auto.maxPerDay || "不限"}；免打扰 ${auto.quiet || "不设"}`,
    ];
    if (subs.length) {
      lines.push("已订阅的群：");
      subs.forEach((s) => {
        const level = { low: "低", mid: "中", high: "高" }[s.level] || s.level || "?";
        const blocked = s.blockedUntil * 1000 > Date.now() ? "（冷却中）" : "";
        lines.push(`· ${s.group}：${level} ${s.pct}%/小时，今天 ${s.sentToday} 条${blocked}`);
      });
    } else {
      lines.push("还没有群订阅自动推送。");
    }
    if (offed.length) {
      lines.push(`被自动关掉的群 ${offed.length} 个（没权限/已不在群里）：`);
      offed.forEach((s) => {
        lines.push(`· ${String(s.umo).slice(-8)}：${s.reason === "gone" ? "机器人不在群里" : "群没开主动消息"}`);
      });
    }
    el("statAuto").title = lines.join("\n");
  }
  renderAuto(payload.auto || {});
  el("statMode").textContent = payload.running?.captionMode || "—";
  el("statProvider").textContent = payload.running?.moderationProvider || "未配";
  el("statProvider").title = payload.running?.moderationProvider || "";
  el("subtitle").textContent =
    `数据目录 ${payload.running?.dataDir || "—"}` +
    (payload.running?.proxy ? ` · 代理 ${payload.running.proxy}` : "");

  renderChips(payload.origins || []);
  renderGrid();
  renderBans(payload.blacklist || []);
  renderCardInfo(payload.card || {});
  syncLibraryMore();
}

/* 图库的「加载更多」什么时候该出现：有下一页 **且** 拒收页签没打开。
 * 两处都会改这个按钮的可见性（加载图库、切页签），各算各的就会互相覆盖 ——
 * 这个函数是唯一的答案。 */
function syncLibraryMore() {
  const open = el("rejected") ? !el("rejected").classList.contains("hidden") : false;
  el("loadMore")?.classList.toggle("hidden", open || !state.hasMore);
}

function renderAuto(auto) {
  const box = el("autoGroups");
  const summary = el("autoSummary");
  if (!box || !summary) return;
  const pcts = auto.pcts || {};
  const subs = Array.isArray(auto.subscribed) ? auto.subscribed : [];
  const offed = Array.isArray(auto.offed) ? auto.offed : [];
  const LEVEL = { low: "低", mid: "中", high: "高" };

  summary.innerHTML =
    (auto.enabled ? "总开关：开" : "<b>总开关：关</b>") +
    ` · 三档 低 ${pcts.low ?? 10}% / 中 ${pcts.mid ?? 25}% / 高 ${pcts.high ?? 50}%` +
    ` · 每群每天上限 ${auto.maxPerDay || "不限"}` +
    ` · 免打扰 ${auto.quiet || "不设"}` +
    ` · <b>${subs.length} 个群订阅中</b>`;

  if (!subs.length) {
    box.innerHTML = '<div class="msg">还没有群订阅自动推送。在群里发 /meme，点卡片上的「自动订阅」即可。</div>';
  } else {
    box.innerHTML = subs
      .map((row) => {
        const level = LEVEL[row.level] || row.level || "?";
        const cooling = row.blockedUntil * 1000 > Date.now();
        return `<div class="auto-row">
          <span class="auto-group">群 ${escapeHtml(row.group)}</span>
          <span class="auto-level">${level}档 ${row.pct}%/小时</span>
          <span class="auto-count">今天 ${row.sentToday} 条</span>
          ${cooling ? '<span class="auto-flag">冷却中</span>' : ""}
        </div>`;
      })
      .join("");
  }

  const offedBox = el("autoOffed");
  if (offedBox) {
    offedBox.innerHTML = offed.length
      ? "被自动关掉的群：" +
        offed
          .map(
            (row) =>
              `${escapeHtml(String(row.umo).slice(-8))}（${
                row.reason === "gone" ? "机器人不在群里" : "群没开主动消息"
              }）`
          )
          .join("、")
      : "";
  }
}

function renderBans(entries) {
  const box = el("banList");
  if (!entries.length) {
    box.innerHTML = '<div class="msg">还没有人被禁止投稿。</div>';
    return;
  }
  box.innerHTML = entries
    .map(
      (entry) => `
      <div class="ban-row">
        <span class="who">${escapeHtml(entry.sender_id)}</span>
        <span class="why">${escapeHtml(entry.reason || "未写理由")} · 累计 ${entry.hits || 1} 次</span>
        <button class="ghost" data-unban="${escapeHtml(entry.sender_id)}">解禁</button>
      </div>`
    )
    .join("");
}

function renderCardInfo(card) {
  if (!card || !Object.keys(card).length) {
    el("cardInfo").textContent = "";
    return;
  }
  el("cardInfo").textContent = card.enabled
    ? `卡片已开（按钮：${card.showAgain ? "再来一张 " : ""}${card.showHowto ? "我要投稿" : ""}）· ` +
      `素材图 ${card.materials}/${card.materialKeep} 张`
    : "卡片已关（发的是纯图片）";
}

function renderChips(origins) {
  const counts = {};
  origins.forEach((entry) => { counts[entry.origin] = entry.count; });
  const total = origins.reduce((sum, entry) => sum + entry.count, 0);
  counts.all = total;
  el("originChips").innerHTML = ORIGIN_ORDER.map((key) => {
    const label = ORIGIN_FALLBACK[key] || key;
    const count = counts[key];
    if (key !== "all" && !count) return "";
    return `<button class="chip ${state.origin === key ? "on" : ""}" data-origin="${key}">${label}${count ? ` ${count}` : ""}</button>`;
  }).join("");
}

function renderGrid() {
  const grid = el("grid");
  if (!state.cards.length) {
    grid.innerHTML = `<div class="empty">还没有符合条件的图。<br />把图片拖进上面的框里就能进来。</div>`;
    return;
  }
  grid.innerHTML = state.cards.map((card) => {
    const badges = [
      `<span class="badge">${escapeHtml(card.originLabel)}</span>`,
      card.nsfwLevel > 0 ? `<span class="badge warn">NSFW ${card.nsfwLevel}</span>` : "",
      card.active ? "" : '<span class="badge off">已停用</span>',
      card.hasFile ? "" : '<span class="badge warn">文件缺失</span>',
    ].join("");
    const caption = card.captionZh || card.caption || "";
    return `
      <article class="card ${card.active ? "" : "disabled"}" data-id="${card.id}">
        <div class="card-thumb" data-act="open">
          ${card.thumb ? `<img src="${card.thumb}" alt="" loading="lazy" />` : ""}
          <div class="badges">${badges}</div>
        </div>
        <div class="card-body">
          <div class="card-caption">${caption ? escapeHtml(caption) : "<em>（无图注）</em>"}</div>
          <div class="card-meta">
            <span>#${card.id}</span>
            <span>发过 ${card.usedCount}</span>
          </div>
          <div class="card-actions">
            <button class="ghost" data-act="open">大图</button>
            <button class="ghost" data-act="toggle">${card.active ? "停用" : "启用"}</button>
            <button class="danger" data-act="delete">删除</button>
          </div>
        </div>
      </article>`;
  }).join("");
}

/* ---------------- 上传 ---------------- */

/* 页面跑在面板的沙盒 iframe 里：sandbox="allow-scripts allow-forms
 * allow-downloads" —— 没有 allow-modals。所以 window.confirm / alert / prompt
 * 全部是静默失效（confirm 直接返回 false，点「确定」什么也不会发生）。
 * 「点删除没反应」就是栽在这里，这里改成页面内的确认框。 */
let confirmResolve = null;

function ask(text, { okText = "确定", cancelText = "取消" } = {}) {
  const box = el("confirm");
  if (!box) return Promise.resolve(false);
  el("confirmText").textContent = text;
  el("confirmOk").textContent = okText;
  el("confirmCancel").textContent = cancelText;
  box.classList.remove("hidden");
  return new Promise((resolve) => { confirmResolve = resolve; });
}

function closeConfirm(answer) {
  el("confirm").classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(answer);
}

async function pushUploadOptions() {
  try {
    await apiPost("ui/options", {
      skip_review: el("optSkipReview").checked,
    });
  } catch (error) {
    logUpload(`设置选项失败：${error.message}`, true);
  }
}

function logUpload(text, bad = false) {
  const line = document.createElement("div");
  line.className = bad ? "bad" : "ok";
  line.textContent = text;
  el("uploadLog").prepend(line);
  while (el("uploadLog").childElementCount > 12) el("uploadLog").lastChild.remove();
}

function setProgress(text) {
  const bar = el("uploadProgress");
  if (!bar) return;
  bar.textContent = text || "";
  bar.classList.toggle("hidden", !text);
}

/* 从一次拖拽里把「能当图片的东西」抠出来。
 * 三种情况都要接：
 *  1. 从文件管理器拖：dataTransfer.files 有 File；
 *  2. 某些浏览器只给 items（Chrome 拖文件夹/虚拟文件时 files 是空的）；
 *  3. 从网页/聊天窗口拖图片：文件拿不到，只有 URL —— 交给服务端去下。 */
function collectDrop(dataTransfer) {
  const files = [];
  const urls = [];
  if (!dataTransfer) return { files, urls };

  Array.from(dataTransfer.files || []).forEach((file) => {
    if (file && (!file.type || file.type.startsWith("image/"))) files.push(file);
  });
  if (!files.length) {
    Array.from(dataTransfer.items || []).forEach((item) => {
      if (item.kind !== "file") return;
      const file = item.getAsFile?.();
      if (file && (!file.type || file.type.startsWith("image/"))) files.push(file);
    });
  }

  const uriList = dataTransfer.getData?.("text/uri-list") || "";
  const plain = dataTransfer.getData?.("text/plain") || "";
  const html = dataTransfer.getData?.("text/html") || "";
  const candidates = [
    ...uriList.split(/\r?\n/),
    ...plain.split(/\s+/),
    ...Array.from(html.matchAll(/<img[^>]+src=["']([^"']+)["']/gi)).map((m) => m[1]),
  ];
  candidates.forEach((value) => {
    const url = (value || "").trim();
    if (/^https?:\/\//i.test(url) && !urls.includes(url)) urls.push(url);
  });

  return { files, urls };
}

async function importUrls(urls) {
  const skip = el("optSkipReview").checked;
  let done = 0;
  for (const url of urls) {
    done += 1;
    setProgress(`正在让服务端下载 ${done}/${urls.length}：${url.slice(0, 60)}…`);
    try {
      const data = await apiPost("ui/fetch_url", {
        url,
        skip_review: skip,
      }) || {};
      const id = data.meme?.id ? `#${data.meme.id}` : "";
      if (data.ok) logUpload(`${url.slice(0, 60)} → ${id || "已入库"}${data.duplicate ? " · 库里已有" : ""}`);
      else logUpload(`${url.slice(0, 60)} → 没收：${data.message || "未知原因"}`, true);
    } catch (error) {
      logUpload(`${url.slice(0, 60)} → 下载入库失败：${error.message}`, true);
    }
  }
  setProgress("");
}

async function uploadFiles(files) {
  const list = Array.from(files || []).filter((file) => !file.type || file.type.startsWith("image/"));
  if (!list.length) return;
  const skip = el("optSkipReview").checked;
  let done = 0;
  for (const file of list) {
    done += 1;
    setProgress(`正在上传 ${done}/${list.length}：${file.name}`);
    try {
      const result = await bridge.upload("ui/upload", file);
      const data = unwrap(result) || {};
      const id = data.meme?.id ? `#${data.meme.id}` : "";
      if (data.ok) {
        logUpload(`${file.name} → ${id || "已入库"}${skip ? "（免审）" : ""}${data.duplicate ? " · 库里已有" : ""}`);
      } else {
        logUpload(`${file.name} → 没收：${data.message || "未知原因"}`, true);
      }
    } catch (error) {
      logUpload(`${file.name} → 上传失败：${error.message}`, true);
    }
  }
  setProgress("");
  await loadState();
}

async function handleDrop(dataTransfer) {
  const { files, urls } = collectDrop(dataTransfer);
  if (!files.length && !urls.length) {
    logUpload(
      "这次拖拽里既没有图片文件、也没有图片网址。从网页/聊天窗口拖图时如果浏览器没给网址，" +
      "先把图存到本地再拖，或者用上面的「点这里选文件 / 整个文件夹批量导入」。",
      true,
    );
    return;
  }
  if (files.length) {
    logUpload(`收到 ${files.length} 个文件`);
    await uploadFiles(files);
  }
  if (urls.length) {
    logUpload(`收到 ${urls.length} 个网址（服务端去下载）`);
    await importUrls(urls);
    await loadState();
  }
}

/* ---------------- 单张操作 ---------------- */

async function openDetail(id) {
  const detail = await apiGet("ui/detail", { id });
  if (!detail) return;
  el("modalImage").src = detail.image || "";
  el("modalInfo").innerHTML = `
    <span>编号 #${detail.id}</span>
    <span>来源 ${escapeHtml(detail.origin)}</span>
    <span>状态 ${detail.status === "active" ? "在用" : "已停用"}</span>
    <span>NSFW ${detail.nsfwLevel}</span>
    <span>发过 ${detail.usedCount} 次</span>
    <span>投稿人 ${escapeHtml(detail.contributor || "—")}</span>
    <span>入库 ${fmtTime(detail.createdAt)}</span>
    <span>上次发送 ${fmtTime(detail.usedAt)}</span>
    <span style="grid-column:1/-1">图注 ${escapeHtml(detail.caption || "—")}</span>
    <span style="grid-column:1/-1">中文 ${escapeHtml(detail.captionZh || "—")}</span>
    <span style="grid-column:1/-1">路径 ${escapeHtml(detail.filePath || "—")}</span>`;
  el("modalActions").innerHTML = `
    <button id="modalRecheck" class="ghost">重新审核</button>
    <button id="modalToggle" class="ghost">${detail.status === "active" ? "停用" : "启用"}</button>
    <button id="modalDelete" class="danger">删除</button>`;
  el("modal").classList.remove("hidden");
  el("modal").dataset.id = String(detail.id);
  el("modal").dataset.active = detail.status === "active" ? "1" : "0";
}

async function act(action, id) {
  try {
    if (action === "delete") {
      const yes = await ask(`确定下架 #${id}？图片文件也会一起删掉。`, { okText: "下架" });
      if (!yes) return;
      const result = await apiPost("ui/delete", { id });
      logUpload(result.message || "已删除");
    } else if (action === "toggle") {
      const active = el("modal")?.dataset.id === String(id)
        ? el("modal").dataset.active !== "1"
        : !(state.cards.find((card) => card.id === Number(id))?.active);
      const result = await apiPost("ui/toggle", { id, active });
      logUpload(result.message || "已切换");
    } else if (action === "recheck") {
      const result = await apiPost("ui/recheck", { id });
      logUpload(result.message || "已重审", !result.accepted);
    }
    el("modal").classList.add("hidden");
    await loadState();
  } catch (error) {
    logUpload(`操作失败：${error.message}`, true);
  }
}

/* ---------------- 调参 ---------------- */

async function loadSettings() {
  const payload = await apiGet("ui/config");
  state.settings = payload;
  const flags = payload.flags || [];
  const values = payload.values || {};

  el("settingsBody").innerHTML = (payload.fields || []).map((field) => {
    const value = values[field.key];
    if (field.kind === "flags") {
      const picked = new Set(value || []);
      const chips = flags.map((flag) =>
        `<span class="flag ${picked.has(flag.key) ? "on" : ""}" data-flag="${flag.key}">${escapeHtml(flag.label)}</span>`
      ).join("");
      return `<div class="field"><div class="field-head"><b>${escapeHtml(field.label)}</b></div>
        <div class="field-flags" data-key="${field.key}">${chips}</div>
        <small>${escapeHtml(field.hint || "")}</small></div>`;
    }
    if (field.kind === "bool") {
      return `<div class="field"><div class="field-head"><b>${escapeHtml(field.label)}</b></div>
        <label class="switch"><input type="checkbox" data-key="${field.key}" ${value ? "checked" : ""} /><span>${value ? "开" : "关"}</span></label>
        <small>${escapeHtml(field.hint || "")}</small></div>`;
    }
    if (field.kind === "choice") {
      const options = (field.choices || []).map((choice) =>
        `<option value="${choice}" ${choice === value ? "selected" : ""}>${choice}</option>`).join("");
      return `<div class="field"><div class="field-head"><b>${escapeHtml(field.label)}</b></div>
        <select data-key="${field.key}">${options}</select>
        <small>${escapeHtml(field.hint || "")}</small></div>`;
    }
    if (field.kind === "int" || field.kind === "float") {
      const step = field.kind === "int" ? 1 : 0.1;
      return `<div class="field"><div class="field-head"><b>${escapeHtml(field.label)}</b><span>${value ?? "—"}</span></div>
        <input type="range" data-key="${field.key}" min="${field.min}" max="${field.max}" step="${step}" value="${value ?? field.min}" />
        <small>${escapeHtml(field.hint || "")}</small></div>`;
    }
    return `<div class="field"><div class="field-head"><b>${escapeHtml(field.label)}</b></div>
      <input type="text" data-key="${field.key}" value="${escapeHtml(value ?? "")}" />
      <small>${escapeHtml(field.hint || "")}</small></div>`;
  }).join("");

  // 拖动滑块时把当前值显示出来
  el("settingsBody").querySelectorAll('input[type="range"]').forEach((input) => {
    input.addEventListener("input", () => {
      const head = input.closest(".field")?.querySelector(".field-head span");
      if (head) head.textContent = input.value;
    });
  });
  el("settingsBody").querySelectorAll(".flag").forEach((chip) => {
    chip.addEventListener("click", () => chip.classList.toggle("on"));
  });
  el("settingsBody").querySelectorAll(".switch input").forEach((input) => {
    input.addEventListener("change", () => {
      const span = input.closest(".switch")?.querySelector("span");
      if (span) span.textContent = input.checked ? "开" : "关";
    });
  });

  const readonly = payload.readonly || {};
  el("readonlyBox").innerHTML =
    "<b>这几项在插件配置页改</b>" +
    (payload.readonlyMeta || []).map((meta) =>
      `<span>${escapeHtml(meta.label)}：${escapeHtml(readonly[meta.key] || "（未设置）")}</span>`
    ).join("");
}

function collectSettings() {
  const values = {};
  el("settingsBody").querySelectorAll("[data-key]").forEach((node) => {
    const key = node.dataset.key;
    if (node.classList.contains("field-flags")) {
      values[key] = Array.from(node.querySelectorAll(".flag.on")).map((chip) => chip.dataset.flag);
    } else if (node.type === "checkbox") {
      values[key] = node.checked;
    } else if (node.type === "range") {
      values[key] = Number(node.value);
    } else {
      values[key] = node.value;
    }
  });
  return values;
}

async function saveSettings() {
  const msg = el("settingsMsg");
  msg.className = "msg";
  msg.textContent = "保存中…";
  try {
    const result = await apiPost("ui/config", { values: collectSettings() });
    const errors = result.errors || [];
    msg.textContent = (result.message || "已保存") + (errors.length ? `（${errors.join("；")}）` : "");
    msg.className = errors.length ? "msg bad" : "msg ok";
    await loadSettings();
  } catch (error) {
    msg.textContent = `保存失败：${error.message}`;
    msg.className = "msg bad";
  }
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  on("refresh", "click", () => loadState().catch(showError));
  on("loadMore", "click", () => loadState({ append: true }).catch(showError));

  on("originChips", "click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    state.origin = chip.dataset.origin;
    loadState().catch(showError);
  });

  let searchTimer = 0;
  on("search", "input", (event) => {
    state.q = event.target.value.trim();
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadState().catch(showError), 250);
  });
  on("sort", "change", (event) => {
    state.sort = event.target.value;
    loadState().catch(showError);
  });
  on("statusFilter", "change", (event) => {
    state.status = event.target.value;
    loadState().catch(showError);
  });
  on("grid", "click", (event) => {
    const card = event.target.closest(".card");
    if (!card) return;
    const action = event.target.closest("[data-act]")?.dataset.act || "open";
    const id = card.dataset.id;
    if (action === "open") openDetail(id).catch(showError);
    else act(action, id);
  });

  on("modalClose", "click", () => el("modal").classList.add("hidden"));
  on("modal", "click", (event) => {
    if (event.target === el("modal")) el("modal").classList.add("hidden");
  });
  on("modalActions", "click", (event) => {
    const id = el("modal").dataset.id;
    if (event.target.id === "modalRecheck") act("recheck", id);
    if (event.target.id === "modalToggle") act("toggle", id);
    if (event.target.id === "modalDelete") act("delete", id);
  });

  const zone = el("dropzone");
  const markHot = (on) => zone.classList.toggle("hot", on);
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      markHot(true);
    });
  });
  zone.addEventListener("dragleave", (event) => {
    // 拖到子元素上也会触发 dragleave，用 relatedTarget 排除这种假离开
    if (!zone.contains(event.relatedTarget)) markHot(false);
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    markHot(false);
    handleDrop(event.dataTransfer).catch((error) => logUpload(`处理拖拽失败：${error.message}`, true));
  });
  // 拖到框外面时别让浏览器直接把图打开（那样页面就没了）
  ["dragover", "drop"].forEach((name) => {
    window.addEventListener(name, (event) => {
      if (!zone.contains(event.target)) event.preventDefault();
    });
  });

  on("pickFiles", "click", () => el("fileInput").click());
  on("fileInput", "change", (event) => {
    uploadFiles(event.target.files);
    event.target.value = "";
  });
  on("pickFolder", "click", () => el("folderInput").click());
  on("folderInput", "change", (event) => {
    uploadFiles(event.target.files);
    event.target.value = "";
  });

  on("confirmOk", "click", () => closeConfirm(true));
  on("confirmCancel", "click", () => closeConfirm(false));
  on("confirm", "click", (event) => {
    if (event.target === el("confirm")) closeConfirm(false);
  });
  on("optSkipReview", "change", pushUploadOptions);

  on("hostLibrary", "click", () => hostLibrary().catch(showError));

  on("toggleBans", "click", () => {
    const box = el("bans");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) loadState().catch(showError);
  });
  on("banList", "click", async (event) => {
    const button = event.target.closest("[data-unban]");
    if (!button) return;
    const id = button.dataset.unban;
    const yes = await ask(`恢复 ${id} 的投稿权限？`, { okText: "解禁" });
    if (!yes) return;
    try {
      const result = await apiPost("ui/unban", { id });
      logUpload(result.message || "已解禁");
      await loadState();
    } catch (error) {
      logUpload(`解禁失败：${error.message}`, true);
    }
  });
  on("cardProbe", "click", async () => {
    el("probeMsg").textContent = "自检中…";
    el("probeMsg").className = "msg";
    try {
      const report = await apiPost("ui/card_probe", {});
      const checks = (report.checks || [])
        .map((check) => `${check.name}${check.ok ? "✔" : "✘"}（${check.detail}）`)
        .join("；");
      el("probeMsg").textContent = `${report.message}｜${checks}`;
      el("probeMsg").className = report.ok ? "msg ok" : "msg bad";
    } catch (error) {
      el("probeMsg").textContent = `自检失败：${error.message}`;
      el("probeMsg").className = "msg bad";
    }
  });

  on("toggleSettings", "click", () => {
    const box = el("settings");
    const open = box.classList.toggle("hidden") === false;
    // 按钮文字跟着变，否则「点了像没反应」—— 参数区在页面下方，很容易看不见
    const button = el("toggleSettings");
    if (button) button.textContent = open ? "收起调参" : "调参";
    if (open) {
      box.scrollIntoView({ behavior: "smooth", block: "start" });
      if (!state.settings) {
        loadSettings().catch((error) => {
          const msg = el("settingsMsg");
          if (msg) {
            msg.textContent = `读取参数失败：${error.message || error}`;
            msg.className = "msg bad";
          }
          showError(error);
        });
      }
    }
  });
  on("saveSettings", "click", saveSettings);
  on("resetSettings", "click", () => loadSettings().catch(showError));

  /* 拒收页签 */
  on("toggleRejected", "click", () => {
    const box = el("rejected");
    if (!box) return;
    const open = box.classList.toggle("hidden") === false;
    const button = el("toggleRejected");
    if (button) button.classList.toggle("on", open);
    // 图库主体让位，免得两套网格上下堆在一起看着像重复渲染
    el("grid")?.classList.toggle("hidden", open);
    syncLibraryMore();
    el("originChips")?.closest(".toolbar")?.classList.toggle("hidden", open);
    if (open) {
      box.scrollIntoView({ behavior: "smooth", block: "start" });
      if (!rejected.loaded) {
        rejected.loaded = true;
        loadRejected().catch(showError);
      }
    }
  });

  on("rejectedMore", "click", () => loadRejected({ append: true }).catch(showError));

  el("rejectedGrid")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    const card = event.target.closest(".rejected-card");
    if (!card) return;
    const sha = card.dataset.sha;
    const act = button?.dataset.act;
    if (act === "accept") acceptRejected(sha).catch(showError);
    else if (act === "drop") dropRejected(sha).catch(showError);
    else if (act === "open") openRejectedDetail(sha).catch(showError);
  });
}

/* ---------------- 拒收页 ----------------
 *
 * 审核模型判死的图，人工在这里过一遍。两条路：
 *   接收 → 不再走 AI 审核，直接入库（用户要的就是「点一下就完事」）
 *   丢弃 → 删记录 + 删图，以后这张还能重新投稿
 *
 * 注意 0.6.4 之前拒收的图**从来没写过盘**（判死当场就把字节丢了），
 * 那些行只有 sha256 和原因，页面标一句「当时没存图」并且只给「丢弃」。
 */
const rejected = {
  cards: [],
  offset: 0,
  limit: 24,
  hasMore: false,
  loaded: false,
};

function fmtSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return i === 0 ? `${value}B` : `${value.toFixed(1)}${units[i]}`;
}

async function loadRejected({ append = false } = {}) {
  const offset = append ? rejected.offset + rejected.limit : 0;
  const payload = await apiGet("ui/rejected", { limit: rejected.limit, offset });
  rejected.offset = payload.offset;
  rejected.cards = append ? rejected.cards.concat(payload.cards) : payload.cards;
  rejected.hasMore = Boolean(payload.hasMore);
  renderRejected(payload.total);
}

function renderRejected(total) {
  const grid = el("rejectedGrid");
  const summary = el("rejectedSummary");
  if (!grid) return;

  const noFile = rejected.cards.filter((card) => !card.hasFile).length;
  if (summary) {
    const bits = [`共 ${total ?? rejected.cards.length} 条拒收`];
    if (noFile) {
      bits.push(
        `其中 ${noFile} 条是 0.6.4 之前留下的，当时候没存图 —— 只能看原因，不能接收`
      );
    }
    summary.textContent = bits.join(" · ");
  }

  if (!rejected.cards.length) {
    grid.innerHTML =
      '<div class="empty">还没有被拒收的图。<br />以后审核判死的图会自动出现在这里。</div>';
    el("rejectedMore")?.classList.add("hidden");
    return;
  }

  grid.innerHTML = rejected.cards
    .map((card) => {
      const thumb = card.thumb
        ? `<img src="${card.thumb}" alt="" loading="lazy" />`
        : '<div class="rejected-noimg">这张图<br />没存下来</div>';
      const meta = [
        card.sizeText ? `<span>${escapeHtml(card.sizeText)}</span>` : "",
        card.origin ? `<span>${escapeHtml(card.origin)}</span>` : "",
      ]
        .filter(Boolean)
        .join("");
      return `
      <article class="card rejected-card" data-sha="${escapeHtml(card.sha256)}">
        <div class="card-thumb" data-act="open">
          ${thumb}
          <div class="badges"><span class="badge warn">被拒</span></div>
        </div>
        <div class="card-body">
          <div class="card-caption">${escapeHtml(card.reason || "（没写原因）")}</div>
          <div class="card-meta">
            <span>${fmtTime(card.createdAt)}</span>
            ${meta}
          </div>
          <div class="card-actions">
            <button class="ghost" data-act="open">大图</button>
            ${
              card.hasFile
                ? '<button data-act="accept">接收</button>'
                : '<button class="ghost" disabled title="这张的图当时没存下来">接收</button>'
            }
            <button class="danger" data-act="drop">丢弃</button>
          </div>
        </div>
      </article>`;
    })
    .join("");
  el("rejectedMore")?.classList.toggle("hidden", !rejected.hasMore);
}

function closeModal() {
  el("modal")?.classList.add("hidden");
}

async function openRejectedDetail(sha) {
  const payload = await apiGet("ui/rejected_detail", { sha });
  const info = [
    `拒收原因：${payload.reason || "（没写）"}`,
    `投稿人：${payload.contributor || "—"}`,
    `拒收时间：${fmtTime(payload.createdAt)}`,
    payload.size ? `大小：${fmtSize(payload.size)}（${payload.mime || "?"}）` : "",
    payload.origin ? `来源：${payload.origin}` : "",
    payload.sourceUrl ? `原链接：${payload.sourceUrl}` : "",
    payload.hasFile ? "" : "⚠ 这张图当时没存下来（0.6.4 之前的拒收不存图），没法接收",
    payload.alreadyInLibrary ? "ℹ 库里已经有同一张了" : "",
  ]
    .filter(Boolean)
    .join("<br />");

  el("modalImage").src = payload.image || "";
  el("modalImage").style.display = payload.image ? "" : "none";
  el("modalInfo").innerHTML = info;

  const actions = el("modalActions");
  actions.innerHTML = "";
  if (payload.hasFile) {
    const accept = document.createElement("button");
    accept.textContent = "接收（直接入库）";
    accept.addEventListener("click", () => {
      closeModal();
      acceptRejected(payload.sha256).catch(showError);
    });
    actions.appendChild(accept);
  }
  const drop = document.createElement("button");
  drop.className = "danger";
  drop.textContent = "丢弃";
  drop.addEventListener("click", () => {
    closeModal();
    dropRejected(payload.sha256).catch(showError);
  });
  actions.appendChild(drop);

  el("modal").classList.remove("hidden");
}

async function acceptRejected(sha) {
  if (
    !(await ask("接收后会直接进图库开始发（不再走 AI 审核）。确定？", {
      okText: "接收",
    }))
  ) {
    return;
  }
  const result = await apiPost("ui/rejected_accept", { sha });
  logUpload(result.message || "已入库");
  await loadRejected();
  await loadState();
}

async function dropRejected(sha) {
  if (
    !(await ask("丢弃会删掉这条记录和那张图，以后这张还能重新投稿。确定？", {
      okText: "丢弃",
    }))
  ) {
    return;
  }
  const result = await apiPost("ui/rejected_drop", { sha });
  logUpload(result.message || "已丢弃");
  await loadRejected();
  await loadState();
}

function showError(error) {
  const message = error?.message || String(error);
  el("subtitle").textContent = `出错了：${message}`;
  el("grid").innerHTML = `<div class="empty">读取失败：${escapeHtml(message)}</div>`;
  console.error("[meme page]", error);
}

async function boot() {
  try {
    bind();
  } catch (error) {
    // 兜底：bind 再出意外也要把数据读出来（统计能看比按钮全死强）
    console.error("[meme page] 绑定事件出错", error);
  }
  try {
    if (bridge?.ready) await bridge.ready();
    const options = await apiGet("ui/options");
    if (options) {
      el("optSkipReview").checked = options.skipReview !== false;
    }
    await loadState();
  } catch (error) {
    showError(error);
  }
}

boot();


/* ---------------- 搬到自建图床 ----------------

 * 为什么是一批一批搬而不是一次搬完：一次请求要读 100 多个文件、算 sha256、
 * 逐个写盘，面板那条请求会挂在那儿几十秒，看不出是在干活还是卡死。每批 20 张
 * 就把进度显式打出来，而且中途失败也只丢这一批。
 *
 * 只写图床、不改本地文件：`hosted` 表只是多一列 URL，media/ 里的图一张不动，
 * 所以搬错了随时可以回退（把 prefer_image_host 关掉）。
 */
async function hostLibrary() {
  const button = el("hostLibrary");
  if (button) button.disabled = true;   // 搬的时候别让人再点一次
  let moved = 0;
  let skipped = 0;
  const failed = [];
  try {
    for (let round = 0; round < 50; round += 1) {
      const result = await apiPost("ui/host_library", { limit: 20 });
      moved += result.moved || 0;
      skipped += result.skipped || 0;
      if (result.failed?.length) failed.push(...result.failed);
      const left = result.remaining ?? 0;
      logUpload(`搬到图床：新传 ${moved} 张、复用 ${skipped} 张，还剩 ${left} 张`);
      // 一张都没动就退出：否则这里会空转 50 圈（比如图床探活过了但每张都写失败）
      if (left <= 0 || (!result.moved && !result.skipped)) break;
    }
  } finally {
    if (button) button.disabled = false;
  }
  const tail = failed.length ? `，失败 ${failed.length} 张（${failed[0]}）` : "";
  logUpload(`搬到图床完成：新传 ${moved} 张、复用 ${skipped} 张${tail}`, failed.length > 0);
  await loadState();
}
