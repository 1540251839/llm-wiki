const state = {
  health: null,
  overview: null,
  activeView: "dashboard",
  modelConfigured: false,
  pollTimer: null,
  askRequestId: 0,
  graph: { nodes: [], edges: [], visibleTypes: new Set(), scale: 1, panX: 0, panY: 0, dragging: false },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const escapeHTML = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    let detail = typeof payload === "object" ? payload.detail : payload;
    if (Array.isArray(detail)) detail = detail.map((item) => item?.msg || JSON.stringify(item)).join("；");
    else if (detail && typeof detail === "object") detail = JSON.stringify(detail);
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return payload;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2800);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith?.("Z") ? value : value.replace(" ", "T") + "Z");
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

const statusLabels = {
  INDEXED: "已索引",
  UPLOADED: "已上传",
  EXTRACTING: "抽取中",
  EXTRACTED: "已抽取",
  WIKI_CREATED: "编译中",
  FAILED: "处理失败",
};

function statusClass(status) {
  if (status === "INDEXED") return "ok";
  if (status === "FAILED") return "error";
  return "processing";
}

function showView(name) {
  state.activeView = name;
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${name}`));
  const labels = {
    dashboard: ["OVERVIEW", "知识工作台"],
    documents: ["SOURCE MANAGEMENT", "资料中心"],
    wiki: ["COMPILED KNOWLEDGE", "Wiki 页面"],
    graph: ["KNOWLEDGE GRAPH", "知识图谱"],
    ask: ["SOURCE-AWARE QA", "证据问答"],
    evaluation: ["REPLAYABLE QUALITY", "质量评估"],
    reviews: ["HUMAN REVIEW", "Review 队列"],
    settings: ["RUNTIME", "系统设置"],
  };
  $("#view-eyebrow").textContent = labels[name][0];
  $("#view-title").textContent = labels[name][1];
  if (name === "documents") loadDocuments();
  if (name === "wiki") loadWiki();
  if (name === "graph") loadGraph();
  if (name === "evaluation") loadEvaluation();
  if (name === "reviews") loadReviews();
  if (name === "settings") loadSettings();
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    state.health = health;
    state.modelConfigured = Boolean(health.llm?.configured);
    $("#api-dot").className = "system-dot online";
    $("#api-status").textContent = "本地 API 正常";
    $("#version-label").textContent = `v${health.version}`;
    const modelLabel = `${health.llm.provider} · ${health.llm.model}`;
    $("#model-chip").textContent = `${modelLabel} · ${state.modelConfigured ? "已配置" : "未配置 Key"}`;
    $("#hero-provider").textContent = health.llm.provider.toUpperCase();
    $("#hero-model").textContent = health.llm.model;
  } catch (error) {
    $("#api-dot").className = "system-dot offline";
    $("#api-status").textContent = "API 不可达";
    $("#model-chip").textContent = "无法读取模型状态";
  }
}

function renderMetric(icon, value, label) {
  return `<article class="panel metric"><span class="metric-icon">${icon}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(label)}</small></article>`;
}

function qualityBadge(quality) {
  const level = quality?.level || "warning";
  const labels = { ok: "正常", warning: "需核对", error: "异常", processing: "处理中" };
  const title = (quality?.reasons || []).join("；");
  return `<span class="badge ${escapeHTML(level)}" title="${escapeHTML(title)}">${labels[level] || level}</span>`;
}

async function loadOverview() {
  try {
    const value = await api("/api/overview");
    state.overview = value;
    const processing = ["PENDING", "RUNNING", "PAUSED"].reduce((sum, key) => sum + Number(value.queue?.[key] || 0), 0);
    $("#metric-grid").innerHTML = [
      renderMetric("SOURCE / 01", value.documents, "资料总数（以数据库为准）"),
      renderMetric("WIKI / 02", value.wiki_pages, "持久 Wiki 页面"),
      renderMetric("LINK / 03", value.links, "页面内部关系"),
      renderMetric("QUEUE / 04", processing, "当前处理中"),
    ].join("");
    $("#dataflow").innerHTML = (value.dataflow || []).map((step, index) => `
      <div class="flow-step"><span>0${index + 1}</span><strong>${escapeHTML(step.label)}</strong><small>${escapeHTML(step.detail)}</small></div>
    `).join("");
    $("#quality-summary").innerHTML = `
      <div class="quality-row"><span>资料质量警告</span><strong>${value.quality.documents_with_warnings}</strong></div>
      <div class="quality-row"><span>待审核条目</span><strong>${value.open_reviews || 0}</strong></div>
      <div class="quality-row"><span>孤立论文页面</span><strong>${value.quality.orphan_paper_pages}</strong></div>
      <div class="quality-row"><span>已索引资料</span><strong>${value.statuses.INDEXED || 0}</strong></div>
      <p class="quality-note">“已索引”只表示流水线完成；质量警告会另外显示，不把处理成功等同于内容正确。</p>
    `;
    renderRecent(value.recent_documents || []);
  } catch (error) {
    $("#metric-grid").innerHTML = `<div class="empty-state">工作台加载失败：${escapeHTML(error.message)}</div>`;
  }
}

function renderRecent(items) {
  const root = $("#recent-documents");
  if (!items.length) {
    root.innerHTML = `<div class="empty-state">尚未导入资料。</div>`;
    return;
  }
  root.innerHTML = items.map((item) => `
    <div class="compact-item">
      <div><strong>${escapeHTML(item.title)}</strong><small>${escapeHTML(item.source_name || item.raw_name)}</small></div>
      <span class="badge ${statusClass(item.status)}">${statusLabels[item.status] || escapeHTML(item.status)}</span>
      <time>${escapeHTML(formatDate(item.updated_at))}</time>
    </div>
  `).join("");
}

let documentSearchTimer;
async function loadDocuments() {
  const q = $("#document-search").value.trim();
  const status = $("#status-filter").value;
  try {
    const value = await api(`/api/documents?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}&page_size=100`);
    $("#document-count").textContent = value.items.length < value.total ? `${value.items.length} / ${value.total} 条` : `${value.total} 条`;
    const body = $("#documents-body");
    $("#documents-empty").classList.toggle("hidden", value.items.length > 0);
    body.innerHTML = value.items.map((item) => {
      const authors = item.authors?.length ? item.authors.slice(0, 2).join("、") : "—";
      return `<tr>
        <td><span class="doc-title">${escapeHTML(item.title)}</span><span class="doc-file">${escapeHTML(item.source_name || item.raw_name || item.id.slice(0, 12))}${item.job?.stage ? ` · ${escapeHTML(item.job.stage)}` : ""}</span></td>
        <td><span class="badge ${statusClass(item.status)}">${statusLabels[item.status] || escapeHTML(item.status)}</span></td>
        <td>${qualityBadge(item.quality)}</td>
        <td>${escapeHTML(authors)}${item.year ? ` · ${escapeHTML(item.year)}` : ""}</td>
        <td>${escapeHTML(formatDate(item.updated_at))}</td>
        <td><button class="row-action" data-document-id="${escapeHTML(item.id)}">详情</button></td>
      </tr>`;
    }).join("");
    $$('[data-document-id]', body).forEach((button) => button.addEventListener("click", () => openDocument(button.dataset.documentId)));
  } catch (error) {
    toast(`资料加载失败：${error.message}`);
  }
}

async function openDocument(id) {
  const dialog = $("#document-dialog");
  const root = $("#document-detail");
  root.innerHTML = `<div class="empty-state">正在读取…</div>`;
  if (!dialog.open) dialog.showModal();
  try {
    const item = await api(`/api/documents/${encodeURIComponent(id)}`);
    $("#detail-title").textContent = item.title;
    const extraction = item.extraction || {};
    const reasons = item.quality.reasons || [];
    const job = item.job || {};
    const reviews = item.reviews || [];
    const linkedPages = (item.wiki_pages || []).map((page) => `<button class="source-card" type="button" data-related-page="${page.id}" data-related-title="${escapeHTML(page.title)}"><strong>${escapeHTML(page.title)}</strong><br>${escapeHTML(page.type)} · ${escapeHTML(page.path)}</button>`).join("");
    root.innerHTML = `
      <div class="detail-grid">
        <div class="detail-cell"><span>处理状态</span><strong>${escapeHTML(statusLabels[item.status] || item.status)}</strong></div>
        <div class="detail-cell"><span>内容质量</span>${qualityBadge(item.quality)}</div>
        <div class="detail-cell"><span>原始文件</span><strong>${escapeHTML(item.raw_name || item.source_name || "—")}</strong></div>
        <div class="detail-cell"><span>文档 ID</span><strong title="${escapeHTML(item.id)}">${escapeHTML(item.id.slice(0, 16))}…</strong></div>
        <div class="detail-cell"><span>队列阶段</span><strong>${escapeHTML(job.stage || "—")}</strong></div>
        <div class="detail-cell"><span>处理模式 / 尝试</span><strong>${escapeHTML(job.quality_mode || "standard")} · ${escapeHTML(job.attempts || 0)}/${escapeHTML(job.max_attempts || 0)}</strong></div>
      </div>
      ${reasons.length ? `<div class="notice">${reasons.map(escapeHTML).join("；")}</div>` : ""}
      ${item.error_message ? `<div class="detail-section"><h3>失败原因</h3><p>${escapeHTML(item.error_message)}</p></div>` : ""}
      <div class="detail-section"><h3>结构化抽取</h3>
        <p>${escapeHTML(extraction.abstract || "暂无摘要")}</p>
        <p><strong>方法：</strong>${escapeHTML((extraction.methods || []).map((x) => x.name || x).join("、") || "—")}</p>
        <p><strong>数据集：</strong>${escapeHTML((extraction.datasets || []).map((x) => x.name || x).join("、") || "—")}</p>
        <p><strong>主题：</strong>${escapeHTML((extraction.topics || []).join("、") || "—")}</p>
      </div>
      ${linkedPages ? `<div class="detail-section"><h3>关联 Wiki</h3><div class="source-list">${linkedPages}</div></div>` : ""}
      ${reviews.length ? `<div class="detail-section"><h3>待审核</h3><div class="review-list">${reviews.map((review) => `<div class="review-item"><div><span class="badge ${review.severity === "error" ? "error" : "warning"}">${escapeHTML(review.kind)}</span><p>${escapeHTML(review.message)}</p></div><button class="row-action" data-resolve-review="${review.id}">标为已处理</button></div>`).join("")}</div></div>` : ""}
      <div class="dialog-actions">
        ${item.raw_name ? `<a class="secondary" href="/api/documents/${escapeHTML(item.id)}/file">下载原文</a>` : ""}
        <button class="danger" id="delete-document">移到回收区</button>
        ${item.retryable ? `<button class="secondary" id="retry-document-high">高质量重试</button><button class="primary" id="retry-document">标准重试</button>` : ""}
      </div>`;
    $$('[data-related-page]', root).forEach((button) => button.addEventListener("click", () => goToWikiPage(button.dataset.relatedPage, button.dataset.relatedTitle)));
    $("#retry-document")?.addEventListener("click", async () => {
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/retry?quality_mode=standard`, { method: "POST" });
        toast("已重新加入处理队列");
        dialog.close();
        startPolling(id);
      } catch (error) { toast(error.message); }
    });
    $("#retry-document-high")?.addEventListener("click", async () => {
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/retry?quality_mode=high`, { method: "POST" });
        toast("已按高质量模式重新加入队列");
        dialog.close();
        startPolling(id);
      } catch (error) { toast(error.message); }
    });
    $$('[data-resolve-review]', root).forEach((button) => button.addEventListener("click", async () => {
      try {
        await api(`/api/reviews/${button.dataset.resolveReview}/resolve`, { method: "POST" });
        toast("审核项已处理");
        await openDocument(id);
      } catch (error) { toast(error.message); }
    }));
    $("#delete-document")?.addEventListener("click", async () => {
      if (!window.confirm("资料、原始副本和仅由它生成的页面将移到工作区 data/trash，可手动恢复。继续吗？")) return;
      try {
        const value = await api(`/api/documents/${encodeURIComponent(id)}?confirm=true`, { method: "DELETE" });
        toast(`已移到 ${value.recoverable_from}`);
        dialog.close();
        await Promise.all([loadDocuments(), loadOverview()]);
      } catch (error) { toast(error.message); }
    });
  } catch (error) {
    root.innerHTML = `<div class="empty-state">读取失败：${escapeHTML(error.message)}</div>`;
  }
}

async function loadWiki() {
  const q = $("#wiki-search").value.trim();
  try {
    const value = await api(`/api/wiki/pages?q=${encodeURIComponent(q)}&limit=500`);
    $("#wiki-count").textContent = value.items.length < value.total ? `${value.items.length} / ${value.total} 页` : `${value.total} 页`;
    const root = $("#wiki-list");
    root.innerHTML = value.items.map((item) => `<button class="wiki-item" data-page-id="${item.id}" data-page-title="${escapeHTML(item.title)}">${escapeHTML(item.title)}<small>${escapeHTML(item.type)} · ${escapeHTML(item.path)}</small></button>`).join("") || `<div class="empty-state">没有页面。</div>`;
    $$('[data-page-id]', root).forEach((button) => button.addEventListener("click", () => openWikiPage(button)));
  } catch (error) { toast(`Wiki 加载失败：${error.message}`); }
}

async function goToWikiPage(id, title = "") {
  $("#document-dialog")?.close();
  $("#wiki-search").value = title;
  showView("wiki");
  await loadWiki();
  const button = $(`[data-page-id="${CSS.escape(String(id))}"]`, $("#wiki-list"));
  if (button) await openWikiPage(button);
}

async function goToWikiTitle(title) {
  $("#wiki-search").value = title;
  showView("wiki");
  await loadWiki();
  const button = $$('[data-page-title]', $("#wiki-list")).find((item) => item.dataset.pageTitle === title);
  if (button) await openWikiPage(button);
}

async function openWikiPage(button) {
  $$(".wiki-item").forEach((node) => node.classList.toggle("active", node === button));
  const root = $("#wiki-detail");
  root.innerHTML = `<div class="empty-state">正在读取…</div>`;
  try {
    const page = await api(`/api/wiki/pages/${button.dataset.pageId}`);
    const sourceCards = (page.sources || []).map((source) => `<div class="source-card"><strong>${escapeHTML(source.original_name || source.source_id)}</strong><br>sha256:${escapeHTML((source.source_id || "").slice(0, 12))} · ${source.page_number ? `第 ${source.page_number} 页` : "来源级"} · ${escapeHTML(source.locator || "未定位")}<br><span>${escapeHTML(source.evidence_text || source.raw_path || source.source_path || "")}</span></div>`).join("");
    const relatedCards = (page.related || []).map((item) => `<button class="source-card" type="button" data-related-page="${item.id}" data-related-title="${escapeHTML(item.title)}"><strong>${escapeHTML(item.title)}</strong><br>${escapeHTML(item.type)} · score ${Number(item.score || 0).toFixed(1)}</button>`).join("");
    root.innerHTML = `<div class="section-head"><div><p class="eyebrow">${escapeHTML(page.type)}</p><h3>${escapeHTML(page.title)}</h3></div><span class="truth-label">${escapeHTML(page.path)}</span></div>${sourceCards ? `<div class="evidence-strip"><span>来源证据</span>${sourceCards}</div>` : ""}<pre class="markdown"></pre>${relatedCards ? `<div class="detail-section"><h3>关系推荐</h3><div class="source-list">${relatedCards}</div></div>` : ""}`;
    $(".markdown", root).textContent = page.content || "";
    $$('[data-related-page]', root).forEach((related) => related.addEventListener("click", () => goToWikiPage(related.dataset.relatedPage, related.dataset.relatedTitle)));
  } catch (error) { root.innerHTML = `<div class="empty-state">读取失败：${escapeHTML(error.message)}</div>`; }
}

function openUpload() {
  const message = $("#upload-message");
  const submit = $("#upload-submit");
  if (!state.modelConfigured) {
    message.textContent = "尚未配置 DEEPSEEK_API_KEY。浏览与搜索可用，但新资料无法抽取。";
    submit.disabled = true;
  } else {
    message.textContent = "";
    submit.disabled = false;
  }
  $("#upload-dialog").showModal();
}

async function submitUpload(event) {
  event.preventDefault();
  if (!state.modelConfigured) return;
  const file = $("#file-input").files[0];
  if (!file) return;
  const button = $("#upload-submit");
  const message = $("#upload-message");
  button.disabled = true;
  message.textContent = "正在保存资料并创建处理任务…";
  const data = new FormData();
  data.append("file", file);
  data.append("quality_mode", $("#quality-mode").value);
  try {
    const value = await api("/api/documents", { method: "POST", body: data });
    $("#upload-dialog").close();
    $("#upload-form").reset();
    showView("documents");
    if (value.action === "retry_required") {
      toast("该资料已有失败记录，请在详情中重试");
      await openDocument(value.document.id);
    } else if (value.action === "already_indexed") {
      toast("该资料已完成索引，没有重复处理");
    } else {
      toast(value.action === "already_processing" ? "该资料已在处理队列中" : "资料已进入处理队列");
      startPolling(value.document.id);
    }
  } catch (error) {
    message.textContent = `上传失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function startPolling(id) {
  window.clearTimeout(state.pollTimer);
  let attempts = 0;
  const startedAt = Date.now();
  const poll = async () => {
    attempts += 1;
    try {
      const item = await api(`/api/documents/${encodeURIComponent(id)}`);
      await Promise.all([loadDocuments(), loadOverview()]);
      if (["INDEXED", "FAILED"].includes(item.status)) {
        if (item.status === "INDEXED") toast("资料处理完成");
        if (item.status === "FAILED") toast("资料处理失败，请查看详情");
        return;
      }
    } catch (_) { /* A later poll may recover from a transient local error. */ }
    if (Date.now() - startedAt >= 15 * 60 * 1000) {
      toast("处理仍未结束，列表保留当前状态；可稍后手动查看");
      return;
    }
    const delay = attempts < 24 ? 2500 : 5000;
    state.pollTimer = window.setTimeout(poll, delay);
  };
  state.pollTimer = window.setTimeout(poll, 1000);
}

async function loadReviews() {
  const root = $("#review-list");
  root.innerHTML = `<div class="empty-state">正在读取…</div>`;
  try {
    const value = await api("/api/reviews?status=OPEN&limit=500");
    $("#review-count").textContent = `${value.total} 条`;
    root.innerHTML = value.items.map((item) => `
      <article class="review-item">
        <div>
          <div class="review-meta"><span class="badge ${item.severity === "error" ? "error" : "warning"}">${escapeHTML(item.severity)}</span><code>${escapeHTML(item.kind)}</code></div>
          <strong>${escapeHTML(item.message)}</strong>
          <small>${escapeHTML(item.source_id ? `来源 ${item.source_id.slice(0, 12)}` : item.page_path || "全局")}</small>
        </div>
        <button class="row-action" data-resolve-review="${item.id}">标为已处理</button>
      </article>`).join("") || `<div class="empty-state">当前没有待审核问题。</div>`;
    $$('[data-resolve-review]', root).forEach((button) => button.addEventListener("click", async () => {
      try {
        await api(`/api/reviews/${button.dataset.resolveReview}/resolve`, { method: "POST" });
        toast("审核项已处理");
        await Promise.all([loadReviews(), loadOverview()]);
      } catch (error) { toast(error.message); }
    }));
  } catch (error) {
    root.innerHTML = `<div class="empty-state">Review 加载失败：${escapeHTML(error.message)}</div>`;
  }
}

function renderAnswer(value, result, mode) {
  const modeLabels = { llm: `${value.provider} · ${value.model}`, local_evidence: "本地证据降级", no_evidence: "无证据拒答" };
  mode.textContent = modeLabels[value.mode] || value.mode;
  result.className = "answer ready";
  const context = value.context || {};
  result.innerHTML = `<div class="answer-text"></div>${value.fallback_reason ? `<div class="notice">远程模型不可用，已降级：${escapeHTML(value.fallback_reason)}</div>` : ""}<div class="context-meter">上下文 ${escapeHTML(context.used_chars || 0)} / ${escapeHTML(context.page_budget_chars || 0)} 字符 · ${escapeHTML(context.pages_included || 0)} / ${escapeHTML(context.pages_retrieved || 0)} 页</div><div class="source-list">${(value.sources || []).map((source) => { const evidence = (source.provenance || []).map((item) => { const location = item.page_number ? `第 ${item.page_number} 页` : item.locator || "来源级"; const excerpt = item.evidence_text ? ` · ${item.evidence_text.slice(0, 120)}` : ""; return `${item.name || "原始资料"} · ${location}${excerpt}`; }).join("；"); return `<button class="source-card" type="button" data-source-title="${escapeHTML(source.title)}"><strong>[${escapeHTML(source.citation_index || "?")}] ${escapeHTML(source.title)}</strong><br>${escapeHTML(source.type)} · ${escapeHTML(source.path)}${evidence ? `<br><span>${escapeHTML(evidence)}</span>` : ""}</button>`; }).join("")}</div>`;
  $(".answer-text", result).textContent = value.answer;
  $$('[data-source-title]', result).forEach((button) => button.addEventListener("click", () => goToWikiTitle(button.dataset.sourceTitle)));
}

async function submitQuestion(event) {
  event.preventDefault();
  const input = $("#ask-input");
  const result = $("#ask-result");
  const mode = $("#ask-mode");
  const submit = $("#ask-submit");
  const question = input.value.trim();
  if (!question) return;
  const requestId = ++state.askRequestId;
  submit.disabled = true;
  mode.textContent = "正在检索证据…";
  result.className = "answer empty-state";
  result.textContent = "正在读取 Wiki 并组织答案…";
  try {
    const response = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const eventName = block.match(/^event:\s*(.+)$/m)?.[1];
        const dataLine = block.match(/^data:\s*(.+)$/m)?.[1];
        if (!dataLine || requestId !== state.askRequestId) continue;
        const payload = JSON.parse(dataLine);
        if (eventName === "status") mode.textContent = payload.message;
        if (eventName === "error") throw new Error(payload.message);
        if (eventName === "result") renderAnswer(payload, result, mode);
      }
      if (done) break;
    }
  } catch (error) {
    if (requestId !== state.askRequestId) return;
    mode.textContent = "请求失败";
    result.className = "answer empty-state";
    result.textContent = error.message;
  } finally {
    if (requestId === state.askRequestId) submit.disabled = false;
  }
}

