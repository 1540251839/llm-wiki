# ResearchWiki 0.2

ResearchWiki 是一个本地优先、可追溯的论文知识工作台。它接收 PDF、DOCX、Markdown 和纯文本资料，保存原文和结构化抽取，再编译成跨论文的 Paper、Method、Dataset、Topic 页面，供图谱、检索和研究问答使用。

当前默认使用本地规则抽取论文基础信息，**不需要配置 DeepSeek 密钥**。只有用户主动选择深度抽取、跨论文综合或基于模型的问答时，才向已配置的模型 API 发送相应论文内容或检索片段。原文、Wiki 和索引均保存在本机。

## 双层抽取（基础离线 / 按需深度）

上传资料时选择 **基础抽取（basic）**，只用本地文档解析和保守规则提取首页候选标题、明确标注的作者/关键词、年份、摘要、已知数据集的**提及**及文件来源，不调用大模型；未识别到的字段保留为空。纯扫描版 PDF 仍需先 OCR。数据集名称出现不代表用于实验，须复核。

选择 **深度抽取（standard）** 或 **高质量深度抽取（high）**，会把论文文本（最多约 50,000 字符，长文保留首尾片段）发送到配置的模型。深度抽取补充方法定义及原理、研究主题、实验设置等；high 模式先分析再结构化抽取。深度分析是可选的，而且目前字段内容尚无逐条来源证据校验。

已经使用 basic 建立索引的资料，可在「资料中心 → 详情」选择 **使用模型深度分析**，通过 `POST /api/documents/{id}/enrich?quality_mode=standard` 补充抽取。该操作需要模型密钥，失败时会尽量保留原有已索引内容。

对于至少包含两篇深度抽取论文的 Topic，可以在 Wiki 主题页面主动点击 **用模型生成跨论文综合**，或使用 `rw synthesize "Topic 名称"`。结果保存在 `data/syntheses/`，仅在与现有论文抽取版本匹配时显示，明确标为待人工复核。普通 `rw compile` 不会调用模型。

**注意**：现在上传完成时即使用统一知识编译器生成 Paper / Topic / Method / Dataset 专属模板，无需再额外执行 `rw compile` 才能看到新页面。`rw compile --dry-run` 仍可用于预览后续重建的变化。

## 默认语言：中文优先，英文原文保留

在 `config.yaml` 中将 `wiki.language` 保持为 `zh-CN`（项目默认值）。**深度抽取**生成的研究问题、方法解释、实验说明、数据集介绍、跨论文综合与证据问答默认使用简体中文；用户在问答时明确要求其他语言则遵从用户选择。英语论文标题、作者、原始摘要、作者关键词、方法和数据集的规范名称、缩写、DOI、页码定位及证据引文始终保持原文。

深度抽取在独立的 `abstract_zh` 中保存中文摘要，并将原文摘要保存在 `abstract`；`name_zh` 和 `topic_labels_zh` 只是中文展示别名，不用于实体 ID、路径或关联匹配。Wiki 页面先展示中文摘要，再展示英文原文；Method/Topic/Dataset 在存在中文别名时显示「中文名称（Original Name）」。需要英文解释可把 `wiki.language` 设置成 `en`，原始数据同样保留。

**基础离线抽取不会翻译**：只保存原文中的真实内容，未生成中文摘要会明确显示缺失，不以推测补足。旧版英文缓存不会因为执行 `rw compile` 而自动翻译；如需新增中文摘要和中文解释，请在「资料中心 → 详情」点击「重新生成中文解读」（或基础抽取资料的「使用模型深度分析」），系统会在确认后重新调用模型，保留原文和人工笔记。生成结果仍需对照英文原文核验。

## 新版使用流程：少入口、可读知识与按需增强

主导航已收敛为 **资料库 / 知识 Wiki / 知识图谱 / 研究问答 / 设置**。已移除固定题库与专用评估模块；论文导入、索引、问答等功能仍通过常规自动测试检查；知识编译、审核项和检索检查分别放在高级管理入口，便于排查但不打断阅读。

