# ResearchWiki 数据流与接口边界

## 1. 写入主链

| 阶段 | 输入 | 持久化输出 | 状态 | 失败后保留 |
| --- | --- | --- | --- | --- |
| 接收 | 本地路径或 Web 上传 | `sources`、`data/inbox/`（仅 Web） | `UPLOADED` | 来源身份与上传文件 |
| 排队 | source id、处理模式 | SQLite `ingest_jobs` | `PENDING/RUNNING` | 阶段、次数、错误 |
| 固化原文 | 文件字节 | `raw/papers/<sha256>.<ext>` | `UPLOADED` | 原文副本与哈希 |
| 解析 | PDF / MD / TXT | 内存中的 `ParsedDocument` | `EXTRACTING` | 原文、错误信息 |
| 结构化抽取 | 正文文本 | `data/extractions/<sha256>.json`；高质量模式另有 `data/analyses/` | `EXTRACTED` | 原文、分析、数据库记录 |
| Wiki 编译 | 结构化论文对象 | staging 后整批提交 `wiki/` | `WIKI_CREATED` | 写入失败时回滚已替换页面 |
| 建立索引 | 全部 Wiki Markdown 与解析页 | pages、chunks、FTS5、带页码/字符区间/摘录的 `page_sources`、links、可选 embeddings | `INDEXED` | 全部产物 |
| 图谱投影 | Wiki 页面、来源证据、页面类型、互链 | `graph_nodes`、`graph_edges`、`graph_runs` | `INDEXED` | 确定性 digest 与最近运行 |

文档 ID 是原始文件的完整 SHA-256。文件名、论文标题或模型输出发生变化时，内容身份仍保持稳定。

Web 接口校验后先持久化 job 再返回 `202`，单工作线程领取任务。前端轮询状态；同一 hash 同时最多有一个活动任务。服务重启后仍为 `PENDING` 的任务会继续执行，运行中断的任务会标为可重试 `FAILED`。

## 2. 读取主链

```text
GET /api/search
  → SQLite FTS5 + 可选 embedding 向量
  → Wiki link / source overlap 图关系扩展
  → 标题、类型、相对路径、证据摘录

POST /api/ask 或 /api/ask/stream
  → 混合召回 Wiki 页面
  → 全局 context budget（页面上限 + 回答预留）
  → 没有证据：直接拒答，mode=no_evidence
  → 有证据且模型可用：DeepSeek 综合，mode=llm
  → 有证据但模型失败：本地摘录，mode=local_evidence
  → 编号引用映射到 page_sources 的页码、字符区间与证据摘录
```

问答返回 `mode`、`sources[].citation_index`、`sources[].provenance`、`context` 和可选的 `fallback_reason`。调用者可以检查原始 source hash、来源页码、字符区间、证据摘录和实际装入上下文的页数。

## 3. 图谱与评估读取链

```text
POST /api/graph/extract
  → 从 SQLite Wiki、page_sources 和 links 重新投影
  → 检查无孤儿边、无自环、稳定节点 ID
  → graph_nodes / graph_edges 原子替换 + graph_runs digest

POST /api/evaluation/run?profile=baseline|optimized
  → 固定 evaluation/questions.json
  → baseline 词法检索 / optimized 混合检索与图扩展
  → 检索命中、拒答正确、答案有据、来源定位覆盖
  → evaluation_runs / evaluation_cases
```

## 4. 信任边界

- 浏览器只连接同源的本地 FastAPI 服务。
- `DEEPSEEK_API_KEY` 只由 Python 服务读取，不出现在健康检查、文档接口或前端资源中。
- 绝对文件路径不会作为公开 API 字段返回；API 只返回工作区相对路径或文件名。
- 原文下载只允许解析到工作区的 `raw/papers/` 内。
- 上传格式限定为 PDF、TXT、MD 和 Markdown，单文件最大 50 MiB。
- 调用 DeepSeek 时会发送待抽取的正文；证据问答会发送问题和召回的 Wiki 内容。

## 5. 处理状态与内容质量

处理状态回答“流水线运行到哪里”，质量标记回答“产物是否值得人工核对”，两者不能混为一谈。

当前质量检查会标记：

- 任务状态为 `FAILED`；
- 原始文件丢失；
- 已索引但结构化抽取 JSON 缺失或无法读取；
- 标题疑似来自期刊页眉，或摘要、方法、主题同时过于稀疏；
- Wiki 中存在没有对应数据库资料的孤立论文页面。

质量检查是提醒，不会擅自删除历史页面或改写人工笔记。不同原文如果被模型抽取成与已有资料完全相同的论文标题，会转为 `FAILED`，防止静默覆盖已有 Wiki 页面。

## 6. 并发与恢复

- Web 上传先同步保存文件和数据库记录，再交给单工作线程抽取。
- SQLite 使用 WAL 和 busy timeout；适合单机工作台，不是多节点任务队列。
- 服务启动时会继续领取持久化的 `PENDING` job；遗留 `RUNNING` job 和处理中资料标为 `FAILED/PROCESS_INTERRUPTED`，由用户显式重试。
- 失败会把异常类型和原因写入 `logs/researchwiki.log`，并把可展示的错误保存到资料记录。
- 失败重试优先读取不可变原文副本，其次读取上传来源；两者都缺失时要求重新上传。

## 7. 当前边界

- PDF 目前依赖文本层；扫描版 PDF 尚未接 OCR。
- 可选向量检索依赖外部 OpenAI-compatible `/embeddings` 服务，默认关闭；失败时仍使用 FTS5 和图关系。
- 队列持久化，但执行仍限于单机单工作线程；不支持多节点协调。
- 工作台没有用户系统或公网鉴权，默认仅监听 `127.0.0.1`。
- `configured=true` 只表示密钥存在，不代表 DeepSeek 网络连通、余额充足或模型请求一定成功。
- 上传按扩展名校验并一次读入内存，没有恶意文件扫描；`data/inbox/` 当前不会自动清理。
- `page_sources` 已记录页面级最佳证据的 PDF 页码、字符区间和摘录；一个 Wiki 页面内的逐句 claim 尚未拆成多条独立证据记录。
- 每次 ingest 会全量重建索引，适合本地小规模知识库。
