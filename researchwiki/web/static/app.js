const state = {
  health: null,
  overview: null,
  activeView: "documents",
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
  graph: { nodes: [], edges: [], visibleTypes: new Set(), scale: 1, panX: 0, panY: 0, dragging: false, center: "", renderer: "svg", g6: null },
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
  if (!document.getElementById(`view-${name}`)) name = "documents";
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
    reviews: ["HUMAN REVIEW", "Review 队列"],
    settings: ["RUNTIME", "系统设置"],
  };
  const label = labels[name] || labels.documents;
  $("#view-eyebrow").textContent = label[0];
  $("#view-title").textContent = label[1];
  if (name === "documents") loadDocuments();
  if (name === "compilation") loadCompilation();
  if (name === "wiki") loadWiki();
  if (name === "graph") loadGraph();
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
        <div class="detail-cell"><span>原文检索</span><strong>${item.original_text_indexed ? `已索引 · ${item.original_text_chunks} 个片段` : "未建立索引"}</strong></div>
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
        ${item.status === "INDEXED" && state.modelConfigured && !["PENDING", "RUNNING"].includes(job.status) ? `<button class="primary" id="enrich-document">${extraction.extraction_mode === "basic" ? "使用模型深度分析" : "重新生成中文解读"}</button>` : ""}
        ${item.status === "INDEXED" && state.modelConfigured ? `<button class="secondary" data-supplement-focus="method">只补方法</button><button class="secondary" data-supplement-focus="experiment">只补实验</button><button class="secondary" data-supplement-focus="overview">只补研究概览</button>` : ""}
        <button class="danger" id="delete-document">移到回收区</button>
        ${item.retryable ? `<button class="secondary" id="retry-document-high">高质量重试</button><button class="primary" id="retry-document">标准重试</button>` : ""}
      </div>`;
    $$('[data-related-page]', root).forEach((button) => button.addEventListener("click", () => goToWikiPage(button.dataset.relatedPage, button.dataset.relatedTitle)));
    $("#enrich-document")?.addEventListener("click", async () => {
      if (!window.confirm("将调用模型更新自动生成的知识内容；原始英文和人工笔记会保留。继续吗？")) return;
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/enrich?quality_mode=standard`, { method: "POST" });
        toast("已加入深度分析队列");
        dialog.close();
        startPolling(id);
      } catch (error) { toast(error.message); }
    });
    $('[data-supplement-focus]',root).forEach(control=>control.addEventListener("click", async()=>{
      const focus=control.dataset.supplementFocus;
      if(!window.confirm("只更新所选知识字段，其余论文内容保持不变。此操作会调用已配置的模型。继续吗？"))return;
      const old=control.textContent;control.disabled=true;control.textContent="正在补充…";
      try{
        await api(`/api/documents/${encodeURIComponent(id)}/supplement?focus=${encodeURIComponent(focus)}`,{
          method:"POST",headers:{"X-RW-Config-Action":"1"}
        });
        toast("字段补充完成；可查看更新后的 Wiki");
        dialog.close();
        await Promise.all([loadDocuments(),loadOverview()]);
      }catch(error){toast(`补充失败：${error.message}`);}
      finally{control.disabled=false;control.textContent=old;}
    }));
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
    root.innerHTML = value.items.map((item) => `<button class="wiki-item" data-page-id="${item.id}" data-page-title="${escapeHTML(item.title)}">${escapeHTML(item.display_title || item.title)}<small>${escapeHTML(wikiTypeLabels[item.type] || item.type)} · ${escapeHTML(item.path)}</small></button>`).join("") || `<div class="empty-state">没有页面。</div>`;
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

const wikiQuestions = {
  paper: "这篇论文研究什么、怎么做、得到什么结果？",
  topic: "这个方向是什么、有哪些具体研究问题？",
  method: "这个方法是什么、怎样工作、在哪些研究中使用？",
  dataset: "这个数据集研究什么任务，哪些论文怎样使用它？",
};