const graphColors = { PAGE: "#6ea8ff", SOURCE: "#55d6a7", PAGE_TYPE: "#f5bd62", TOPIC: "#9b87f5" };

function graphTransform() {
  const graph = state.graph;
  return `translate(${graph.panX} ${graph.panY}) translate(500 340) scale(${graph.scale}) translate(-500 -340)`;
}

function renderGraph() {
  const svg = $("#graph-canvas");
  const graph = state.graph;
  const query = $("#graph-search").value.trim().toLocaleLowerCase();
  const visible = graph.nodes.filter((node) => graph.visibleTypes.has(node.type));
  const positions = new Map();
  const groups = [...new Set(visible.map((node) => node.type))].sort();
  groups.forEach((type, groupIndex) => {
    const items = visible.filter((node) => node.type === type);
    const radius = 105 + groupIndex * 95;
    items.forEach((node, index) => {
      const angle = (Math.PI * 2 * index / Math.max(1, items.length)) - Math.PI / 2 + groupIndex * .3;
      positions.set(node.id, { x: 500 + Math.cos(angle) * radius, y: 340 + Math.sin(angle) * radius });
    });
  });
  const edges = graph.edges.filter((edge) => positions.has(edge.source) && positions.has(edge.target));
  const edgeMarkup = edges.map((edge) => { const a = positions.get(edge.source); const b = positions.get(edge.target); return `<line class="graph-edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" data-semantic="${escapeHTML(edge.semantic)}" />`; }).join("");
  const nodeMarkup = visible.map((node) => { const p = positions.get(node.id); const match = !query || node.name.toLocaleLowerCase().includes(query); const radius = node.type === "PAGE" ? 8 : 6; return `<g class="graph-node ${match ? "match" : "dim"}" data-graph-node="${escapeHTML(node.id)}" role="button" tabindex="0" aria-label="${escapeHTML(node.type + " " + node.name)}" transform="translate(${p.x} ${p.y})"><circle r="${radius}" fill="${graphColors[node.type] || graphColors.TOPIC}"/><text y="${radius + 13}" text-anchor="middle">${escapeHTML(node.name.slice(0, 24))}</text></g>`; }).join("");
  svg.innerHTML = `<g id="graph-stage" transform="${graphTransform()}">${edgeMarkup}${nodeMarkup}</g>`;
  $$('[data-graph-node]', svg).forEach((node) => {
    node.addEventListener("click", () => selectGraphNode(node.dataset.graphNode));
    node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectGraphNode(node.dataset.graphNode); } });
  });
}

