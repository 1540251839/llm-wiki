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
  libraryId: null,
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
    syncLibraryIdentity(health.storage?.library_id);
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

function renderJobProgress(job) {
  const progress = job.progress && typeof job.progress === "object" ? job.progress : {}, model = progress.model || {}, usage = model.usage || {};
  const stageLabels = { COPY_SOURCE: "保存原件", PARSE: "解析文档", ANALYZE: "深度分析", EXTRACT: "结构化抽取", VERIFY_EVIDENCE: "证据定位", COMMIT_WIKI: "编译知识页", REBUILD_INDEX: "建立索引", total_seconds: "本次总耗时" };
  const statusLabels = { started: "等待模型响应", completed: "已完成", cached: "已复用断点", failed: "失败", split: "拆小后继续", resuming_split: "从子块断点继续", cache_invalid: "旧断点无效" };
  const elapsed = { ...(job.stage_timings && typeof job.stage_timings === "object" ? job.stage_timings : {}) };
  const updated = String(job.updated_at || "").replace(" ", "T");
  const updatedEpoch = Date.parse(/Z$|[+-]\d\d:\d\d$/.test(updated) ? updated : updated + "Z");
  if (job.status === "RUNNING" && Number.isFinite(updatedEpoch)) {
    const waiting = Math.max(0, (Date.now() - updatedEpoch) / 1000);
    elapsed.total_seconds = Number(elapsed.total_seconds || 0) + waiting;
    if (job.stage) elapsed[job.stage] = Number(elapsed[job.stage] || 0) + waiting;
  }
  const timing = Object.entries(elapsed).map(([stage, seconds]) => `<span>${escapeHTML(stageLabels[stage] || stage)} ${Number(seconds).toFixed(2)} 秒</span>`).join(" · ");
  if (!progress.total && !timing) return '<p class="aside-note">该历史任务没有逐块计时记录。新一轮处理会显示当前块、断点复用和阶段耗时。</p>';
  const totals = Object.entries(progress.model_totals || {}).map(([stage, stats]) => `${stage === "analysis" ? "分析" : "抽取"}本次请求 ${Number(stats.requests || 0)} 次 · 累计输出 ${Number(stats.completion_tokens || 0)} tokens`).join("；");
  return `<div class="detail-section"><h3>处理进度与耗时</h3>${progress.total ? `<p>${progress.stage === "analysis" ? "分析" : "抽取"} · 当前第 ${escapeHTML(progress.current || 0)}/${escapeHTML(progress.total)} 块 · 已完成 ${escapeHTML(progress.completed || 0)} · 本次复用 ${escapeHTML(progress.cached || 0)}</p><p>${escapeHTML(statusLabels[progress.status] || progress.status || "处理中")}${progress.split_depth ? ` · 拆分层数 ${escapeHTML(progress.split_depth)}` : ""}</p><small>${escapeHTML(progress.chunk_id || "")}</small>` : ""}${Object.keys(model).length && progress.status !== "started" ? `<p>模型结束原因 ${escapeHTML(model.finish_reason || "未提供")} · 输出 ${escapeHTML(usage.completion_tokens ?? "—")} tokens${progress.status === "cached" ? "（历史缓存，本次未调用模型）" : ` · 请求尝试 ${escapeHTML(model.requests ?? model.request_count ?? model.request_attempts ?? model.http_attempts ?? "—")}`}</p>` : ""}${totals ? `<p>${escapeHTML(totals)}</p>` : ""}${timing ? `<p class="aside-note">${timing}</p>` : ""}</div>`;
}

