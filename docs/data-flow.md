# ResearchWiki 数据流与接口边界

产品中的六个主环节为：资料中心 → 知识编译 → Wiki 页面、引用与内链 → 图谱节点/边与检索对象 → 可溯源问答 → 固定题库与评估。每个环节消费上一环节的真实产物；总览和设置分别提供状态入口与运行条件。

## 1. 写入主链

| 阶段 | 输入 | 持久化输出 | 状态 | 失败后保留 |
| --- | --- | --- | --- | --- |
| 接收 | 本地路径或 Web 上传 | `sources`、`data/inbox/`（仅 Web） | `UPLOADED` | 来源身份与上传文件 |
| 排队 | source id、处理模式 | SQLite `ingest_jobs` | `PENDING/RUNNING` | 阶段、次数、错误 |
| 固化原文 | 文件字节 | `raw/papers/<sha256>.<ext>` | `UPLOADED` | 原文副本与哈希 |
| 解析 | PDF / DOCX / MD / TXT | 内存中的 `ParsedDocument` | `EXTRACTING` | 原文、错误信息 |
| 结构化抽取 | 正文文本 | `data/extractions/<sha256>.json`；高质量模式另有 `data/analyses/` | `EXTRACTED` | 原文、分析、数据库记录 |
| 导入时写 Wiki | 单篇结构化论文对象 | staging 后整批提交论文页和共享实体页 | `WIKI_CREATED` | 写入失败时回滚已替换页面 |
| 独立知识编译 | 全部活动资料的原件与抽取缓存 | 跨来源页面、`manifest.json`、`index.md`、追加 `log.md`、运行报告 | `ready/blocked → succeeded/failed` | 原件、个人笔记、未知人工页；提交失败时回滚 |
| 建立索引 | 全部 Wiki Markdown 与解析页 | pages、chunks、FTS5、带页码/字符区间/摘录的 `page_sources`、links、可选 embeddings | `INDEXED` | 全部产物 |
| 图谱投影 | Wiki 页面、来源证据、页面类型、互链 | `graph_nodes`、`graph_edges`、`graph_runs` | `INDEXED` | 确定性 digest 与最近运行 |

文档 ID 是原始文件的完整 SHA-256。文件名、论文标题或模型输出发生变化时，内容身份仍保持稳定。

Web 接口校验后先持久化 job 再返回 `202`，单工作线程领取任务。前端轮询状态；同一 hash 同时最多有一个活动任务。服务重启后仍为 `PENDING` 的任务会继续执行，运行中断的任务会标为可重试 `FAILED`。

独立编译由 `rw compile` 或 `POST /api/compilation/build` 触发；`rw compile --dry-run`、`GET /api/compilation` 预览输入、拟更新页面、保留页面和诊断，`GET /api/compilation/history` 读取历史报告。编译只用 `data/extractions/<hash>.json`，不调用模型。API 将编译与导入串行化，成功后重建索引及图谱；`rw build` 仅重建现有 Wiki 的索引，不会从抽取缓存重新生成正文。

编译器校验原件 SHA-256 与抽取缓存的 `source_hash`，以来源哈希识别论文，以规范化名称识别方法、数据集和主题。清单包含来源版本、页面 ID、路径、来源、内链、证据和内容摘要。运行时间只进入日志与运行报告，不进入生成内容摘要；同样输入产生同样生成内容，个人笔记不影响 `content_digest`。旧版生成文件仅在可重现其生成区时接管；人工内容冲突阻止覆盖，失去当前来源的旧页保留并标记待复核。

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
  → 有证据但模型不可用/输出引用无效：本地摘录，mode=local_evidence
  → 过于含糊的问题：澄清，mode=clarification
  → 编号引用映射到 page_sources 的页码/段落、字符区间与证据摘录
```

问答返回 `mode`、`sources[].citation_index`、`sources[].provenance`、`context` 和可选的 `fallback_reason`。调用者可以检查原始 source hash、来源页码、字符区间、证据摘录和实际装入上下文的页数。

流式接口依次发送 `status`、`sources`、模型实时 `delta` 和最终 `result`。模型中途失败或最终编号不合法时发送 `reset`，清除部分生成内容后返回本地摘录；不可恢复异常发送 `error`。本地摘录按段发送增量。前端收到最终回答前应保留生成中状态，不将部分文本视为已经通过引用检查。

证据卡携带 `file_url` 与 `evidence_url`。`GET /api/documents/{id}/evidence?page=N&start=A&end=B` 再次校验原件哈希和字符边界，返回原文片段及 `source_hash_verified=true`。PDF 的 `pagination=physical`，DOCX/MD/TXT 的 `pagination=logical`；DOCX 使用单个逻辑文本流，页号不代表 Word 的物理排版页。索引重建优先采用清单中经原文切片验证的精确证据，确保 Wiki、问答和图谱使用同一定位；回退摘录保留 `confidence` 标记。

## 3. 图谱与评估读取链

```text
POST /api/graph/extract
  → 从 SQLite Wiki、page_sources 和 links 重新投影
  → PAGE / TOPIC / PAGE_TYPE / SOURCE 节点
  → HAS_SOURCE / HAS_TYPE / LINKS_TO 边
  → 检查无缺失端点、无自环、稳定节点 ID
  → graph_nodes / graph_edges 原子替换 + graph_runs digest