function selectGraphNode(id) {
  const node = state.graph.nodes.find((item) => item.id === id);
  if (!node) return;
  const connections = state.graph.edges.filter((edge) => edge.source === id || edge.target === id);
  $("#graph-detail").innerHTML = `<p class="eyebrow">${escapeHTML(node.type)}</p><h3>${escapeHTML(node.name)}</h3><div class="settings-list"><div><span>稳定 ID</span><code>${escapeHTML(node.id)}</code></div><div><span>连接数</span><strong>${connections.length}</strong></div><div><span>来源定位</span><strong>${node.page_number ? `第 ${node.page_number} 页 · ${node.char_start}-${node.char_end}` : "元数据对象"}</strong></div><div><span>置信类型</span><strong>${escapeHTML(node.confidence)}</strong></div></div>${node.page_id ? `<button class="primary" data-open-graph-page="${node.page_id}">打开 Wiki 页面</button>` : ""}<div class="detail-section"><h3>关系</h3>${connections.slice(0, 24).map((edge) => `<p><code>${escapeHTML(edge.semantic)}</code> ${escapeHTML(edge.source === id ? `→ ${edge.target}` : `${edge.source} →`)}</p>`).join("") || "<p>无连接</p>"}</div>`;
  $('[data-open-graph-page]')?.addEventListener("click", () => goToWikiPage(node.page_id, node.name));
}

