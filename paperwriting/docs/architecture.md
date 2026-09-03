# 架构与模块职责

## 分层

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 网页 | `frontend/` | 三栏编辑、材料管理、候选审阅与后台状态展示 |
| HTTP | `papercoding/presentation/` | 解析请求、组装服务、返回状态与文件；不在浏览器中存密钥 |
| 业务 | `papercoding/application/` | 文献/写作/翻译用例、任务执行、LangGraph 节点与人工审批 |
| 适配 | `papercoding/infrastructure/` | SQLite 读写、模型请求、向量索引、Word 导出 |
| 共享领域 | `papercoding/domain/` | 空白论文结构、冲突异常、序列化和文本指纹 |

这是务实的模块分层，不宣称完全实现依赖倒置：HTTP 作为组装入口会访问适配层；业务层也直接使用适配器。领域层和适配层不反向导入业务或 HTTP 层。

## 核心文件

- `application/contracts.py`：共同写作原则和候选结构校验。
- `application/services.py`：文献解析、风格归纳、提纲/全文候选、翻译与合并。
- `application/jobs.py`：本机线程池与普通后台任务状态。
- `application/workflow.py`：LangGraph 图、有限返修、检查点、模型回执和审批恢复。
- `infrastructure/storage.py`：论文、文件、版本、任务和最终写入事务。
- `infrastructure/llm.py`：配置驱动的 JSON 模型调用、超时与重试。
- `infrastructure/retrieval.py`：分块、关键词召回、本地向量编码、RRF 和缓存。
- `infrastructure/exporting.py`：Markdown、模板驱动 DOCX 和确认英文校验。
- `presentation/api.py`：HTTP 路由、文件上传与应用组装。

## 保存和审批

论文和研究材料共同使用 revision 校验，避免候选生成后再覆盖作者的新修改。候选应用之前会检查授权章节、非空正文、审批检查点、候选是否改变，并在 SQLite 事务中备份旧稿。

LangGraph 检查点保存在 `langgraph.sqlite`，业务数据与调用回执保存在 `papercoding.sqlite`。按 run_id 加文件锁；已经保存的模型回执在恢复时复用。远程请求成功但本地尚未保存回执的窗口仍可能重试，不能保证远程请求 exactly-once。

备份包含业务库、存在时的图检查点及上传文件；向量索引可重建，不放入备份。恢复前停止程序并另存当前数据，再解压到配置的数据目录。不要只复制正在写入的单个 SQLite 文件。

## 检索

文献以 1200 字符分块、1020 字符步进，片段 ID 由文献 ID 与文本哈希组成。关键词使用英文词及中文二字片段，不是 BM25。

可选向量通过 FastEmbed 在本地编码；长片段使用短窗口归一化后平均池化。向量按模型名和编码策略隔离缓存，以 NumPy 精确余弦扫描召回。关键词和向量候选通过等权 RRF 融合，默认 top 8。该实现适合本地小规模材料，未提供大规模性能保证。

## 模型适配

生成模型通过 `Provider.json(system, user, max_tokens)` 交互，返回 `(dict, token_count)`。可替换基础地址和模型名，也可直接实现符合此方法的适配器并在 `create_app(provider=...)` 中注入。

应用已经包含 Agent 节点及编排。使用者需要提供模型服务和材料，而不是自行重写模型架构。API 必须满足 JSON 合约；模型遵循指令的能力影响实际结果。