function renderWikiReading(markdown, type) {
  const body = String(markdown || "").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "");
  const matches = [...body.matchAll(/^##\s+(.+)$/gm)];
  if (!matches.length) return renderMarkdown(body, true, "wiki");
  return matches.map((match, index) => {
    const name = match[1].trim();
    const start = match.index + match[0].length;
    const end = matches[index + 1]?.index ?? body.length;
    const section = body.slice(start, end).trim();
    const content = renderMarkdown(section, true, "wiki");
    const supplementary = /^(原文摘要|字段级出处|原文来源|来源证据|证据说明|关键词)/.test(name);
    return supplementary
      ? `<details class="wiki-supplement"><summary>${escapeHTML(name)}</summary><div class="prose">${content}</div></details>`
      : `<section class="wiki-section"><h3>${escapeHTML(name)}</h3><div class="prose">${content}</div></section>`;
  }).join("");
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
    const displayTitle = page.display_title || page.title;
    const nativeTitle = displayTitle !== page.title ? `<p class="wiki-original-title">原始名称：${escapeHTML(page.title)}</p>` : "";
    const synthesisControl = page.type === "topic" && state.modelConfigured && (page.sources || []).length >= 2
      ? '<button class="secondary" id="synthesize-topic" type="button">生成跨论文综合（需复核）</button>' : "";
    root.innerHTML = `
      <header class="wiki-reading-header">
        <p class="eyebrow">${escapeHTML(wikiTypeLabels[page.type] || page.type)} · ${(page.sources || []).length} 个来源</p>
        <h2>${escapeHTML(displayTitle)}</h2>
        ${nativeTitle}
        <p class="wiki-reading-question">${escapeHTML(wikiQuestions[page.type] || "阅读并核对相关知识")}</p>
        ${synthesisControl}
      </header>
      <article class="wiki-reading-body">${renderWikiReading(page.content, page.type)}</article>
      <details class="wiki-supplement">
        <summary>查看来源、原文定位与证据片段（${(page.sources || []).length}）</summary>
        <div class="evidence-strip">${sourceCards || '<p class="muted">尚无可回查来源，请先审核。</p>'}</div>
      </details>
      <details class="wiki-supplement">
        <summary>引用它的页面与相关知识</summary>
        <div class="backlink-list">${backlinks || '<p class="muted">暂无反向链接。</p>'}</div>
        ${relatedCards ? `<div class="source-list">${relatedCards}</div>` : ""}
      </details>
      <details class="wiki-supplement"><summary>高级信息：路径与 Markdown</summary>
        <p class="wiki-location">${escapeHTML(page.path)}</p>
        <pre class="markdown">${escapeHTML(page.content)}</pre>
      </details>`;
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
  message.textContent = offline ? "快速导入将在本机建立全文索引，不调用模型。" : state.modelConfigured ? "深度解读将调用已配置的模型服务。" : "未配置模型密钥，请改用基础抽取。";
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

const graphColors = {
  PAPER: "#397fb7", TOPIC: "#8b63b7", METHOD: "#258f84", DATASET: "#d08b3e"
};
const graphTypeNames = {PAPER:"论文", TOPIC:"主题", METHOD:"方法", DATASET:"数据集"};
const graphRelationNames = {
  STUDIES_TOPIC: "研究主题", REFERENCES_METHOD: "涉及方法",
  MENTIONS_DATASET: "提及数据集", TESTED_ON: "测试关联（待核对）",
  TRAINED_ON: "训练关联（待核对）", VALIDATED_ON: "验证关联（待核对）", RELATED: "相关"
};

function graphTransform() {
  const graph = state.graph;
  return `translate(${graph.panX} ${graph.panY}) translate(500 340) scale(${graph.scale}) translate(-500 -340)`;
}

function stableHash(value) {
  let h = 2166136261;
  for (const char of String(value)) { h ^= char.charCodeAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function graphPositions(visible, edges) {
  const graph = state.graph;
  const positions = new Map();
  visible.forEach((node, index) => {
    const seed = stableHash(node.id);
    const angle = ((seed % 997) / 997) * Math.PI * 2;
    const radius = 110 + (Math.floor(seed / 997) % 180);
    positions.set(node.id, {x: 500 + Math.cos(angle) * radius, y: 340 + Math.sin(angle) * radius});
  });
  if (graph.center && positions.has(graph.center)) positions.set(graph.center, {x:500,y:340});
  const width = 950, height = 630;
  for (let step = 0; step < 64; step++) {
    const forces = new Map(visible.map(node => [node.id, {x:0,y:0}]));
    for (let i = 0; i < visible.length; i++) {
      const a = positions.get(visible[i].id);
      for (let j = i + 1; j < visible.length; j++) {
        const b = positions.get(visible[j].id);
        const dx = a.x-b.x, dy = a.y-b.y, d2 = dx*dx+dy*dy+120;
        const force = Math.min(28, 14000/d2);
        const len = Math.sqrt(d2);
        forces.get(visible[i].id).x += dx/len*force;
        forces.get(visible[i].id).y += dy/len*force;
        forces.get(visible[j].id).x -= dx/len*force;
        forces.get(visible[j].id).y -= dy/len*force;
      }
    }
    for (const edge of edges) {
      const u = positions.get(edge.source),v = positions.get(edge.target);
      if (!u || !v) continue;
      const dx=v.x-u.x, dy=v.y-u.y, distance=Math.hypot(dx,dy)||1;
      const force=(distance-155)*0.014;
      forces.get(edge.source).x += dx/distance*force;
      forces.get(edge.source).y += dy/distance*force;
      forces.get(edge.target).x -= dx/distance*force;
      forces.get(edge.target).y -= dy/distance*force;
    }
    visible.forEach(node => {
      if (node.id === graph.center) return;
      const p=positions.get(node.id), f=forces.get(node.id);
      p.x=Math.min(width,Math.max(45,p.x+Math.max(-18,Math.min(18,f.x+(500-p.x)*0.004))));
      p.y=Math.min(height,Math.max(42,p.y+Math.max(-18,Math.min(18,f.y+(340-p.y)*0.004))));
    });
  }
  return positions;
}

function visibleResearchGraph() {
  const graph=state.graph;
  const nodes=graph.nodes.filter(n=>graph.visibleTypes.has(n.type));
  const ids=new Set(nodes.map(n=>n.id));
  const kind=$("#graph-edge-filter").value;
  const edges=graph.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)&&(!kind||e.semantic===kind));
  return {nodes,edges};
}

function renderGraph() {
  const graph=state.graph;
  const {nodes,edges}=visibleResearchGraph();
  const query=$("#graph-search").value.trim().toLocaleLowerCase();
  $("#graph-visible-count").textContent=`${nodes.length} 个科研对象 · ${edges.length} 条关系`;
  if (graph.renderer === "g6" && window.G6?.Graph) {
    renderG6ResearchGraph(nodes,edges);
    return;
  }
  $("#graph-canvas").classList.remove("hidden");
  $("#graph-g6").classList.add("hidden");
  const svg=$("#graph-canvas");
  const positions=graphPositions(nodes,edges);
  const edgeMarkup=edges.map(edge=>{
    const a=positions.get(edge.source),b=positions.get(edge.target);
    return `<line class="graph-edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}">
      <title>${escapeHTML(graphRelationNames[edge.semantic]||edge.semantic)}</title></line>`;
  }).join("");
  const nodeMarkup=nodes.map(node=>{
    const p=positions.get(node.id),match=!query||node.name.toLowerCase().includes(query);
    const selected=graph.selectedId===node.id;
    return `<g class="graph-node ${match?"match":"dim"} ${selected?"selected":""}"
      data-graph-node="${escapeHTML(node.id)}" role="button" tabindex="0"
      aria-label="${escapeHTML((graphTypeNames[node.type]||node.type)+" "+node.name)}"
      transform="translate(${p.x} ${p.y})"><circle r="${selected?13:10}"
      fill="${graphColors[node.type]||"#778899"}"/>
      <text y="27" text-anchor="middle">${escapeHTML(node.name.slice(0,20))}</text>
      <title>${escapeHTML(node.name)}</title></g>`;
  }).join("");
  svg.innerHTML=nodes.length?`<g id="graph-stage" transform="${graphTransform()}">${edgeMarkup}${nodeMarkup}</g>`
    : '<text x="500" y="340" text-anchor="middle" fill="currentColor">尚无科研关系，请先导入并编译论文</text>';
  $$('[data-graph-node]',svg).forEach(node=>{
    node.addEventListener("click",()=>selectGraphNode(node.dataset.graphNode));
    node.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectGraphNode(node.dataset.graphNode);}});
  });
}