async function loadGraph() {
  const svg = $("#graph-canvas");
  svg.innerHTML = `<text x="500" y="340" text-anchor="middle" fill="#8d98ad">正在读取图谱…</text>`;
  try {
    const value = await api("/api/graph?limit=2000");
    state.graph.nodes = value.nodes || [];
    state.graph.edges = value.edges || [];
    state.graph.visibleTypes = new Set(state.graph.nodes.map((node) => node.type));
    const types = Object.entries(value.node_types || {});
    $("#graph-type-filters").innerHTML = types.map(([type, count]) => `<label><input type="checkbox" data-graph-type="${escapeHTML(type)}" checked />${escapeHTML(type)} <small>${count}</small></label>`).join("");
    $$('[data-graph-type]').forEach((input) => input.addEventListener("change", () => { input.checked ? state.graph.visibleTypes.add(input.dataset.graphType) : state.graph.visibleTypes.delete(input.dataset.graphType); renderGraph(); }));
    $("#graph-stats").innerHTML = [renderMetric("NODE", value.stats.nodes, "稳定节点"), renderMetric("EDGE", value.stats.edges, "有效关系"), renderMetric("HASH", String(value.digest || "").slice(0, 10), "确定性产物"), renderMetric("MODE", "LOCAL", "本地规则抽取")].join("");
    renderGraph();
  } catch (error) { svg.innerHTML = `<text x="500" y="340" text-anchor="middle" fill="#ff7d8b">${escapeHTML(error.message)}</text>`; }
}

