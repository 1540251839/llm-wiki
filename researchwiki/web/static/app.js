const state = {
  health: null,
  overview: null,
  activeView: "dashboard",
  modelConfigured: false,
  pollTimer: null,
  askRequestId: 0,
  documentPage: 1,
  documentPageSize: 12,
  documentRequest: 0,
  askController: null,
  askHistory: [],
  currentAnswer: null,
  compilationBusy: false,
  evaluationRuns: [],
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
  const text = String(value).replace(" ", "T");
  const date = new Date(/Z$|[+-]\d\d:\d\d$/.test(text) ? text : text + "Z");
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
  if (!document.getElementById(`view-${name}`)) name = "dashboard";
  state.activeView = name;
  history.replaceState(null, "", `#${name}`);
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${name}`));
  const labels = {
    dashboard: ["OVERVIEW", "知识工作台"],
    documents: ["SOURCE MANAGEMENT", "资料中心"],
    compilation: ["KNOWLEDGE COMPILER", "知识编译"],
    wiki: ["COMPILED KNOWLEDGE", "Wiki 页面"],
    graph: ["KNOWLEDGE GRAPH", "图谱与检索"],
    ask: ["SOURCE-AWARE QA", "可溯源问答"],
    evaluation: ["REPLAYABLE QUALITY", "固定题库与评估"],
    reviews: ["HUMAN REVIEW", "Review 队列"],
    settings: ["RUNTIME", "系统设置"],
  };
  const label = labels[name] || labels.dashboard;
  $("#view-eyebrow").textContent = label[0];
  $("#view-title").textContent = label[1];
  if (name === "documents") loadDocuments();
  if (name === "compilation") loadCompilation();
  if (name === "wiki") loadWiki();
  if (name === "graph") loadGraph();
  if (name === "evaluation") loadEvaluation();
  if (name === "reviews") loadReviews();
  if (name === "settings") loadSettings();
  if (name === "ask") renderAskHistory();
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
  const requestId = ++state.documentRequest;
  const q = $("#document-search").value.trim();
  const status = $("#status-filter").value;
  const type = $("#type-filter").value;
  try {
    const value = await api(`/api/documents?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}&file_type=${encodeURIComponent(type)}&page=${state.documentPage}&page_size=${state.documentPageSize}`);
    if (requestId !== state.documentRequest) return;
    if (!value.items.length && state.documentPage > 1) { state.documentPage -= 1; return loadDocuments(); }
    $("#document-count").textContent = `${value.total} 条资料`;
    const pages = Math.max(1, Math.ceil(value.total / state.documentPageSize));
    $("#documents-page-info").textContent = `第 ${state.documentPage} / ${pages} 页 · 每页 ${state.documentPageSize} 条`;
    $("#documents-prev").disabled = state.documentPage <= 1;
    $("#documents-next").disabled = state.documentPage >= pages;
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
    $("#documents-body").innerHTML = `<tr><td colspan="6"><div class="error-state">资料加载失败：${escapeHTML(error.message)}</div></td></tr>`;
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
    const reasons = item.quality?.reasons || [];
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
        <div class="detail-cell"><span>处理模式 / 尝试</span><strong>${escapeHTML(extraction.extraction_mode || job.quality_mode || "basic")} · ${escapeHTML(job.attempts || 0)}/${escapeHTML(job.max_attempts || 0)}</strong></div>
      </div>
      ${reasons.length ? `<div class="notice">${reasons.map(escapeHTML).join("；")}</div>` : ""}
      ${item.error_message || job.error_message || job.last_error ? `<div class="detail-section"><h3>失败日志</h3><pre class="failure-log">${escapeHTML(item.error_message || job.error_message || job.last_error)}</pre></div>` : ""}
      <div class="detail-section"><h3>结构化抽取</h3>
        <p>${escapeHTML(extraction.abstract || "暂无摘要")}</p>
        <p><strong>关键词：</strong>${escapeHTML((extraction.keywords || []).join("、") || "—")}</p>
        <p><strong>方法：</strong>${escapeHTML((extraction.methods || []).map((x) => x.name || x).join("、") || "—")}</p>
        <p><strong>数据集：</strong>${escapeHTML((extraction.datasets || []).map((x) => x.name || x).join("、") || "—")}</p>
        <p><strong>主题：</strong>${escapeHTML((extraction.topics || []).join("、") || "—")}</p>
      </div>
      ${linkedPages ? `<div class="detail-section"><h3>关联 Wiki</h3><div class="source-list">${linkedPages}</div></div>` : ""}
      ${reviews.length ? `<div class="detail-section"><h3>待审核</h3><div class="review-list">${reviews.map((review) => `<div class="review-item"><div><span class="badge ${review.severity === "error" ? "error" : "warning"}">${escapeHTML(review.kind)}</span><p>${escapeHTML(review.message)}</p></div><button class="row-action" data-resolve-review="${review.id}">标为已处理</button></div>`).join("")}</div></div>` : ""}
      <div class="dialog-actions">
        ${item.raw_name ? `<a class="secondary" target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(item.id)}/file">查看原始资料 ↗</a>` : ""}
        ${item.status === "INDEXED" && extraction.extraction_mode === "basic" && state.modelConfigured && !["PENDING", "RUNNING"].includes(job.status) ? '<button class="primary" id="enrich-document">使用模型深度分析</button>' : ""}
        <button class="danger" id="delete-document">移到回收区</button>
        ${item.retryable ? `<button class="secondary" id="retry-document-high">高质量重试</button><button class="primary" id="retry-document">标准重试</button>` : ""}
      </div>`;
    $$('[data-related-page]', root).forEach((button) => button.addEventListener("click", () => goToWikiPage(button.dataset.relatedPage, button.dataset.relatedTitle)));
    $("#enrich-document")?.addEventListener("click", async () => {
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/enrich?quality_mode=standard`, { method: "POST" });
        toast("已加入深度分析队列");
        dialog.close();
        startPolling(id);
      } catch (error) { toast(error.message); }
    });
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

const wikiTypeLabels = {paper: "论文", topic: "主题", method: "方法", dataset: "数据集"};

