# ResearchWiki 0.2

ResearchWiki 是一个本地优先、可追溯的研究资料知识库。它把 PDF、Markdown 和纯文本资料转成结构化 JSON 与互相链接的 Markdown Wiki，并提供持久化导入队列、来源证据表、混合检索、证据问答、本地 API 和浏览器工作台。

当前默认使用 DeepSeek 做论文结构化抽取和基于证据的答案生成。原文、Wiki 和索引保存在本机；调用模型时，资料正文和检索到的 Wiki 片段会发送到当前配置的 DeepSeek API。

## 直接运行网页版

在当前项目上运行：

```bash
cd /Users/w/Documents/ChatGPT/llmwiki
source .venv/bin/activate
export DEEPSEEK_API_KEY='替换为你的-key'
rw web
```

然后打开 <http://127.0.0.1:8000>。停止服务按 `Ctrl+C`。

如果是第一次在新环境安装：

```bash
cd /Users/w/Documents/ChatGPT/llmwiki
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export DEEPSEEK_API_KEY='替换为你的-key'
rw init
rw web --host 127.0.0.1 --port 8000
```

也可以把 `DEEPSEEK_API_KEY=...` 写入项目根目录的 `.env`。`.env` 已加入 `.gitignore`，初始化和运行都不会覆盖一个已经存在的 `.env`。

开发时自动重载：

```bash
rw web --reload
```

默认只监听 `127.0.0.1`。工作台没有登录系统，不应直接暴露到公网。OpenAPI 调试页位于 <http://127.0.0.1:8000/docs>。

## Web 工作台

工作台目前包含：

- 总览：资料、Wiki 页面、关系数量、处理队列和质量警告。
- 资料中心：上传、搜索、状态筛选、抽取详情、原文下载和失败重试。
- Wiki 页面：浏览 Paper、Method、Dataset 和 Topic，并查看原始来源、PDF 页码、字符区间、证据摘录及关系推荐。
- 知识图谱：从 Wiki、来源和页面类型确定性生成稳定节点/边合同，支持搜索、类型显隐、缩放、详情和重新抽取。
- 证据问答：融合 FTS5、可选向量和 Wiki 关系，通过 SSE 返回检索状态、编号引用和可定位证据。
- 质量评估：用固定题库回放 baseline/optimized，记录检索命中、拒答正确、答案有据和来源定位覆盖。
- Review 队列：集中处理标题异常、抽取稀疏和缺少局限性等问题。
- 系统设置：只读展示服务端模型与检索配置，并可发起真实、最小的 DeepSeek 连接测试。
- 可恢复删除：资料及仅由它生成的页面会移到 `data/trash/`，共享实体和人工笔记保留。

浏览器只调用本地 `/api/*`；DeepSeek 密钥保留在 Python 服务端，不会下发到前端。没有密钥时，已有 Wiki 的浏览与检索仍可用，问答会返回 `local_evidence`；完全没有检索证据时返回 `no_evidence`，不会调用模型。

图谱和评估也可从命令行重放：

```bash
rw build                 # 重建索引、来源定位和图谱合同
rw graph                 # 只重新投影图谱节点/边
rw eval --profile baseline
rw eval --profile optimized
```

固定题库位于 `evaluation/questions.json`。评估默认使用确定性的本地证据回答，不会产生模型费用；系统设置页的“连接测试”会真实调用当前模型。

## 数据流

```text
上传 / CLI 文件
      │
      ├─ sources + SHA-256 → raw/papers/<sha256>.<ext>  来源身份 / 不可变原文
      ├─ ingest_jobs → 持久化阶段、重试和错误
      ├─ 本地解析 → ParsedDocument                    PDF / MD / TXT
      ├─ DeepSeek → 标准抽取或“分析→生成”高质量模式
      ├─ staging → 原子提交 wiki/**/*.md              sources[] / 用户笔记
      └─ 索引重建 → SQLite                            FTS5 / page_sources / links / embeddings
                         │
                         ├─ 页码 / 字符区间 / 证据片段
                         ├─ 确定性 graph_nodes / graph_edges
                         ├─ 词法 + 可选向量 + 图关系
                         └─ 上下文预算 → SSE 证据问答 → 可重放评估
```