function renderG6ResearchGraph(nodes,edges) {
  const graph=state.graph;
  const host=$("#graph-g6");
  $("#graph-canvas").classList.add("hidden");
  host.classList.remove("hidden");
  if (graph.g6) { graph.g6.destroy(); graph.g6=null; }
  try {
    const instance=new window.G6.Graph({
      container:host,
      autoFit:"view",
      data:{
        nodes:nodes.map(n=>({id:n.id,style:{size:26,fill:graphColors[n.type],
          labelText:n.name.length>22?n.name.slice(0,21)+"…":n.name,
          labelPlacement:"bottom",labelFontSize:11}})),
        edges:edges.map(e=>({id:e.id,source:e.source,target:e.target,
          style:{stroke:"#98a6b5",lineWidth:1.1}}))
      },
      layout:{type:"d3-force",animation:false,manyBody:{strength:-250},
        link:{distance:145},x:{},y:{}},
      behaviors:["drag-canvas","zoom-canvas","drag-element"],
    });
    graph.g6=instance;
    instance.on("node:click",(ev)=>{const id=ev.target?.id||ev.item?.id;if(id)selectGraphNode(id);});
    const result=instance.render();
    if(result?.catch)result.catch(()=>{graph.renderer="svg";renderGraph();toast("G6 渲染失败，已切换本地图谱");});
  } catch(error) {
    graph.renderer="svg";
    toast("G6 不可用，已切换本地图谱");
    renderGraph();
  }
}