function setWikiType(type) {
  $("#wiki-type-filter").value = type;
  $$(".wiki-type-tab").forEach((button) => {
    const selected = button.dataset.wikiType === type;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  loadWiki();
}

async function loadWiki() {
  const q = $("#wiki-search").value.trim();
  try {
    const value = await api(`/api/wiki/pages?q=${encodeURIComponent(q)}&type=${encodeURIComponent($("#wiki-type-filter").value)}&limit=500`);
    $("#wiki-count").textContent = value.items.length < value.total ? `${value.items.length} / ${value.total} 页` : `${value.total} 页`;
    const root = $("#wiki-list");
    root.innerHTML = value.items.map((item) => `<button class="wiki-item" data-page-id="${item.id}" data-page-title="${escapeHTML(item.title)}">${escapeHTML(item.title)}<small>${escapeHTML(wikiTypeLabels[item.type] || item.type)} · ${escapeHTML(item.path)}</small></button>`).join("") || `<div class="empty-state">没有页面。</div>`;
    $$('[data-page-id]', root).forEach((button) => button.addEventListener("click", () => openWikiPage(button)));
  } catch (error) { toast(`Wiki 加载失败：${error.message}`); }
}

async function goToWikiPage(id, title = "") {
  $("#document-dialog")?.close();
  $("#wiki-search").value = title;
  setWikiType("");
  showView("wiki");
  await loadWiki();
  const button = $(`[data-page-id="${CSS.escape(String(id))}"]`, $("#wiki-list"));
  if (button) await openWikiPage(button);
}

async function goToWikiTitle(title) {
  $("#wiki-search").value = title;
  $("#wiki-type-filter").value = "";
  showView("wiki");
  await loadWiki();
  const button = $$('[data-page-title]', $("#wiki-list")).find((item) => item.dataset.pageTitle === title);
  if (button) await openWikiPage(button);
}

function inlineMarkdown(text, citations = false, citationScope = "qa") {
  let value = escapeHTML(text);
  value = value.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_, title, label) => `<button class="inline-link" data-wiki-title="${title}">${label || title}</button>`);
  value = value.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>');
  if (citations) value = value.replace(/\[(\d+)\]/g, `<button class="citation-link" data-${citationScope === "wiki" ? "wiki-citation" : "citation"}="$1" aria-label="查看引用 $1">[$1]</button>`);
  return value;
}

function renderMarkdown(text, citations = false, citationScope = "qa") {
  const body = String(text || "").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").replace(/<!--[\s\S]*?-->/g, "");
  let code = false;
  let list = false;
  let quote = false;
  const output = [];
  for (const line of body.split(/\r?\n/)) {
    if (quote && !/^>\s?/.test(line)) { output.push("</blockquote>"); quote = false; }
    if (line.startsWith("```")) {
      if (list) { output.push("</ul>"); list = false; }
      output.push(code ? "</code></pre>" : '<pre class="code-block"><code>'); code = !code; continue;
    }
    if (code) { output.push(escapeHTML(line) + "\n"); continue; }
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
    if (li) { if (!list) output.push("<ul>"); list = true; output.push(`<li>${inlineMarkdown(li[1], citations, citationScope)}</li>`); continue; }
    if (list) { output.push("</ul>"); list = false; }
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    if (heading) { const level = Math.min(4, heading[1].length + 1); output.push(`<h${level}>${inlineMarkdown(heading[2], citations, citationScope)}</h${level}>`); }
    else if (/^>\s?/.test(line)) { if (!quote) { output.push("<blockquote>"); quote = true; } output.push(`${inlineMarkdown(line.replace(/^>\s?/, ""), false, citationScope)}<br>`); }
    else if (/^---+$/.test(line)) output.push("<hr>");
    else if (line.trim()) output.push(`<p>${inlineMarkdown(line, citations, citationScope)}</p>`);
  }
  if (list) output.push("</ul>");
  if (quote) output.push("</blockquote>");
  if (code) output.push("</code></pre>");
  return output.join("");
}

function sourceFileURL(source) {
  const rawURL = source.file_url || (source.source_id ? `/api/documents/${encodeURIComponent(source.source_id)}/file?inline=true` : "");
  if (!rawURL || !rawURL.startsWith("/api/documents/")) return "";
  return `${rawURL.split("#")[0]}${source.page_number ? `#page=${encodeURIComponent(source.page_number)}` : ""}`;
}

function evidenceMarkup(source) {
  const fileURL = sourceFileURL(source);
  const fallback = /fallback/i.test(String(source.confidence || "") + String(source.section || ""));
  const location = source.page_number ? `第 ${escapeHTML(source.page_number)} 页` : "来源级引用";
  return `<div class="evidence-snippet"><div class="evidence-meta"><strong>${escapeHTML(source.name || source.original_name || "原始资料")}</strong><span>${location}${source.paragraph ? ` · 第 ${escapeHTML(source.paragraph)} 段` : ""}</span></div>${source.section ? `<small>${escapeHTML(source.section)}</small>` : ""}${fallback ? '<small class="badge warning">背景片段，尚未精确匹配论断</small>' : ""}<blockquote>${escapeHTML(source.evidence_text || "当前来源暂无片段级证据，请打开原文核对。")}</blockquote><div class="evidence-actions">${fileURL ? `<a href="${escapeHTML(fileURL)}" target="_blank" rel="noopener">查看原文${source.page_number ? `第 ${escapeHTML(source.page_number)} 页` : ""} ↗</a>` : ""}${source.locator ? `<code title="${escapeHTML(source.locator)}">${escapeHTML(source.locator)}</code>` : ""}</div></div>`;
}

function bindWikiLinks(root) {
  $$('[data-related-page]', root).forEach((related) => related.addEventListener("click", () => goToWikiPage(related.dataset.relatedPage, related.dataset.relatedTitle)));
  $$('[data-wiki-title]', root).forEach((button) => button.addEventListener("click", () => goToWikiTitle(button.dataset.wikiTitle)));
}

