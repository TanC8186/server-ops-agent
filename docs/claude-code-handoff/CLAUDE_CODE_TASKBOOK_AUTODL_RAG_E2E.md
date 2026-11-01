# ClaudeCode 下一阶段执行任务书：AutoDL 官方 RAG、真实 SSH 验收与双模式闭环

日期：2026-10-29  
目标分支：`g6-permissions-chat-consistency`  
项目目录：`E:\On_Call_Agent\on_call_agent`  

## 0. 执行规则

本轮不要限制 token、上下文长度或执行时长。主 Agent 必须建立 workflow，并行启动子 Agent，避免把连接、语料、应用接线、评测和文档工作全部堆在一个上下文中。

推荐拆成以下子 Agent：

| 子 Agent | 职责 | 必须交付 |
|---|---|---|
| `autodl_connection` | AutoDL SSH 配置、端口、密钥路径、真实只读访问 | `AUTODL_CONNECTION_REPORT.md` |
| `official_corpus` | 抓取 AutoDL 官方文档、保存 raw/normalized/notices/manifest | `AUTODL_CORPUS_REPORT.md` |
| `rag_ingestion` | 文档切分、metadata、Milvus revision、引用返回 | `AUTODL_RAG_REPORT.md` |
| `adapter_runtime` | AutoDL adapter/merged 模型接线、端侧推理启动 | `AUTODL_ADAPTER_RUNTIME_REPORT.md` |
| `live_benchmark` | 本地与 cloud_multi 同题真实对比 | `AUTODL_LIVE_BENCHMARK_REPORT.md` |
| `security_claims` | 凭据、日志、README、对外口径审查 | `AUTODL_SECURITY_CLAIMS_AUDIT.md` |

执行顺序：

1. 所有子 Agent 先做只读审查和依赖盘点；
2. `autodl_connection`、`official_corpus`、`adapter_runtime` 可以并行；
3. `rag_ingestion` 等官方语料产物就绪后再开始；
4. `live_benchmark` 只使用已经核对过的真实目标和固定题集；
5. 最后由一个 integration Agent 合并共享文件、运行完整验收并生成总报告。

共享代码文件不要由多个 Agent 同时编辑。每个 Agent 先提交报告或补丁，integration Agent 负责最终接线，避免互相覆盖。

## 1. 当前基线，先确认再修改

已有 AutoDL 专用训练产物：

- 数据集：`training/data_autodl_ssh/`
- adapter：`training/out/qwen15-autodl-ssh-v1/`
- 合并模型：`training/out/qwen15-autodl-ssh-v1/merged/`
- 训练结果：`docs/claude-code-handoff/AUTODL_RETRAIN_RESULT_2026-10-29.md`
- 官方来源清单：`docs/claude-code-handoff/AUTODL_OFFICIAL_SOURCES.md`

数据集 manifest 已固定为 `168` 条：`136 train / 16 val / 16 test`。新 adapter 只适用于 AutoDL SSH 目标，不能覆盖原 `qwen15-lora-v9`。

已有 AutoDL 专项离线结果：

| 指标 | 原 v9 | 新 AutoDL adapter |
|---|---:|---:|
| JSON 解析率 | 0.812 | 1.000 |
| Schema 有效率 | 0.750 | 0.938 |
| Tool F1 | 0.410 | 0.766 |
| Critical Tool Recall | 0.260 | 0.709 |
| Severity Accuracy | 0.062 | 0.625 |
| Category Accuracy | 0.688 | 1.000 |

这些数字只代表当前固定的 16 条 AutoDL 专项测试集，不能直接写成线上准确率或泛化能力。

## 2. P0：AutoDL SSH 目标配置与真实只读验收

### 2.1 统一连接参数

检查并统一：

- `services.json`
- `app/core/targets.py`
- `servers/ssh_server.py`
- `.env.example`
- `docs/ENVIRONMENT.md`
- `tests/test_runtime_target_binding.py`
- `tests/test_server_env_isolation.py`
- `tests/test_ssh_safety.py`

要求：

1. `Target` 明确保存 `kind=ssh` 和 `ssh_port`；
2. `services.json` 记录 AutoDL 的 host 和 port，密钥路径只来自运行时环境；
3. 当前 AutoDL 端口按已记录环境为 `12792`，若实际控制台显示不同，以当前授权实例为准并更新报告；
4. `SSH_SERVER_TYPE=linux`；
5. 不配置密码登录；
6. 启动日志只能输出 host、port、配置状态和不可逆 hash，不得输出 key、密码、完整环境变量或代理地址；
7. 目标绑定不能出现 host/port 与全局 SSH 配置分裂的情况；
8. 保留腾讯云旧配置的历史文件，但不能让 AutoDL 目标重新获得云监控工具。

### 2.2 真实只读验收

在获得授权的 AutoDL 实例上执行真实流程：

1. `list_all_services`
2. `get_system_info`
3. `get_process_list`
4. `check_disk_usage`
5. `search_system_logs`
6. `read_log_file`
7. `retrieve_knowledge`