1. 进入「设置」，在密码框中保存 **DeepSeek API Key**；本地配置立即生效，无需手动编辑 `.env`。保存到 `data/private/deepseek_api_key`（POSIX 权限 0600，Git 忽略），响应和日志不返回完整密钥。此路径是本机受限文件，并非硬件加密或云端密钥管家；删除本地文件不会撤销 DeepSeek 平台的密钥。密码写入接口限制本地/同源请求，工作台仍不支持公网暴露。
2. 在「资料库」导入文件，默认基础离线提取；深度模式才调用模型。
3. 阅读 Wiki 时先看研究问题和要点；原文摘要、字段原文、哈希与 Markdown 默认收起。每个字段的核验状态应以原始来源定位为准，而不能仅凭页面级摘录判断。
4. 在已索引论文的资料详情里，可点击「只补方法」「只补实验」「只补研究概览」。模型只抽取所选方面，其他已保存字段保持不变；编译器保留用户笔记，增量索引复用未变化的页面。
5. 在「知识图谱」默认浏览 **论文、主题、方法、数据集** 四种科研对象的局部关系。可展开一跳邻居或打开全局图；需要联网且用户主动点击时，才加载 AntV G6 力导向画布，离线一直可以使用本地 SVG 布局。

### 解析与证据增强

- 长论文深度抽取采用按章节选取的证据上下文，覆盖开头、方法、实验和结论，不再仅截取首尾；仍须注意被省略的信息。
- 可选部署本地 GROBID `http://127.0.0.1:8070`，然后在 `config.yaml` 的 `parser.grobid_url` 填入该地址。只允许本机 GROBID，调用头信息解析并关闭跨库元数据合并；没有服务时自动回退 PyMuPDF/规则解析。默认不开启额外服务。
- 深度抽取可要求原文证据句并写入 `field_evidence`，系统验证该句确实出现在原始页码与字符区间；验证通过表示**文本匹配**，不代表模型解释已经得到逻辑支持。找不到片段的主张必须人工复核。
- Dataset 关系分为「仅提及」和「训练/验证/测试」等用途；实验用途必须基于明确证据，不能从任意单词命中推断。
- 仍有边界：本地 GROBID 只做可选论文头信息增强，尚无扫描件 OCR；复杂数学表格与整表指标抽取需后续建立针对性基准。

## 直接运行网页版

在当前项目上运行：

```bash
cd /Users/w/Documents/ChatGPT/llmwiki
source .venv/bin/activate
rw web --host 127.0.0.1 --port 8765
```

然后打开 <http://127.0.0.1:8765>。密钥从已有 `.env` 或环境变量读取；停止服务按 `Ctrl+C`。更新代码后，在原服务终端停止再运行同一命令即可重启。若提示 `address already in use`，先用 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 确认占用进程，并关闭旧服务。

如果是第一次在新环境安装：

```bash
cd /Users/w/Documents/ChatGPT/llmwiki
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# 只有使用深度抽取 / 跨论文综合时才需要配置 DeepSeek Key：
# export DEEPSEEK_API_KEY='替换为你的-key'
rw init
rw web --host 127.0.0.1 --port 8765
```

也可以把 `DEEPSEEK_API_KEY=...` 写入项目根目录的 `.env`。`.env` 已加入 `.gitignore`，初始化和运行都不会覆盖一个已经存在的 `.env`。

开发时自动重载：

```bash
rw web --host 127.0.0.1 --port 8765 --reload
```

默认只监听 `127.0.0.1`。上述命令使用本项目的 8765 端口；省略 `--port` 时 CLI 的默认值仍为 8000。工作台没有登录系统，不应直接暴露到公网。OpenAPI 调试页位于 <http://127.0.0.1:8765/docs>。

## Web 工作台

工作台功能包括（主导航只显示五个常用入口）：