async function reextractGraph() {
  const button = $("#graph-extract");
  button.disabled = true;
  try { const value = await api("/api/graph/extract", { method: "POST" }); toast(`图谱已重建：${value.stats.nodes} 节点 / ${value.stats.edges} 边`); await loadGraph(); }
  catch (error) { toast(`图谱重建失败：${error.message}`); }
  finally { button.disabled = false; }
}

function renderEvaluation(value) {
  const metrics = value.metrics || {};
  $("#evaluation-metrics").innerHTML = [renderMetric("SCORE", metrics.score ?? "—", "综合分"), renderMetric("RECALL", Math.round((metrics.retrieval_hit || 0) * 100) + "%", "检索命中"), renderMetric("CITE", Math.round((metrics.source_locator_coverage || 0) * 100) + "%", "来源定位覆盖"), renderMetric("REJECT", Math.round((metrics.rejection_accuracy || 0) * 100) + "%", "拒答正确")].join("");
  $("#evaluation-run-label").textContent = `${value.profile} · ${value.question_count} 题 · ${formatDate(value.finished_at)}`;
  $("#evaluation-cases").innerHTML = (value.cases || []).map((item) => `<article class="evaluation-case"><div><span class="badge ${item.metrics.retrieval_hit ? "ok" : "error"}">${escapeHTML(item.mode)}</span><strong>${escapeHTML(item.question)}</strong><small>${escapeHTML(item.answer.slice(0, 220))}</small></div><div class="case-score">${Math.round(Object.values(item.metrics).reduce((a, b) => a + Number(b), 0) / Math.max(1, Object.keys(item.metrics).length) * 100)}</div></article>`).join("");
}