async function openWikiPage(button) {
  $$(".wiki-item").forEach((node) => node.classList.toggle("active", node === button));
  const root = $("#wiki-detail");
  root.innerHTML = `<div class="empty-state">正在读取…</div>`;
  try {
    const page = await api(`/api/wiki/pages/${button.dataset.pageId}`);
    const sourceCards = (page.sources || []).map((source, index) => { const citationIndex = Number(source.citation_index || index + 1); return `<article class="citation-card wiki-source-card" id="wiki-source-${citationIndex}" tabindex="-1"><span class="citation-number">${citationIndex}</span>${evidenceMarkup(source)}</article>`; }).join("");
    const relatedCards = (page.related || []).map((item) => `<button class="source-card" type="button" data-related-page="${item.id}" data-related-title="${escapeHTML(item.title)}"><strong>${escapeHTML(item.title)}</strong><br>${escapeHTML(item.type)} · score ${Number(item.score || 0).toFixed(1)}</button>`).join("");
    const backlinks = (page.backlinks || []).map((item) => `<button class="inline-link" data-related-page="${item.id}" data-related-title="${escapeHTML(item.title)}">↶ ${escapeHTML(item.title)}</button>`).join("");
    root.innerHTML = `<div class="section-head"><div><p class="eyebrow">${escapeHTML(wikiTypeLabels[page.type] || page.type)} · ${(page.sources || []).length} 个来源</p><h2>${escapeHTML(page.title)}</h2></div></div><div class="wiki-location">${escapeHTML(page.path)}</div>${page.type === "topic" && state.modelConfigured && (page.sources || []).length >= 2 ? '<div class="detail-section"><button class="secondary" id="synthesize-topic" type="button">用模型生成跨论文综合（需复核）</button></div>' : ""}<div class="prose wiki-prose">${renderMarkdown(page.content, true, "wiki")}</div><details class="raw-details"><summary>查看 Markdown 原文</summary><pre class="markdown">${escapeHTML(page.content)}</pre></details><div class="detail-section"><h3>来源证据</h3><div class="evidence-strip">${sourceCards || '<p class="muted">暂无可回查来源，此页需要审核。</p>'}</div></div><div class="detail-section"><h3>反向链接</h3><div class="backlink-list">${backlinks || '<p class="muted">暂无页面引用此主题。</p>'}</div></div>${relatedCards ? `<div class="detail-section"><h3>相关主题</h3><div class="source-list">${relatedCards}</div></div>` : ""}`;
    bindWikiLinks(root);
    $("#synthesize-topic", root)?.addEventListener("click", async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      control.textContent = "正在比较已提取的论文…";
      try {
        await api(`/api/wiki/topics/${encodeURIComponent(page.id)}/synthesize`, { method: "POST" });
        toast("跨论文综合已保存到 Topic 页面，请核对原始证据");
        await openWikiPage(button);
      } catch (error) { toast(`跨论文综合失败：${error.message}`); control.disabled = false; control.textContent = "用模型生成跨论文综合（需复核）"; }
    });
    $$('[data-wiki-citation]', root).forEach((citation) => citation.addEventListener("click", () => { const card = $(`#wiki-source-${citation.dataset.wikiCitation}`, root); if (!card) return toast("该编号没有对应的来源证据"); $$(".wiki-source-card", root).forEach((item) => item.classList.remove("highlight")); card.classList.add("highlight"); card.scrollIntoView({ behavior: "smooth", block: "center" }); card.focus({ preventScroll: true }); }));
  } catch (error) { root.innerHTML = `<div class="empty-state">读取失败：${escapeHTML(error.message)}</div>`; }
}

function openUpload() {
  const message = $("#upload-message");
  const submit = $("#upload-submit");
  const offline = $("#quality-mode").value === "basic";
  message.textContent = offline ? "基础抽取在本机执行，不调用模型。" : state.modelConfigured ? "深度抽取将调用已配置的模型服务。" : "未配置模型密钥，请改用基础抽取。";
  submit.disabled = !offline && !state.modelConfigured;
  $("#upload-dialog").showModal();
}