- 总览：资料、Wiki 页面、关系数量、处理队列和质量警告。
- 资料中心：上传、搜索、类型和状态筛选、分页、抽取详情、原文下载、任务取消和失败重试。
- 知识编译：预览输入与受影响页面，校验原件哈希，重建跨来源知识页，并查看清单、维护历史和冲突诊断。
- Wiki 页面：浏览 Paper、Method、Dataset 和 Topic，保留受保护的个人笔记，查看原始来源、页码或逻辑段落、字符区间与证据摘录。
- 知识图谱：从 Wiki、来源和页面类型确定性生成稳定节点/边合同，支持搜索、类型显隐、缩放、详情和重新抽取。
- 证据问答：融合 FTS5、可选向量和 Wiki 关系，实时接收模型 SSE 文本增量，通过编号引用打开 Wiki、原文与证据定位。
- Review 队列：集中处理标题异常、抽取稀疏和缺少局限性等问题。
- 系统设置：本地安全保存、删除和测试 DeepSeek Key，并展示当前阅读语言与检索信息。
- 可恢复删除：资料及仅由它生成的页面会移到 `data/trash/`，共享实体和人工笔记保留。

浏览器只调用本地 `/api/*`；DeepSeek 密钥保留在 Python 服务端，不会下发到前端。没有密钥时，已有 Wiki 的浏览与检索仍可用，问答会返回 `local_evidence`；完全没有检索证据时返回 `no_evidence`，不会调用模型。

知识编译、索引和图谱可分别从命令行执行：

```bash
rw compile --dry-run     # 查看输入、受影响页面、保留文件和诊断，不写 Wiki
rw compile               # 从抽取缓存编译知识页；成功后重建索引与图谱
rw build                 # 只从已有 Wiki 重建索引、来源定位和图谱
rw graph                 # 只重新投影图谱节点/边
```

## 数据流

```text
资料中心：SHA-256 原件 + 持久化任务 + 本地基础抽取（默认）/ 可选模型深度抽取
    ↓
知识编译：校验原件 / 聚合跨来源主题 / 预览差异 / 确定性提交
    ↓
Wiki 页面、引用、内链：Markdown + manifest.json + index.md + log.md
    ↓
图谱节点与边 / 检索对象：page_sources + FTS5 + links + 可选向量
    ↓
可溯源问答：上下文预算 + SSE delta + [n] 引用 + 原文定位
```

资料状态依次为 `UPLOADED → EXTRACTING → EXTRACTED → WIKI_CREATED → INDEXED`。队列另外记录 `PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED` 和当前 stage。任一步失败都会保留原文、错误和尝试次数；Web 服务重启时，未领取的任务继续执行，已中断任务转为可重试失败。

`INDEXED` 只表示处理链完成，不代表抽取内容一定正确。工作台会把标题异常、抽取稀疏、结构化文件缺失等问题单独标记为质量警告。

独立知识编译读取原件和 `data/extractions/<sha256>.json`，不重复调用模型。原件哈希不符或输出路径发生人工内容冲突时阻止写入；未识别为生成产物的文件、失去当前来源的旧页和个人笔记会保留并报告。生成区使用稳定 ID，同一输入的 `content_digest` 不受运行时间和个人笔记影响。首次接管旧版生成页时会核对可重建的生成内容。

图谱有 `PAGE`、`TOPIC`、`PAGE_TYPE`、`SOURCE` 四种节点，以及 `HAS_SOURCE`、`HAS_TYPE`、`LINKS_TO` 三种边。页面节点 ID 由 Wiki 相对路径生成，数据库行号只用于界面跳转；移动工作区或重建 SQLite 不应改变同一语料的图谱摘要。来源边保存页码、字符区间、定位符与摘录，关系只表达来源、类型和内链，不推断论文结论之间的因果关系。

更完整的字段边界和 API 映射见 [docs/data-flow.md](docs/data-flow.md)。

## CLI