async function loadEvaluation() {
  try { const runs = await api("/api/evaluation/runs?limit=1"); if (runs.items.length) renderEvaluation(await api(`/api/evaluation/runs/${runs.items[0].id}`)); }
  catch (error) { toast(`评估读取失败：${error.message}`); }
}

async function runEvaluation(profile) {
  const button = profile === "baseline" ? $("#run-baseline") : $("#run-optimized");
  button.disabled = true;
  $("#evaluation-run-label").textContent = `正在运行 ${profile}…`;
  try { const value = await api(`/api/evaluation/run?profile=${profile}`, { method: "POST" }); renderEvaluation(value); toast(`评估完成：${value.metrics.score}`); }
  catch (error) { toast(`评估失败：${error.message}`); }
  finally { button.disabled = false; }
}

async function loadSettings() {
  try {
    const value = await api("/api/settings");
    $("#settings-model").innerHTML = `<div><span>Provider</span><strong>${escapeHTML(value.llm.provider)}</strong></div><div><span>Model</span><strong>${escapeHTML(value.llm.model)}</strong></div><div><span>API 地址</span><code>${escapeHTML(value.llm.base_url)}</code></div><div><span>密钥</span><strong>${value.llm.configured ? "已配置（已脱敏）" : "未配置"}</strong></div>`;
    $("#settings-retrieval").innerHTML = `<div><span>Top K</span><strong>${value.retrieval.top_k}</strong></div><div><span>图关系扩展</span><strong>${value.retrieval.graph_expansion ? "开启" : "关闭"}</strong></div><div><span>向量检索</span><strong>${value.retrieval.vector_enabled ? "开启" : "关闭"}</strong></div><div><span>上下文预算</span><strong>${value.retrieval.max_context_chars} 字符</strong></div>`;
  } catch (error) { toast(`设置读取失败：${error.message}`); }
}