每个结果必须记录：

- `status`
- `source=ssh` 或 `source=knowledge`
- `live=true/false`
- `target`
- duration
- 工具参数摘要
- 失败原因分类：认证失败、端口错误、权限不足、路径不存在、远端工具不可用、知识库无答案

禁止把连接失败写成“服务器异常”，也禁止把文档示例、合成数据或旧缓存写成当前 AutoDL 观测。

这是真实只读验收，不要添加独立的“冒烟测试”层、测试替身或无限重试逻辑。沿用生产工具链直接验证，失败时保留一次可诊断的错误证据。

## 3. P0：AutoDL 官方文档正式进入 RAG

### 3.1 固定来源

只抓取下列官方来源，不递归整站，不把用户输入 URL 直接交给抓取器：

- https://www.autodl.com/docs/env/
- https://www.autodl.com/docs/quick_start/
- https://www.autodl.com/docs/image/
- https://www.autodl.com/docs/instance_data/
- https://api.autodl.com/docs/gpu/
- https://www.autodl.com/docs/scp/
- https://www.autodl.com/docs/qa/
- https://www.autodl.com/docs/fs/
- https://www.autodl.com/docs/instance_pro_api/

优先覆盖：容器环境、系统盘/数据盘、`/root/autodl-tmp`、SSH/SCP、GPU 选型、实例释放、文件系统、Pro API 字段和常见 SSH 问题。

### 3.2 语料产物

建立独立目录，例如：

```text
knowledge/sources/autodl/
  raw/
  normalized/
  notices/
  manifest.json
```

每个来源必须保存：

- 原始 HTML 或官方 API 返回内容；
- 规范化 Markdown；
- source URL；
- 抓取时间；
- HTTP 状态、content type、字节数；
- 原文 SHA-256；
- revision；
- license/usage 状态；
- 抽取失败或动态渲染说明。

### 3.3 Metadata 合同

每个文档和 chunk 至少带：

```json
{
  "product": "autodl",
  "os_family": "linux",
  "target_kind": "ssh",
  "source_kind": "manual",
  "product_version": "unknown-or-recorded",
  "source_uri": "https://official.example/docs/page",
  "document_id": "...",
  "revision": "...",
  "chunk_id": "...",
  "valid_from": "...",
  "valid_until": null,
  "license_id": "..."
}
```

注意：上面的 `source_uri` 示例只表示字段，不得把示例文本写入实际 metadata。所有 AutoDL 文档必须使用实际来源 URL。

入库前执行敏感信息扫描，删除或脱敏：密码、token、实例连接信息、用户 key、代理地址和用户环境变量。官方示例命令可以作为知识证据，不能转化为自动执行授权。

### 3.4 Milvus 接入

要求：

1. 使用独立 revision，不覆盖已有腾讯云语料；
2. collection 中保留 `document_id/revision/chunk_id/source_uri`；
3. 检索 AutoDL 目标时默认过滤 `target_kind=ssh`、`os_family=linux` 和 `product=autodl`；
4. 允许加入 `product=linux/gpu/docker/vllm` 的官方文档，但必须显式标记 source family；
5. 更新失败必须零残留；
6. 删除或回滚后，active revision、chunk 数量和 manifest 必须一致；
7. 返回引用时显示文档标题、revision、locator 和来源 URL；
8. 文档知识只能补充处置建议，不能替代 `get_system_info`、`get_process_list`、`check_disk_usage` 等真实观测。

## 4. P0：本地 adapter 与应用运行时接线

### 4.1 目标路由

实现明确的目标类型路由：

```text
target.kind=ssh + product=autodl -> qwen15-autodl-ssh-v1
其他目标或未匹配 -> 原通用 adapter/model
cloud_multi -> 在线多智能体编排
```

不能把 AutoDL adapter 设置为所有目标的默认模型。

### 4.2 本地模式

本地模式必须：

- 使用当前 `merged` 模型或 adapter 目录；
- 只接收 AutoDL SSH 目标实际可用的工具；
- 不把 `query_cpu_metrics`、`query_memory_metrics`、`query_disk_metrics`、`query_network_metrics` 注入 AutoDL 目标；
- 对 CPU、内存、磁盘、进程、系统日志问题优先规划 SSH 工具；
- `retrieve_knowledge` 只能补充手册知识，不能伪造实时状态；
- 本地模型规划失败时停止本地路径并报告原因，不自动偷偷切换在线模型；
- 日志中记录模型版本、target kind、tool catalog hash，不记录密钥和完整 prompt 中的敏感配置。

### 4.3 前端交互

检查初始界面：

- 用户能清楚选择“本地 AutoDL SSH 小模型”或“在线 cloud_multi”；
- 选择本地模式后显示目标、模型版本和只读工具能力；
- 选择在线模式后显示云端编排状态；
- 失败信息能区分本地模型不可用、SSH 认证失败、知识库无答案和工具返回空数据；
- 不在页面展示 key、密码、完整 host 配置或原始环境变量；
- 证据卡片区分 `live SSH evidence` 和 `manual knowledge`；
- 引用卡片显示 revision 与来源 URL；
- 不添加与产品无关的防御性状态层或重复确认步骤。