```text
rw init                              创建 raw/wiki/schema/data/logs
rw parse PAPER                       离线基础抽取，不写 Wiki
rw parse PAPER --deep                可选深度抽取，需要模型 Key
rw ingest PAPER                      默认离线基础抽取并建立 Wiki 与索引
rw ingest PAPER --quality standard   可选 DeepSeek 深度抽取
rw ingest PAPER --quality high       高质量双调用深度分析
rw ingest PAPER --quality standard --resume  对已导入论文追加深度抽取
rw synthesize "Topic 名称"             按需生成跨论文比较并标为待复核
rw ingest PAPER --resume             重新处理已有或失败资料
rw search QUERY --top-k N            搜索本地 Wiki
rw ask QUESTION --top-k N            基于检索证据回答
rw build                             从现有 Wiki 重建元数据、FTS5 和关系
rw compile [--dry-run]                从缓存重编译知识层，或只预览输入与差异
rw graph                             重新生成图谱合同
rw jobs [--status STATUS]            查看持久化导入任务
rw reviews [--status STATUS]         查看质量审核项
rw delete SOURCE_ID --yes            可恢复地删除资料及其独占生成内容
rw web [--host HOST] [--port PORT]   启动本地 API 和浏览器工作台
```

所有路径默认相对于当前目录。全局参数必须放在子命令前，例如：

```bash
rw --root /path/to/workspace web
```

## 本地 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health`、`/api/health` | 本地服务、存储和模型配置状态 |
| GET | `/api/overview` | 工作台统计、质量和数据流 |
| GET/POST | `/api/documents` | 查询或 multipart 上传资料 |
| GET | `/api/documents/{id}` | 资料、抽取结果和质量详情 |
| POST | `/api/documents/{id}/retry` | 重试失败资料 |
| DELETE | `/api/documents/{id}?confirm=true` | 移到工作区回收区并安全清理来源关系 |
| GET | `/api/documents/{id}/file` | 下载本地原文副本 |
| GET | `/api/documents/{id}/evidence?page=1&start=0&end=500` | 校验原件哈希后返回指定原文片段及物理/逻辑定位 |
| GET/POST | `/api/jobs`、`/api/jobs/{id}/retry` | 查询和重试持久化任务 |
| POST | `/api/jobs/{id}/cancel` | 取消尚未运行的导入任务 |
| GET/POST | `/api/reviews`、`/api/reviews/{id}/resolve` | 查询和处理审核项 |
| GET | `/api/compilation`、`/api/compilation/history` | 编译预览、清单、运行历史与忙碌状态 |
| POST | `/api/compilation/build` | 编译知识层，并重建索引与图谱 |
| GET | `/api/wiki/pages` | 查询 Wiki 页面 |
| GET | `/api/wiki/pages/{id}` | 读取 Wiki Markdown |
| GET | `/api/graph`、`/api/graph/nodes`、`/api/graph/edges` | 图谱合同、节点和关系查询 |
| POST | `/api/graph/extract` | 从当前索引重新抽取图谱 |
| GET | `/api/search?q=...&top_k=8` | 全文检索 |
| POST | `/api/ask` | 证据问答 |
| POST | `/api/ask/stream` | SSE 状态、来源、文本增量和最终回答 |
| GET/POST | `/api/settings`、`/api/settings/test` | 查看脱敏配置、真实模型连接测试 |

问答请求示例：

```json
{"question": "这些论文怎样处理标签噪声？", "top_k": 8}
```

健康检查中的 `llm.configured=true` 只表示检测到密钥，并不表示已向远程模型发起测试请求。

SSE 按 `status → sources → delta → result` 返回，`delta` 直接转发模型的文本增量。模型连接中断或最终编号引用无效时，服务发送 `reset`，界面清除尚未验证的部分回答，再展示本地证据摘录；异常可通过 `error` 事件报告。引用编号有效只证明编号指向已提供来源，不能证明每一句生成内容都得到原文支持。