async function openDocument(id) {
  state.documentDetailId = id;
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
      <div id="ingest-progress">${renderJobProgress(job)}</div>
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
        ${item.status === "INDEXED" && state.modelConfigured && !["PENDING", "RUNNING"].includes(job.status) ? `<button class="primary" id="enrich-document">${extraction.extraction_mode === "basic" ? "使用模型深度分析" : "继续深度分析（复用断点）"}</button>` : ""}
        ${item.original_text_indexed && state.modelConfigured && !["PENDING", "RUNNING"].includes(job.status) ? `<button class="secondary" data-supplement-focus="method">只补方法</button><button class="secondary" data-supplement-focus="experiment">只补实验</button><button class="secondary" data-supplement-focus="overview">只补研究概览</button><button class="secondary" id="reextract-document">重新完整抽取（标准）</button><button class="secondary" id="reextract-document-high">重新完整抽取（高质量）</button>` : ""}
        <button class="danger" id="delete-document">移到回收区</button>
        ${item.retryable ? `<button class="secondary" id="retry-document-high">高质量重试</button><button class="primary" id="retry-document">标准重试</button>` : ""}
      </div>`;
    $$('[data-related-page]', root).forEach((button) => button.addEventListener("click", () => goToWikiPage(button.dataset.relatedPage, button.dataset.relatedTitle)));
    $("#enrich-document")?.addEventListener("click", async () => {
      if (!window.confirm("将调用模型更新自动生成的知识内容；原始英文和人工笔记会保留。继续吗？")) return;
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/enrich?quality_mode=standard`, { method: "POST" });
        toast("已加入深度分析队列，将复用有效断点");
        startPolling(id);
        await openDocument(id);
      } catch (error) { toast(error.message); }
    });
    ["standard", "high"].forEach((mode) => $(mode === "high" ? "#reextract-document-high" : "#reextract-document")?.addEventListener("click", async () => {
      if (!window.confirm(`将完整重新读取论文并发送到当前模型 API，跳过旧断点，可能产生费用；旧人工批准将失效。${mode === "high" ? "高质量模式会逐块先分析再抽取。" : "标准模式每块直接抽取。"}是否继续？`)) return;
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/enrich?force_refresh=true&confirm=true&quality_mode=${mode}`, { method: "POST" });
        toast("完整分块抽取已进入队列"); startPolling(id); await openDocument(id);
      } catch (error) { toast(error.message); }
    }));
    $$('[data-supplement-focus]',root).forEach(control=>control.addEventListener("click", async()=>{
      const focus=control.dataset.supplementFocus;
      if(!window.confirm("只更新所选知识字段，其余论文内容保持不变。此操作会调用已配置的模型。继续吗？"))return;
      const old=control.textContent;control.disabled=true;control.textContent="正在补充…";
      try{
        await api(`/api/documents/${encodeURIComponent(id)}/supplement?focus=${encodeURIComponent(focus)}`,{
          method:"POST",headers:{"X-RW-Config-Action":"1"}
        });
        toast("字段补充已加入持久队列，将复用有效断点");
        startPolling(id);
        await openDocument(id);
      }catch(error){toast(`补充失败：${error.message}`);}
      finally{control.disabled=false;control.textContent=old;}
    }));
    $("#retry-document")?.addEventListener("click", async () => {
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/retry?quality_mode=standard`, { method: "POST" });
        toast("已重新加入队列，将复用有效分块断点");
        startPolling(id);
      } catch (error) { toast(error.message); }
    });
    $("#retry-document-high")?.addEventListener("click", async () => {
      try {
        await api(`/api/documents/${encodeURIComponent(id)}/retry?quality_mode=high`, { method: "POST" });
        toast("高质量模式逐块先分析再抽取，步骤更多；将复用有效断点");
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
  const fragmentURL = String(source.evidence_url || "").startsWith("/api/documents/") ? source.evidence_url : "";
  const fallback = /fallback/i.test(String(source.confidence || "") + String(source.section || ""));
  const location = source.page_number ? `第 ${escapeHTML(source.page_number)} 页` : "来源级引用";
  return `<div class="evidence-snippet"><div class="evidence-meta"><strong>${escapeHTML(source.name || source.original_name || "原始资料")}</strong><span>${location}${source.paragraph ? ` · 第 ${escapeHTML(source.paragraph)} 段` : ""}</span></div>${source.section ? `<small>${escapeHTML(source.section)}</small>` : ""}${fallback ? '<small class="badge warning">背景片段，尚未精确匹配论断</small>' : ""}${source.confidence === "keyword_hit" ? '<small class="badge warning">关键词命中，不表示论断支持</small>' : ""}<blockquote>${escapeHTML(source.evidence_text || source.quote || source.excerpt || "当前来源暂无片段级证据，请打开原文核对。")}</blockquote><div class="evidence-actions">${fileURL ? `<a href="${escapeHTML(fileURL)}" target="_blank" rel="noopener">查看原文${source.page_number ? `第 ${escapeHTML(source.page_number)} 页` : ""} ↗</a>` : ""}${fragmentURL ? `<a href="${escapeHTML(fragmentURL)}" target="_blank" rel="noopener">查看精确片段 ↗</a>` : ""}${source.locator ? `<code title="${escapeHTML(source.locator)}">${escapeHTML(source.locator)}</code>` : ""}</div></div>`;
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
        toast("跨论文综合已缓存为待核验解读，请核对原始证据");
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
  message.textContent = offline ? "快速导入将在本机建立全文索引，不调用模型。" : state.modelConfigured ? "深度解读将完整分块调用模型，支持 JSON 重试与断点恢复。" : "未配置模型密钥，请改用快速导入。";
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
      if ($("#document-dialog").open && state.documentDetailId === id && $("#ingest-progress")) $("#ingest-progress").innerHTML = renderJobProgress(item.job || {});
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
  loadClaims();
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

const claimStatusLabels = { supported: "原文支持", contradicted: "原文矛盾", insufficient: "证据不足", pending_review: "待语义审核" };
async function loadClaims() {
  const root = $("#claim-list");
  root.innerHTML = '<div class="empty-state">正在读取事实与原文定位…</div>';
  try {
    const value = await api(`/api/claims?limit=200&status=${encodeURIComponent($("#claim-status").value)}`);
    $("#claim-summary").textContent = Object.entries(claimStatusLabels).map(([key, label]) => `${label} ${value.summary[key] || 0}`).join(" · ");
    root.innerHTML = value.items.map((item) => `<details class="claim-card"><summary><span class="badge ${item.verification.status === "supported" ? "ok" : "warning"}">${escapeHTML(claimStatusLabels[item.verification.status])}</span> ${escapeHTML(item.statement)}</summary><div class="claim-body"><strong>${escapeHTML(item.source_title)}</strong><small>${escapeHTML(item.claim_id)} · ${escapeHTML(item.verification.method)}</small><p>${escapeHTML(item.verification.reason || "")}</p><div class="notice">关联对象：${(item.entities || []).map((entity) => escapeHTML(`${entity.type}: ${entity.name}`)).join("；")}</div>${evidenceMarkup({ ...item.evidence, evidence_text: item.evidence.quote, file_url: item.file_url, original_name: item.source_title, confidence: item.evidence.located ? "located_quote" : "source_excerpt_fallback" })}<div class="case-sources">${(item.wiki_pages || []).map((page) => `<button class="inline-link" data-wiki-title="${escapeHTML(page.title)}">${escapeHTML(page.title)} →</button>`).join("") || '<small>尚未编入 Wiki</small>'}</div><label>事实陈述<textarea data-claim-statement rows="3">${escapeHTML(item.statement)}</textarea></label><label>原文引文<textarea data-claim-quote rows="4">${escapeHTML(item.evidence.quote || "")}</textarea></label><label>审核结论<select data-claim-decision>${Object.entries(claimStatusLabels).map(([status, label]) => `<option value="${status}" ${status === item.verification.status ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>审核理由<input data-claim-reason maxlength="2000" placeholder="说明该引文如何支持或不支持此陈述" /></label><button class="secondary" data-review-claim="${escapeHTML(item.claim_id)}" data-source="${escapeHTML(item.source_id)}">保存审核并重编译</button></div></details>`).join("") || '<div class="empty-state">没有此状态的论断；可离线导入资料后按需深度抽取。</div>';
    if (value.truncated) root.insertAdjacentHTML("beforeend", '<p class="notice">仅显示前 200 条；请按状态筛选。</p>');
    bindWikiLinks(root);
    $$('[data-review-claim]', root).forEach((button) => button.addEventListener("click", async () => {
      const card = button.closest(".claim-card");
      const reason = $("[data-claim-reason]", card).value.trim();
      if (!reason) return toast("请输入审核理由");
      button.disabled = true;
      try {
        const value = await api(`/api/documents/${encodeURIComponent(button.dataset.source)}/claims/${encodeURIComponent(button.dataset.reviewClaim)}/review`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ status: $("[data-claim-decision]", card).value, reason, statement: $("[data-claim-statement]", card).value, quote: $("[data-claim-quote]", card).value }) });
        toast(value.reindex_error ? value.message : "事实审核已保存，知识层已重编译");
        await Promise.all([loadClaims(), loadOverview()]);
      } catch (error) { toast(error.message); } finally { button.disabled = false; }
    }));
  } catch (error) { root.innerHTML = `<div class="error-state">事实读取失败：${escapeHTML(error.message)}</div>`; }
}

function renderAnswer(value, result, mode) {
  const modeLabels = { llm: `${value.provider} · ${value.model}`, local_evidence: "本地证据降级", no_evidence: "无证据拒答" };
  mode.textContent = modeLabels[value.mode] || value.mode;
  result.className = "answer ready";
  const context = value.context || {};
  result.innerHTML = `<div class="answer-text prose">${renderMarkdown(value.answer, true)}</div>${value.fallback_reason ? `<div class="notice">模型生成未完成或引用无效，已切换为可核对的本地证据：${escapeHTML(value.fallback_reason)}</div>` : ""}<div class="context-meter">使用 ${escapeHTML(value.sources?.length ?? context.pages_included ?? 0)} 个证据对象 · 上下文 ${escapeHTML(context.used_chars || 0)} / ${escapeHTML(context.page_budget_chars || 0)} 字符</div>`;
  renderEvidence(value.sources || []);
  bindCitations(result);
  bindWikiLinks(result);
  state.currentAnswer = value;
}

function renderEvidence(sources) {
  $("#evidence-count").textContent = String(sources.length);
  $("#ask-evidence-list").innerHTML = sources.map((source, index) => {
    const raw = ["original_text", "raw", "raw_excerpt"].includes(source.evidence_type) || source.type === "original_text" || !source.page_id;
    const provenance = source.provenance?.length ? source.provenance : raw ? [source] : [];
    return `<article class="citation-card" id="citation-${Number(source.citation_index || index + 1)}" tabindex="-1"><div class="citation-heading"><span class="citation-number">${Number(source.citation_index || index + 1)}</span><div><strong>${escapeHTML(source.title)}</strong><small>${raw ? "原文定位（未自动审核）" : "已支持 Wiki 证据"} · ${escapeHTML(source.match_type || "检索来源")}</small></div></div>${provenance.map(evidenceMarkup).join("") || '<p class="muted">暂无原文片段，请核查来源。</p>'}${raw ? "" : `<button class="text-button" data-wiki-title="${escapeHTML(source.title)}">阅读 Wiki 主题 →</button>`}</article>`;
  }).join("") || '<div class="empty-state">没有检索到可用证据。试试具体的方法名、数据集名，或先导入相关论文。</div>';
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

function syncLibraryIdentity(identity) {
  if (typeof identity !== "string" || !/^[a-f0-9]{32}$/.test(identity) || state.libraryId === identity) return;
  state.askController?.abort();
  state.askRequestId += 1;
  state.askController = null;
  state.currentAnswer = null;
  state.askHistory = [];
  state.libraryId = identity;
  try {
    const previousIdentity = localStorage.getItem("researchwiki.libraryId");
    if (previousIdentity !== identity) localStorage.removeItem("researchwiki.askHistory");
    localStorage.setItem("researchwiki.libraryId", identity);
    const saved = JSON.parse(localStorage.getItem("researchwiki.askHistory") || "null");
    if (saved?.library_id === identity && Array.isArray(saved.items)) {
      state.askHistory = saved.items.filter((item) => item && typeof item.question === "string" && item.result && typeof item.result.answer === "string").slice(0, 25);
    } else {
      // Never carry unscoped legacy answers into a new/emptied library.
      localStorage.removeItem("researchwiki.askHistory");
    }
  } catch (_) { state.askHistory = []; }
  $("#ask-result").className = "answer empty-state";
  $("#ask-result").textContent = "当前资料库尚未提问；旧库回答不会混入这里。";
  $("#ask-question").textContent = "";
  $("#ask-question").classList.add("hidden");
  $("#ask-phases").innerHTML = "";
  $("#ask-evidence-list").innerHTML = '<div class="empty-state">检索后显示当前资料库的来源与定位。</div>';
  $("#evidence-count").textContent = "0";
  $("#ask-submit").disabled = false;
  $("#ask-stop").classList.add("hidden");
  renderAskHistory();
}

function saveAnswer(question, result) {
  state.askHistory.unshift({ question, result, created_at: new Date().toISOString() });
  state.askHistory = state.askHistory.slice(0, 25);
  try { if (state.libraryId) localStorage.setItem("researchwiki.askHistory", JSON.stringify({library_id: state.libraryId, items: state.askHistory})); } catch (_) { toast("浏览器存储空间不足，本次历史仅保留在当前会话"); }
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
  result.textContent = "正在读取已支持 Wiki 与原文片段并组织答案…";
  try {
    const response = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, evidence_scope: $("#ask-evidence-scope").value }), signal: controller.signal });
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
    result.className = "answer empty-state";
    result.textContent = `${message}。已清除未完成回答；右侧保留可核对的原文证据，不作为研究结论。`;
  } finally {
    if (requestId === state.askRequestId) { submit.disabled = false; state.askController = null; $("#ask-stop").classList.add("hidden"); }
  }
}

const graphColors = {
  PAPER: "#397d78", METHOD: "#5d78bb", DATASET: "#b58b44", TOPIC: "#9276b8"
};
const graphSoftColors = {PAPER:"#eaf5f2", METHOD:"#eef2fc", DATASET:"#faf3e7", TOPIC:"#f4eef9"};
const graphTypeNames = {PAPER:"论文", TOPIC:"主题", METHOD:"方法", DATASET:"数据集"};
const graphRelationNames = {
  STUDIES_TOPIC: "研究主题", REFERENCES_METHOD: "关联方法",
  MENTIONS_DATASET: "提及数据集", TESTED_ON: "已支持测试用途",
  TRAINED_ON: "已支持训练用途", VALIDATED_ON: "已支持验证用途", RELATED: "相关研究"
};

function graphTransform() {
  const graph = state.graph;
  const {width=1000, height=680} = graph.dimensions || {};
  return `translate(${graph.panX} ${graph.panY}) translate(${width/2} ${height/2}) scale(${graph.scale}) translate(${-width/2} ${-height/2})`;
}

function stableHash(value) {
  let h = 2166136261;
  for (const char of String(value)) { h ^= char.charCodeAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function graphLabelLines(value, width, maxLines=2) {
  const limit=Math.max(6,(width-26)/13);
  const lines=[]; let line="", units=0;
  const measure=text=>Array.from(text).reduce((sum,char)=>sum+(/[^\u0000-\u00ff]/.test(char)?1:.56),0);
  // Keep short English terms intact while still wrapping Chinese titles.
  const tokens=(String(value).match(/[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*|[\s\S]/gu)||[])
    .flatMap(token=>measure(token)>limit?Array.from(token):[token]);
  for(const token of tokens){
    const cost=measure(token);
    if(units+cost>limit && line){
      lines.push(line.trim());line="";units=0;
      if(lines.length===maxLines){lines[maxLines-1]=lines[maxLines-1].replace(/.$/u,"")+"…";return lines;}
    }
    if(!line&&/^\s+$/.test(token))continue;
    line+=token;units+=cost;
  }
  if(line.trim())lines.push(line.trim());
  return lines.length?lines:["未命名对象"];
}

function graphPositions(visible, edges) {
  const graph=state.graph, svg=$("#graph-canvas");
  const width=Math.max(320,svg.clientWidth||920), mobile=width<680;
  const positions=new Map(); positions.frames=[];
  const degree=new Map(visible.map(n=>[n.id,0]));
  edges.forEach(e=>{degree.set(e.source,(degree.get(e.source)||0)+1);degree.set(e.target,(degree.get(e.target)||0)+1);});
  const ordered=[...visible].sort((a,b)=>a.type.localeCompare(b.type)||a.name.localeCompare(b.name));
  const add=(node,x,y,center=false)=>positions.set(node.id,{x,y,w:center?(mobile?190:216):(mobile?150:174),h:center?96:72});
  const arrange=(nodes, top=0)=>{
    const hub=nodes.find(n=>n.id===graph.center)||nodes.reduce((a,n)=>(!a||(degree.get(n.id)||0)>(degree.get(a.id)||0))?n:a,null);
    if(!hub)return top;
    const peers=nodes.filter(n=>n.id!==hub.id);
    if(mobile || peers.length>10){
      add(hub,width/2,top+88,true);
      const cols=mobile?2:Math.max(2,Math.floor((width-36)/192));
      const rows=Math.ceil(peers.length/cols), cell=(width-32)/cols;
      peers.forEach((n,i)=>{const col=i%cols,row=Math.floor(i/cols),solo=i===peers.length-1&&peers.length%cols===1;
        add(n,solo?width/2:16+cell*(col+.5),top+232+row*104);});
      return top+Math.max(230,232+Math.max(0,rows-1)*104+62);
    }
    add(hub,width/2,top+270,true);
    const rx=Math.max(170,width/2-112),ry=194;
    peers.forEach((n,i)=>{const angle=-Math.PI/2+i*Math.PI*2/Math.max(1,peers.length);
      add(n,width/2+Math.cos(angle)*rx,top+270+Math.sin(angle)*ry);});
    return top+542;
  };
  const global=graph.center==="__all__";
  const connected=ordered.filter(n=>(degree.get(n.id)||0)>0);
  let height;
  if(!global && edges.length){
    height=arrange(ordered);
  }else{
    let top=12;
    if(connected.length){
      positions.frames.push({title:"已有科研关系",subtitle:connected.length+" 个关联对象",y:20});
      top=arrange(connected,32)+26;
    }
    const isolated=ordered.filter(n=>!positions.has(n.id));
    if(isolated.length){
      positions.frames.push({title:connected.length?"暂无可展示关系的对象":"知识对象",subtitle:isolated.length+" 个对象 · 不补造连线",y:top+20});
      const cols=mobile?2:Math.max(2,Math.floor((width-36)/192)),cell=(width-32)/cols;
      isolated.forEach((n,i)=>add(n,16+cell*(i%cols+.5),top+86+Math.floor(i/cols)*92));
      top+=Math.ceil(isolated.length/cols)*92+74;
    }
    height=top;
  }
  height=Math.max(mobile?430:530,height||430);
  graph.dimensions={width,height,mobile};
  svg.setAttribute("viewBox","0 0 "+width+" "+height);
  svg.style.height=height+"px";
  svg.closest?.(".graph-stage-body")?.classList.toggle("graph-scrollable",height>(mobile?480:650));
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

function graphMessage(title, description, error=false) {
  const box=$("#graph-empty-status"); if(!box)return;
  box.innerHTML='<span class="graph-empty-symbol" aria-hidden="true">'+(error?"!":"↗")+'</span><strong>'+escapeHTML(title)+'</strong><p>'+escapeHTML(description)+'</p>';
  box.classList.remove("hidden");
}

function graphEdgeGeometry(a,b,mobile) {
  const dx=b.x-a.x,dy=b.y-a.y;
  if(mobile){
    const dir=dy>=0?1:-1, ax=a.x,ay=a.y+dir*a.h/2,bx=b.x,by=b.y-dir*b.h/2;
    const branch=by-dir*22,sign=bx>=ax?1:-1;
    const path=Math.abs(bx-ax)<25?`M ${ax} ${ay} L ${bx} ${by}`
      :`M ${ax} ${ay} L ${ax} ${branch-dir*10} Q ${ax} ${branch} ${ax+sign*10} ${branch} L ${bx-sign*10} ${branch} Q ${bx} ${branch} ${bx} ${branch+dir*10} L ${bx} ${by}`;
    return {path,x:(ax+bx)/2,y:branch-8};
  }
  const exit=(p,x,y)=>{const factor=Math.min((p.w/2+3)/Math.max(1,Math.abs(x)),(p.h/2+3)/Math.max(1,Math.abs(y)));
    return {x:p.x+x*factor,y:p.y+y*factor};};
  const s=exit(a,dx,dy),t=exit(b,-dx,-dy);
  const len=Math.hypot(dx,dy)||1,cx=(s.x+t.x)/2-dy/len*14,cy=(s.y+t.y)/2+dx/len*14;
  return {path:`M ${s.x} ${s.y} Q ${cx} ${cy} ${t.x} ${t.y}`,x:(s.x+2*cx+t.x)/4,y:(s.y+2*cy+t.y)/4-6};
}

function renderGraphMetrics(nodes, edges) {
  const graph=state.graph, summary=graph.libraryStats;
  const inView=nodes.filter(node=>node.type==="PAPER").length;
  const allPapers=summary?.page_types?.paper ?? (graph.center==="__all__"?graph.nodes.filter(node=>node.type==="PAPER").length:"—");
  $("#graph-stats").innerHTML=[
    renderMetric("SOURCES",summary?.documents??"—","已上传资料"),
    renderMetric("ALL PAPERS",allPapers,"全库论文节点"),
    renderMetric("IN VIEW",inView,"当前视图论文"),
    renderMetric("RELATIONS",edges.length,"当前视图关系")
  ].join("");
  const note=$("#graph-library-note");
  if(note){
    const failed=summary?.statuses?.FAILED||0;
    note.innerHTML='<strong>'+(graph.center==="__all__"?"全局视图":"局部 · 一跳")+'</strong> · '+
      (graph.center==="__all__"?"类型筛选中的数量是全局对象数。":"当前只显示中心论文的直接邻居，不代表全部上传资料。")+
      (failed?' <span class="graph-import-warning">'+failed+' 份资料处理失败，未完成的知识编译可能尚未入图。</span>':"")+
      ' <a href="#documents">查看资料库与任务 ↗</a>';
  }
  const list=$("#graph-paper-list");
  if(list){
    const papers=graph.nodes.filter(node=>node.type==="PAPER");
    list.classList.toggle("hidden",graph.center!=="__all__"||!papers.length);
    list.innerHTML='<div class="graph-paper-head"><h3>全库论文 <small>'+escapeHTML(allPapers)+'</small></h3><span>点击论文，查看它的一跳关系</span></div><div class="graph-paper-grid">'+
      papers.map(node=>'<button class="graph-paper-entry" data-graph-paper="'+escapeHTML(node.id)+'"><span class="graph-paper-icon" aria-hidden="true">P</span><span>'+escapeHTML(node.name)+'</span><span class="graph-paper-arrow" aria-hidden="true">↗</span></button>').join("")+'</div>';
    $$('[data-graph-paper]',list).forEach(button=>button.addEventListener("click",()=>loadGraph(button.dataset.graphPaper)));
  }
}

function renderGraph() {
  const graph=state.graph, {nodes,edges}=visibleResearchGraph();
  renderGraphMetrics(nodes,edges);
  const query=$("#graph-search").value.trim().toLocaleLowerCase();
  $("#graph-visible-count").textContent=nodes.length+" 个对象 · "+edges.length+" 条关系";
  const matches=new Set(nodes.filter(n=>n.name.toLocaleLowerCase().includes(query)).map(n=>n.id));
  const peers=new Set(edges.filter(e=>e.source===graph.selectedId||e.target===graph.selectedId).flatMap(e=>[e.source,e.target]));
  const status=$("#graph-status");
  if(status)status.textContent=query?(matches.size+" 个匹配对象"):(edges.length?"选择对象，追溯关联证据":"当前对象尚未形成可展示的科研关系");
  if(graph.renderer==="g6" && window.G6?.Graph && nodes.length){
    $("#graph-empty-status")?.classList.add("hidden");
    renderG6ResearchGraph(nodes,edges);return;
  }
  $("#graph-canvas").classList.remove("hidden");$("#graph-g6").classList.add("hidden");
  const svg=$("#graph-canvas"),positions=graphPositions(nodes,edges);
  $("#graph-empty-status")?.classList.add("hidden");
  if(!nodes.length){
    svg.innerHTML="";
    graphMessage(graph.nodes.length?"当前筛选没有对象":"还没有知识对象",graph.nodes.length?"勾选对象类型，或返回全局图查看。":"先导入论文，完成知识编译后即可查看关系。");
    return;
  }
  const frames=(positions.frames||[]).map(f=>'<text class="graph-section-title" x="18" y="'+f.y+'">'+escapeHTML(f.title)+'</text><text class="graph-section-note" x="18" y="'+(f.y+17)+'">'+escapeHTML(f.subtitle)+'</text>').join("");
  const edgeMarkup=edges.map(edge=>{
    const a=positions.get(edge.source),b=positions.get(edge.target),g=graphEdgeGeometry(a,b,graph.dimensions.mobile);
    const active=edge.source===graph.selectedId||edge.target===graph.selectedId;
    const dim=query&&!matches.has(edge.source)&&!matches.has(edge.target);
    const label=edge.label||graphRelationNames[edge.semantic]||edge.semantic;
    const labelWidth=Math.max(52,Array.from(label).reduce((sum,c)=>sum+(/[^\u0000-\u00ff]/.test(c)?9:5),0)+16);
    return '<path class="graph-edge '+(active?"selected ":"")+(dim?"dim":"")+'" d="'+g.path+'" marker-end="url(#graph-arrow)"><title>'+escapeHTML(label)+'</title></path>'+
      '<g class="graph-edge-label '+(active?"selected ":"")+(dim?"dim":"")+'" transform="translate('+g.x+' '+g.y+')"><rect x="'+(-labelWidth/2)+'" y="-11" width="'+labelWidth+'" height="21" rx="6"/><text text-anchor="middle" y="3">'+escapeHTML(label)+'</text></g>';
  }).join("");
  const nodeMarkup=nodes.map(node=>{
    const p=positions.get(node.id),selected=graph.selectedId===node.id,central=node.id===graph.center;
    const lines=graphLabelLines(node.name,p.w,central?3:2),start=-p.h/2+42;
    const muted=query&&!matches.has(node.id);
    return '<g class="graph-node '+(selected?"selected ":"")+(central?"center ":"")+(peers.has(node.id)&&!selected?"neighbor ":"")+(muted?"dim":"match")+'" data-type="'+escapeHTML(node.type.toLowerCase())+'" data-graph-node="'+escapeHTML(node.id)+'" role="button" tabindex="0" aria-label="'+escapeHTML((graphTypeNames[node.type]||node.type)+" "+node.name)+'" transform="translate('+p.x+' '+p.y+')">'+
      '<rect class="graph-node-ring" x="'+(-p.w/2-4)+'" y="'+(-p.h/2-4)+'" width="'+(p.w+8)+'" height="'+(p.h+8)+'" rx="16"/>'+
      '<rect class="graph-node-card" x="'+(-p.w/2)+'" y="'+(-p.h/2)+'" width="'+p.w+'" height="'+p.h+'" rx="12"/>'+
      '<rect class="graph-node-icon" x="'+(-p.w/2+12)+'" y="'+(-p.h/2+10)+'" width="22" height="22" rx="7"/>'+
      '<text class="graph-node-icon-label" x="'+(-p.w/2+23)+'" y="'+(-p.h/2+26)+'" text-anchor="middle">'+escapeHTML({PAPER:"P",METHOD:"M",DATASET:"D",TOPIC:"T"}[node.type]||"·")+'</text>'+
      '<text class="graph-node-kicker" x="'+(-p.w/2+42)+'" y="'+(-p.h/2+25)+'">'+escapeHTML(graphTypeNames[node.type]||node.type)+(central?" · 中心对象":"")+'</text>'+
      lines.map((line,i)=>'<text class="graph-node-label" x="'+(-p.w/2+13)+'" y="'+(start+i*17)+'">'+escapeHTML(line)+'</text>').join("")+
      '<title>'+escapeHTML(node.name)+'</title></g>';
  }).join("");
  svg.innerHTML='<defs><marker id="graph-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M 0 0 L 7 3.5 L 0 7 Z" fill="var(--muted)" opacity=".65"/></marker></defs><g id="graph-stage" transform="'+graphTransform()+'">'+frames+edgeMarkup+nodeMarkup+'</g>';
  $$('[data-graph-node]',svg).forEach(node=>{
    node.addEventListener("click",()=>selectGraphNode(node.dataset.graphNode));
    node.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectGraphNode(node.dataset.graphNode);}});
  });
}

function renderG6ResearchGraph(nodes,edges) {
  const graph=state.graph,host=$("#graph-g6");
  $("#graph-canvas").classList.add("hidden");host.classList.remove("hidden");
  host.closest?.(".graph-stage-body")?.classList.remove("graph-scrollable");
  if(graph.g6){graph.g6.destroy();graph.g6=null;}
  try{
    const instance=new window.G6.Graph({
      container:host,autoFit:"view",
      node:{state:{selected:{lineWidth:3,stroke:"#397d78",halo:true}}},
      data:{
        nodes:nodes.map(n=>({id:n.id,style:{size:n.type==="PAPER"?34:26,fill:graphSoftColors[n.type]||"#eef2f5",stroke:graphColors[n.type]||"#8796a5",lineWidth:2,
          labelText:n.name.length>24?n.name.slice(0,23)+"…":n.name,labelPlacement:"bottom",labelFontSize:12,labelFill:"#53616b"}})),
        edges:edges.map(e=>({id:e.id,source:e.source,target:e.target,style:{stroke:"#9eafbb",lineWidth:1.3,endArrow:true}}))
      },
      layout:{type:"d3-force",animation:false,manyBody:{strength:-480},link:{distance:175},collide:{radius:75},x:{},y:{}},
      behaviors:["drag-canvas","zoom-canvas","drag-element"]
    });
    graph.g6=instance;
    const fail=()=>{if(graph.g6!==instance)return;graph.g6=null;try{instance.destroy();}catch(_){}
      graph.renderer="svg";$("#graph-enable-g6").textContent="G6 力导向（可选）";renderGraph();toast("G6 暂不可用，已切换本地布局");};
    instance.on("node:click",ev=>{const id=ev.target?.id||ev.item?.id;if(id)selectGraphNode(id);});
    Promise.resolve(instance.render()).then(()=>{
      if(graph.g6===instance&&graph.selectedId)return instance.setElementState?.(graph.selectedId,["selected"],false);
    }).catch(fail);
  }catch(error){graph.renderer="svg";graph.g6=null;$("#graph-enable-g6").textContent="G6 力导向（可选）";renderGraph();toast("G6 暂不可用，已切换本地布局");}
}

async function enableG6ResearchGraph() {
  const btn=$("#graph-enable-g6");
  if(state.graph.renderer==="g6"){
    if(state.graph.g6){state.graph.g6.destroy();state.graph.g6=null;}
    state.graph.renderer="svg";btn.textContent="G6 力导向（可选）";renderGraph();return;
  }
  if(!window.G6?.Graph){
    btn.disabled=true;btn.textContent="正在加载 G6…";
    try{
      await new Promise((resolve,reject)=>{
        const tag=document.createElement("script");
        const timer=window.setTimeout(()=>{tag.remove();reject(new Error("G6 load timeout"));},12000);
        tag.src="https://cdn.jsdelivr.net/npm/@antv/g6@5.1.1/dist/g6.min.js";tag.async=true;
        tag.onload=()=>{window.clearTimeout(timer);resolve();};tag.onerror=()=>{window.clearTimeout(timer);tag.remove();reject(new Error("G6 unavailable"));};
        document.head.appendChild(tag);
      });
    }catch{toast("无法加载 G6，仍可使用本地布局");btn.disabled=false;btn.textContent="G6 力导向（可选）";return;}
    btn.disabled=false;
  }
  state.graph.renderer="g6";btn.textContent="返回本地布局";renderGraph();
}

async function selectGraphNode(id) {
  const graph=state.graph,node=graph.nodes.find(item=>item.id===id);if(!node)return;
  const previous=graph.selectedId;graph.selectedId=id;
  const connections=graph.edges.filter(edge=>edge.source===id||edge.target===id);
  if(graph.renderer==="svg")renderGraph();
  else if(graph.g6?.setElementState){
    const states={[id]:["selected"]};if(previous&&previous!==id)states[previous]=[];
    Promise.resolve(graph.g6.setElementState(states,false)).catch(()=>{});
  }
  const name=graphTypeNames[node.type]||node.type;
  $("#graph-detail").innerHTML='<p class="eyebrow">'+escapeHTML(name)+' · 知识对象</p><h3>'+escapeHTML(node.name)+'</h3>'+
    '<p class="muted">'+(connections.length?"当前视图包含 "+connections.length+" 条关联。关系仅代表已生成的知识链接，不代表因果。":"暂无可展示的科研关系；基础导入或待核验内容可能尚未形成连线。")+'</p>'+
    '<div class="settings-actions"><button class="primary" id="graph-expand-node">以此为中心</button>'+
    (node.page_id?'<button class="secondary" data-open-graph-page="'+node.page_id+'">阅读 Wiki ↗</button>':"")+'</div>'+
    '<div class="detail-section"><h3>关联对象 <small>'+connections.length+'</small></h3>'+
    (connections.map(edge=>{const peer=edge.source===id?edge.target:edge.source,linked=graph.nodes.find(item=>item.id===peer);
      return '<button class="graph-connection" data-connected-node="'+escapeHTML(peer)+'"><code>'+escapeHTML(edge.label||graphRelationNames[edge.semantic]||edge.semantic)+'</code><span>'+escapeHTML(linked?.name||peer)+'</span></button>';}).join("")||'<p class="muted">可切换全局图浏览其他对象。</p>')+
    '</div><details class="advanced-details" id="graph-node-evidence"><summary>原始证据与定位</summary><p class="muted">正在读取来源定位…</p></details>';
  $("#graph-expand-node")?.addEventListener("click",()=>loadGraph(id));
  $('[data-open-graph-page]')?.addEventListener("click",()=>goToWikiPage(node.page_id,node.original_name||node.name));
  $$('[data-connected-node]').forEach(button=>button.addEventListener("click",()=>selectGraphNode(button.dataset.connectedNode)));
  if(node.page_id){
    try{const page=await api("/api/wiki/pages/"+node.page_id);if(graph.selectedId===id)$("#graph-node-evidence").innerHTML='<summary>原始证据与定位</summary>'+((page.sources||[]).map(evidenceMarkup).join("")||'<p class="muted">此对象暂无已支持论断的原文定位；可在 Wiki 查看待核验解读。</p>');}
    catch{if(graph.selectedId===id)$("#graph-node-evidence").innerHTML='<summary>原始证据与定位</summary><p class="muted">暂时无法读取定位，请稍后重试。</p>';}
  }
}

async function loadGraph(center = "__all__") {
  const graph=state.graph,svg=$("#graph-canvas"),request=graph.requestId=(graph.requestId||0)+1;
  graphMessage("正在读取图谱","加载真实知识对象与科研关系…");
  try{
    const [value,summary]=await Promise.all([
      api("/api/graph/research?center="+encodeURIComponent(center)+"&depth=1&limit=220"),
      api("/api/overview").catch(()=>null)
    ]);
    if(request!==graph.requestId)return;
    graph.libraryStats=summary;
    graph.nodes=value.nodes||[];graph.edges=value.edges||[];graph.center=center==="__all__"?"__all__":value.center||"";
    graph.visibleTypes=new Set(graph.nodes.map(node=>node.type));graph.scale=1;graph.panX=0;graph.panY=0;
    if(!graph.nodes.some(n=>n.id===graph.selectedId))graph.selectedId="";
    const semantics=[...new Set(graph.edges.map(edge=>edge.semantic))].sort();
    $("#graph-edge-filter").innerHTML='<option value="">全部关系</option>'+semantics.map(key=>'<option value="'+escapeHTML(key)+'">'+escapeHTML(value.relation_labels?.[key]||graphRelationNames[key]||key)+'</option>').join("");
    $("#graph-edge-filter").value="";
    $("#graph-type-filters").innerHTML=Object.entries(value.node_types||{}).map(([type,count])=>'<label title="当前视图中的'+escapeHTML(graphTypeNames[type]||type)+'节点"><input type="checkbox" data-graph-type="'+escapeHTML(type)+'" checked>'+escapeHTML(graphTypeNames[type]||type)+' <small>'+count+'</small></label>').join("");
    $$('[data-graph-type]').forEach(input=>input.addEventListener("change",()=>{input.checked?graph.visibleTypes.add(input.dataset.graphType):graph.visibleTypes.delete(input.dataset.graphType);renderGraph();}));
    const global=center==="__all__";
    $("#graph-global").classList.toggle("active",global);$("#graph-global").textContent=global?"切换局部图":"查看全部论文";
    if($("#graph-view-badge"))$("#graph-view-badge").textContent=global?"全局 · 全部对象":"局部 · 一跳";
    if($("#graph-stage-title"))$("#graph-stage-title").textContent=global?"研究知识全景":"以论文为中心，探索关联";
    if($("#graph-stage-subtitle"))$("#graph-stage-subtitle").textContent=global?"全库知识对象分区展示；没有连线的论文仍保留，关系不补造。":"仅展示中心对象及其直接邻居；点击「查看全部论文」返回全局。";
    renderGraph();
    if(!global&&graph.center)await selectGraphNode(graph.center);
    else if(graph.selectedId)await selectGraphNode(graph.selectedId);
  }catch(error){
    if(request!==graph.requestId)return;
    graph.nodes=[];graph.edges=[];graph.selectedId="";
    if(graph.g6){try{graph.g6.destroy();}catch(_){}graph.g6=null;}
    svg.innerHTML="";$("#graph-g6").classList.add("hidden");svg.classList.remove("hidden");
    $("#graph-visible-count").textContent="图谱尚未加载";
    graphMessage("暂时无法读取图谱",error.message||"请稍后重试。",true);
    if($("#graph-status"))$("#graph-status").textContent="加载失败";
  }
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
  $("#quality-mode").addEventListener("change", () => { if ($("#upload-dialog").open) { const selected = $("#quality-mode").value, offline = selected === "basic"; $("#upload-submit").disabled = !offline && !state.modelConfigured; $("#upload-message").textContent = offline ? "快速导入在本机执行，不调用模型。" : state.modelConfigured ? selected === "high" ? "高质量模式逐块先分析再抽取，步骤更多；支持完整分块与断点恢复。" : "标准深读完整分块，支持截断检测、JSON 重试和断点恢复。" : "未配置模型密钥，请选择快速导入。"; } });
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
  $("#claim-status").addEventListener("change", loadClaims);
  $("#ask-stop").addEventListener("click", () => state.askController?.abort());
  $("#new-question").addEventListener("click", () => { if (state.askController) return toast("请先停止当前回答"); $("#ask-input").value = ""; $("#ask-input").focus(); $("#ask-question").classList.add("hidden"); $("#ask-phases").innerHTML = ""; $("#ask-result").className = "answer empty-state"; $("#ask-result").textContent = "开始一个新的研究问题。"; $("#ask-evidence-list").innerHTML = '<div class="empty-state">下一次检索的证据会显示在这里。</div>'; $("#evidence-count").textContent = "0"; });
  $$('[data-question]').forEach((button) => button.addEventListener("click", () => { $("#ask-input").value = button.dataset.question; $("#ask-input").focus(); }));
  $("#refresh-reviews").addEventListener("click", loadReviews);
  $("#graph-search").addEventListener("input", renderGraph);
  $("#graph-global").addEventListener("click",()=>loadGraph(state.graph.center==="__all__"?"":"__all__"));
  $("#graph-enable-g6").addEventListener("click",enableG6ResearchGraph);
  $("#graph-edge-filter").addEventListener("change", renderGraph);
  $("#graph-extract").addEventListener("click", reextractGraph);
  $("#graph-zoom-in").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.zoomBy?.(1.18);else{state.graph.scale=Math.min(2.4,state.graph.scale*1.18);renderGraph();} });
  $("#graph-zoom-out").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.zoomBy?.(.85);else{state.graph.scale=Math.max(.45,state.graph.scale/1.18);renderGraph();} });
  $("#graph-reset").addEventListener("click", () => { if(state.graph.g6&&state.graph.renderer==="g6")state.graph.g6.fitView?.();else{state.graph.scale=1;state.graph.panX=0;state.graph.panY=0;renderGraph();} });
  $("#graph-canvas").addEventListener("wheel", (event) => {
    // Ordinary scrolling must remain available for tall/mobile graphs.
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    state.graph.scale = Math.max(.45, Math.min(2.4, state.graph.scale * (event.deltaY < 0 ? 1.1 : .9)));
    renderGraph();
  }, { passive: false });
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
  let graphResizeTimer;
  window.addEventListener("resize",()=>{window.clearTimeout(graphResizeTimer);graphResizeTimer=window.setTimeout(()=>{if(state.activeView==="graph")renderGraph();},150);});
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
}

async function boot() {
  try { document.documentElement.dataset.theme = localStorage.getItem("researchwiki.theme") || "light"; } catch (_) { document.documentElement.dataset.theme = "light"; }
  bindEvents();
  await loadHealth();
  await loadOverview();
  showView(location.hash ? location.hash.slice(1) : "documents");
  renderAskHistory();
}

boot();
