# ResearchWiki 架构与实现讲解

> 版本：0.2.0 ｜ 适用代码：`researchwiki/` 全部模块 ｜ 文档定位：给需要阅读、修改或扩展这套系统的人

本文讲解 ResearchWiki **为什么这样设计** 以及 **代码是怎么实现的**。功能清单见 [README.md](../README.md)，字段与接口边界见 [data-flow.md](data-flow.md)，本文侧重框架逻辑与实现细节。

---

## 目录

1. [项目定位与问题](#1-项目定位与问题)
2. [三条核心设计原则](#2-三条核心设计原则)
3. [分层架构](#3-分层架构)
4. [存储模型：双身份系统](#4-存储模型双身份系统)
5. [写入主链](#5-写入主链)
6. [读取主链](#6-读取主链)
7. [接口层：HTTP / CLI / Web](#7-接口层http--cli--web)
8. [四个派生契约](#8-四个派生契约)
9. [质量、信任与边界](#9-质量信任与边界)
10. [配置系统](#10-配置系统)
11. [并发、恢复与生命周期](#11-并发恢复与生命周期)
12. [扩展指南](#12-扩展指南)
13. [已知边界与实测问题](#13-已知边界与实测问题)
14. [附录](#14-附录)

---

## 1. 项目定位与问题

ResearchWiki 要做的事只有一句：

> 把研究资料编译为**可追溯、可检索、可由原始来源复核**的本地知识层。

### 1.1 它解决的不是"总结论文"

一般的 RAG 工具回答"这篇论文讲了什么"。ResearchWiki 要回答的是一个更严格的问题：

> **这句话是从哪篇论文的第几页第几个字符来的？我能不能自己打开原文核对？**

这个差别决定了整套架构。因为要"可核对"，就必须解决四个工程问题：

| 问题 | 朴素做法为什么不行 |
|---|---|
| 文档身份 | 用文件名或标题当 ID → 改个名字就变成两篇；同名不同文又会互相覆盖 |
| 抽取可信度 | 直接信任 LLM 输出 → 幻觉、期刊页眉当标题、静默覆盖历史页面 |
| 证据定位 | 只存"来自某论文" → 无法定位到页，人工复核成本等于重读全文 |
| 模型可用性 | 假设模型永远在线 → 没密钥/超时/限流时整个系统不可用 |

ResearchWiki 对这四个问题各给了一个明确答案，构成下面的三条原则。

---

## 2. 三条核心设计原则

### 原则一：内容身份与知识身份分离

```
原始文件 ──SHA-256──► 来源身份（永不变化）
                          │
                          │ frontmatter: sources: [sha256]
                          ▼
LLM 抽取 ──title────► 知识身份（随模型输出变化）
```

- 文档主键 = 原始文件**完整 SHA-256**（`app.py:77`）。
- Wiki 页面主键 = slug / frontmatter `title`。

好处：换模型、改提示词、重新抽取，**溯源链不断裂**。文件重命名不产生新实体。

代码位置：`app.py:_hash_file` / `app.py:228`，绑定逻辑在 `index/database.py:976-999`。

### 原则二：Markdown 是事实来源，数据库是可重建投影

```
wiki/**/*.md   ←── 事实来源（人类可读、可手改、可 git 管理）
      │
      │  rw build  /  Database.rebuild()
      ▼
data/researchwiki.db  ←── 派生投影（索引、关系、证据、图谱）
```

删掉整个 `.db` 文件，跑一次 `rw build` 就能从 Markdown 完整重建索引、链接、证据定位与图谱。这条性质是 `index/database.py:rebuild()` 存在的根本原因，也意味着**数据库里的数据永远不应该比 Markdown 更权威**。

### 原则三：诚实降级，绝不假装成功

系统有四种降级路径，每一种都如实上报状态而不是静默兜底：

| 情况 | 行为 | 上报 |
|---|---|---|
| 没有检索证据 | 拒答，**不调用模型** | `mode: no_evidence` |
| 有证据但模型未配置 | 本地证据摘录 | `mode: local_evidence` |
| 有证据但模型调用失败 | 本地证据摘录 | `mode: local_evidence` + `fallback_reason` |
| 向量服务不可用 | 退回 FTS5 + 图关系 | 检索仍返回结果，`retrieval_signals` 无 `vector` |

同样地，抽取定位找不到精确匹配时，会明确标注 `section: "source excerpt (fallback)"`（`database.py:685`），而不是假装精确。

---

## 3. 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ 表现层   web/static/{index.html,app.js,styles.css}          │
│          8 视图单页工作台   ｜   cli.py  (rw 命令, 12 子命令) │
├─────────────────────────────────────────────────────────────┤
│ 接口层   web/app.py   FastAPI, 28 个唯一路径, SSE           │
├─────────────────────────────────────────────────────────────┤
│ 编排层   app.py       ingest / rebuild / delete_source      │
│                       do_search / do_ask / initialize       │
├─────────────────────────────────────────────────────────────┤
│ 能力层   parsing  extraction  llm  wiki                     │
│          index    retrieval   qa   graph   evaluation       │
├─────────────────────────────────────────────────────────────┤
│ 存储层   wiki/**/*.md  ｜  data/researchwiki.db  ｜  raw/    │
├─────────────────────────────────────────────────────────────┤
│ 配置层   config.py   DEFAULT_CONFIG ⊕ config.yaml ⊕ .env     │
└─────────────────────────────────────────────────────────────┘
```

**依赖方向是单向向下的**，但有两条刻意的例外值得注意：

1. `index/database.py` 反向 import `wiki/manager.py`（`database.py:9`）——因为索引器必须解析 frontmatter 和 `[[链接]]`，复用 Wiki 层的解析函数比复制一份更安全。
2. `graph/extractor.py` 依赖 `index/database.py`——图谱是**从索引投影出来的**，不是独立数据源。

### 各模块职责一句话

| 模块 | 文件 | 职责 |
|---|---|---|
| 配置 | `config.py` | 三层配置合并 + 从 provider 推导密钥环境变量名 |
| 数据模型 | `models.py` | `Page` / `ParsedDocument` / `StructuredPaper` / `EntityMention` |
| 解析 | `parsing/pdf.py` | PDF（PyMuPDF，逐页）与纯文本，产出 `ParsedDocument` |
| 抽取 | `extraction/paper.py` | 标准 / 高质量两阶段，产出 `StructuredPaper` |
| 提示词 | `extraction/prompts.py` | 抽取、分析、问答三套 system prompt |
| LLM | `llm/client.py` | 零 SDK 依赖的 OpenAI 兼容客户端 + JSON 容错解析 |
| Wiki | `wiki/manager.py` | Markdown 编译、staging 原子提交、用户笔记保护、可恢复删除 |
| 索引 | `index/database.py` | 12 张业务表、FTS5、中文兜底、证据定位、图谱落库 |
| 检索 | `retrieval/search.py` | 三路召回 + RRF 融合 |
| 向量 | `retrieval/vector.py` | 可选 embedding 客户端 + 余弦检索 + 增量同步 |
| 问答 | `qa/engine.py` + `qa/context.py` | 上下文预算装配 + 三态回答 |
| 图谱 | `graph/extractor.py` | 确定性投影 + 校验 + digest |
| 评估 | `evaluation/runner.py` | 固定题库重放 + 四指标 |
| 编排 | `app.py` | 把上述能力串成流水线 |
| 接口 | `web/app.py` / `cli.py` | HTTP 与命令行两个入口 |

---

## 4. 存储模型：双身份系统

### 4.1 目录布局

```
<workspace>/
├── config.yaml              用户配置（覆盖 DEFAULT_CONFIG）
├── purpose.md               全局研究意图，注入抽取提示词
├── .env                     DEEPSEEK_API_KEY / EMBEDDING_API_KEY（gitignored）
├── raw/papers/              ★ 不可变原文，按内容哈希命名
│   └── <sha256>.<ext>
├── wiki/                    ★ 知识层事实来源
│   ├── papers/  methods/  datasets/  topics/  synthesis/
├── data/
│   ├── researchwiki.db      ★ 派生投影
│   ├── extractions/<sha256>.json    结构抽取结果
│   ├── analyses/<sha256>.md         高质量模式第一阶段分析
│   ├── inbox/                       Web 上传暂存区
│   └── trash/<stamp>-<id>/          可恢复删除区 + manifest.json
├── schema/*.md              工作区说明文件（非运行时模板）
└── logs/researchwiki.log    JSON Lines 事件流
```

### 4.2 来源身份：`sources` + `papers`

```sql
sources(id PK, sha256, original_name, original_path, raw_path,
        media_type, size_bytes, created_at, updated_at, deleted_at)

papers(id PK, title, authors, year, source_path, raw_path,
       status, error_message, created_at, updated_at)
```

两张表**共用同一个主键**——都是文件的 SHA-256。分工是：

- `sources` 记录"这是什么文件"（物理事实，几乎不变，`deleted_at` 支持软删除）；
- `papers` 记录"处理到哪一步"（流水线状态，频繁更新）。

**为什么拆两张表？** 因为删除是软删除：`sources.deleted_at` 标记后，`papers.status` 变 `DELETED`，但原文哈希身份仍然保留在库里，用于回溯与冲突检测。`rebuild()` 查询时会用 `COALESCE(s.deleted_at,'')=''` 过滤掉（`database.py:947`）。

### 4.3 知识身份：`wiki_pages`

```sql
wiki_pages(id PK AUTOINCREMENT, title, type, path UNIQUE, created_at, updated_at)
```

- `path` 唯一 —— 一个 Markdown 文件对应一条记录。
- `type` 来自 frontmatter 的 `type` 字段，缺失时从父目录名推导（`database.py:963`）。

### 4.4 两者的绑定：`page_sources`

```sql
page_sources(page_id, source_id, source_path, locator, page_number,
             section, evidence_text, char_start, char_end, created_at,
             PRIMARY KEY(page_id, source_id))
```

这是**整个溯源系统的枢纽表**。它回答"这个 Wiki 页面是由哪些原始资料的哪个位置生成的"。

绑定通过四路匹配建立（`database.py:976-999`）：

```
1. frontmatter sources: [hash]      → 直接精确
2. frontmatter source: <raw_path>   → 路径匹配，失败则用文件名哈希前缀兜底
3. paper 页标题 == papers.title      → 同名匹配
4. 正文 [[链接]] 指向的论文页         → 传递匹配
```

第 4 条之后还有一个**传播轮次**（`database.py:1032-1040`）：如果一个实体页没有直接来源，但它链接到的页面有来源，就把来源传递过来，最多迭代 3 轮。这解决了旧版本实体页 frontmatter 不带 `sources[]` 的兼容问题。

然后 `enrich_source_evidence`（`database.py:624`）为每个绑定回填**可复现定位符**：

```
优先：在源文本中 casefold 查找页面标题         → exact
退而：取页面正文前 8 个候选句，找 80 字符针   → 找到即用该页
兜底：源文件第 1 个非空页                     → section="source excerpt (fallback)"
```

产出形如：

```
locator      = "page:9#chars=81-710"
page_number  = 9
char_start   = 81
char_end     = 710
evidence_text= "Statistics of LeCaRD and ELAM. LeCaRD ELAM # total queries 107 85 # ca..."
section      = "ELAM"  或  "source excerpt (fallback)"
```

### 4.5 其余表速查

| 表 | 作用 | 关键约束 |
|---|---|---|
| `links` | Wiki `[[双链]]` 图 | `UNIQUE(source_id,target_id,relation)` |
| `chunks` | 页面全文（一页一行） | 供无 FTS5 时 LIKE 兜底 |
| `wiki_fts` | FTS5 虚表 `(title,type,path,content,page_id UNINDEXED)` | 创建失败则 `fts_available=False` |
| `ingest_jobs` | 持久化导入队列 | 部分唯一索引保证同源单活动任务 |
| `reviews` | 质量审核项 | 去重：`(kind,message,source_id,page_path)` |
| `embeddings` | 页面向量 | `page_id` 主键，`content_hash` 做增量判断 |
| `graph_nodes` / `graph_edges` / `graph_runs` | 图谱契约与历史 | 边 `UNIQUE(source,target,semantic)` |
| `evaluation_runs` / `evaluation_cases` | 评估历史 | 按 profile 索引 |
| `aliases` | 实体别名 | 已建表，**当前未使用** |

**注意** `idx_jobs_one_active_source` 是一个**部分唯一索引**：

```sql
CREATE UNIQUE INDEX idx_jobs_one_active_source
  ON ingest_jobs(source_id) WHERE status IN ('PENDING','RUNNING','PAUSED');
```

它在数据库层面（而非应用层面）保证同一资料同时最多一个活动任务，即使应用层逻辑有 bug 也不会重复排队。

---

## 5. 写入主链

### 5.1 全景

```
 上传 / CLI
     │
     ▼
┌─────────────┐   sources + SHA-256 → raw/papers/<sha256>.<ext>
│ 1 固化原文   │   UPLOADED
└─────────────┘
     ▼
┌─────────────┐   ingest_jobs: PENDING → RUNNING
│ 2 持久化队列 │   stage: QUEUED → COPY_SOURCE → PARSE → ...
└─────────────┘
     ▼
┌─────────────┐   ParsedDocument(text, pages[Page(number,text)])
│ 3 本地解析   │   EXTRACTING
└─────────────┘
     ▼
┌─────────────┐   标准: 一次 structured()
│ 4 抽取       │   高质量: analyze_paper() → extract_paper()
└─────────────┘   EXTRACTED + data/extractions/<hash>.json
     ▼
┌─────────────┐   复制 wiki → 临时 staging → 生成 → 逐文件 os.replace
│ 5 原子提交   │   任一失败 → 按原内容回滚
└─────────────┘   WIKI_CREATED
     ▼
┌─────────────┐   pages/chunks/FTS5/page_sources/links 全量重建
│ 6 索引重建   │   + enrich_source_evidence()
└─────────────┘
     ▼
┌─────────────┐   graph_nodes/graph_edges/graph_runs
│ 7 图谱投影   │   + sync_vector_index()（可选）
└─────────────┘   INDEXED
```

状态机：`UPLOADED → EXTRACTING → EXTRACTED → WIKI_CREATED → INDEXED`，任一步失败 → `FAILED`（保留原文、错误、尝试次数）。

### 5.2 阶段一：固化原文

```python
# app.py:85
def _raw_copy(config, source, file_hash):
    suffix = source.suffix.lower() or ".bin"
    target = config.root / "raw" / "papers" / (file_hash + suffix)
    if source.resolve() != target.resolve():
        if not target.exists():
            shutil.copy2(source, target)
    return target
```

三个设计要点：

1. **按内容哈希命名** → 同内容不同名只存一份，天然去重。
2. **`if not target.exists()`** → 已存在就不覆盖，保证**不可变性**。
3. **`raw_path` 参与后续所有证据定位** → 原文是不可变锚点，证据定位才有意义。

### 5.3 阶段二：持久化队列

**入队**（`database.py:289`）：

```python
# 先查是否已有活动任务 → 有则直接返回（幂等）
active = SELECT * FROM ingest_jobs
         WHERE source_id=? AND status IN ('PENDING','RUNNING','PAUSED') ...
if active: return active
# 否则插入 PENDING
INSERT INTO ingest_jobs(...,status='PENDING',stage='QUEUED',attempts=0,...)
```

**领取**（`database.py:316`）——必须原子：

```python
self.conn.execute("BEGIN IMMEDIATE")     # 立即取写锁，避免并发双领
row = SELECT * FROM ingest_jobs
      WHERE status='PENDING' AND attempts < max_attempts
      ORDER BY created_at,id LIMIT 1
if not row: commit; return None
UPDATE ingest_jobs SET status='RUNNING', stage='STARTING',
       attempts=attempts+1, started_at=datetime('now') WHERE id=?
commit
```

`attempts < max_attempts` 写在查询条件里，**重试上限由数据库保证**而不是应用层计数。

**阶段推进**：整个 ingest 过程中，`_job_stage()`（`app.py:186`）在每一步写 `stage`：

```
QUEUED → COPY_SOURCE → PARSE → [ANALYZE] → EXTRACT
       → COMMIT_WIKI → REBUILD_INDEX → DONE
```

所以前端能显示细粒度进度，而不只是"处理中"。

### 5.4 阶段三：解析

```python
# parsing/pdf.py:9
def parse_file(path):
    if suffix in {".txt",".md",".markdown"}:
        text = read_text(...)
        return ParsedDocument(text=text, pages=[Page(number=1, text=text)])
    document = pymupdf.open(str(source))
    pages = [Page(number=i+1, text=page.get_text("text")) for i,page in enumerate(document)]
    return ParsedDocument(text="\n\n".join(p.text for p in pages), pages=pages)
```

**关键点：保留逐页结构。** `pages` 列表就是后面 `page_number` 定位的唯一来源。如果把 PDF 一次性拍平成字符串，页码信息就永久丢失了。

纯文本文件被当作"只有第 1 页的文档"，因此 TXT/MD 的定位永远是 `page:1#chars=a-b`。

### 5.5 阶段四：抽取（双模式）

```python
# app.py:260
if mode == "high":
    _job_stage(db, job_id, "ANALYZE")
    analysis = analyze_paper(document, llm=llm, purpose=_purpose(config))
    _atomic_write_text(analysis_path, analysis)      # data/analyses/<hash>.md
_job_stage(db, job_id, "EXTRACT")
paper = extract_paper(document, llm=llm, analysis=analysis, purpose=_purpose(config))
```

| 模式 | 调用次数 | 中间产物 | 适用 |
|---|---|---|---|
| `standard` | 1 次 `structured()` | 无 | 常规论文 |
| `high` | 2 次（`generate` + `structured`） | `data/analyses/<hash>.md` | 重要论文、抽取质量差时重试 |

提示词预算（`prompts.py:29`）：

```python
limit = 50000
excerpt = text if len(text) <= limit else text[:38000] + "\n...[middle omitted]...\n" + text[-12000:]
```

**为什么头 38k + 尾 12k？** 大多数论文的标题/摘要/方法在开头，结论/局限性在结尾。中间（通常是实验细节和参考文献）信息密度最低。这是一个刻意的取舍，也是"抽取不是逐页全文证据抽取"这一边界的技术根因。

`StructuredPaper.from_dict`（`models.py:48`）做**防御性解析**：`methods`/`datasets` 既接受 `[{"name":...}]` 也接受 `["字符串"]`，年份转 int 失败则置 None，标题缺失则落到 `"Untitled Paper"`。所以模型输出的轻微 schema 偏差不会直接炸掉流水线。

**同名冲突防护**（`app.py:267`）：

```python
title_duplicate = db.find_indexed_title_duplicate(paper.title, file_hash)
if title_duplicate:
    raise RuntimeError("A different document is already indexed with the extracted title '...'")
```

**不同原文**被抽成**完全相同标题**时直接失败，防止静默覆盖已有 Wiki 页面。这是"LLM 输出不可信"原则的直接体现。

### 5.6 阶段五：原子提交（最精妙的一段）

`WikiManager.ingest`（`wiki/manager.py:336`）：

```python
def ingest(self, paper, raw_path):
    with tempfile.TemporaryDirectory(prefix=".researchwiki-stage-", dir=self.root.parent) as directory:
        stage_root = Path(directory) / "wiki"
        if self.root.exists():
            shutil.copytree(self.root, stage_root, dirs_exist_ok=True)   # ① 全量复制
        stage = WikiManager(stage_root)
        staged_paper, staged_entities = stage._ingest_direct(paper, raw_path)  # ② 在副本上生成
        planned = {}
        for staged_path in [staged_paper, *staged_entities]:
            relative = staged_path.relative_to(stage_root)
            planned[self.root / relative] = staged_path.read_text(encoding="utf-8")  # ③ 计算差异
        self._commit(planned)                                            # ④ 提交
    return output_paths[0], output_paths[1:]
```

**为什么要复制整个 wiki？** 因为实体页是**累积**的——方法页要把新论文追加到"在不同论文中的使用"列表里，这需要读取已有内容。在副本上做，就保证了原目录在整个生成过程中完全不被触碰。

提交逻辑（`wiki/manager.py:111`）：

```python
def _commit(self, planned):
    originals = {path: path.read_text() if path.exists() else None for path in planned}  # 快照
    committed = []
    try:
        for path, content in planned.items():
            if originals[path] == content:      # 内容未变则跳过，减少磁盘写入
                continue
            self._atomic_write(path, content)
            committed.append(path)
    except Exception:
        for path in reversed(committed):        # 逆序回滚
            original = originals[path]
            if original is None:
                path.unlink()                   # 新建的文件删掉
            else:
                self._atomic_write(path, original)  # 已存在的恢复原内容
        raise
```

两层保护：

1. **单文件原子**：`_atomic_write`（`manager.py:95`）用 `mkstemp` 写临时文件 → `fsync` → `os.replace`。`os.replace` 在同文件系统内是原子的，所以**不存在"写了一半的 Markdown"**。
2. **整批回滚**：任一文件失败，逆序恢复所有已替换文件。

**用户笔记保护**（`manager.py:74-92`）——这是"人工编辑不被机器覆盖"的机制：

```
<!-- user-notes:start -->
（用户手写内容，自动更新永不触碰）
<!-- user-notes:end -->
```

```python
def _compose(self, path, generated):
    old = path.read_text() if path.exists() else ""
    notes = self._user_notes(old)                    # 提取旧文件的笔记块
    if NOTE_START in generated:
        generated = generated.split(NOTE_START, 1)[0].rstrip()
    return generated.rstrip() + "\n\n## 我的笔记\n\n" + notes + "\n"
```

`_user_notes` 兼容两种历史格式：显式标记块，以及 `## 我的笔记` 标题之后的所有内容。所以即使是手工写的旧文件，也能被正确保护。

### 5.7 阶段六：索引重建

`Database.rebuild`（`database.py:941`）每篇 ingest 都会**全量重扫** `wiki/**/*.md`：

```python
for path in wiki.all_pages():
    content = path.read_text()
    front = parse_frontmatter(content)
    page_type = front.get("type") or path.parent.name.rstrip("s")
    title = front.get("title") or front.get("name") or path.stem.replace("-", " ")
    # 1. upsert wiki_pages
    # 2. 删除并重建 chunks
    # 3. 解析 sources[] 四路匹配 → 重建 page_sources
    # 4. 删除并重建 wiki_fts 行
# 5. 清理 stale 页面（Markdown 已删但库还在的记录）
# 6. 全量重建 links（DELETE FROM links 后重扫所有 [[链接]]）
```

**全量重建是刻意的简化**（对比增量更新）：

- ✅ 实现简单、无增量状态不一致风险、`rw build` 与 ingest 走同一条路径 → 手动修复 Markdown 后一条命令即可同步。
- ❌ O(全部页面)，每次 ingest 都付这个成本。

对本地小规模知识库（几十到几百页）完全可接受，这也写在已知边界里。

清理 stale 时的删除顺序很重要（`database.py:1012-1020`）：

```python
DELETE FROM chunks       WHERE page_id=?
DELETE FROM page_sources WHERE page_id=?
DELETE FROM embeddings   WHERE page_id=?
DELETE FROM wiki_pages   WHERE id=?
DELETE FROM wiki_fts     WHERE page_id=?
```

因为 `wikis_fts` 的 `page_id` 是 `UNINDEXED` 列（不参与索引，但仍是数据），必须显式删；而 FTS5 虚表没有外键约束，所以清理必须显式做，否则会留下指向已删页面的幽灵检索结果。

### 5.8 阶段七：图谱投影与向量同步

```python
graph_stats = extract_graph(db)                                  # 见 §8.2
vector_stats = sync_vector_index(db, config.vector_config)       # 可选，失败不阻断
db.upsert_paper(file_hash, ..., "INDEXED")                       # 最后才标记完成
```

注意顺序：**只有全部派生步骤成功，状态才推进到 `INDEXED`**。`sync_vector_index` 内部捕获所有异常并返回 `{"error": ...}`（`vector.py:104`），因为向量是增强能力，不该阻断主流程。

失败处理（`app.py:298-335`）——三层 try/except 保证"失败也要留痕"：

```python
except Exception as exc:
    if "file_hash" in locals():
        try: db.upsert_paper(..., "FAILED", str(exc)[:2000])     # 尽力写失败状态
        except Exception: pass
        if job_id: try: db.update_ingest_job(job_id, status="FAILED", ...) except: pass
        try: _log(config, {"event":"ingest_failed", ...})         except: pass
    raise                                                        # 仍向上抛，调用方要知道
```

---

## 6. 读取主链

### 6.1 混合检索：三路召回 + RRF 融合

```
                    ┌──────────────────────┐
query ──────┬──────►│ FTS5 bm25            │──rank──► 1.0/(60+rank)
            │       └──────────────────────┘
            │       ┌──────────────────────┐
            ├──────►│ embedding 余弦（可选）│──rank──► 0.85/(60+rank)
            │       └──────────────────────┘
            │       ┌──────────────────────┐
            └──────►│ related_pages 图扩展  │──rank──► 0.22/(60+seed+related)
                    └──────────────────────┘
                              │
                              ▼
                   按 page_id 融合 hybrid_score
                              │
                              ▼  附 sources（证据定位）
                          top_k 结果
```

代码（`retrieval/search.py`）：

```python
lexical = database.search(query, top_k=max(top_k*2, 12))
vectors = vector_search(database, query, vector_config, max(top_k*2, 12))
fused = {}
for rank, item in enumerate(lexical, 1):  add(item, 1.0/(60+rank), "lexical")
for rank, item in enumerate(vectors, 1):  add(item, 0.85/(60+rank), "vector")
if graph_expansion:
    seeds = top 5 by hybrid_score
    for seed_rank, seed in enumerate(seeds, 1):
        for related_rank, related in enumerate(database.related_pages(seed.page_id, limit=4), 1):
            add(related, 0.22/(60+seed_rank+related_rank), "graph")
```

**为什么是 `1/(60+rank)` 而不是原始分数？** 因为三路信号的分数**量纲完全不同**：bm25 是负值且无上界，余弦是 [-1,1]，图分数是自定义加权。RRF（Reciprocal Rank Fusion）只用**排名**，彻底绕开分数归一化问题。常数 60 是 RRF 论文的经验值，作用是压平头部差异，让第 1 名和第 2 名的差距不至于过大。

**权重 `1.0 / 0.85 / 0.22` 的含义**：词法最可信（精确匹配），向量次之（语义可能漂移），图扩展最弱（只是"相关"，不保证切题）。所以图扩展只能把页面**推上来**，很难单独把不相关页面顶到第一。

**关键细节：每个融合结果都带上 `sources`**：

```python
for value in ranked:
    value["sources"] = database.get_page_sources(int(value["page_id"]))
```

这一步让检索结果天然携带证据定位，后面的上下文装配和引用映射才有材料可用。

### 6.2 图扩展的确定性打分

`related_pages`（`database.py:741`）不调用任何模型，纯规则：

```python
if candidate in adjacency[page_id]:  score += 3.0;  reasons.append("direct_link")
overlap = len(own_sources & candidate_sources)
if overlap:                          score += 4.0 * overlap; reasons.append("shared_source")
common = adjacency[page_id] & adjacency[candidate_id]
if common:                           score += 1.5 * len(common); reasons.append("common_neighbor")
if candidate.type == page.type:      score += 0.5;  reasons.append("same_type")
```

**共享来源权重最高（4.0/个）** —— 因为"由同一篇论文生成"是最强的相关性证据：论文页、它的方法页、数据集页、主题页天然构成一个语义簇。

打分带 `reasons` 一起返回，所以前端能显示"为什么推荐这个页面"，而不是给一个无法解释的数字。

### 6.3 中文检索兜底

SQLite 默认 FTS5 分词器**不切分中文**——整句 `"这些论文怎样处理标签噪声"` 会被当成一个 token，几乎不可能匹配。解决方案（`database.py:24`）是生成替代查询词项：

```python
def _fallback_search_terms(query):
    terms = []
    for token in _LATIN_TOKEN.findall(query):      # 拉丁词直接 lowercase
        terms.append(token.lower())
    for run in _CJK_TOKEN.findall(query):          # 连续 CJK 串
        for width in (4, 3, 2):                    # 生成 2/3/4 字滑窗
            for start in range(len(run) - width + 1):
                terms.append(run[start:start+width])
    for triggers, expansions in _QUERY_EXPANSIONS: # 双语扩展表
        if any(t in query for t in triggers):
            terms.extend(expansions)
    return dedup(terms)
```

配合一张手工双语映射表：

```python
_QUERY_EXPANSIONS = (
    (("标签噪声","噪声标签"), ("label noise","noisy label")),
    (("噪声","噪音"),        ("noise","noisy")),
    (("法律检索","案例检索","案件检索"), ("legal case retrieval","case retrieval")),
    (("不确定性",),          ("uncertainty","evidential")),
    (("数据集",),            ("dataset","benchmark")),
    (("方法",),              ("method","approach")),
)
```

**为什么要滑窗 2–4 字？** 因为 Wiki 页面里出现的往往是"方法""数据集"这类独立词，滑窗让自然语言问句能命中它们。

**双阶段策略**（`database.py:1044`）：先尝试用**原始查询**做 MATCH（精确、高分）；只有当返回空结果时，才降级到滑窗 OR 查询：

```python
rows = FTS5 MATCH (query)                     # 第一次：精确
if not rows:
    tokens = [t for t in _fallback_search_terms(query) if t not in stopwords]
    match = " OR ".join(_fts_quote(t) for t in tokens)
    rows = FTS5 MATCH (match)                 # 第二次：宽松
```

这个顺序很重要——如果直接用滑窗 OR，精确查询的高分排序会被大量弱匹配稀释。

最后一道过滤（`database.py:64`）：**占位页排除**

```python
def _is_placeholder_page(content):
    return ("作者：Unknown" in content
            and "未识别到摘要" in content
            and "None identified" in content)
```

抽取失败产出的空壳页面不参与检索，避免污染问答证据。

### 6.4 上下文预算装配

`assemble_context`（`qa/context.py`）——注意**用字符而不是 token**：

```python
max_context      = 48000   # 总预算
response_reserve = 6000    # 给回答留出
per_page         = 9000    # 单页上限
fixed_overhead   = min(6000, 1200 + len(question))
page_budget      = max(1000, max_context - response_reserve - fixed_overhead)
```

**为什么用字符？** 因为项目支持 DeepSeek / Moonshot / OpenAI 多家 provider，各自的 tokenizer 不同。引入 tiktoken 之类的依赖会绑定 provider，而字符计量对中文（1 字 ≈ 1–2 token）和英文（4 字符 ≈ 1 token）都只是保守近似——**宁可少装，不可超限**。

装配循环：

```python
for citation_index, item in enumerate(results, 1):
    source_lines = [f"- {label} [sha256:{id[:12]}] @ {locator}" for source in item["sources"]]
    header = f"SOURCE [{citation_index}]: [[{title}]] ({type})\nPROVENANCE:\n" + "\n".join(source_lines)
    remaining = page_budget - used - len(header) - 8
    if remaining <= 0: break
    excerpt = content[:min(per_page, remaining)]
    if not excerpt.strip(): continue
    pieces.append(header + excerpt)
    included.append({**item, "citation_index": citation_index})
    used += len(block) + 8
```

**这是引用系统的根基**：`citation_index` 在装配时确定，写进 SOURCE 标头。模型看到的 `[1]` 就是这里的第 1 个块，所以**引用编号是装配时绑定的，不是模型编造的**。返回的 `sources[].citation_index` 可以直接映射回 `page_sources` 的页码、字符区间和证据摘录。

### 6.5 三态回答

`answer_with_metadata`（`qa/engine.py:79`）的分支逻辑：

```python
if not results:
    return {mode: "no_evidence", ...}                    # ① 无检索结果 → 拒答，不调模型
context, included, stats = assemble_context(...)
if not included:
    return {mode: "no_evidence", ...}                    # ② 有结果但无正文 → 拒答
if llm and llm.enabled:
    try:
        generated = llm.generate(QA_SYSTEM, "Question:\n"+q+"\n\nResearchWiki context:\n"+context, temperature=0.2)
        return {mode: "llm", ...}                        # ③ 成功
    except Exception as exc:
        fallback_reason = str(exc)                       # ④ 失败 → 记录原因，落入本地路径
# 本地证据摘录
return {mode: "local_evidence", fallback_reason: fallback_reason, ...}
```

本地路径的摘录策略（`qa/engine.py:125`）：用问题中的实词（≥3 字符，去停用词）匹配页面句子，最多 6 条；匹配不到则用 `_section_evidence` 按章节标题（方法/数据集/结论/局限性/摘要）抽取。

答案结尾固定加一句免责：

> 离线模式未调用 LLM；以上为基于关键词的证据摘录，不等同于跨论文综合结论。

`QA_SYSTEM`（`prompts.py:52`）的核心约束：

```
You answer questions using only the supplied ResearchWiki pages.
Be explicit when evidence is insufficient. Preserve disagreements between papers.
Use inline numbered citations in the form [1], [2], matching the supplied SOURCE numbers,
and finish with a Sources section. Never cite a source number that was not supplied.
Do not fabricate page numbers or claims.
```

`Preserve disagreements between papers` 这条特别值得注意：**明确要求保留论文间的分歧，而不是强行综合成一致结论**。这是研究工具该有的态度。

---

## 7. 接口层：HTTP / CLI / Web

### 7.1 FastAPI 端点（28 个唯一路径）

| 分组 | 方法与路径 | 说明 |
|---|---|---|
| **健康** | `GET /health`、`GET /api/health` | 存储/检索/模型三态；`configured ≠ 已连通` |
| **总览** | `GET /api/overview` | 统计 + 质量警告 + 数据流说明 |
| **资料** | `GET /api/documents` | 分页、搜索、状态筛选 |
| | `GET /api/documents/{id}` | 详情 + 抽取 JSON + Wiki 页面 + Review |
| | `POST /api/documents` | multipart 上传，返回 202 |
| | `POST /api/documents/{id}/retry` | 重试失败资料，返回 202 |
| | `DELETE /api/documents/{id}?confirm=true` | 可恢复删除 |
| | `GET /api/documents/{id}/file` | 下载原文（带目录逃逸校验） |
| **队列** | `GET /api/jobs` | 任务列表 |
| | `POST /api/jobs/{id}/retry` | 重试失败/取消任务 |
| | `POST /api/jobs/{id}/cancel` | 取消待执行任务 |
| **审核** | `GET /api/reviews` | Review 列表 |
| | `POST /api/reviews/{id}/resolve` | 标记已解决 |
| **Wiki** | `GET /api/wiki/pages` | 页面列表（含总数） |
| | `GET /api/wiki/pages/{id}` | Markdown + 来源证据 + 相关推荐 |
| **图谱** | `GET /api/graph` | 节点 + 边（带截断标记） |
| | `GET /api/graph/nodes`、`GET /api/graph/edges` | 分面查询 |
| | `POST /api/graph/extract` | 重新投影 |
| **检索** | `GET /api/search?q=&top_k=` | 混合检索 |
| **问答** | `POST /api/ask` | 结构化返回（mode/sources/context） |
| | `POST /api/ask/stream` | SSE：`status` → `status` → `result` |
| **评估** | `GET /api/evaluation/questions` | 题库 |
| | `GET /api/evaluation/runs`、`GET .../{run_id}` | 历史与明细 |
| | `POST /api/evaluation/run?profile=` | 运行评估 |
| **设置** | `GET /api/settings` | 只读展示模型与检索配置 |
| | `POST /api/settings/test` | 真实最小连接测试 |
| **页面** | `GET /` | 工作台入口 |

### 7.2 输出净化（三个必须做的转换）

**① 绝对路径 → 工作区相对路径**

```python
def _relative_name(root, value):
    try: return str(Path(value).resolve().relative_to(root.resolve()))
    except (OSError, ValueError): return Path(value).name
```

转换失败时**只返回文件名**（而不是回退到绝对路径）——失败方向是"更少信息"而不是"泄露路径"。

答案文本里的路径也要就地替换（`web/app.py:609-620`）：

```python
answer_text = answer_text.replace(private_path, public_path)
```

因为 LLM 会把上下文里的路径复述进答案。

**② 原文下载的目录逃逸校验**

```python
path = Path(paper["raw_path"]).resolve()
raw_root = (config.root / "raw" / "papers").resolve()
if not path.exists() or not path.is_relative_to(raw_root):
    raise HTTPException(404, "Source file not found")
```

即使数据库里的 `raw_path` 被污染，也无法下载工作区外的文件。

**③ 上传文件名消毒**

```python
def _safe_upload_name(filename):
    base = Path(filename or "document").name                 # 先剥离目录
    safe = re.sub(r"[^\w.()\-\u3400-\u9fff]+", "-", base, flags=re.UNICODE).strip("-.")
    return safe[:160] or "document"
```

保留 `\u3400-\u9fff`（CJK）是刻意的——中文文件名不应该被清洗成 `document`。

### 7.3 SSE 流式问答

```python
def generate():
    yield event("status", {"stage": "retrieval", "message": "正在检索 Wiki 证据"})
    result = ask(request)
    yield event("status", {"stage": "generation" if result["mode"]=="llm" else "evidence", ...})
    yield event("result", result)
```

**诚实的说明：这是状态事件流，不是 token 流。** 答案是一次性生成的，SSE 只推送阶段进展。README 的当前边界里也明确写了"没有流式回答"。

### 7.4 CLI（12 个子命令）

```bash
rw init                        # 建 raw/wiki/schema/data/logs + 首次建库
rw parse PAPER                 # 只抽取，不写 Wiki（调试提示词用）
rw ingest PAPER [--resume] [--quality standard|high]
rw search QUERY [--top-k N]
rw ask QUESTION [--top-k N]
rw build                       # 从 Markdown 重建索引 + 证据 + 图谱 + Review
rw graph                       # 只重投影图谱
rw eval --profile baseline|optimized
rw jobs [--status STATUS]
rw reviews [--status STATUS]
rw delete SOURCE_ID --yes [--keep-raw]
rw web [--host] [--port] [--reload]
rw --root /path/to/workspace web        # 全局参数必须在子命令前
```

`rw parse` 是一个**重要的调试工具**：它只跑解析 + 抽取并打印 JSON，不碰 Wiki。调整提示词时用它验证效果，不会污染知识库。

### 7.5 浏览器工作台（8 视图）

| # | 视图 | 主要能力 |
|---|---|---|
| 01 | 知识工作台 | 统计、队列、质量警告、数据流示意 |
| 02 | 资料中心 | 上传、搜索、筛选、抽取详情、原文下载、失败重试 |
| 03 | Wiki 页面 | 浏览四类页面、来源证据（页码/字符区间/摘录）、相关推荐 |
| 04 | 知识图谱 | 搜索、类型显隐、缩放、详情、重新抽取 |
| 05 | 证据问答 | SSE 状态、编号引用、可定位证据 |
| 06 | 质量评估 | baseline/optimized 回放与指标对比 |
| 07 | Review 队列 | 集中处理质量问题 |
| 08 | 系统设置 | 只读配置展示 + 真实连接测试 |

前端是**零构建步骤**的原生 JS（`app.js` 595 行 + `index.html` 211 行 + `styles.css` 269 行），浏览器只调用同源 `/api/*`。这个选择很务实：本地工具不该要求 Node 工具链。

---

## 8. 四个派生契约

### 8.1 FTS5 索引

```sql
CREATE VIRTUAL TABLE wiki_fts USING fts5(title, type, path, content, page_id UNINDEXED)
```

创建失败时（某些 SQLite 构建不含 FTS5）优雅降级：

```python
try:
    self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(...)")
except sqlite3.OperationalError:
    self.fts_available = False
```

降级路径（`database.py:1060`）：用 `chunks.content LIKE '%query%'`，`score` 恒为 0——排序退化为插入顺序，但检索仍可用。

### 8.2 图谱：确定性投影

`graph/extractor.py` 的核心特征：**没有 LLM 参与**。

```
节点：
  PAGE      每张 Wiki 页面         id = "page:<page_id>"
  PAGE_TYPE 每个页面类型           id = "page_type:<type>"
  SOURCE    每份原始资料           id = "source:<sha256>"

边（三种语义）：
  HAS_TYPE    PAGE → PAGE_TYPE     evidence = "Wiki frontmatter type=..."
  HAS_SOURCE  PAGE → SOURCE        evidence = page_sources.evidence_text[:700]
  LINKS_TO    PAGE → PAGE          evidence = "Compiled Wiki link"
```

边 ID 是内容派生的稳定哈希：

```python
def _edge_id(source, target, semantic):
    return "edge:" + hashlib.sha256((source+"\0"+target+"\0"+semantic).encode()).hexdigest()[:20]
```

写入前**强校验**（`database.py:804-809`）：

```python
if edge["source"] not in node_ids or edge["target"] not in node_ids:
    raise ValueError("graph edge references a missing endpoint")
if edge["source"] == edge["target"]:
    raise ValueError("graph self-loop is not allowed")
```

整个图排序后计算 digest：

```python
nodes.sort(key=lambda v: (v["type"], v["name"].casefold(), v["id"]))
edges = sorted(unique_edges.values(), key=lambda v: (v["semantic"], v["source"], v["target"]))
canonical = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, sort_keys=True, separators=(",",":"))
digest = hashlib.sha256(canonical.encode()).hexdigest()
```

**同样的 Wiki 必然得到同样的 digest** —— 可复现性是可验证的。实测两次运行 digest 完全一致（`66044b87...`），这就是证明。

替换是原子事务（`replace_graph`）：`BEGIN IMMEDIATE` → 清空 → 插入 → 记录 run → 失败则 `rollback`。

`confidence` 字段区分证据强度：`metadata_exact`（页面元数据）、`source_hash_exact`（源哈希）、`graph`（链接推导）。

### 8.3 可恢复删除

`delete_source`（`app.py:340`）的三步：

```
1. WikiManager.remove_source()  → 论文页直接移入 trash；
                                  实体页移除该来源的贡献（删段落、删链接、改 frontmatter sources）
                                  若移除后 sources 为空 && 无用户笔记 && 无其他链接 → 整页移入 trash
2. 移动 extractions / analyses / 原文 → trash/files/
3. 写 manifest.json（完整的删除快照）+ db.mark_source_deleted() + 重建索引
```

关键判断（`wiki/manager.py:396`）：

```python
if not remaining and not note_body and not has_other_links:
    # 整页移入 trash
else:
    # 保留页面，只移除该来源的贡献
```

**共享实体不会被误删**：如果一个方法页被 3 篇论文引用，删除其中 1 篇只会移除那 1 篇的段落和 frontmatter 中的哈希。同样，**只要用户写过笔记，页面就永不自动删除**。

`manifest.json` 记录了完整的删除上下文（源记录、论文记录、Wiki 变更、移动的文件列表），使操作可审计、可手工还原。

### 8.4 评估：可重放的质量基线

固定题库（`evaluation/questions.json`）包含一道**必拒答题**：

```json
{"id": "reject", "question": "__researchwiki_out_of_scope_quantum_volcano_7391__", "expected_no_evidence": true}
```

四个指标（`runner.py:44`）：

```python
retrieval_hit            = rejected if expected_no_evidence else bool(sources)   # 该召回的召回了？
rejection_accuracy       = (rejected == expected_no_evidence)                    # 该拒答的拒答了？
grounded_answer          = rejected or bool(sources)                             # 有据可依？
source_locator_coverage  = len(located)/len(provenance)  # located = 同时有 locator 和 evidence_text
```

总分 = 四项平均 × 100。

两个 profile 的差异：

| profile | 检索路径 |
|---|---|
| `baseline` | `database.search(q, top_k=5)` 纯词法 |
| `optimized` | `search(db, q, top_k=8, graph_expansion=True)` 混合 + 图扩展 |

**全程 `llm=None`** —— 零模型费用、完全确定性，所以它可以进 CI 当回归测试。

实测对比（本仓库真实数据）：

| 指标 | baseline | optimized |
|---|---|---|
| retrieval_hit | 0.8 | 0.8 |
| rejection_accuracy | 0.8 | 0.8 |
| grounded_answer | 1.0 | 1.0 |
| **source_locator_coverage** | **0.4** | **1.0** |
| **score** | **75.0** | **90.0** |

图扩展把来源定位覆盖率从 40% 提升到 100%。**改进被量化记录下来，而不是靠感觉说"更好了"。**

---

## 9. 质量、信任与边界

### 9.1 处理状态 ≠ 内容质量

这是整套设计里最容易被误解、也最重要的区分：

| | 处理状态 | 内容质量 |
|---|---|---|
| 回答 | "流水线跑到哪了" | "产物值不值得人工核对" |
| 载体 | `papers.status` | `reviews` 表 |
| 取值 | `UPLOADED/EXTRACTING/EXTRACTED/WIKI_CREATED/INDEXED/FAILED/DELETED` | `OPEN/RESOLVED` + severity |
| 含义 | `INDEXED` 只表示**流程完成** | 表示**需要人看一眼** |

README 明确写着：**`INDEXED` 只表示处理链完成，不代表抽取内容一定正确。**

### 9.2 五类自动质量检查

| kind | severity | 触发条件 | 代码 |
|---|---|---|---|
| `suspicious_title` | error | 标题含 "contents lists available"、以 "information sciences " 开头、或等于 "untitled paper" | `app.py:122` |
| `sparse_extraction` | warning | abstract、methods、datasets、topics 全为空 | `app.py:130` |
| `missing_limitations` | info | 未抽到局限性 | `app.py:137` |
| `missing_extraction` / `invalid_extraction` | error | 抽取 JSON 缺失或无法解析 | `app.py:154,166` |
| `orphan_paper_page` | warning | Wiki 有论文页但数据库无对应资料 | `app.py:173` |

**检查只提醒，从不擅自删除历史页面或改写人工笔记。** 这是刻意的：自动清理会毁掉人工修正的成果。

`_audit_existing_reviews`（`app.py:147`）在 `rw build` 时重跑全部检查，所以修复抽取后重新 build 会得到更新的审核状态。`add_review` 自带去重（按 `kind + message + source_id + page_path`），重复 build 不会产生重复条目。

### 9.3 信任边界清单

| 边界 | 实现 |
|---|---|
| 密钥只在服务端 | `LLMClient` 只在 Python 进程读取 `DEEPSEEK_API_KEY`；`/api/settings` 和 `/api/health` 都只返回 provider/model/configured |
| `configured` 的准确含义 | 只表示"检测到密钥"，健康检查里显式带 `note` 说明**未向远端发过请求** |
| 路径不泄露 | 所有 API 出参走 `_relative_name`，失败回退到文件名 |
| 下载受限 | `is_relative_to(raw_root)` 校验 |
| 上传受限 | 扩展名白名单 + 50 MiB + 一次性读入内存 |
| 无鉴权 | 默认只监听 `127.0.0.1`，README 明确警告不要暴露公网 |
| 数据外发知情 | README 与 data-flow 都明确说明：抽取会发送正文，问答会发送问题 + 召回内容 |

### 9.4 提示词与"不发明"约束

三套 system prompt 都以**反幻觉**为核心：

`PAPER_EXTRACTION_SYSTEM`：
```
Do not invent information. Use null or [] when the paper does not provide an answer.
Distinguish author claims from your own summary. Keep entity names concise and canonical.
```

`PAPER_ANALYSIS_SYSTEM`：
```
Produce concise final analysis notes only; do not reveal chain-of-thought.
Mark missing or uncertain facts explicitly. Do not invent.
```

**`do not reveal chain-of-thought`** 值得注意——高质量模式的第一阶段要求模型输出"最终分析说明"而非推理过程，避免把思维链当作事实依据落盘到 `data/analyses/`。

第二阶段还会显式提醒（`prompts.py:44`）：

```
STAGE 1 ANALYSIS (context only; verify every field against the paper text):
```

即"第一阶段的分析仅供参考，每个字段都要回到原文核对"。

---

## 10. 配置系统

### 10.1 三层合并

```
DEFAULT_CONFIG (config.py:9)
      ⊕  config.yaml（若存在，逐 section 深合并一层）
      ⊕  .env / 环境变量（只用于填充 api_key）
```

深合并的粒度是**一层**（`config.py:164-169`）：

```python
for key, value in data.items():
    if isinstance(value, dict):
        merged[key] = dict(DEFAULT_CONFIG.get(key, {}), **value)   # 一层深合并
    else:
        merged[key] = value
```

所以 `config.yaml` 里只写 `retrieval.vector.enabled: true` 不会丢掉 `retrieval` 下的其他默认值。

### 10.2 密钥推导

```python
provider_keys = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    "kimi":     ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
    "openai":   ("OPENAI_API_KEY",),
}
names = provider_keys.get(provider, (provider.upper() + "_API_KEY",))
```

顺序：`config.yaml` 的 `llm.api_key` → 环境变量 → `.env` 文件。未注册的 provider 按 `<PROVIDER>_API_KEY` 约定推导。

**`if not os.environ.get(key)`**（`config.py:129`）——真实环境变量优先于 `.env`，不会被文件覆盖。

### 10.3 关键配置项

```yaml
llm:
  provider: deepseek
  model: deepseek-flash
  base_url: https://api.deepseek.com
  timeout_seconds: 180
  max_retries: 1              # 429/5xx/超时会退避重试

retrieval:
  top_k: 8
  max_context_chars: 48000    # 上下文总预算
  response_reserve_chars: 6000
  max_page_chars: 9000
  graph_expansion: true
  vector:
    enabled: false            # 默认关闭
    provider: openai
    model: text-embedding-3-small
    base_url: https://api.openai.com/v1

ingestion:
  quality_mode: standard
  max_attempts: 3             # 队列重试上限
```

### 10.4 配置的边界值保护

`_int_config`（`qa/context.py:6`）对所有整数配置做钳制：

```python
def _int_config(config, key, default, minimum, maximum):
    try: return max(minimum, min(maximum, int(config.get(key, default))))
    except (TypeError, ValueError): return default
```

所以 `max_context_chars: -5` 或 `"abc"` 都不会导致崩溃。`Config` 的各种 property（`top_k` / `quality_mode` / `max_ingest_attempts`）也都做了类型与范围校验。

`LLMClient._effective_temperature`（`llm/client.py:48`）是一个 provider 特例：

```python
if self.provider.lower() == "moonshot" and self.model.lower() == "kimi-k2.6":
    return 1.0
```

因为该端点只接受 `temperature=1`。**把 provider 怪癖集中在一处**，而不是散落在每个调用点。

### 10.5 零 SDK 依赖的 HTTP 客户端

`LLMClient` 用标准库 `urllib.request` 而非 `openai` SDK（`openai` 只在可选依赖里）：

```python
body = {"model":..., "temperature":..., "messages":[{"role":"system",...},{"role":"user",...}]}
if json_mode and provider == "deepseek":
    body["response_format"] = {"type": "json_object"}
    body["thinking"] = {"type": "disabled"}     # 关闭 DeepSeek 思考模式
request = urllib.request.Request(base_url + "/chat/completions", data=..., headers={"Authorization": "Bearer "+key}, method="POST")
```

重试策略（仅对可恢复错误）：

```python
except urllib.error.HTTPError as exc:
    if exc.code in {429, 500, 502, 503, 504} and attempt+1 < attempts:
        time.sleep(min(2 ** attempt, 4))     # 指数退避，上限 4 秒
        continue
    raise RuntimeError(...)                   # 4xx（除 429）立即失败
```

**JSON 容错解析**（`llm/client.py:116`）——应对模型不听话：

```python
def structured(self, system, user):
    response = self.generate(system, user, temperature=0, json_mode=True)
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.S|re.I)  # 剥 ``` 围栏
    candidate = fenced.group(1) if fenced else response
    start, end = candidate.find("{"), candidate.rfind("}")                 # 取最外层大括号
    if start >= 0 and end > start:
        candidate = candidate[start:end+1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value
```

即使开了 `json_object` 模式，模型仍可能包上 Markdown 围栏或加前后说明文字。这三步剥离让解析足够健壮。

---

## 11. 并发、恢复与生命周期

### 11.1 线程模型

```python
_INGEST_LOCK = threading.Lock()          # 保护整个 drain 循环
_UPLOAD_LOCK = threading.Lock()          # 保护上传时的查重+入队临界区
ingest_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="researchwiki-ingest")
```

**单工作线程是刻意选择**，不是偷懒：

1. 本地工作台场景下并发导入没有实际收益，而模型调用本身就慢（180s 超时）；
2. 全量索引重建彼此冲突——两个并发 ingest 同时重建索引会互相破坏；
3. `_INGEST_LOCK` + `max_workers=1` 双保险，即使有人误加 worker 也不会并发重建。

`_drain_ingest_queue`（`web/app.py:118`）是**循环领取**而不是每次领一个：

```python
with _INGEST_LOCK:
    while True:
        job = claim_next_ingest_job()
        if not job: return
        ...
        ingest(config, source, resume=True, quality_mode=..., job_id=job["id"])
```

所以一次提交可以清空整个积压队列，不必为每个任务重启一次线程。

### 11.2 SQLite 并发设置

```python
self.conn = sqlite3.connect(str(self.path))
self.conn.row_factory = sqlite3.Row
self.conn.execute("PRAGMA busy_timeout=5000")   # 遇锁等待 5 秒而不是立即报错
self.conn.execute("PRAGMA journal_mode=WAL")    # 读写并发：读者不阻塞写者
```

WAL + busy_timeout 组合使"一个线程在写、HTTP 请求线程在读"成为可能。但这不是多节点方案——WAL 依赖本地文件系统。

### 11.3 启动自愈

`create_app`（`web/app.py:169`）在应用创建时立刻做两件修复：

```python
startup_db = Database(config.db_path)
interrupted = startup_db.fail_interrupted()            # papers: 中间状态 → FAILED
interrupted_jobs = startup_db.fail_interrupted_jobs()  # jobs: RUNNING → FAILED
```

```sql
-- papers: 进程死在任何中间状态
UPDATE papers SET status='FAILED',
       error_message='PROCESS_INTERRUPTED: the previous Web process stopped before ingestion completed; retry this document.'
WHERE status IN ('UPLOADED','EXTRACTING','EXTRACTED','WIKI_CREATED');

-- jobs: RUNNING 但进程已死
UPDATE ingest_jobs SET status='FAILED', stage='INTERRUPTED',
       error_message='PROCESS_INTERRUPTED: the previous Web process stopped before this job completed.'
WHERE status='RUNNING';
```

然后 lifespan 启动时提交一次 drain：

```python
@asynccontextmanager
async def lifespan(_app):
    ingest_executor.submit(_drain_ingest_queue, config.root)   # 续跑 PENDING
    yield
    ingest_executor.shutdown(wait=True, cancel_futures=False)  # 优雅等待
```

**恢复语义是清晰的二分**：
- `PENDING` → 继续执行（任务还没开始，重跑安全）
- `RUNNING` → 标记可重试失败（任务可能已部分完成，**必须人工确认**再重跑）

这个区分很关键。自动重跑一个中断的 RUNNING 任务可能造成半成品页面被当成正常结果。

### 11.4 完整状态机

```
papers.status:
  UPLOADED ──► EXTRACTING ──► EXTRACTED ──► WIKI_CREATED ──► INDEXED
      │             │              │              │
      └─────────────┴──────────────┴──────────────┴──► FAILED ──(retry)──► UPLOADED
                                                          │
                                                          ▼
                                                      DELETED（软删除）

ingest_jobs.status:
  PENDING ──► RUNNING ──► SUCCEEDED
     │           │
     │           └──► FAILED ──(retry)──► PENDING
     └──► CANCELLED
     └──► PAUSED（预留状态，当前未使用）

ingest_jobs.stage:
  QUEUED → COPY_SOURCE → PARSE → [ANALYZE] → EXTRACT
         → COMMIT_WIKI → REBUILD_INDEX → DONE
         │                              （或 STARTING / FAILED / INTERRUPTED / CANCELLED）
```

---

## 12. 扩展指南

### 12.1 换模型 / 加 provider

1. 在 `config.yaml` 改 `llm.provider` / `model` / `base_url`；
2. 若 provider 不在已知表中，按 `<PROVIDER>_API_KEY` 命名环境变量（`llm/client.py:28` 和 `config.py:173` 两处映射表都要加，否则配置层读不到）；
3. 若有非标准行为（如只接受特定 temperature），加在 `_effective_temperature`。

**不需要改的地方**：抽取、问答、评估全部通过 `LLMClient` 抽象，不感知 provider。

### 12.2 新增 Wiki 页面类型

1. `WikiManager.TYPES` 加目录名（`wiki/manager.py:43`）；
2. `app.py:42` 的目录创建列表加同名；
3. 写生成函数（参考 `write_paper` / `_entity_generated`）；
4. 在 `update_entities` 或 `write_paper` 中调用。

索引层自动适配：`rebuild` 从 frontmatter `type` 读类型，缺失则从目录名推导（去掉复数 s），所以**新类型无需改 `database.py`**。

### 12.3 替换解析器（例如加 OCR）

`parsing/pdf.py:parse_file` 是唯一入口，返回 `ParsedDocument` 即可：

```python
@dataclass
class ParsedDocument:
    text: str                    # 全文（供抽取）
    pages: List[Page]            # 逐页（供证据定位）
```

**唯一硬约束：`pages[].number` 必须真实对应物理页**，否则所有证据定位都会错位。加 OCR 时应当保留页码，例如对扫描页用 OCR 结果填充 `Page.text`。

### 12.4 调整检索权重

三个权重都在 `retrieval/search.py:31-48`，是字面量。图扩展的打分在 `database.py:760-779`。

调完务必跑 `rw eval --profile optimized` 对比——**这是本仓库唯一被授权的"改了就验证"机制**，能从 `evaluation_runs` 表看到历史曲线。

### 12.5 新增评估指标

1. `evaluation/runner.py:_case_metrics` 加字段；
2. `metric_names` 元组加同名（`runner.py:108`），否则不会被聚合；
3. `evaluation_runs.metrics` 是 JSON 列，**无需改表结构**；
4. `evaluation_cases.metrics` 同理。

### 12.6 新增 API 端点

在 `web/app.py:create_app` 内加装饰器函数即可。注意三条纪律：

1. **路径**：出参必须过 `_relative_name`（不要泄露绝对路径）；
2. **数据库**：每个请求新建 `Database` 连接并 `finally: db.close()`（现有代码全部遵循此模式，连接不跨请求共享）；
3. **写操作**：如果是触发处理，应当**先持久化再返回 202**，然后 `app.state.ingest_executor.submit(_drain_ingest_queue, config.root)`。

---

## 13. 已知边界与实测问题

### 13.1 代码声明的边界（诚实清单）

| 边界 | 技术根因 |
|---|---|
| 无 OCR | `pymupdf.get_text()` 只提取文本层；扫描件返回空 |
| 抽取上限 5 万字符 | `prompts.py:29` 的头 38k + 尾 12k 截断 |
| 证据是**页面级**最佳匹配 | `page_sources` 主键是 `(page_id, source_id)`，一页一源一条记录；没有把页内每句话拆成独立证据 |
| 索引每次全量重建 | `Database.rebuild` 遍历全部页面，O(N) |
| 单机单线程 | `ThreadPoolExecutor(max_workers=1)` + SQLite WAL |
| 无鉴权 | 默认只监听 127.0.0.1，无登录系统 |
| 无 token 级流式 | SSE 只推 `status` / `result` 事件 |
| `data/inbox/`、`data/trash/` 不自动清理 | 无清理任务 |
| `aliases` 表已建未用 | 实体别名归一化尚未实现 |

### 13.2 我在本仓库实测发现的两个真实问题

> 以下是通过检查 `wiki/` 与数据库实际内容发现的，属于代码缺陷而非设计选择。

**问题一：主题实体未做词形归一化 → 重复页面**

```
wiki/topics/Intelligent-legal-system.md     ← 无 sources（孤立页）
wiki/topics/Intelligent-legal-systems.md    ← 有 sources
```

**根因**：`WikiManager.find_page`（`manager.py:60`）用 `normalize_name` 匹配，而它只做 casefold + 非词字符转空格：

```python
def normalize_name(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()
```

`"Intelligent legal system"` 与 `"Intelligent legal systems"` 归一化后仍不同，因此创建了两个页面。

**影响**：知识层碎片化；同一主题的证据分散在两张页面，检索和问答都只能看到一半。

**修复方向**：在 `normalize_name` 中加简单的英文单复数归一（末尾 `s`/`es` 折叠）+ 首轮导入后按相似度做一次合并审查。注意归一化必须谨慎——过度折叠会把 `"Loss"` 和 `"Los"` 混为一谈，建议只对长度 > 6 的词做复数折叠。

**问题二：同一 PDF 重复导入留下两篇论文页**

```
wiki/papers/Information-Sciences-702-2025-121915.md                    ← 期刊页眉式标题（抽取错误）
wiki/papers/Uncertainty-aware-evidential-learning-for-legal-case-...md  ← 正确标题
```

两篇都对应同一个 hash（`b4a986af06bb...`）。库里只有 1 条 `sources` 记录、2 条 `papers` 记录。

**根因**：这是修复"同名冲突防护"（`app.py:267`）之前的历史残留——早期版本没有阻止第二次导入覆盖/新建，而当时第一次抽取把期刊页眉当成了标题。数据库中那条已解决的 `orphan_paper_page` review 正是这个问题的痕迹。

**影响**：检索会把错误标题页面当成一篇独立论文返回（实测 `rw ask` 已能正确识别并只引用正确的那篇，说明模型层的 `Preserve disagreements` 约束起了作用，但底层数据仍是脏的）。

**修复方向**：`rw build` 时的 `_audit_existing_reviews` 已经能检测到孤立论文页，可在此基础上加一个**显式的清理命令**（如 `rw prune --orphans --dry-run`），列出候选并让用户确认后移入 trash，而不是自动删除。

### 13.3 实测验证结果

| 项目 | 结果 |
|---|---|
| 测试套件 | **11 passed**（0.52s） |
| 索引 | 21 页面 / 36 链接 / FTS5 21 行 |
| 证据定位覆盖 | **21/21 全部有 locator + 页码 + 字符区间 + 摘录** |
| 图谱 | 27 节点 / 78 边；两次运行 digest 一致 `66044b87...` |
| 评估 | baseline 75.0 → optimized 90.0（定位覆盖率 0.4 → 1.0） |
| 检索（中文自然语言） | ✅ 命中正确论文页 |
| 问答（带引用） | ✅ 正确识别单篇相关论文，并如实说明其余主题页证据不足 |

---

## 14. 附录

### 14.1 源文件索引

| 文件 | 行数 | 核心导出 |
|---|---|---|
| `researchwiki/config.py` | 193 | `Config`、`load_config`、`DEFAULT_CONFIG` |
| `researchwiki/models.py` | 85 | `Page`、`ParsedDocument`、`StructuredPaper`、`EntityMention` |
| `researchwiki/app.py` | 462 | `initialize`、`ingest`、`rebuild`、`delete_source`、`do_search`、`do_ask*`、`parse_only` |
| `researchwiki/cli.py` | 130 | `main`（12 子命令） |
| `researchwiki/parsing/pdf.py` | 26 | `parse_file` |
| `researchwiki/extraction/paper.py` | 52 | `analyze_paper`、`extract_paper` |
| `researchwiki/extraction/prompts.py` | 56 | 三套 system prompt + 两个 prompt 构造函数 |
| `researchwiki/llm/client.py` | 126 | `LLMClient`（`generate`、`structured`） |
| `researchwiki/wiki/manager.py` | 431 | `WikiManager`、`parse_frontmatter`、`replace_frontmatter`、`wiki_links` |
| `researchwiki/index/database.py` | 1063 | `Database`（全部持久化与检索） |
| `researchwiki/retrieval/search.py` | 59 | `search`（RRF 融合） |
| `researchwiki/retrieval/vector.py` | 137 | `EmbeddingClient`、`sync_vector_index`、`vector_search` |
| `researchwiki/qa/context.py` | 66 | `assemble_context` |
| `researchwiki/qa/engine.py` | 178 | `answer`、`answer_with_metadata` |
| `researchwiki/graph/extractor.py` | 108 | `extract_graph` |
| `researchwiki/evaluation/runner.py` | 116 | `run_evaluation`、`load_questions` |
| `researchwiki/web/app.py` | 727 | `create_app`（28 路径） |
| `researchwiki/web/static/*` | 1075 | 工作台前端 |

### 14.2 关键常量

| 常量 | 值 | 位置 | 含义 |
|---|---|---|---|
| `MAX_UPLOAD_BYTES` | 50 MiB | `web/app.py:30` | 上传上限 |
| `ALLOWED_SUFFIXES` | `.pdf .txt .md .markdown` | `web/app.py:29` | 格式白名单 |
| 提示词 `limit` | 50000 | `prompts.py:29,38` | 抽取输入字符上限 |
| 头尾切分 | 38000 + 12000 | `prompts.py:30,39` | 中间省略 |
| `max_context_chars` | 48000 | `config.py:21` | 上下文总预算 |
| `response_reserve_chars` | 6000 | `config.py:22` | 回答预留 |
| `max_page_chars` | 9000 | `config.py:23` | 单页上限 |
| RRF 常数 | 60 | `search.py:31` | 排名平滑 |
| 三路权重 | 1.0 / 0.85 / 0.22 | `search.py:31,33,48` | lexical / vector / graph |
| 图扩展种子数 | 5 | `search.py:36` | 参与扩展的种子页 |
| 每种子扩展数 | 4 | `search.py:38` | `related_pages(limit=4)` |
| 页级来源上限 | 5 | `engine.py:13,153` | 返回的 `sources` 条数 |
| 本地摘录句子上限 | 6 | `engine.py:134` | `local_evidence` 模式 |
| CJK 滑窗宽度 | 4/3/2 | `database.py:43` | 中文兜底词项 |
| 来源传播轮次 | 3 | `database.py:1032` | `page_sources` 传递 |
| `busy_timeout` | 5000 ms | `database.py:79` | SQLite 锁等待 |
| 边 ID 哈希长度 | 20 hex | `graph/extractor.py:12` | 稳定边 ID |
| `graph` 默认 limit | 500 | `database.py:875` | 节点上限 |

### 14.3 常用命令

```bash
# 环境
cd /Users/w/Documents/ChatGPT/llmwiki
source .venv/bin/activate
export DEEPSEEK_API_KEY='...'          # 或写入 .env

# 初始化与启动
rw init
rw web                                  # → http://127.0.0.1:8000
rw web --reload                         # 开发模式

# 导入与调试
rw parse paper.pdf                      # 只抽取不写 Wiki
rw ingest paper.pdf                     # 标准模式
rw ingest paper.pdf --quality high      # 两阶段
rw ingest paper.pdf --resume            # 重跑

# 检索与问答
rw search "标签噪声" --top-k 5
rw ask "这些论文怎样处理标签噪声" --top-k 4

# 重建与验证
rw build                                # 索引 + 证据 + 图谱 + Review
rw graph                                # 只重投影图谱
rw eval --profile baseline
rw eval --profile optimized

# 运维
rw jobs --status FAILED
rw reviews
rw delete <source_id> --yes
python -m pytest -q                     # 11 passed
```

### 14.4 相关文档

- [README.md](../README.md) —— 功能清单、快速开始、API 表
- [data-flow.md](data-flow.md) —— 字段边界、阶段持久化、信任边界
- [purpose.md](../purpose.md) —— 全局研究意图（会被注入抽取提示词）
- [schema/](../schema) —— 页面类型说明文件（非运行时模板）

---

*本文档基于 0.2.0 代码与本地实际数据核对编写；所有行号、常量与实测结果均来自当前仓库状态。*