### 可选向量检索

默认使用 FTS5 + Wiki 关系，不需要额外服务。若已有支持 OpenAI-compatible `/embeddings` 的服务，可在 `config.yaml` 中设置：

```yaml
retrieval:
  vector:
    enabled: true
    provider: openai
    model: text-embedding-3-small
    base_url: https://api.openai.com/v1
```

再通过 `.env` 设置 `EMBEDDING_API_KEY`。向量服务不可用时，检索会继续使用词法与图关系结果，不影响已有 Wiki。

## 文件与日志

- `raw/papers/`：按内容哈希保存的原始资料副本。
- `data/inbox/`：Web 上传暂存区；当前不会自动清理。
- `data/extractions/`：本地基础抽取或可选模型深度抽取的结构化 JSON。
- `data/analyses/`：高质量模式第一阶段的最终分析说明。
- `data/syntheses/`：用户主动生成的跨论文比较缓存（含来源哈希与审核状态）。
- `data/dataset_catalog.json`（可选）：自定义已知数据集名称列表，例如 `["LeCaRD", "ELAM"]`。
- `data/trash/`：删除操作产生的可恢复文件。
- `wiki/`：由原始资料派生的 Markdown 知识层；原始资料仍是复核依据。
- `wiki/manifest.json`：来源哈希、稳定页面 ID、内容摘要、引用、内链和精确证据定位。
- `wiki/index.md`、`wiki/log.md`：人类导航与追加式维护记录，不进入检索索引。
- `data/compilation/runs/`：编译输入、差异、诊断与成功/失败报告。
- `data/researchwiki.db`：资料状态、页面元数据、关系与 FTS5 索引。
- `logs/researchwiki.log`：JSON Lines 格式的导入成功与失败事件。
- 运行 `rw web` 的终端：HTTP 请求、启动错误和后台任务警告。

失败资料的 `error_message` 也会持久化到数据库，并显示在资料详情中。

用户笔记由 `<!-- user-notes:start -->` 和 `<!-- user-notes:end -->` 保护；自动更新只替换标记上方的生成内容。`schema/*.md` 当前是工作区说明文件，并非可直接修改的运行时模板。

旧版数据库中的 `evaluation_runs`、`evaluation_cases` 表不会被自动删除；它们不再创建、不再读取，也不会在网页、CLI 或 API 中暴露。如果不需要这些历史记录，可自行备份后清理旧数据库。

## 当前边界

- PDF 依赖文本层，没有 OCR；纯扫描版和未解锁的加密 PDF 会提示先预处理。DOCX 提取正文段落，使用逻辑文本流和字符区间；其 `page=1` 不代表 Word 渲染后的物理第一页。
- 基础抽取采用保守规则与可选本地 PDF 元数据，复杂排版、多栏正文、未明确标注的作者/关键词可能遗漏；需要人工复核。
- 深度抽取提示最多使用约 50,000 个字符，不是逐页全文证据抽取。
- 向量检索默认关闭，需要在 `config.yaml` 中启用并配置支持 `/embeddings` 的服务；DeepSeek 聊天模型本身不等同于嵌入模型。
- 引用记录原始资料哈希、页码或段落、字符区间和原文摘录；尚未实现 PDF 坐标框高亮或每条论断的独立证据核验。未命中主题原词的来源摘录会明确标记为 fallback。
- 队列状态持久化在 SQLite，但执行器仍是单机单工作线程，不是多节点任务系统。
- 每次导入会全量重建 Wiki 索引，适合本地小规模资料库，不适合多节点和高并发。
- 当前没有用户权限和逐条论断标注界面；规则型拒答和提示注入检查只覆盖常见模式，不保证穷尽所有对抗输入。
- 删除默认移入工作区回收区；系统不会自动永久清空 `data/trash/`。

## 验证

```bash
source .venv/bin/activate
python -m pytest -q
```

测试不调用真实 DeepSeek API。