async function testModel() {
  const button = $("#test-model"); const result = $("#test-model-result"); button.disabled = true; result.textContent = "正在发起真实最小请求…";
  try { const value = await api("/api/settings/test", { method: "POST" }); result.textContent = `连接成功：${value.provider} / ${value.model} · ${value.latency_ms} ms · ${value.response}`; }
  catch (error) { result.textContent = `连接失败：${error.message}`; }
  finally { button.disabled = false; }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$('[data-jump]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.jump)));
  $("#header-upload").addEventListener("click", openUpload);
  $("#documents-upload").addEventListener("click", openUpload);
  $("#upload-form").addEventListener("submit", submitUpload);
  $("#upload-close").addEventListener("click", () => $("#upload-dialog").close());
  $("#upload-cancel").addEventListener("click", () => $("#upload-dialog").close());
  $("#detail-close").addEventListener("click", () => $("#document-dialog").close());
  $("#status-filter").addEventListener("change", loadDocuments);
  $("#document-search").addEventListener("input", () => {
    window.clearTimeout(documentSearchTimer);
    documentSearchTimer = window.setTimeout(loadDocuments, 250);
  });
  $("#wiki-search").addEventListener("input", () => {
    window.clearTimeout(documentSearchTimer);
    documentSearchTimer = window.setTimeout(loadWiki, 250);
  });
  $("#ask-form").addEventListener("submit", submitQuestion);
  $("#refresh-reviews").addEventListener("click", loadReviews);
  $("#graph-search").addEventListener("input", renderGraph);
  $("#graph-extract").addEventListener("click", reextractGraph);
  $("#graph-zoom-in").addEventListener("click", () => { state.graph.scale = Math.min(2.4, state.graph.scale * 1.18); renderGraph(); });
  $("#graph-zoom-out").addEventListener("click", () => { state.graph.scale = Math.max(.45, state.graph.scale / 1.18); renderGraph(); });
  $("#graph-reset").addEventListener("click", () => { state.graph.scale = 1; state.graph.panX = 0; state.graph.panY = 0; renderGraph(); });
  $("#graph-canvas").addEventListener("wheel", (event) => { event.preventDefault(); state.graph.scale = Math.max(.45, Math.min(2.4, state.graph.scale * (event.deltaY < 0 ? 1.1 : .9))); renderGraph(); }, { passive: false });
  $("#graph-canvas").addEventListener("pointerdown", (event) => { if (event.target.closest?.('[data-graph-node]')) return; state.graph.dragging = true; event.currentTarget.setPointerCapture?.(event.pointerId); });
  $("#graph-canvas").addEventListener("pointermove", (event) => { if (!state.graph.dragging) return; state.graph.panX += event.movementX; state.graph.panY += event.movementY; $("#graph-stage")?.setAttribute("transform", graphTransform()); });
  $("#graph-canvas").addEventListener("pointerup", () => { state.graph.dragging = false; });
  $("#graph-canvas").addEventListener("pointercancel", () => { state.graph.dragging = false; });
  $("#run-baseline").addEventListener("click", () => runEvaluation("baseline"));
  $("#run-optimized").addEventListener("click", () => runEvaluation("optimized"));
  $("#test-model").addEventListener("click", testModel);
}

async function boot() {
  bindEvents();
  await loadHealth();
  await loadOverview();
}

boot();
