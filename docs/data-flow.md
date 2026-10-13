# ResearchWiki 0.4 数据流

## 写入链

1. 文件 SHA-256 固化原件；解析保留原始页文本、章节块和字符位置。
2. basic 默认离线，保存书目和完整可读原文片段。扫描/乱码标记质量问题，不编造缺失字段。
3. standard/high 由持久化任务显式触发。正文完整分块，高质量逐块分析后提取。模型完成原因、JSON 格式与成功块分别验证；断点身份绑定来源、解析器、语言、操作范围、模型、配置和提示。
4. 最终完整缓存准备 Claim；原文定位由解析器决定，忽略模型自报页码。只有完整逐字论断或人工审核结果可获 supported。
5. 编译书目、待核验阅读内容和受支持事实；缓存、Claim 与 Wiki 发布失败恢复旧版本。旧基础资料仍可通过原文检索使用。
6. 增量重建 Wiki、精确 page_evidence、关系和图谱。图谱实验用途绑定具体实体、角色及支持证据。

任务记录 basic/standard/high、operation、focus、force_refresh、progress、stage_timings。重启后的中断任务需用户重试，未领取任务继续处理。普通 retry 复用有效断点，fresh 全部重新生成并使旧批准失效。

## 读取链

```text
Wiki 独立搜索 → FTS/加权词法/图关系/可选向量 RRF
原文独立搜索 → source_chunks/FTS + 原件哈希/解析版本/页内切片验证
混合问答     → supported Claim 与原文对象 → 同一预算 → 一次答案生成
```

检索键带类型和证据身份，不能混用 Wiki 与原文整数主键。证据编号指向实际装入上下文的论断/片段；源文件相同不代表是同一证据。supported 与 raw_excerpt 分开表达，引用定位有效不证明模型推断成立。

用户笔记、待核验模型解释、矛盾/缺证据论断不进入事实 prompt。原件修改、解析偏移失效或来源已删除时排除证据。无密钥返回原文/已支持事实摘录，无证据拒答；SSE status → sources → delta → result，失败 reset 清除部分生成后回退。

## 主要接口

| 接口 | 合同 |
| --- | --- |
| GET /api/search | Wiki 独立搜索 |
| GET /api/source-search | 原文独立搜索，可按 source_id 过滤 |
| POST /api/ask、/api/ask/stream | evidence_scope 默认 hybrid，也支持 wiki |
| POST /api/documents/{id}/enrich | standard/high 深读，默认有效断点恢复 |
| POST /api/documents/{id}/supplement | method/experiment/overview，持久化 focus |
| POST /api/documents/{id}/retry | 默认复用断点；force_refresh 需显式确认 |
| GET /api/claims、POST /api/documents/{id}/claims/{claim}/review | 定位、审核或纠正论断，成功后重新编译 |
| GET /api/documents/{id}/evidence | 再校验哈希和页内字符边界 |
| GET /api/graph/research | 四类科研对象，局部/全局视图 |
| GET/POST /api/settings | 服务端脱敏状态、本地密钥管理 |

PDF 物理页；DOCX/MD/TXT 为逻辑页，不能假装 Word 物理分页。API 不返回密钥或模型原始私人响应。默认本机监听，没有公网权限系统。

## 模型与故障边界

普通导入和编译不调用模型。只有深读、局部补充、跨论文综合、模型问答和明确连接测试调用提供方。跨论文综合超预算按批完整处理，缓存仅在输入版本匹配时使用，输出始终待审核。不存在自动题库评估或资料迁移入口。

SQLite 单机单工作线程；向量/GROBID 默认关闭。扫描件需外部 OCR。编号校验与引句位置校验不能替代人工语义核验。