POST /api/evaluation/run?profile=baseline|optimized
  → 冻结 evaluation/questions.json、Wiki 内容和证据定位
  → baseline / optimized 两套确定性本地排序与证据回答
  → 七项指标和逐题结果
  → evaluation_runs / evaluation_cases + data/evaluation/snapshots/<hash>.json

POST /api/evaluation/compare
  → 同一题库与语料快照运行两套策略
  → 逐题逐指标比较，任何退步都会进入 regressions

POST /api/evaluation/runs/{id}/replay
  → 校验历史快照哈希与规则版本
  → 恢复原语料、题库、上下文配置和逻辑时间
  → 生成新的评估记录，不依赖当前 Wiki 内容
```

图谱页面节点 ID 来自 Wiki 相对路径哈希，`page_id` 仅用于当前数据库中的页面跳转。规范化摘要排除数据库行号和绝对工作区路径，移动目录或重建 SQLite 不改变同一合同的 digest；页面改名/移动会改变其路径身份。`HAS_SOURCE` 边携带来源 ID、页码、字符区间、`locator`、`confidence` 和摘录。三类边表达可验证的文档结构关系，不表示论文之间的因果、支持或反驳关系。

当前默认题库为 18 道针对示例论文的问题；七项评分是 `completion`、`retrieval_hit`、`evidence_coverage`、`citation_validity`、`faithfulness`、`honest_refusal`、`robustness`。另记录 `source_locator_coverage` 作为辅助指标，不重复计入总分；不适用项以空值排除，保存实际参与评分的题数。`faithfulness` 衡量本地抽取片段是否出现在所引 Wiki 中，不证明模型答案或原论文结论正确。对比和重放均不调用远程模型；快照哈希或规则版本不一致时拒绝重放。题库、语料或评分规则不同的运行分数不能直接比较。

## 4. 信任边界

- 浏览器只连接同源的本地 FastAPI 服务。
- `DEEPSEEK_API_KEY` 只由 Python 服务读取，不出现在健康检查、文档接口或前端资源中。
- 绝对文件路径不会作为公开 API 字段返回；API 只返回工作区相对路径或文件名。
- 原文下载只允许解析到工作区的 `raw/papers/` 内。
- 上传格式限定为 PDF、DOCX、TXT、MD 和 Markdown，单文件最大 50 MiB；DOCX 主文档 XML 的解压后文本解析上限为 20 MiB。
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

- PDF 目前依赖文本层；扫描版 PDF 尚未接 OCR。DOCX 提取正文段落，不能恢复可靠的物理分页；图片中的文字不会自动识别。
- 可选向量检索依赖外部 OpenAI-compatible `/embeddings` 服务，默认关闭；失败时仍使用 FTS5 和图关系。
- 队列持久化，但执行仍限于单机单工作线程；不支持多节点协调。
- 工作台没有用户系统或公网鉴权，默认仅监听 `127.0.0.1`。
- `configured=true` 只表示密钥存在，不代表 DeepSeek 网络连通、余额充足或模型请求一定成功。
- 上传按扩展名校验并一次读入内存，没有恶意文件扫描；`data/inbox/` 当前不会自动清理。
- `page_sources` 已记录页面级最佳证据的 PDF 页码、字符区间和摘录；一个 Wiki 页面内的逐句 claim 尚未拆成多条独立证据记录。
- SSE 引用检查验证编号存在且属于已提供来源，不能替代每一句模型答案的事实核验；规则型拒答和对抗输入检测也不保证覆盖所有输入。
- 每次 ingest 会全量重建索引，适合本地小规模知识库。

## 8. 本地启动与回归检查

```bash
cd /Users/w/Documents/ChatGPT/llmwiki
source .venv/bin/activate
rw web --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>；重启时在原终端按 `Ctrl+C` 后重新运行。开发回归可执行 `python -m pytest -q`，知识层和评估分别用 `rw compile --dry-run`、`rw compile`、`rw eval --compare` 检查。真实模型连接测试单独通过设置页发起，不计入离线评估。
