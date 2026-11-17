/* 梗图库页面。
 *
 * 一切后端访问都走桥接层（window.AstrBotPluginPage）：页面是嵌在面板里的
 * iframe，直连后端会被令牌拦掉，桥接层帮我们把请求带上作用域令牌。
 */
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
const el = (id) => document.getElementById(id);

const state = {
  groups: [],
  groupId: "",
  origin: "all",
  q: "",
  sort: "new",
  status: "all",
  limit: 24,
  offset: 0,
  cards: [],
  settings: null,
};

const ORIGIN_ORDER = ["all", "bundled", "submitted", "api", "generated"];
const ORIGIN_FALLBACK = { all: "全部", bundled: "内置", submitted: "投稿", api: "接口", generated: "AI 现画" };

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
    group_id: state.groupId,
    limit: state.limit,
    offset,
  });

  state.offset = payload.offset;
  state.cards = append ? state.cards.concat(payload.cards) : payload.cards;
  state.groups = payload.groups || [];

  el("statGroup").textContent = payload.groupTotal;
  el("statTotal").textContent = payload.libraryTotal;
  el("statRejected").textContent = payload.rejected;
  el("statMode").textContent = payload.running?.captionMode || "—";
  el("statProvider").textContent = payload.running?.moderationProvider || "未配";
  el("statProvider").title = payload.running?.moderationProvider || "";
  el("subtitle").textContent =
    `数据目录 ${payload.running?.dataDir || "—"}` +
    (payload.running?.proxy ? ` · 代理 ${payload.running.proxy}` : "");

  renderGroupFilter();
  renderChips(payload.origins || []);
  renderGrid();
  el("loadMore").classList.toggle("hidden", !payload.hasMore);
}

function renderGroupFilter() {
  const select = el("groupFilter");
  const current = state.groupId;
  const items = ['<option value="">全部群视角</option>'];
  state.groups.forEach((group) => {
    const label = `${group.groupId || group.umo}${group.dailyEnabled ? " · 每日" : ""}`;
    items.push(`<option value="${escapeHtml(group.umo)}">${escapeHtml(label)}</option>`);
  });
  select.innerHTML = items.join("");
  select.value = current;
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
      card.scope === "global" ? '<span class="badge">全局</span>' : "",
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
            <span>#${card.id} · ${escapeHtml(card.scopeLabel)}</span>
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

async function pushUploadOptions() {
  try {
    await apiPost("ui/options", {
      scope: el("optScope").value,
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

async function uploadFiles(files) {
  const list = Array.from(files || []).filter((file) => file.type.startsWith("image/"));
  if (!list.length) return;
  const skip = el("optSkipReview").checked;
  el("uploadLog").innerHTML = "";
  for (const file of list) {
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
  await loadState();
}

/* ---------------- 单张操作 ---------------- */

async function openDetail(id) {
  const detail = await apiGet("ui/detail", { id });
  if (!detail) return;
  el("modalImage").src = detail.image || "";
  el("modalInfo").innerHTML = `
    <span>编号 #${detail.id}</span>
    <span>来源 ${escapeHtml(detail.origin)}</span>
    <span>范围 ${escapeHtml(detail.scope)}</span>
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
      if (!window.confirm(`确定删除 #${id}？（图片文件也会删掉）`)) return;
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
  el("refresh").addEventListener("click", () => loadState().catch(showError));
  el("loadMore").addEventListener("click", () => loadState({ append: true }).catch(showError));

  el("originChips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    state.origin = chip.dataset.origin;
    loadState().catch(showError);
  });

  let searchTimer = 0;
  el("search").addEventListener("input", (event) => {
    state.q = event.target.value.trim();
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadState().catch(showError), 250);
  });
  el("sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    loadState().catch(showError);
  });
  el("statusFilter").addEventListener("change", (event) => {
    state.status = event.target.value;
    loadState().catch(showError);
  });
  el("groupFilter").addEventListener("change", (event) => {
    state.groupId = event.target.value;
    loadState().catch(showError);
  });

  el("grid").addEventListener("click", (event) => {
    const card = event.target.closest(".card");
    if (!card) return;
    const action = event.target.closest("[data-act]")?.dataset.act || "open";
    const id = card.dataset.id;
    if (action === "open") openDetail(id).catch(showError);
    else act(action, id);
  });

  el("modalClose").addEventListener("click", () => el("modal").classList.add("hidden"));
  el("modal").addEventListener("click", (event) => {
    if (event.target === el("modal")) el("modal").classList.add("hidden");
  });
  el("modalActions").addEventListener("click", (event) => {
    const id = el("modal").dataset.id;
    if (event.target.id === "modalRecheck") act("recheck", id);
    if (event.target.id === "modalToggle") act("toggle", id);
    if (event.target.id === "modalDelete") act("delete", id);
  });

  const zone = el("dropzone");
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("hot");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("hot"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("hot");
    uploadFiles(event.dataTransfer?.files);
  });
  el("pickFiles").addEventListener("click", () => el("fileInput").click());
  el("fileInput").addEventListener("change", (event) => uploadFiles(event.target.files));
  el("optSkipReview").addEventListener("change", pushUploadOptions);
  el("optScope").addEventListener("change", pushUploadOptions);

  el("toggleSettings").addEventListener("click", () => {
    const box = el("settings");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden") && !state.settings) loadSettings().catch(showError);
  });
  el("saveSettings").addEventListener("click", saveSettings);
  el("resetSettings").addEventListener("click", () => loadSettings().catch(showError));
}

function showError(error) {
  const message = error?.message || String(error);
  el("subtitle").textContent = `出错了：${message}`;
  el("grid").innerHTML = `<div class="empty">读取失败：${escapeHtml(message)}</div>`;
  console.error("[meme page]", error);
}

async function boot() {
  bind();
  try {
    if (bridge?.ready) await bridge.ready();
    const options = await apiGet("ui/options");
    if (options) {
      el("optScope").value = options.scope || "group";
      el("optSkipReview").checked = options.skipReview !== false;
    }
    await loadState();
  } catch (error) {
    showError(error);
  }
}

boot();
