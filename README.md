# ResearchWiki 0.4

本地论文知识工作台：默认离线快速导入，主动深度解读时完整分块；问答结合受支持的 Wiki 论断与可定位的原文片段。论文、Wiki 和索引保存在本机，模型调用仅在用户主动选择时发生。

## 启动

首次安装（Python 3.9+）：

```bash
git clone https://github.com/1540251839/llm-wiki.git
cd llm-wiki
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
rw init
rw web --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765/>。停止按 Ctrl+C。后续启动或重启，在项目目录执行：

```bash
source .venv/bin/activate
rw web --host 127.0.0.1 --port 8765
```

默认仅监听本机，无公网登录或权限系统。配置及密钥只由服务端读取；设置页可以保存、删除本机 DeepSeek Key。健康检查的 configured 仅表示存在密钥，不表示网络或余额正常。连接测试会真实调用模型并可能收费。

## 两种导入方式

- **快速导入（basic，默认）**：离线解析 PDF、DOCX、MD、TXT，保存基础信息和完整可读原文索引。不翻译，不生成研究结论；不确定标题、缺失字段、扫描件明确提示。
- **完整深度解读（standard）**：正文按默认 12,000 字符预算分块，提取中文解释及原文论断。不会因长文而丢弃中间章节。结构化响应检测截断，有限重试 JSON；耗尽预算后有界拆分，不把残缺响应保存为成功。
- **高质量（high）**：先逐块分析，再逐块结构化，通常更多模型请求。局部补充只更新方法、实验或概览字段；相关范围无法可靠识别时完整处理正文。
- 成功块立即保存原子断点，失败重试复用身份兼容的结果。模型、语言、范围、原文、解析版本和提示改变会使相应断点失效。资料详情展示当前块、复用量、耗时和本轮请求/Token统计。
- `--fresh` 或明确“重新完整抽取”跳过旧断点、重新收费生成；旧批准失效。普通重试不等于 fresh。局部补充不应撤销完全未改变的论断审核。
- 深读失败保留此前成功的基础资料、缓存及知识层。扫描件没有文字层时先 OCR，本项目不自动 OCR。

英文标题、原始摘要、实体规范名称和引文保持原文，中文摘要与展示别名独立保存。

## 知识与证据边界

Wiki 包含 Paper、Topic、Method、Dataset 四类专属模板。书目信息、待核验解读、已支持论断分别展示，不把模型解释与已审核事实混在一起。

Claim 状态为 supported、pending_review、insufficient、contradicted。自动 supported 只证明完整逐字陈述和数字可在原文找到；不证明论文结果在现实中为真。概括的语义支持关系仍需人工审核。只有 supported 论断进入事实 Wiki 和对应图谱实验用途关系；原文引句匹配 quote_matched 不自动升级。

编译只读取缓存，不调用模型。原件哈希不符、内容冲突时拒绝覆盖；保护用户笔记。用户笔记与待核验模型解读不进入事实问答上下文。按需跨论文综合单独调用模型，超预算显式分批，输出标记待审核，不自动成为受支持事实。

五个主入口为资料库、知识 Wiki、知识图谱、研究问答、设置。编译与论断审核位于高级入口。专用题库和评估模块已经移除，常规自动测试继续保留。

## 检索与问答

- `GET /api/search`：独立 Wiki 检索，FTS/词法、图关系、可选向量融合；向量默认关闭。
- `GET /api/source-search?q=术语&top_k=8`：独立原文检索，返回页码、字符区间及原文摘录。原件与解析定位必须匹配。
- `POST /api/ask`、`POST /api/ask/stream`：默认 hybrid，同时检索支持论断和原文片段，在统一上下文预算下生成一次答案。
- 请求可带 `"evidence_scope":"wiki"` 只用 Wiki；默认 `"hybrid"`。原文可定位不是事实已审核；模型回答不会自动写回 Wiki。
- 每个证据对象有独立编号，同一论文的不同片段不会按文件身份压成一个引用。证据卡回查原文哈希、页码和位置。
- 无 Key 返回可核对摘录；无证据拒答；SSE 中断或引用无效发送 reset，清除未完成回答后回退。编号有效不等于每个模型推断都通过语义核验。
- G6 仅主动启用时联网加载，离线保留 SVG 图谱；本机 GROBID 是可选增强，默认关闭。
- 图谱默认打开全局视图，列出全部已生成的论文节点，并区分已上传资料、全库论文节点与当前视图数量。点击论文可查看一跳局部关系；basic 论文没有连线也保留展示，处理失败的资料单独提示。

## CLI 与任务 API

```bash
rw ingest /path/paper.pdf                         # 默认离线
rw ingest /path/paper.pdf --quality standard      # 主动完整深读
rw ingest /path/paper.pdf --quality high --resume  # 高质量，复用有效断点
rw ingest /path/paper.pdf --quality standard --fresh
rw ask "这篇论文如何处理标签噪声？"
rw ask "这篇论文如何处理标签噪声？" --evidence-scope wiki
rw search "方法名称"
rw compile --dry-run
rw compile
rw build
rw jobs
rw reviews
rw synthesize "主题名称"                           # 主动模型调用
```

全局路径参数放在命令前：`rw --root /path/workspace web`。Web 导入、enrich、supplement 先持久化任务，再由单线程执行；mode、operation、focus、fresh、阶段进度及耗时可从任务接口读取。失败任务不会自动收费重跑。

`force_refresh=true` 需要网页明确确认；旧 `force=true` 保持完整重算语义。普通 retry 复用断点。删除移到项目回收区并移除来源相关索引，原件目录之外的文件不会删除。

## 存储与安全

`raw/papers/` 为哈希原件，`data/extractions/` 为最终结构化缓存，`data/claims/` 为论断与审核，`data/ingest_checkpoints/` 为成功块和拆分计划，`data/syntheses/` 和 `data/synthesis_checkpoints/` 为主动跨论文综合结果及断点。SQLite 保存资料、任务、原文片段、页面证据与索引。

原件、Wiki、运行缓存、日志、私有密钥均不应加入 Git。已有 Git 历史中的文件不会因忽略规则而从历史消失。本次整合从空资料库初始化，不自动恢复或迁移旧资料。调用模型时所选正文或证据会发送到配置的提供方。

## 验证

JavaScript 语法检查及前端回归测试需要 Node.js 18+；仅运行 Python 后端不要求 Node。

```bash
python -m pytest -q
node --check researchwiki/web/static/app.js
git diff --check
```

自动测试使用临时资料和模拟模型，不调用收费 API。更多运行合同见 [数据流](docs/data-flow.md) 和 [架构](docs/architecture.md)。