async function enableG6ResearchGraph() {
  if (!window.G6?.Graph) {
    const btn=$("#graph-enable-g6");btn.disabled=true;btn.textContent="正在加载 G6…";
    try {
      await new Promise((resolve,reject)=>{
        const tag=document.createElement("script");
        tag.src="https://cdn.jsdelivr.net/npm/@antv/g6@5.1.1/dist/g6.min.js";
        tag.async=true;tag.onload=resolve;tag.onerror=reject;
        document.head.appendChild(tag);
      });
    } catch { toast("无法加载 G6，仍可使用本地布局");btn.disabled=false;btn.textContent="G6 力导向（可选）";return; }
    btn.disabled=false;btn.textContent="G6 力导向";
  }
  state.graph.renderer=state.graph.renderer==="g6"?"svg":"g6";
  $("#graph-enable-g6").textContent=state.graph.renderer==="g6"?"本地布局":"G6 力导向";
  renderGraph();
}

async function selectGraphNode(id) {
  const graph=state.graph;
  const node=graph.nodes.find(item=>item.id===id);
  if (!node) return;
  graph.selectedId=id;
  const connections=graph.edges.filter(edge=>edge.source===id||edge.target===id);
  if (graph.renderer==="svg") renderGraph();
  $("#graph-detail").innerHTML=`<p class="eyebrow">${escapeHTML(graphTypeNames[node.type]||node.type)}</p>
    <h3>${escapeHTML(node.name)}</h3>
    <p class="muted">与 ${connections.length} 个知识关系相连。点击可展开更多关联。</p>
    <div class="settings-actions">
      <button class="primary" id="graph-expand-node">展开一跳关系</button>
      ${node.page_id?`<button class="secondary" data-open-graph-page="${node.page_id}">阅读 Wiki</button>`:""}
    </div>
    <div class="detail-section"><h3>关联对象</h3>
      ${connections.slice(0,30).map(edge=>{
        const peer=edge.source===id?edge.target:edge.source;
        const linked=graph.nodes.find(item=>item.id===peer);
        const type=graphRelationNames[edge.semantic]||edge.semantic;
        return `<button class="graph-connection" data-connected-node="${escapeHTML(peer)}">
          <code>${escapeHTML(type)}</code>
          <span>${escapeHTML(linked?.name||peer)}</span></button>`;
      }).join("")||"<p>当前视图没有关联对象。</p>"}
    </div>
    <details class="advanced-details" id="graph-node-evidence"><summary>原始证据与定位</summary>
      <p class="muted">打开后查看原始论文页码和摘录。</p></details>`;
  $("#graph-expand-node")?.addEventListener("click",()=>loadGraph(id));
  $('[data-open-graph-page]')?.addEventListener("click",()=>goToWikiPage(node.page_id,node.original_name||node.name));
  $$('[data-connected-node]').forEach(button=>button.addEventListener("click",()=>selectGraphNode(button.dataset.connectedNode)));
  if (node.page_id) {
    try {
      const page=await api(`/api/wiki/pages/${node.page_id}`);
      if (graph.selectedId===id)$("#graph-node-evidence").innerHTML=
        `<summary>原始证据与定位</summary>${(page.sources||[]).map(evidenceMarkup).join("")||'<p class="muted">尚无来源证据。</p>'}`;
    } catch { /* The graph remains useful if source preview cannot load. */ }
  }
}