## 5. P1：RAG 评测与真实双模式评测

### 5.1 RAG 固定题集

建立至少 30 条固定题，按模板分组切分，不能让同一模板同时出现在训练和评测中。至少覆盖：

- SSH 连接、非默认端口和 SCP；
- CPU/内存/磁盘/inode；
- GPU、显存、CUDA/MUSA；
- Docker、容器磁盘、镜像；
- vLLM、模型加载、显存不足；
- systemd/journalctl；
- 文档版本冲突；
- 无答案问题；
- 与腾讯云无关的 AutoDL 目标过滤。

记录：

- Recall@3、MRR、nDCG@5；
- 有答案和无答案的分母；
- no-answer 误答数；
- 引用支持率；
- 文档 revision/source URI；
- 过滤前后的候选集合；
- 失败题的真实原因。

### 5.2 同题本地/云端对比

使用同一批固定任务，让两条链路分别运行：

- 本地 `qwen15-autodl-ssh-v1`；
- 在线 `cloud_multi`。

分别统计：

- p50/p95 延迟；
- 输入/输出 token；
- 计划工具召回、tool F1、参数 exact；
- 真实 SSH 工具成功率；
- RAG 引用支持率；
- 任务完成率；
- 失败原因分类。

必须把以下因素分开：

1. 模型规划质量；
2. 工具执行质量；
3. AutoDL 账户或实例本身不可用；
4. 网络、SSH、权限和日志路径问题；
5. RAG 检索质量。

不能把云账户权限失败、指标不存在或 SSH 主机不可达归因于模型优劣。

## 6. P1：安全与凭据收尾

检查：

- Git tracked files；
- Docker build context；
- 镜像层；
- 测试报告、截图、trace、日志；
- `%TEMP%`、探针目录和临时 env 导出；
- `.gitignore`、`.dockerignore`、`.env.example`。

必须确认：

- 无真实 key、密码、token、代理地址进入 Git；
- 无真实凭据进入 Docker 镜像；
- 报告只写 set/unset、hash 或状态；
- 旧镜像和临时容器清理范围只包含本项目；
- 不删除其他项目的容器、卷和镜像。

## 7. 验收证据与报告格式

最终报告必须包含：

1. Git revision 和工作树状态；
2. 数据集、模型、adapter、RAG manifest 的 hash；
3. 真实 AutoDL SSH 验收记录；
4. RAG 入库 revision、文档数量、chunk 数量和 collection 信息；
5. RAG 固定题集指标及分母；
6. 本地/云端同题指标；
7. 前端关键路径截图或浏览器记录；
8. 后端、前端、构建和相关回归结果；
9. 未完成项及其原因；
10. 不得作为结论的主张。

结果状态使用以下口径：

- `VERIFIED`：真实依赖、固定输入、可重跑且证据完整；
- `ANALYZED`：已经完成一次分析或实验，但独立重跑/真实联调不足；
- `UNVERIFIED`：没有真实证据，不能写成已完成。

不要使用“全部完成”“生产可用”“模型准确率提升”等无法由证据直接支持的措辞。

## 8. 禁止事项

1. 不恢复腾讯云监控 API 作为 AutoDL 工具；
2. 不把 RAG 文档示例命令当成真实观测；
3. 不把合成题集结果写成真实 AutoDL 泛化能力；
4. 不把文档检索命中写成模型已经学会 AutoDL；
5. 不加入无限重试、通用兜底链或大规模防御性编程；
6. 不新增独立“冒烟测试”替代生产链路验收；
7. 不为了测试通过伪造 SSH、Milvus、GPU 或云 API 返回；
8. 不覆盖原 `qwen15-lora-v9`；
9. 不把真实凭据、连接信息或用户实例状态写入报告；
10. 未授权时不执行安装、重启、删除、修改配置或其他写操作。

## 9. 最终交付清单

完成本任务书后，仓库中应存在：

- `AUTODL_CONNECTION_REPORT.md`
- `AUTODL_CORPUS_REPORT.md`
- `AUTODL_RAG_REPORT.md`
- `AUTODL_ADAPTER_RUNTIME_REPORT.md`
- `AUTODL_LIVE_BENCHMARK_REPORT.md`
- `AUTODL_SECURITY_CLAIMS_AUDIT.md`
- 一个总验收报告，例如 `AUTODL_PHASE_RESULT_YYYY-MM-DD.md`
- 可重跑的抓取、规范化、入库和评测命令
- RAG raw/normalized/notices/manifest 产物
- AutoDL adapter 与原通用 adapter 的明确路由配置

最终结论必须回答三个问题：

1. 真实 AutoDL SSH 目标是否能被正确绑定并完成只读观测？
2. AutoDL 官方文档是否已经作为带来源和版本的知识进入 RAG？
3. 本地 AutoDL adapter 与 cloud_multi 在同一批真实任务上的差异是什么？