资料状态依次为 `UPLOADED → EXTRACTING → EXTRACTED → WIKI_CREATED → INDEXED`。队列另外记录 `PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED` 和当前 stage。任一步失败都会保留原文、错误和尝试次数；Web 服务重启时，未领取的任务继续执行，已中断任务转为可重试失败。

`INDEXED` 只表示处理链完成，不代表抽取内容一定正确。工作台会把标题异常、抽取稀疏、结构化文件缺失等问题单独标记为质量警告。

更完整的字段边界和 API 映射见 [docs/data-flow.md](docs/data-flow.md)。

## CLI

```text
rw init                              创建 raw/wiki/schema/data/logs
rw parse PAPER                       调用模型检查抽取，不写 Wiki
rw ingest PAPER                      标准模式导入、抽取、编译 Wiki 并建立索引
rw ingest PAPER --quality high       两阶段高质量导入
rw ingest PAPER --resume             重新处理已有或失败资料
rw search QUERY --top-k N            搜索本地 Wiki
rw ask QUESTION --top-k N            基于检索证据回答
rw build                             从现有 Wiki 重建元数据、FTS5 和关系
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
| GET/POST | `/api/jobs`、`/api/jobs/{id}/retry` | 查询和重试持久化任务 |
| GET/POST | `/api/reviews`、`/api/reviews/{id}/resolve` | 查询和处理审核项 |
| GET | `/api/wiki/pages` | 查询 Wiki 页面 |
| GET | `/api/wiki/pages/{id}` | 读取 Wiki Markdown |
| GET | `/api/graph` | 返回 Wiki 节点和关系边 |
| GET | `/api/search?q=...&top_k=8` | 全文检索 |
| POST | `/api/ask` | 证据问答 |

问答请求示例：

```json
{"question": "这些论文怎样处理标签噪声？", "top_k": 8}
```

健康检查中的 `llm.configured=true` 只表示检测到密钥，并不表示已向远程模型发起测试请求。

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
- `data/extractions/`：模型返回并通过校验的结构化 JSON。
- `data/analyses/`：高质量模式第一阶段的最终分析说明。
- `data/trash/`：删除操作产生的可恢复文件。
- `wiki/`：Markdown 知识层，也是可读内容的事实来源。
- `data/researchwiki.db`：资料状态、页面元数据、关系与 FTS5 索引。
- `logs/researchwiki.log`：JSON Lines 格式的导入成功与失败事件。
- 运行 `rw web` 的终端：HTTP 请求、启动错误和后台任务警告。

失败资料的 `error_message` 也会持久化到数据库，并显示在资料详情中。

用户笔记由 `<!-- user-notes:start -->` 和 `<!-- user-notes:end -->` 保护；自动更新只替换标记上方的生成内容。`schema/*.md` 当前是工作区说明文件，并非可直接修改的运行时模板。

## 当前边界

- PDF 依赖文本层，没有 OCR；扫描版、加密或损坏 PDF 可能失败。
- 抽取提示最多使用约 50,000 个字符，不是逐页全文证据抽取。
- 向量检索默认关闭，需要在 `config.yaml` 中启用并配置支持 `/embeddings` 的服务；DeepSeek 聊天模型本身不等同于嵌入模型。
- 引用已记录到 Wiki 页面和原始资料哈希；目前仍不到逐条 claim 的 PDF 坐标/span 粒度。
- 队列状态持久化在 SQLite，但执行器仍是单机单工作线程，不是多节点任务系统。
- 每次导入会全量重建 Wiki 索引，适合本地小规模资料库，不适合多节点和高并发。
- 当前没有 OCR、流式回答、用户权限和逐 claim 标注界面。
- 删除默认移入工作区回收区；系统不会自动永久清空 `data/trash/`。

## 验证

```bash
source .venv/bin/activate
python -m pytest -q
```

测试不调用真实 DeepSeek API。