async function loadGraph(center = "") {
  const svg=$("#graph-canvas");
  if(graph.renderer!=="g6")svg.innerHTML='<text x="500" y="340" text-anchor="middle" fill="currentColor">正在读取科研关系…</text>';
  try {
    const value=await api(`/api/graph/research?center=${encodeURIComponent(center)}&depth=1&limit=220`);
    state.graph.nodes=value.nodes||[];
    state.graph.edges=value.edges||[];
    state.graph.center=value.center||"";
    state.graph.visibleTypes=new Set(state.graph.nodes.map(node=>node.type));
    const semantics=[...new Set(state.graph.edges.map(edge=>edge.semantic))].sort();
    $("#graph-edge-filter").innerHTML='<option value="">全部关系</option>'+
      semantics.map(key=>`<option value="${escapeHTML(key)}">${escapeHTML(graphRelationNames[key]||key)}</option>`).join("");
    const types=Object.entries(value.node_types||{});
    $("#graph-type-filters").innerHTML=types.map(([type,count])=>
      `<label><input type="checkbox" data-graph-type="${escapeHTML(type)}" checked />${escapeHTML(graphTypeNames[type]||type)} <small>${count}</small></label>`).join("");
    $$('[data-graph-type]').forEach(input=>input.addEventListener("change",()=>{
      input.checked?state.graph.visibleTypes.add(input.dataset.graphType):state.graph.visibleTypes.delete(input.dataset.graphType);
      renderGraph();
    }));
    $("#graph-stats").innerHTML=[
      renderMetric("OBJECTS",value.stats?.nodes??0,"知识对象总数"),
      renderMetric("LINKS",value.stats?.edges??0,"科研对象关系"),
      renderMetric("VISIBLE",value.nodes?.length??0,"当前视图"),
      renderMetric("VIEW",center==="__all__"?"GLOBAL":"LOCAL","关系范围"),
    ].join("");
    if (!center && state.graph.nodes.length) selectGraphNode(state.graph.center);
    else renderGraph();
  } catch(error) {svg.innerHTML=`<text x="500" y="340" text-anchor="middle" fill="currentColor">${escapeHTML(error.message)}</text>`;}
}

async function searchOriginalSource(event) {
  event.preventDefault();
  const query = $("#source-search-input").value.trim();
  if (!query) return;
  const root = $("#source-search-results");
  root.innerHTML = '<div class="empty-state">正在从原始论文中查找…</div>';
  try {
    const value = await api(`/api/source-search?q=${encodeURIComponent(query)}&top_k=12`);
    root.innerHTML = (value.items || []).map(item => `
      <article class="original-result">
        <div class="original-result-head">
          <strong>${escapeHTML(item.title || item.filename)}</strong>
          <small>第 ${escapeHTML(item.page_number)} 页 · 原文直接命中</small>
        </div>
        <p>${escapeHTML(item.excerpt)}</p>
        <a href="${escapeHTML(item.file_url)}" target="_blank" rel="noopener noreferrer">打开原文核对 ↗</a>
      </article>
    `).join("") || '<div class="empty-state">没有找到原文片段。可以尝试更具体的术语；扫描型 PDF 需要先 OCR。</div>';
  } catch (error) {
    root.innerHTML = `<div class="error-state">原文检索失败：${escapeHTML(error.message)}</div>`;
  }
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

async function loadSettings() {
  try {
    const value = await api("/api/settings");
    const configured = Boolean(value.credentials?.configured || value.llm?.configured);
    $("#settings-model").innerHTML = `<div><span>提供方</span><strong>${escapeHTML(value.llm.provider)}</strong></div><div><span>当前模型</span><strong>${escapeHTML(value.llm.model)}</strong></div><div><span>连接状态</span><strong>${configured ? "已配置密钥" : "未配置密钥"}</strong></div><div><span>本机保存</span><strong>${value.credentials?.stored_locally ? "已保存（不显示密钥）" : "无本地凭据文件"}</strong></div>`;
    $("#settings-retrieval").innerHTML = `<div><span>知识语言</span><strong>${value.wiki?.language === "zh-CN" ? "中文优先 · 英文原文保留" : "English"}</strong></div><div><span>索引</span><strong>SQLite FTS5</strong></div><div><span>可选向量检索</span><strong>${value.retrieval.vector_enabled ? "开启" : "关闭"}</strong></div>`;
  } catch (error) { toast(`设置读取失败：${error.message}`); }
}

async function saveDeepSeekKey(event) {
  event.preventDefault();
  const input = $("#deepseek-key");
  const control = $("#save-model-key");
  const result = $("#test-model-result");
  control.disabled = true;
  result.textContent = "正在保存本地密钥…";
  try {
    await api("/api/settings/deepseek-key", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-RW-Config-Action": "1"},
      body: JSON.stringify({api_key: input.value.trim()}),
    });
    input.value = "";
    result.textContent = "已保存在本机。可点击“测试连接”验证有效性。";
    await Promise.all([loadSettings(), loadHealth()]);
  } catch (error) { result.textContent = `保存失败：${error.message}`; }
  finally { control.disabled = false; }
}