async function submitUpload(event) {
  event.preventDefault();
  if (!state.modelConfigured && $("#quality-mode").value !== "basic") return;
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
    $("#drop-label").textContent = "拖入文件，或点击选择";
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
      const jobStatus = item.job?.status || "";
      if (jobStatus === "FAILED") { toast(item.status === "INDEXED" ? "深度分析失败；已有 Wiki 已保留" : "资料处理失败，请查看详情"); return; }
      if (jobStatus === "SUCCEEDED" || (!jobStatus && ["INDEXED", "FAILED"].includes(item.status))) {
        toast(item.status === "INDEXED" ? "资料处理完成" : "资料处理失败，请查看详情");
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

async function loadCompilation() {
  try {
    const [value, jobs] = await Promise.all([api("/api/compilation"), api("/api/jobs?limit=20")]);
    const inventory = value.inventory || {};
    const manifest = value.manifest || {};
    state.compilationBusy = Boolean(value.busy);
    $("#compile-build").disabled = state.compilationBusy;
    $("#compilation-metrics").innerHTML = [renderMetric("SOURCE", inventory.source_count ?? 0, "不可变原始资料"), renderMetric("READY", inventory.ready_sources ?? 0, "可参与编译"), renderMetric("PAGE", inventory.page_count ?? 0, "计划主题页"), renderMetric("CHANGE", (inventory.changes?.created?.length || 0) + (inventory.changes?.updated?.length || 0), "待创建或更新")].join("");
    const diagnostics = inventory.diagnostics || [];
    $("#compilation-progress").innerHTML = `<span class="badge ${value.busy ? "processing" : inventory.status === "blocked" ? "error" : "ok"}">${value.busy ? "正在处理" : inventory.status === "blocked" ? "需要处理问题" : "可编译"}</span><span>${value.busy ? "资料流水线或知识编译正在执行，请稍后刷新。" : `${inventory.ready_sources ?? 0} 份资料已具备抽取结果，${inventory.skipped_sources ?? 0} 份暂不参与。`}</span>${diagnostics.length ? `<details><summary>${diagnostics.length} 条诊断</summary>${diagnostics.map((item) => `<p class="diagnostic ${escapeHTML(item.severity)}">${escapeHTML(item.message)}</p>`).join("")}</details>` : ""}`;
    const changes = inventory.changes || {};
    const changeLabels = { created: "新建", updated: "更新", unchanged: "未变化", preserved: "保留手工内容", stale: "历史产物" };
    const pages = manifest.pages || [];
    $("#manifest-label").textContent = pages.length ? `${pages.length} 项已编译` : "尚无已提交清单";
    $("#compilation-manifest").innerHTML = `<div class="change-summary">${Object.entries(changeLabels).map(([key, label]) => `<span>${label} <strong>${changes[key]?.length || 0}</strong></span>`).join("")}</div><div class="manifest-list">${(inventory.sources || []).map((item) => `<div class="manifest-item"><span class="badge ${item.ready ? "ok" : "warning"}">${item.ready ? "就绪" : "跳过"}</span><div><strong>${escapeHTML(item.title)}</strong><small>${escapeHTML(item.reason || `${item.evidence_pages ?? 0} 个证据页面`)} · ${escapeHTML(item.raw_path || "")}</small></div></div>`).join("") || '<div class="empty-state">先在资料中心导入论文。</div>'}</div>${pages.length ? `<details class="raw-details"><summary>查看已提交页面清单（${pages.length}）</summary><div class="manifest-list">${pages.map((page) => `<div class="manifest-item"><div><strong>${escapeHTML(page.title || page.path)}</strong><small>${escapeHTML(page.type)} · ${escapeHTML(page.path)}</small></div></div>`).join("")}</div></details>` : ""}`;
    const queue = jobs.items || [];
    $("#compilation-jobs").innerHTML = queue.map((job) => `<div class="queue-item"><div><span class="badge ${job.status === "SUCCEEDED" ? "ok" : job.status === "FAILED" ? "error" : "processing"}">${escapeHTML(job.status)}</span><small>${escapeHTML(job.stage || "等待处理")}</small></div><code>${escapeHTML((job.source_id || "").slice(0, 16))}</code>${job.last_error || job.error_message ? `<details><summary>失败日志</summary><pre class="failure-log">${escapeHTML(job.last_error || job.error_message)}</pre></details>` : ""}</div>`).join("") || '<div class="empty-state">当前没有导入任务。</div>';
    $("#compilation-history").innerHTML = (value.history || []).map((run) => `<div class="build-record"><span class="badge ${run.status === "succeeded" ? "ok" : "error"}">${run.status === "succeeded" ? "已完成" : "失败"}</span><div><strong>${escapeHTML(run.page_count ?? run.pages ?? "—")} 页 · ${run.raw_unchanged === true ? "原件哈希未改变" : "查看诊断"}</strong><small>${escapeHTML(formatDate(run.finished_at || run.started_at))} · ${escapeHTML(String(run.content_digest || run.input_digest || "").slice(0, 16))}</small>${run.error ? `<p>${escapeHTML(run.error)}</p>` : ""}</div></div>`).join("") || '<div class="empty-state">尚无正式编译记录。点击“重建知识层”生成可回放产物。</div>';
  } catch (error) {
    $("#compilation-progress").innerHTML = `<div class="error-state">读取编译状态失败：${escapeHTML(error.message)}</div>`;
  }
}

async function buildKnowledge() {
  const button = $("#compile-build");
  button.disabled = true;
  button.textContent = "正在编译…";
  $("#compilation-progress").innerHTML = '<span class="badge processing">编译进行中</span><span>检查来源 → 编译主题页 → 更新证据索引与图谱 → 保存清单和日志</span>';
  try {
    const value = await api("/api/compilation/build", { method: "POST" });
    if (value.status === "failed") throw new Error(value.error || "编译失败，请检查诊断记录");
    toast("知识层已完成重建");
    await Promise.all([loadCompilation(), loadOverview()]);
  } catch (error) {
    $("#compilation-progress").innerHTML = `<div class="error-state">编译失败：${escapeHTML(error.message)}</div>`;
  } finally { button.disabled = false; button.textContent = "重建知识层"; }
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
  result.innerHTML = `<div class="answer-text prose">${renderMarkdown(value.answer, true)}</div>${value.fallback_reason ? `<div class="notice">远程模型不可用，已切换为本地证据：${escapeHTML(value.fallback_reason)}</div>` : ""}<div class="context-meter">使用 ${escapeHTML(context.pages_included ?? value.sources?.length ?? 0)} 个知识页 · 上下文 ${escapeHTML(context.used_chars || 0)} / ${escapeHTML(context.page_budget_chars || 0)} 字符</div>`;
  renderEvidence(value.sources || []);
  bindCitations(result);
  bindWikiLinks(result);
  state.currentAnswer = value;
}

function renderEvidence(sources) {
  $("#evidence-count").textContent = String(sources.length);
  $("#ask-evidence-list").innerHTML = sources.map((source, index) => `<article class="citation-card" id="citation-${Number(source.citation_index || index + 1)}" tabindex="-1"><div class="citation-heading"><span class="citation-number">${Number(source.citation_index || index + 1)}</span><div><strong>${escapeHTML(source.title)}</strong><small>${escapeHTML(source.type || "Wiki")} · ${escapeHTML(source.match_type || "检索来源")}</small></div></div>${(source.provenance || []).map(evidenceMarkup).join("") || '<p class="muted">此页暂无原文片段，请核查来源。</p>'}<button class="text-button" data-wiki-title="${escapeHTML(source.title)}">阅读 Wiki 主题 →</button></article>`).join("") || '<div class="empty-state">没有检索到可用证据。试试具体的方法名、数据集名，或先导入相关论文。</div>';
  bindWikiLinks($("#ask-evidence-list"));
}

function bindCitations(root) {
  $$('[data-citation]', root).forEach((button) => button.addEventListener("click", () => {
    const card = document.getElementById(`citation-${button.dataset.citation}`);
    if (!card) { toast("这个引用编号没有对应证据，请核对回答"); return; }
    $$(".citation-card").forEach((node) => node.classList.remove("highlight"));
    card.classList.add("highlight");
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.focus({ preventScroll: true });
  }));
}

function renderAskHistory() {
  $("#ask-history-list").innerHTML = state.askHistory.map((item, index) => `<button class="history-question" data-history-index="${index}"><strong>${escapeHTML(item.question)}</strong><small>${escapeHTML(formatDate(item.created_at))} · ${item.result.mode === "no_evidence" ? "无证据" : `${item.result.sources?.length || 0} 条来源`}</small></button>`).join("") || '<div class="empty-state">你的研究问题会保存在这里。</div>';
  $$('[data-history-index]').forEach((button) => button.addEventListener("click", () => {
    if (state.askController) return toast("请先等待当前回答完成，或点击停止");
    const item = state.askHistory[Number(button.dataset.historyIndex)];
    $("#ask-question").textContent = item.question; $("#ask-question").classList.remove("hidden");
    $("#ask-input").value = item.question;
    $("#ask-phases").innerHTML = '<span class="badge ok">历史回答</span>';
    renderAnswer(item.result, $("#ask-result"), $("#ask-mode"));
  }));
}

function saveAnswer(question, result) {
  state.askHistory.unshift({ question, result, created_at: new Date().toISOString() });
  state.askHistory = state.askHistory.slice(0, 25);
  try { localStorage.setItem("researchwiki.askHistory", JSON.stringify(state.askHistory)); } catch (_) { toast("浏览器存储空间不足，本次历史仅保留在当前会话"); }
  renderAskHistory();
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
  const controller = new AbortController();
  state.askController = controller;
  submit.disabled = true;
  $("#ask-stop").classList.remove("hidden");
  $("#ask-question").textContent = question; $("#ask-question").classList.remove("hidden");
  $("#ask-phases").innerHTML = '<span class="badge processing">检索证据</span>';
  $("#ask-evidence-list").innerHTML = '<div class="empty-state">正在检索可追溯来源…</div>';
  $("#evidence-count").textContent = "…";
  mode.textContent = "正在检索证据…";
  result.className = "answer empty-state";
  result.textContent = "正在读取 Wiki 并组织答案…";
  try {
    const response = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }), signal: controller.signal });
    if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`); }
    if (!response.body) throw new Error("当前浏览器不支持响应流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamedText = "";
    let completed = false;
    const consumeBlock = (block) => {
      const eventName = block.match(/^event:\s*(.+)$/m)?.[1]?.trim();
      const dataLines = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
      if (!dataLines || requestId !== state.askRequestId) return;
      const payload = JSON.parse(dataLines);
      if (eventName === "status") {
        mode.textContent = payload.message;
        $("#ask-phases").innerHTML = `<span class="badge processing">${escapeHTML(payload.message || payload.stage)}</span>`;
      }
      if (eventName === "sources") renderEvidence(Array.isArray(payload) ? payload : payload.sources || payload.items || []);
      if (eventName === "reset") { streamedText = ""; result.textContent = "正在切换回答方式…"; }
      if (eventName === "delta") { streamedText += payload.text || payload.delta || ""; result.className = "answer ready streaming"; result.innerHTML = `<div class="answer-text prose">${renderMarkdown(streamedText, true)}</div>`; bindCitations(result); }
      if (eventName === "error") throw new Error(payload.message);
      if (eventName === "result") { completed = true; renderAnswer(payload, result, mode); saveAnswer(question, payload); $("#ask-phases").innerHTML = `<span class="badge ${payload.mode === "no_evidence" ? "warning" : "ok"}">${payload.mode === "no_evidence" ? "没有足够证据" : "回答已完成"}</span>`; }
    };
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      buffer = buffer.replaceAll("\r\n", "\n");
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) consumeBlock(block);
      if (done) { if (buffer.trim()) consumeBlock(buffer); break; }
    }
    if (!completed) throw new Error("连接在回答完成前中断。已显示内容未完成，请重新提交问题。");
  } catch (error) {
    if (requestId !== state.askRequestId) return;
    const message = error.name === "AbortError" ? "已停止回答" : error.message;
    mode.textContent = message;
    $("#ask-phases").innerHTML = '<span class="badge warning">未完成</span>';
    const notice = document.createElement("div"); notice.className = "error-state"; notice.textContent = message;
    result.append(notice);
  } finally {
    if (requestId === state.askRequestId) { submit.disabled = false; state.askController = null; $("#ask-stop").classList.add("hidden"); }
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
  const relation = $("#graph-edge-filter").value;
  const edges = graph.edges.filter((edge) => positions.has(edge.source) && positions.has(edge.target) && (!relation || edge.semantic === relation));
  $("#graph-visible-count").textContent = `${visible.length} 个可见节点 · ${edges.length} 条关系${query ? ` · ${visible.filter((node) => node.name.toLocaleLowerCase().includes(query)).length} 个匹配` : ""}`;
  const edgeMarkup = edges.map((edge) => { const a = positions.get(edge.source); const b = positions.get(edge.target); return `<line class="graph-edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" data-semantic="${escapeHTML(edge.semantic)}" />`; }).join("");
  const nodeMarkup = visible.map((node) => { const p = positions.get(node.id); const match = !query || node.name.toLocaleLowerCase().includes(query); const radius = node.type === "PAGE" ? 8 : 6; return `<g class="graph-node ${match ? "match" : "dim"}" data-graph-node="${escapeHTML(node.id)}" role="button" tabindex="0" aria-label="${escapeHTML(node.type + " " + node.name)}" transform="translate(${p.x} ${p.y})"><circle r="${radius}" fill="${graphColors[node.type] || graphColors.TOPIC}"/><text y="${radius + 13}" text-anchor="middle">${escapeHTML(node.name.slice(0, 24))}</text></g>`; }).join("");
  svg.innerHTML = visible.length ? `<g id="graph-stage" transform="${graphTransform()}">${edgeMarkup}${nodeMarkup}</g>` : '<text x="500" y="340" text-anchor="middle" fill="currentColor">暂无可见节点，请先编译知识或调整筛选</text>';
  $$('[data-graph-node]', svg).forEach((node) => {
    node.addEventListener("click", () => selectGraphNode(node.dataset.graphNode));
    node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectGraphNode(node.dataset.graphNode); } });
  });
}

async function selectGraphNode(id) {
  const node = state.graph.nodes.find((item) => item.id === id);
  if (!node) return;
  const connections = state.graph.edges.filter((edge) => edge.source === id || edge.target === id);
  state.graph.selectedId = id;
  $$('[data-graph-node]').forEach((element) => element.classList.toggle("selected", element.dataset.graphNode === id));
  const sourceId = node.source_id || (node.type === "SOURCE" ? node.id.replace(/^source:/, "") : "");
  $("#graph-detail").innerHTML = `<p class="eyebrow">${escapeHTML(node.type)}</p><h3>${escapeHTML(node.name)}</h3><div class="settings-list"><div><span>稳定 ID</span><code>${escapeHTML(node.id)}</code></div><div><span>连接数</span><strong>${connections.length}</strong></div><div><span>来源定位</span><strong>${node.page_number ? `第 ${node.page_number} 页 · ${node.char_start}-${node.char_end}` : "元数据对象"}</strong></div><div><span>证据类型</span><strong>${escapeHTML(node.confidence || "metadata")}</strong></div></div>${node.page_id ? `<button class="primary" data-open-graph-page="${node.page_id}">打开 Wiki 页面</button>` : ""}${sourceId ? `<button class="secondary" data-graph-source="${escapeHTML(sourceId)}">查看来源资料</button>` : ""}<div id="graph-node-evidence"></div><div class="detail-section"><h3>关系</h3>${connections.slice(0, 24).map((edge) => { const peer = edge.source === id ? edge.target : edge.source; const other = state.graph.nodes.find((item) => item.id === peer); return `<button class="graph-connection" data-connected-node="${escapeHTML(peer)}"><code>${escapeHTML(edge.semantic)}</code><span>${edge.source === id ? "→" : "←"} ${escapeHTML(other?.name || peer)}</span></button>`; }).join("") || "<p>无连接</p>"}</div>`;
  $('[data-open-graph-page]')?.addEventListener("click", () => goToWikiPage(node.page_id, node.name));
  $('[data-graph-source]')?.addEventListener("click", () => openDocument(sourceId));
  $$('[data-connected-node]').forEach((button) => button.addEventListener("click", () => selectGraphNode(button.dataset.connectedNode)));
  if (node.page_id) {
    try { const page = await api(`/api/wiki/pages/${node.page_id}`); if (state.graph.selectedId === id) $("#graph-node-evidence").innerHTML = `<div class="detail-section"><h3>来源证据</h3>${(page.sources || []).map(evidenceMarkup).join("") || '<p class="muted">暂无来源证据</p>'}</div>`; }
    catch (error) { if (state.graph.selectedId === id) $("#graph-node-evidence").textContent = `证据加载失败：${error.message}`; }
  }
}

async function loadGraph() {
  const svg = $("#graph-canvas");
  svg.innerHTML = `<text x="500" y="340" text-anchor="middle" fill="#8d98ad">正在读取图谱…</text>`;
  try {
    const value = await api("/api/graph?limit=2000");
    state.graph.nodes = value.nodes || [];
    state.graph.edges = value.edges || [];
    state.graph.visibleTypes = new Set(state.graph.nodes.map((node) => node.type));
    $("#graph-edge-filter").innerHTML = '<option value="">全部关系</option>' + [...new Set(state.graph.edges.map((edge) => edge.semantic))].sort().map((semantic) => `<option value="${escapeHTML(semantic)}">${escapeHTML(semantic)}</option>`).join("");
    const types = Object.entries(value.node_types || {});
    $("#graph-type-filters").innerHTML = types.map(([type, count]) => `<label><input type="checkbox" data-graph-type="${escapeHTML(type)}" checked />${escapeHTML(type)} <small>${count}</small></label>`).join("");
    $$('[data-graph-type]').forEach((input) => input.addEventListener("change", () => { input.checked ? state.graph.visibleTypes.add(input.dataset.graphType) : state.graph.visibleTypes.delete(input.dataset.graphType); renderGraph(); }));
    $("#graph-stats").innerHTML = [renderMetric("NODE", value.stats.nodes, "稳定节点"), renderMetric("EDGE", value.stats.edges, "有效关系"), renderMetric("HASH", String(value.digest || "").slice(0, 10), "确定性产物"), renderMetric("MODE", "LOCAL", "本地规则抽取")].join("");
    renderGraph();
  } catch (error) { svg.innerHTML = `<text x="500" y="340" text-anchor="middle" fill="#ff7d8b">${escapeHTML(error.message)}</text>`; }
}

async function searchRetrieval(event) {
  event.preventDefault();
  const query = $("#retrieval-input").value.trim();
  if (!query) return;
  const root = $("#retrieval-results"); root.innerHTML = '<div class="empty-state">正在查询知识索引…</div>';
  try {
    const value = await api(`/api/search?q=${encodeURIComponent(query)}&top_k=8`);
    root.innerHTML = (value.items || []).map((item, index) => `<article class="retrieval-card"><div><span class="result-rank">${index + 1}</span><span class="badge ok">${escapeHTML(item.type || "Wiki")}</span></div><button class="inline-link" data-wiki-title="${escapeHTML(item.title)}">${escapeHTML(item.title)}</button><p>${escapeHTML(item.snippet || item.excerpt || item.content?.slice(0, 240) || "查看主题页和来源证据")}</p><small>得分 ${Number(item.score || 0).toFixed(3)} · ${escapeHTML(item.match_type || item.retrieval_method || "综合检索")}</small></article>`).join("") || '<div class="empty-state">没有命中。请尝试具体术语，或导入相关论文后重新编译。</div>';
    bindWikiLinks(root);
  } catch (error) { root.innerHTML = `<div class="error-state">检索失败：${escapeHTML(error.message)}</div>`; }
}

async function reextractGraph() {
  const button = $("#graph-extract");
  button.disabled = true;
  try { const value = await api("/api/graph/extract", { method: "POST" }); toast(`图谱已重建：${value.stats.nodes} 节点 / ${value.stats.edges} 边`); await loadGraph(); }
  catch (error) { toast(`图谱重建失败：${error.message}`); }
  finally { button.disabled = false; }
}

const evalMetricLabels = { completion: "回答完成", retrieval_hit: "检索命中", evidence_coverage: "证据覆盖", citation_validity: "引用有效", faithfulness: "答案忠实", honest_refusal: "拒答诚实", robustness: "输入鲁棒" };
const metricPercent = (value) => typeof value === "number" ? `${Math.round(value * 100)}%` : "不适用";

function renderEvaluation(value) {
  state.selectedEvaluationId = value.id;
  $("#replay-evaluation").disabled = !value.id;
  const metrics = value.metrics || {};
  $("#evaluation-metrics").innerHTML = renderMetric("SCORE", metrics.score ?? "—", "当前题库综合分") + Object.entries(evalMetricLabels).map(([key, label]) => renderMetric(key.replaceAll("_", " ").toUpperCase(), metricPercent(metrics[key]), label)).join("");
  $("#evaluation-run-label").textContent = `${value.profile} · ${value.question_count} 题 · ${formatDate(value.finished_at)}`;
  $("#evaluation-cases").innerHTML = (value.cases || []).map((item, index) => {
    const applicable = Object.entries(evalMetricLabels).filter(([key]) => typeof item.metrics?.[key] === "number" || typeof item.metrics?.[key] === "boolean");
    const score = applicable.length ? Math.round(applicable.reduce((sum, [key]) => sum + Number(item.metrics[key]), 0) / applicable.length * 100) : null;
    return `<details class="evaluation-case"><summary><div><span class="badge ${score === null ? "warning" : score >= 100 ? "ok" : "warning"}">${escapeHTML(item.category || item.mode || "评估")}</span><strong>${escapeHTML(item.question)}</strong></div><span class="case-score">${score ?? "—"}</span></summary><div class="case-content"><div class="case-metrics">${Object.entries(evalMetricLabels).map(([key, label]) => { const metric = item.metrics?.[key]; return `<span class="badge ${metric == null ? "neutral" : Number(metric) === 1 ? "ok" : "warning"}">${label} · ${metric == null ? "不适用" : metricPercent(Number(metric))}</span>`; }).join("")}</div><div class="prose">${renderMarkdown(item.answer || "没有答案")}</div><div class="case-sources">${(item.sources || []).map((source) => `<button class="inline-link" data-wiki-title="${escapeHTML(source.title)}">[${escapeHTML(source.citation_index || "?")}] ${escapeHTML(source.title)}</button>`).join("")}</div>${item.notes || item.reason ? `<p class="muted">${escapeHTML(item.notes || item.reason)}</p>` : ""}<button class="secondary" data-replay-case="${index}">在问答工作台重放 →</button></div></details>`;
  }).join("") || '<div class="empty-state">该运行没有逐题结果。</div>';
  bindWikiLinks($("#evaluation-cases"));
  $$('[data-replay-case]').forEach((button) => button.addEventListener("click", () => {
    showView("ask"); $("#ask-input").value = value.cases[Number(button.dataset.replayCase)].question; $("#ask-input").focus();
    toast("问题已填入，可提交以重新检索当前知识库");
  }));
}

function renderComparison(baseline, optimized, gate = null) {
  const root = $("#evaluation-comparison");
  if (!baseline || !optimized) { root.innerHTML = '<div class="empty-state">运行同一题库的基线与优化版，观察变化与逐题退步。</div>'; return; }
  const a = baseline.metrics || {}; const b = optimized.metrics || {};
  const comparable = gate || (a.corpus_fingerprint && a.corpus_fingerprint === b.corpus_fingerprint && a.question_set_hash === b.question_set_hash && a.rules_version === b.rules_version);
  if (!comparable) { root.innerHTML = '<div class="notice">历史运行的知识库快照或题库不同，不能直接比较。点击“运行对照”生成同一快照下的两组结果。</div>'; $("#evaluation-gate").textContent = "需要同快照对照"; return; }
  $("#evaluation-gate").textContent = gate ? (gate.passed ? "回归门通过" : `${gate.regressions?.length || 0} 项退步`) : "同快照对照";
  root.innerHTML = `<div class="table-wrap"><table class="comparison-table"><thead><tr><th>指标</th><th>基线</th><th>优化版</th><th>变化</th></tr></thead><tbody>${Object.entries(evalMetricLabels).map(([key, label]) => { const delta = typeof a[key] === "number" && typeof b[key] === "number" ? (b[key] - a[key]) * 100 : null; return `<tr><td>${label}</td><td>${metricPercent(a[key])}</td><td>${metricPercent(b[key])}</td><td class="${delta < 0 ? "negative" : "positive"}">${delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)} pp`}</td></tr>`; }).join("")}</tbody></table></div>${gate?.regressions?.length ? `<div class="notice">${gate.regressions.map((item) => `${escapeHTML(item.question_id)} · ${escapeHTML(evalMetricLabels[item.metric] || item.metric)} ${metricPercent(item.baseline)} → ${metricPercent(item.optimized)}`).join("<br>")}</div>` : ""}<p class="aside-note">固定题库上的规则评估。分数不代表开放问题的通用准确率。</p>`;
}

async function loadEvaluation() {
  try {
    const [runs, questions] = await Promise.all([api("/api/evaluation/runs?limit=20"), api("/api/evaluation/questions")]);
    state.evaluationRuns = runs.items;
    $("#evaluation-history").innerHTML = runs.items.map((run) => `<button class="evaluation-run" data-evaluation-run="${run.id}"><div><strong>${escapeHTML(run.profile)} <span>#${run.id}</span></strong><small>${escapeHTML(formatDate(run.finished_at))} · ${run.question_count} 题</small></div><span>${run.metrics?.score ?? "—"}</span></button>`).join("") || '<div class="empty-state">暂无运行记录。</div>';
    $$('[data-evaluation-run]').forEach((button) => button.addEventListener("click", async () => { try { renderEvaluation(await api(`/api/evaluation/runs/${button.dataset.evaluationRun}`)); } catch (error) { toast(error.message); } }));
    $("#question-bank-count").textContent = `${questions.total} 题`;
    $("#question-bank").innerHTML = (questions.items || []).map((item) => `<div class="question-bank-item"><code>${escapeHTML(item.id)}</code><span class="badge neutral">${escapeHTML(item.category || item.type || "知识检索")}</span><div><strong>${escapeHTML(item.question)}</strong><small>${item.expected_mode === "clarification" ? "预期：先请求澄清，不猜测用户意图" : item.expected_no_evidence || item.expected_refusal || item.should_refuse || item.expected_mode === "no_evidence" ? "预期：没有充分证据，明确拒答" : `预期：${escapeHTML((item.expected_source_titles || item.expected_titles || item.expected_terms || []).join("、") || "按证据回答")}`}</small></div></div>`).join("");
    if (runs.items.length) renderEvaluation(await api(`/api/evaluation/runs/${runs.items[0].id}`));
    renderComparison(runs.items.find((run) => run.profile === "baseline"), runs.items.find((run) => run.profile === "optimized"));
  } catch (error) { $("#evaluation-cases").innerHTML = `<div class="error-state">评估读取失败：${escapeHTML(error.message)}</div>`; }
}

async function runEvaluation(profile) {
  const buttons = [$("#run-baseline"), $("#run-optimized"), $("#run-comparison")]; buttons.forEach((button) => { button.disabled = true; });
  $("#evaluation-run-label").textContent = `正在运行 ${profile === "compare" ? "同快照对照" : profile}…`;
  try {
    const value = await api(profile === "compare" ? "/api/evaluation/compare" : `/api/evaluation/run?profile=${profile}`, { method: "POST" });
    await loadEvaluation();
    if (profile === "compare") { renderEvaluation(value.optimized); renderComparison(value.baseline, value.optimized, value.gate || value.comparison?.gate); toast(value.gate?.passed ? "对照完成，回归门通过" : "对照完成，请检查逐题变化"); }
    else { renderEvaluation(value); toast(`评估完成：${value.metrics.score}`); }
  } catch (error) { $("#evaluation-run-label").textContent = "运行失败"; toast(`评估失败：${error.message}`); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

async function replayEvaluation() {
  if (!state.selectedEvaluationId) return;
  const button = $("#replay-evaluation"); button.disabled = true;
  const runId = state.selectedEvaluationId;
  button.textContent = "正在重放…";
  try {
    const result = await api(`/api/evaluation/runs/${runId}/replay`, { method: "POST" });
    await loadEvaluation();
    renderEvaluation(result);
    toast(`快照 #${runId} 重放完成：${result.metrics?.score ?? "—"} 分`);
  } catch (error) { toast(`无法重放：${error.message}`); }
  finally { button.disabled = false; button.textContent = "重放本次快照"; }
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
  $("#quality-mode").addEventListener("change", () => { if ($("#upload-dialog").open) { const offline = $("#quality-mode").value === "basic"; $("#upload-submit").disabled = !offline && !state.modelConfigured; $("#upload-message").textContent = offline ? "基础抽取在本机执行，不调用模型。" : state.modelConfigured ? "深度抽取将调用模型服务。" : "未配置模型密钥，请选择基础抽取。"; } });
  $("#upload-close").addEventListener("click", () => $("#upload-dialog").close());
  $("#upload-cancel").addEventListener("click", () => $("#upload-dialog").close());
  $("#detail-close").addEventListener("click", () => $("#document-dialog").close());
  $("#status-filter").addEventListener("change", () => { state.documentPage = 1; loadDocuments(); });
  $("#type-filter").addEventListener("change", () => { state.documentPage = 1; loadDocuments(); });
  $("#documents-prev").addEventListener("click", () => { state.documentPage = Math.max(1, state.documentPage - 1); loadDocuments(); });
  $("#documents-next").addEventListener("click", () => { state.documentPage += 1; loadDocuments(); });
  $("#document-search").addEventListener("input", () => {
    state.documentPage = 1;
    window.clearTimeout(documentSearchTimer);
    documentSearchTimer = window.setTimeout(loadDocuments, 250);
  });
  $("#wiki-search").addEventListener("input", () => {
    window.clearTimeout(documentSearchTimer);
    documentSearchTimer = window.setTimeout(loadWiki, 250);
  });
  $$(".wiki-type-tab").forEach((button) => button.addEventListener("click", () => setWikiType(button.dataset.wikiType)));
  $("#wiki-type-filter").addEventListener("change", () => setWikiType($("#wiki-type-filter").value));
  $("#compile-build").addEventListener("click", buildKnowledge);
  $("#compilation-refresh").addEventListener("click", loadCompilation);
  $("#ask-form").addEventListener("submit", submitQuestion);
  $("#ask-stop").addEventListener("click", () => state.askController?.abort());
  $("#new-question").addEventListener("click", () => { if (state.askController) return toast("请先停止当前回答"); $("#ask-input").value = ""; $("#ask-input").focus(); $("#ask-question").classList.add("hidden"); $("#ask-phases").innerHTML = ""; $("#ask-result").className = "answer empty-state"; $("#ask-result").textContent = "开始一个新的研究问题。"; $("#ask-evidence-list").innerHTML = '<div class="empty-state">下一次检索的证据会显示在这里。</div>'; $("#evidence-count").textContent = "0"; });
  $$('[data-question]').forEach((button) => button.addEventListener("click", () => { $("#ask-input").value = button.dataset.question; $("#ask-input").focus(); }));
  $("#refresh-reviews").addEventListener("click", loadReviews);
  $("#graph-search").addEventListener("input", renderGraph);
  $("#graph-edge-filter").addEventListener("change", renderGraph);
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
  $("#run-comparison").addEventListener("click", () => runEvaluation("compare"));
  $("#replay-evaluation").addEventListener("click", replayEvaluation);
  $("#retrieval-form").addEventListener("submit", searchRetrieval);
  $("#test-model").addEventListener("click", testModel);
  $("#theme-toggle").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme; try { localStorage.setItem("researchwiki.theme", theme); } catch (_) {} });
  $("#file-input").addEventListener("change", () => { $("#drop-label").textContent = $("#file-input").files[0]?.name || "拖入文件，或点击选择"; });
  const zone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("drag-active"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("drag-active"); }));
  zone.addEventListener("drop", (event) => { const file = event.dataTransfer?.files[0]; if (!file) return; if (!/\.(pdf|docx|txt|md|markdown)$/i.test(file.name)) { toast("请选择 PDF、Word、TXT 或 Markdown 文件"); return; } if (file.size > 50 * 1024 * 1024) { toast("单个文件不能超过 50 MiB"); return; } const transfer = new DataTransfer(); transfer.items.add(file); $("#file-input").files = transfer.files; $("#drop-label").textContent = file.name; });
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
}

async function boot() {
  try { document.documentElement.dataset.theme = localStorage.getItem("researchwiki.theme") || "light"; const saved = JSON.parse(localStorage.getItem("researchwiki.askHistory") || "[]"); state.askHistory = Array.isArray(saved) ? saved.filter((item) => item && typeof item.question === "string" && item.result && typeof item.result.answer === "string").slice(0, 25) : []; } catch (_) { document.documentElement.dataset.theme = "light"; }
  bindEvents();
  await loadHealth();
  await loadOverview();
  if (location.hash) showView(location.hash.slice(1));
  renderAskHistory();
}

boot();