async function clearDeepSeekKey() {
  if (!window.confirm("确定删除这台电脑上保存的 DeepSeek Key 吗？这不会在 DeepSeek 平台撤销该密钥。")) return;
  const result = $("#test-model-result");
  try {
    await api("/api/settings/deepseek-key", {method: "DELETE", headers: {"X-RW-Config-Action": "1"}});
    $("#deepseek-key").value = "";
    result.textContent = "本地密钥已清除。如果环境变量中仍有 Key，模型仍可能已配置。";
    await Promise.all([loadSettings(), loadHealth()]);
  } catch (error) { result.textContent = `删除失败：${error.message}`; }
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
  $("#source-search-form").addEventListener("submit", searchOriginalSource);
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
  $("#graph-global").addEventListener("click",()=>loadGraph("__all__"));
  $("#graph-enable-g6").addEventListener("click",enableG6ResearchGraph);
  $("#graph-edge-filter").addEventListener("change", renderGraph);
  $("#graph-extract").addEventListener("click", reextractGraph);
  $("#graph-zoom-in").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.zoomBy?.(1.18);else{state.graph.scale=Math.min(2.4,state.graph.scale*1.18);renderGraph();} });
  $("#graph-zoom-out").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.zoomBy?.(.85);else{state.graph.scale=Math.max(.45,state.graph.scale/1.18);renderGraph();} });
  $("#graph-reset").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.fitView?.();else{state.graph.scale=1;state.graph.panX=0;state.graph.panY=0;renderGraph();} });
  $("#graph-canvas").addEventListener("wheel", (event) => { event.preventDefault(); state.graph.scale = Math.max(.45, Math.min(2.4, state.graph.scale * (event.deltaY < 0 ? 1.1 : .9))); renderGraph(); }, { passive: false });
  $("#graph-canvas").addEventListener("pointerdown", (event) => { if (event.target.closest?.('[data-graph-node]')) return; state.graph.dragging = true; event.currentTarget.setPointerCapture?.(event.pointerId); });
  $("#graph-canvas").addEventListener("pointermove", (event) => { if (!state.graph.dragging) return; state.graph.panX += event.movementX; state.graph.panY += event.movementY; $("#graph-stage")?.setAttribute("transform", graphTransform()); });
  $("#graph-canvas").addEventListener("pointerup", () => { state.graph.dragging = false; });
  $("#graph-canvas").addEventListener("pointercancel", () => { state.graph.dragging = false; });
  $("#retrieval-form").addEventListener("submit", searchRetrieval);
  $("#test-model").addEventListener("click", testModel);
  $("#deepseek-key-form").addEventListener("submit", saveDeepSeekKey);
  $("#delete-model-key").addEventListener("click", clearDeepSeekKey);
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
  showView(location.hash ? location.hash.slice(1) : "documents");
  renderAskHistory();
}

boot();
