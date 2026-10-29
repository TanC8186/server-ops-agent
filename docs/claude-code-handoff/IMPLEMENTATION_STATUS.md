# 实施状态（逐项）

日期：2026-10-27（第三轮：真实依赖闭环）。对象：**当前工作区**，基线 `fca7f1c` + 两轮未提交实现
（本轮已落两个本地提交 `de22339`、`ff81417`）。原始问题定义见
[ENTERPRISE_REVIEW.md](ENTERPRISE_REVIEW.md)，上轮复核见
[COMPLETION_AUDIT_2026-10-26.md](COMPLETION_AUDIT_2026-10-26.md)，本轮反例见
[ACCEPTANCE_REVIEW_2026-10-27.md](ACCEPTANCE_REVIEW_2026-10-27.md)。

三列含义严格区分，不允许互相替代：

- **已实现**：代码在当前工作区存在且被生产路径调用（不是只被测试调用）。
- **已单测**：有针对该行为的自动化测试，且本轮真实执行通过。
- **已验证**：在真实依赖（真实 Milvus / 真实云 API / 真实浏览器 / 真实镜像）上跑过。
  本轮只有浏览器一项进入了这个等级，且仅限 Milvus 不可用时的降级路径。

## 1. 结论摘要

| 维度 | 状态 |
|---|---|
| 后端测试 | **717 passed, 1 skipped**（`-o addopts=""`，2026-10-27 本轮实测） |
| 前端 | `npm run build` 通过 / `npm run lint` 0 warning / `npm test` **53 passed** |
| 静态检查 | `ruff --select F821,F811,F841` 全通过；`docker compose config --quiet` 通过 |
| 三个离线验收探针 | 全部 `bug_reproduced=false`（2026-10-27 修复后复跑，见 §6） |
| 真实浏览器 | **部分**：当前构建 + 容器后端，五档视口（1440/1100/1024/768/390/375）实测无横向溢出、替代入口与 aria-live 正常；知识库面板连真实 Milvus。见 [BROWSER_JOURNEY_2026-10-27.md](BROWSER_JOURNEY_2026-10-27.md) |
| 真实 Milvus | **已验证**：milvusdb/milvus:v2.5.10 staging，11 文档 / 953 chunks 导入、更新/回滚/失败清理/描述符错配/停机恢复全部实测。见 [MILVUS_ACCEPTANCE_2026-10-27.md](MILVUS_ACCEPTANCE_2026-10-27.md) |
| 真实云 API 诊断 | **已验证（有限）**：真实 LLM 跑通完整 cloud_multi，修复了 JSON 解析缺陷。见 [CLOUD_SMOKE_2026-10-27.md](CLOUD_SMOKE_2026-10-27.md) |
| Docker 镜像 | **已验证**：`sha256:7788efe30239...` 构建成功、compose 全栈 healthy、SIGTERM/子进程崩溃/命名卷重建/数据持久化全部实测。见 [DOCKER_ACCEPTANCE_2026-10-27.md](DOCKER_ACCEPTANCE_2026-10-27.md) |

## 2. 启动阻断（上轮 EDIT_GUARD 遗留项）

| 项 | 状态 | 证据 |
|---|---|---|
| `ssh_server.py` 缺 `read_env*` 导入 | 已修复 | `servers/ssh_server.py:24-31` 直接 `from servers._config import read_env, read_env_int`；`ruff --select F821` 通过 |
| 三个 MCP server 仍调 `load_dotenv` | 已修复 | `servers/cls_server.py`、`monitor_server.py` 改为 `read_env/read_env_bool`，`ssh_server.py` 不再调用；`tests/test_server_env_isolation.py::test_no_module_calls_load_dotenv_anymore` 用 **AST** 扫描（不再误报 `_config.py` 的说明文字） |
| 独立脚本启动兼容 | 已修复 | 三个 server 都有 `if __package__ in (None, ""): sys.path.insert(0, 项目根)`；实测 `python servers/ssh_server.py` 能起 FastMCP |
| `test_server_env_isolation` 排序回归 | 已通过 | 该文件 8 项全通过，含子 pytest 的顺序回归 |
| 真正的环境泄漏源（本轮新发现） | 已修复 | `pymilvus/settings.py` 在 import 时无参 `load_dotenv()`，会把 cwd 向上的 `.env` 写进 `os.environ`（已复现：`LEAK_PROBE_KEY` 被注入）。修复见 `app/core/env_guard.py`，由 `app/__init__.py` 在导入任何第三方库前安装 |

## 3. 原始编号逐项

### P0

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| P0-01 | 是 | `test_p0_cloud_pipeline.py` | 否 | 事件信封 `app/core/events.py` 由 `diagnostic_runtime.envelope()` 统一生成；实测 SSE 每个事件 `seq` 从 1 连续、`event_id=run_id:seq` |
| P0-02 | 是 | `test_auth.py`、`test_p0_auth_envfile.py`、`test_server_env_isolation.py` | 否 | `app/api/health.py` 的 `auth` 块只报模式与两个布尔；env 污染路径已封堵 |
| P0-03 | 是 | `test_p0_run_paths.py` | 否 | `app/api/runs.py:56-64` 先校验 32 hex 再 resolve 包含性校验 |
| P0-04 | 是 | `test_p0_upload_index.py`、`test_knowledge_api.py` | 否 | 上传先 staging 再原子替换；索引先插入后删旧；失败回滚。**仍未做**完整 revision 崩溃恢复 |

### 前端

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| FE-01 | 是 | `web/src/state.test.ts`（含本轮新增 step_complete 去重 4 条） | 浏览器（Milvus 不可用） | `web/src/App.tsx:20` 从 `./state` 导入 `applyEvent/replay/summarize`，App 内旧的 `applyEvent` 已删除；`runtime_tools.py` 的 `tool_step/step_complete` 都带 `step_id`+`tool_call_id` |
| FE-02 | 是 | `state.test.ts` | 浏览器步骤 8 | 实时与回放共用同一 reducer；`agent_plan` 走 `planFromAssignments` |
| FE-03 | 部分（A12 补刷新入口） | `view.test.ts` | 否 | 模式卡片来自 `/api/execution-modes`；“重新读取配置”在 `modeError` **或**任一模式未配置时都出现；`/health` 新增 `mcp` 能力如实报告工具缓存，但**仍没有**对模型端点的主动连通性探测（只报配置形态） |
| FE-04 | 是 | `web/src/sse.test.ts`、`chat.test.ts` | 浏览器步骤 5/7 | 诊断与聊天都走 `sse.ts` 的 `SseBuffer`+`applyBlock`+`diagnose`；EOF 无终态判 `truncated` |
| FE-05 | 部分 | `chat.test.ts` | 浏览器步骤 9 | ChatPanel 有请求版本隔离、卸载 abort、服务端历史回填；**TAB 关闭后重开**的历史一致性未验证 |
| FE-06 | 是（含本轮修复） | `test_knowledge_api.py`（14 项） | 浏览器步骤 2/3/11 | 文档/作业/版本/撤销面板；本轮修掉“失败 revision 可被 activate” |
| FE-07 | 是 | `view.test.ts` + 浏览器 | 浏览器步骤 7 | 引用卡片渲染 citations（title/locator/source_uri/snippet/score）；检索状态五态独立标签 |
| FE-08 | 是 | `test_runs_api.py` + `test_run_lifecycle_wiring.py` | 否 | 运行列表与评测分开加载；`?run=` 深链；分页在无可信游标时不截断，任意小分页都能读到终态（A09） |
| FE-09 | 部分（A12 收窄） | `view.test.ts`（新增 6 条，含 shouldOfferModeReload / usesInlineInspector） | **部分**：当前构建 1100px 浏览器实测通过 | 已补：`aria-live` 播报区（`role=status`+`.sr-only`）、≤1180px 证据 inspector 替代入口（`aria-expanded` 切换按钮，点击后 `hidden` 移除、内容完整可达）、IME 组合态判断。**仍缺**：真实中文 IME、真实读屏器朗读、完整任务旅程、375/768/1024 三档 |
| target_required 候选选择 | 是（A04 修复） | `test_target_required_flow.py`、`view.test.ts`、`test_acceptance_targets_budget_paging.py` | 否 | 服务端签发稳定候选 `target_id`；前端按完整身份去重、点击后带 `target_candidate_id` 重发一次，服务端在当前目录重新校验并绑定到具体实例/地域 |

### 后端

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| BE-01 | 是（A06 加严，含真实 payload） | `test_claims.py`、`test_cloud_multi_quality.py`、`test_claim_boundaries.py` | 否 | 按 **(数值, 单位)** 逐对核对被引用证据、检测声明里的**别的目标**；单位来源优先 `data_points[].unit`（真实 MCP 形态），其次顶层 `data.unit`，再按指标名推断，`statistics.count` 不算读数；`999ms`/`12%`/`999s`/`50分钟`/`svc-b` 全部降为假设，查询窗口“最近 10 分钟”不误判 |
| BE-02 | 是（本轮修复） | `test_cls_wrapper_status.py`（9 项） | 否 | CLS 包装层透传 ok/status/source/demo/partial，AccessDenied 不再变成“0 条成功” |
| BE-03 | 是（A04 收口） | `test_target_resolution.py`、`test_acceptance_targets_budget_paging.py` | 否 | 目标目录按**完整目标**去重；同名多实例进入 `target_required` 并各自带稳定 `target_id`；用候选 ID 重发可绑定到具体实例/地域，候选下线时明确 `not_found` 而不是悄悄改绑 |
| BE-04 | 是（A05 修正） | `test_runtime_target_binding.py`、`test_acceptance_targets_budget_paging.py` | 否 | `get_topic_info_by_name` 移出发现工具；补全**只写工具 schema 接受的键**并在补全后再校验一次（曾把 `topic_id` 塞给只接受 `topic_name` 的工具）；越界 topic 与未声明 topics 仍拒绝 |
| BE-05 | 是 | `test_cloud_multi_quality.py`、`test_run_lifecycle_wiring.py` | 浏览器步骤 6 | 本地模式 `complete` 带 lifecycle/quality/status/gaps/tools；云端 `agent_result` 落 observations/hypotheses/gaps（追问依赖它） |
| BE-06 | 是（A07 补齐） | `test_run_lifecycle_wiring.py`、`test_acceptance_targets_budget_paging.py` | 否 | `AdmissionController` 包裹 `run_diagnostic`；A07 起日志类工具参数经 `BoundedLogParams` 收敛（`limit=999999` 被压到上限），`evidence_chars` 在并入证据前真实预占/消费，超额走有界摘要并写入证据缺口 |
| BE-07 | 是（A11 统一） | 同上 + `test_cloud_multi.py` | 否 | `deadline` 下传；本地与云端在流提前关闭时都 `RunTools.cancel()` 并落 `run_cancelled` 审计，云端此前只传播协程取消、标记与审计不一致的问题已修 |
| BE-08 | 部分 | `test_runs_api.py`（A09 新增分页用例） | 否 | `/api/runs` 有 `degraded` 布尔；A09 起混合 seq/无 seq 的分页必然能读到终态（无可信游标时整页返回而不截断）。**未做**：retention 调度、abandoned 恢复、真实访问归属/权限体系 |

### RAG

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| RAG-01 | 是（本轮换入口） | `test_knowledge_pipeline.py`、`test_run_context_and_chat.py` | 否 | `/api/chat` 与 `/api/chat_stream` 走 `knowledge_service.answer_question`（retrieve→gate→answer），不再走旧 `RagAgentService` 的工具循环 |
| RAG-02 | 是 | `test_retrieval_contract.py` | 否 | `retrieval`/`content`/`done` 事件；前端 `sse.ts`+`view.ts` 消费 |
| RAG-03 | 是（A01/A02 收口） | `test_knowledge_api.py`、`test_acceptance_boundaries.py` | 否 | 初始化只在成功后置 `_initialized`；**A01** 检索只返回 active revision 分片（`KnowledgeStore.active_revisions()` 一致快照 + Milvus `_revision in [...]` 预过滤 + 后置核对）；**A02** 批量写入失败按全部预生成 chunk_ids 回滚，清理失败记 `cleanup_failed`/`pending_cleanup` 并可 retry |
| RAG-04 | 是（A03 收紧并接线） | `test_embedding_descriptor.py`、`test_acceptance_boundaries.py` | **否** | 建库 metric 取自描述符；初始化读真实 `metric_type` 比对；缺描述符而 collection 非空 → `invalid_index`。**A03**：`strict_local=True` 不再受远端 provider 影响；本地模式的 `retrieve_knowledge` 注入独立 `LocalOnlyEmbeddings` 检索器（不复用全局单例、不改全局配置），远端工厂调用为 0。真实 Milvus 未验证 |
| RAG-05 | 是 | `test_chunking.py`、`test_token_chunking.py` | 实测语料 | 超长单行代码按 token 二分硬切（实测 951 个 chunk，**0 个超预算**） |
| RAG-06 | 是（本轮修复） | 单测 | **否** | 删掉不存在的 `similarity_search_with_score` 回退；分数按 metric 解释（COSINE/IP 相似度、L2 转距离），metric 未知时全部记 0 |
| RAG-07 | 是（A08 补恢复） | `test_knowledge_api.py`、`test_acceptance_boundaries.py` | 否 | 有界后台 worker、job 状态机、retry、重启把残留 running 标 failed；**A08** 起 `VectorStoreManager` 区分 transient/permanent：transient 在锁内有限退避重试并随依赖恢复，`invalid_index` 永久拒绝 |
| RAG-08 | 是 | `test_knowledge_api.py` | 否 | `document_id` 由规范化 source_uri 派生（`doc-<12hex>`），同名不同目录不复用 |
| RAG-09 | 是（本轮重写） | `test_run_context_and_chat.py` | 否 | 新增 `app/services/chat_history.py`：JSON 原子写、TTL、条数与字符裁剪、session_id 清洗；不再读 checkpoint 内部结构 |
| RAG-10 | 部分 | `test_run_context_and_chat.py`、`test_run_lifecycle_wiring.py` | 否 | 诊断白名单里有 `retrieve_knowledge`（`kind=knowledge`，永不进“当前观测”）；追问 run_id 校验同会话归属。**缺**：知识片段未逐条挂到具体观测；多轮省略追问仍只靠受控运行摘要，未接入有界对话上下文 |

### OPS

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| OPS-01 | 是（A12 补 mcp 项） | `test_health_capabilities.py` | **否** | entrypoint 不再 `sleep` 当就绪：`docker/mcp-healthcheck.py` 逐端口探测 + 监督循环；`/health` capabilities **五类**（local/cloud/**mcp**/rag/tracing），Milvus 故障只降级 rag；`mcp` 只报本进程真实加载过的工具缓存，不假装实时探活 |
| OPS-02 | 是 | — | **否** | 多阶段 Dockerfile：`npm ci` 不回退、`uv sync --frozen --no-dev`。Docker daemon 本轮可用（29.8.2）但**未 build/up**，镜像与恢复仍无实测 |
| OPS-03 | 是（A10 贯通面板） | `test_eval_scoring.py`、`test_runs_api.py` | **否** | `scripts/run_eval.py` 输出 `{manifest, rows}`；A10 起 `/api/eval/results` 兼容旧裸列表与新对象并返回 `manifest`，前端面板展示运行条件与缺失字段。`scripts/eval_retrieval.py` 的离线 dense 基线（阈值在 14 条 dev 上拟合，**不是盲测结论**） |
| A13 冷启动策略 | 是（本轮统一） | — | **否** | `docker-compose.yml` 的 app 依赖 Milvus 改为 `service_started`（软依赖），与 entrypoint “诊断先起、RAG 后恢复”一致；`docker compose config --quiet` 通过。**未验证**：镜像构建、信号传播、卷重建恢复、真实 Milvus 入库 |

## 4. 本轮新增（不属于原始编号）

| 模块 | 说明 | 证据 |
|---|---|---|
| `app/core/env_guard.py` | 阻断第三方库隐式 `load_dotenv` | `app/__init__.py` 首行安装 |
| `app/core/run_context.py` | 追问用的受控运行摘要（同会话校验） | `test_run_context_and_chat.py` |
| `app/services/chat_history.py` | 有界、有 TTL、可持久化的问答历史 | 同上 |
| `app/tools/knowledge_retrieval_tool.py` | 诊断内受范围限制的知识检索 | `runtime_tools.KNOWLEDGE_TOOL` |
| `app/services/knowledge_store.py` + `app/api/knowledge.py` | M2 契约全量路由 | `test_knowledge_api.py` 14 项 |
| `scripts/fetch_corpus.py` | 白名单抓取原始快照（真实执行过） | `knowledge/manifests/import-2026-10-26.json` 11 条 |
| `scripts/normalize_corpus.py` | 快照→Markdown + notices（真实执行过） | `knowledge/manifests/normalized-2026-10-26.json` 11 条 |
| `scripts/eval_retrieval.py` + `eval/rag/dev.jsonl` | 离线检索基线 | `eval/rag/results/offline-*.json` |
| `docker/mcp-healthcheck.py` | 端口就绪探针 | entrypoint 第 3 步 |

## 5. A01–A13 反例修复对照（2026-10-27）

| 编号 | 期望行为 | 已实现 | 测试替身验证 | 真实依赖验证 |
|---|---|---|---|---|
| A01 | 检索只返回 active revision；staged/failed/superseded 不进候选 | 是 | `test_acceptance_boundaries.py`（4 条，含并发一致快照） | 否 |
| A02 | 失败按全部预生成 ID 回滚；清理失败可记录/重试；旧 active 保留 | 是 | 同上（3 条；含“先写一行再抛错”fake） | 否 |
| A03 | strict_local 真正约束 provider；远端 factory/HTTP 次数为 0 | 是（含接线） | 同上 + `test_local_knowledge_tool_uses_local_only_embeddings_and_no_remote_calls` | 否 |
| A04 | 服务端候选 ID、保留同名多实例、点击绑定具体实例 | 是 | `test_acceptance_targets_budget_paging.py`、`view.test.ts` | 否（未在真实浏览器点过） |
| A05 | 补全只用 schema 接受字段；合法 topic 成功、越界拒绝 | 是 | 同上 | 否 |
| A06 | 按 metric/value/unit/target/time 核对；三反例降级、正确对照保留 | 是（含真实 payload） | `test_claim_boundaries.py`（45 条，含 `data_points[].unit`、无单位按指标名推断、duration 读数 vs 查询窗口） | 否 |
| A07 | 日志参数收敛 + 证据字节预算真实生效 | 是 | 同上 | 否 |
| A08 | transient 锁内有限退避恢复；permanent 不重试；并发不重复初始化 | 是 | `test_acceptance_boundaries.py`（3 条） | 否 |
| A09 | 混合 seq/无 seq 的分页必定读到终态（含前端续页） | 是（前后端） | `test_runs_api.py` + `web/src/view.test.ts`（nextRunCursor/isStuckPage/appendRunEvents） | 否 |
| A10 | 评测列表兼容旧 list 与 `{manifest, rows}`，返回 manifest 并贯通面板 | 是 | `test_runs_api.py`、`view.test.ts` | 否 |
| A11 | 云端取消标记/清理/审计/后续工具禁止调用一致 | 是 | `test_cloud_multi.py`（34 项相关）+ RAG/运行时探针 | 否（真实远端任务是否终止未测） |
| A12 | 未配置模式刷新、FE-09、MCP/模型可用性反馈 | 部分 | `view.test.ts`、`test_health_capabilities.py` | **否**：真实浏览器旅程/读屏器未重跑 |
| A13 | 统一 Compose 冷启动依赖策略，补真实镜像/恢复验证 | 部分 | `docker compose config --quiet` | **否**：未 build/up，无镜像与恢复证据 |
| A13(retention/abandoned) | 过期 trace 清理 + 无终态静默运行标 abandoned | 是 | `test_runs_api.py`（+5） | 否 |

## 6. 三维度边界（已实现 / 测试替身验证 / 真实依赖验证）

- **已实现**：代码在生产路径被调用（本表所有 `是/部分` 项）。
- **测试替身验证**：用真实 service/store/runtime 代码 + 仅替换外部 SDK/网络/Milvus I/O 的自动化测试，本轮全部实测通过。
- **真实依赖验证**：真实 Milvus、真实云 API、真实浏览器、真实镜像。**本轮仅修复前已有的有限记录**，A01–A13 的修复**没有一条**进入这一等级。

## 7. 未完成清单与原因

1. **真实 Milvus 入库与检索**：本机 19530 未启动。所有向量断言都建立在 fake store 或内存 cosine 上；
   collection 描述符、索引度量、`num_entities`、删除语义**都没有**真实端到端证据。
2. **真实云 API 诊断质量**：只跑过一次 cloud_multi，metrics/reviewer 两个角色因模型返回非 JSON 失败
   （BROWSER_CHECK D12）。报告结构正确但内容为空，`quality=insufficient`。修复方向是给 `_ask()`
   加“JSON 提取 + 一次重试”，本轮未做。
3. **Docker 镜像构建**：Docker Desktop 守护进程未运行，`docker build` 直接失败。Dockerfile 与
   entrypoint 只有静态检查（`bash -n` 通过）。
4. **11 页语料的许可人工核对**：11 份快照已抓取并规范化，但其中 SD-02、LK-01、RH-01 的
   `license_status=needs_review`，RH-02（文件系统管理）仍未抓取；在人工核对前不得对外分发。
5. **盲测集**：`eval/rag/dev.jsonl` 只有 14 条且阈值在该集上拟合，`offline-*.json` 里的
   no_answer_precision=1.0 **不是盲测结论**。
6. **规范化产物的行尾一致性（已修）**：`scripts/normalize_corpus.py` 原先用默认 newline
   写文件，Windows 落成 CRLF，而 manifest 的 `normalized_sha256` 按 LF 计算，直接对磁盘
   字节校验会失败。现固定 LF，重新生成后 **11/11 条目 sha256 与磁盘字节一致**。
7. **FE-09 剩余项**：`aria-live` 播报与 ≤1180px inspector 替代入口**已实现**（`view.test.ts` 有覆盖）；**仍未做**：真实中文 IME 输入、读屏器播报、以及当前构建的浏览器任务旅程复跑。旧的 BROWSER_CHECK 使用早期构建，其失败/成功截图都不能直接代表当前代码。
8. **BE-08 剩余项**：compose 已有四个命名卷（uploads/logs/eval-results/data）；trace 的 **retention 调度、abandoned 恢复、真实用户/资源归属与权限体系**仍未实现。共享会话 ID 校验不等于完整权限体系。
9. **RAG-10 交叉引用**：知识片段只出现在报告“知识依据”一节，没有逐条挂到具体观测；`api/chat.py` 的历史仅用于展示，**不参与回答生成**，多轮省略追问需要另接有界上下文。
10. **parent 回填**：`document_splitter_service` 已在 child 分片上携带 `_parent_id`/`_parent_text`，但检索与引用仍按 child 呈现，完整 parent 回填未闭环。
11. **DK-03 正文质量**：文件与哈希齐全，但正文提取偏短，需人工核对提取质量（不是哈希校验能替代的）。
12. **独立盲测**：`eval/rag/dev.jsonl` 仅 14 条且阈值在该集上拟合，**没有独立 blind 集**，满分不能作为泛化结论。

---

# G5 增量状态（2026-10-28，第四轮）

本节只记**本轮新增/改变**的项；上面 1–7 节的历史状态保留原样（包括已过时的数字与缺口），
不覆盖、不删除。完整结果见 [G5_RESULTS_2026-10-28.md](G5_RESULTS_2026-10-28.md)。

## G5.1 结论摘要（本轮实测）

| 维度 | 状态 |
|---|---|
| 后端测试 | **809 passed, 1 skipped, 1 warning**（164.42s） |
| 前端 | `npm test` **70 passed**；`tsc -b` / `oxlint` / `vite build` 通过 |
| 真实 Milvus | v2.5.10 `biz`，COSINE 384；检索前过滤的复合 expr、校准、生成评测都在真实 collection 上跑过 |
| 真实 LLM | `DeepSeek-V4.1-Flash-1017`，生成评测 12 条 0 失败 |
| 真实浏览器 | **容器内后端 + 真实 Milvus**：双实例点选、引用卡片、SSE 续读、刷新恢复、失败重试 |
| 镜像 | **`sha256:d93d673ee7d2…`，10.7 GB**，当前工作区重建，compose healthy |

## G5.2 新增能力

| 项 | 已实现 | 已单测 | 已验证 |
|---|---|---|---|
| calibration 集（80 条，blind 冻结） | `eval/rag/calibrate-dev2.jsonl` | `tests/test_calibration_eval.py`（34 条，含 blind sha256 锁定） | 真实 Milvus 检索 + 对照 |
| 分域 gate 校准 | `scripts/eval_calibration.py`，产物 `gate_profile` | 桶回退/阈值选择纯函数 | 真实 Milvus（80 行，0 失败） |
| 生成层三层指标 | `scripts/eval_generation.py` | `--dry-run` 组装校验 | 真实 LLM 12 条 |
| 检索前 OS/产品/版本/有效期/ACL 过滤 | `visible_documents()` + 双 `in` expr + 后置核对 | `tests/test_knowledge_filters.py`（24 条） | 真实 Milvus 复合 expr |
| 单租户/多租户开关 | `KNOWLEDGE_TENANT_MODE` | `tests/test_knowledge_filters.py` | 否（配置语义，单测覆盖） |
| 聊天事件落盘与按 seq 续读 | `app/services/chat_events.py` + `GET /api/chat/events/{run_id}` | `tests/test_chat_events.py`（9）+ `test_run_context_and_chat.py`（3） | 真实浏览器 + 容器内 HTTP |
| 前端断流续读 / tab 恢复 / 引用卡片 / 目标绑定可见 | `web/src/chat.ts`、`ChatPanel.tsx`、`state.ts`、`view.ts`、`App.tsx` | `npm test` 70 条 | 真实浏览器（截图 5 张） |
| 跨进程 trace 锁 | `app/observability.py`（msvcrt/flock，有界等待） | `tests/test_soak_concurrency.py`（真实 4 子进程） | Windows 实测；POSIX 仅静态审查 |
| retry 幂等 / retention 饥饿修复 | `knowledge_store.retry_job`、`runs.purge_expired_traces` | `test_soak_concurrency.py`、`test_trace_retention.py` | 真实文件系统 |
| MCP 同名双实例目录 | `AIOPS_SERVICES_FILE` + `services.staging.json` | 间接（`test_server_env_isolation.py` 未追加断言） | 真实 3 个 MCP 进程 + 浏览器 |
| CI/Compose 防陈旧镜像 | `ci.yml` docker job、`make docker-build-fresh`、compose 注释 | YAML/`compose config` 校验 | **否**（未在 GitHub 上跑过） |
| fastmcp 2.14 → 3.4.0 | `uv.lock` + `uv sync --frozen` | 既有 MCP server 测试 | 三个 server 真实注册工具 |

## G5.3 本轮修掉的真实缺陷

1. **`.venv-train/` 打进镜像**：`COPY . .` 层 5.41 GB → 22.9 MB（镜像 18.9 → 10.7 GB）。
2. **fastmcp 2.14.x 的 `@mcp.tool()` 返回不可调用对象**：`uv sync --frozen` 后
   `search_service_logs` 等 9 条测试 `TypeError`；锁到 fastmcp 3.4.0 解决，23 处调用点不用改。
3. **并发 retry 产生多个新 job**：8 线程实测 8 个 → 1 个。
4. **retention 扫描窗口饥饿**：250 新鲜 + 50 过期连续 3 轮 `deleted=0` → 过期全删。
5. **chat 事件不落盘**：断流后无法续读；现按 `run_id` 落盘 + `after_seq` 续读。
6. **“模型自己拒答”被记成误答**：单列 `model_refusals_in_answered`（本轮实测 0 条命中，
   口径偏严，未为了让数字好看而改判）。

## G5.4 仍然未实现/未验证（与 G5_RESULTS §8 一致）

- 诊断路径（`app/agent/aiops/*`）的知识检索仍未带 principal。
- CUDA torch 未替换（配方已实测可行，未改 `uv.lock`）；镜像不含 HF 权重。
- PyMilvus ORM → MilvusClient 未迁移；`pymilvus>=2.3.5` 下限未钉住。
- SSH 只读联调、真实中文 IME/读屏器未执行。
- `by_category` 只能诊断；`by_product` 负样本来自全局；生成评测只 12/80 条。
- CI docker job、POSIX 文件锁分支未在真实环境跑过。

---

# G6 增量状态（2026-10-28，第五轮）

本轮只做权限与聊天流一致性收尾，未新增泛化防御层或统一兜底框架。
完整数字、分母与不能据此下的结论见 `G6_RESULTS_2026-10-28.md`。

## G6.1 交付项

| 项 | 实现位置 | 单测 | 真实依赖验证 |
|---|---|---|---|
| 知识库作业的 principal 边界 | `knowledge_store._owner_acl_visible` + `list_documents/list_jobs(principal_id=)` + `job_document_id/document_visible_to`；`api/knowledge.py` 三个作业路由解析 key、单条不可见统一 404、retry 先鉴权 | `tests/test_knowledge_jobs_acl.py`（6） | 容器内另一把 key：列表空、详情 404、retry 404 且作业数不变 |
| 隐藏文档不消耗分页 limit | 过滤前置于 `status`/`cursor`/`limit`，`next_cursor` 取过滤后页尾 | 同上（翻页恰好不重不漏） | 同上（真实 store） |
| 诊断 run 与聊天流 run 拆分 | `api/chat.py` 每次请求生成 `chat_run_id`；`chat_events` 落盘带 `session_id`、续读按 session 校验 | `test_run_context_and_chat.py`、`test_chat_events.py` | 容器内两次追问 → 两个 run、两个文件 seq 各从 1 连续 |
| 聊天流生命周期 | lifespan 启动清理；前端网络异常走同一有界续读；停止清 pending；单活动标签页写进界面与 README | `web/src/chat.test.ts` | 容器内 403 跨会话续读 + 原文件不变 |
| 诊断路径 RAG 可见性 | `need_principal_scope` + `knowledge_wiring`/`runtime_tools`/`local_runtime`/`cloud_multi`/`run_diagnostic` 的 owner 传播 | `tests/test_diagnosis_knowledge_principal.py`（8） | 否（fake 向量库 + 真实 store；A03 本地 embedding 约束不变） |
| 评测快照一致性 | `eval_generation._verify_profile_consistency` + `eval_calibration._store_evidence` | `test_calibration_eval.py` | 真实 G5 产物：0 差异 |
| 检索器不再吞 TypeError | `knowledge_service._supports_expr` 前置签名判断 | 同上 | 否（真实 langchain_milvus 签名已核） |
| 口径修正 | README 拆 liveness/readiness；"冒烟"→"抽样生成评测"；kind 固定 | — | — |

## G6.2 本轮修掉的真实缺陷

1. **list_documents 先分页后过滤**：隐藏文档吃掉 `limit`，`next_cursor` 跳过可见项。
2. **list_jobs / get_job / retry_job 完全没有 principal 边界**：知道 job_id 就能读状态、
   错误信息，并对别人的文档发起 retry（会真的创建新作业）。
3. **同一诊断 run 追问两次丢事件**：请求 `run_id` 被当事件文件名，`seq` 都从 1，
   续读按 `event_id` 去重把第二次静默丢弃。
4. **续读只校验会话归属，不校验事件归属**：拿到已知 chat run 的其它 session 可读到内容。
5. **`chat_events.purge_expired()` 没有调用点**：所谓 24h TTL 从未生效。
6. **网络异常不续读**：只有 EOF 分支续读，网络层报错直接判失败。
7. **"停止"被当成待恢复流**：刷新后会把主动取消的流当成断线重连。
8. **诊断路径知识检索不带 principal**：multi 下能命中无权文档。
9. **`except TypeError` 无条件重查**：真实查询的 TypeError 被吞成"不过滤再查一次"，
   静默扩大候选范围（正是 G6 明令禁止的降级）。

## G6.3 仍未做（与 G6_RESULTS §9 一致）

- 旧知识工具路径（`app/tools/knowledge_tool.py`、`rag_agent_service.py`）仍未接 principal。
- 多 key 账号体系不存在（单生效 API Key）；多租户只在测试内以不同 `principal_id` 验证。
- 真实 TCP 断线（后端在链路上被切断）未验证；本轮只验证客户端截断/网络异常后的续读。
- SSH 只读 staging、CUDA torch、PyMilvus ORM、GitHub CI docker job、Linux flock、
  真实 IME/读屏器：均未验证，仍按独立任务登记。
- 本机 apt 源不稳（502），镜像用只改 apt 步骤的本地 Dockerfile 变体构建；
  仓库 `Dockerfile` 未改。

## G6.4 对抗复核后追加修复（同一轮）

六维复核 35 条原始发现 → 10 条经反驳确认，其中 5 条是本轮新代码引入或放大的越权，
已在同一轮修掉并各自有测试：

1. **读写判定混用**：`_enforce_document` 只看 owner 侧表，"有 ACL、无 owner"的文档对
   陌生 principal 可 activate/delete。→ 拆成 `document_visible_to`（读）与
   `document_writable_by`（写，owner 优先）。
2. **retry 越权回滚**：只校验"可见"，只读 ACL 成员能把 active revision 回滚。→ 要求写权限。
3. **同名覆盖**：`document_id` 由 source_uri 派生，B 用同名上传可替换 A 的检索内容。→ 提交前 409。
4. **删除后作业复活**：文档记录消失后作业按"无 owner"判定，删除把 404 变 200。→ 一律不可见。
5. **claim 顺序**：提交后才 claim，失败仍 202，上传者看不到自己的文档。→ claim 前移。

另修：`need_principal_scope` 简化为"有身份就启用"（dev 也走同一可见性规则）；
`store_document_ids` 参与 profile 一致性校验；`retrieve_knowledge` 加进 logs 专科目录
（supervisor 提示词一直承诺知识库能力，目录里却没有，模型照做会被判未授权并让整次运行失败）。

---

# G7 增量状态（2026-10-29，第六轮）

身份：base `ed13769` / code `de9d36e` / HEAD `b27f9f4` / 本轮提交 `024aad3`。
完整数字与分母见 `G7_RESULTS_2026-10-29.md`。

## G7.1 交付项

| 项 | 实现位置 | 测试 | 真实依赖验证 |
|---|---|---|---|
| metadata 前移到 claim 之前 | `api/knowledge.py` 上传/导入两条路径 | `tests/test_knowledge_upload_consistency.py`（11） | 容器内实测 400 后 ACL 侧表 0 条 doc owner |
| 提交失败回滚本次新建 owner | `_claim_uploaded_document` 返回 `owner_before` + `_release_uncommitted_owner` | 同上 | 容器内 2 个 owner 全部对应真实文档，无孤儿 |
| 条件删除归属 | `principal.release_owner_if` | 同上（含并发语义） | — |
| 读取谓词 owner OR ACL | `knowledge_store._owner_acl_visible` + 四处调用点 | 同上 + 既有 ACL 套件 | 容器内 seal 失败仍可列出/查作业/检索 |
| `acl_sealed=false` 如实标注 | `_seal_uploaded_document` 返回 bool | 同上 | 容器内上传响应核对 |
| 快照空集合判 mismatch | `eval_generation._verify_profile_consistency` | `test_calibration_eval.py`（+9） | 真实 profile 0 mismatch；store 清空 4 条硬差异 |
| `consistency` 不再被覆写 | `eval_generation.main` if/elif/else | 同上 | — |
| 损坏 store 明确失败 | `eval_calibration._store_evidence` + `main` | 同上 | 实测 main 返回 1 并打印原因 |
| 取样命名统一 sampled | `eval_blind.py`、`eval_generation.py` 注释、README | `tests/test_eval_naming.py`（新） | — |
| 镜像 provenance | `Dockerfile`（runtime ARG/LABEL）、`ci.yml` 校验步骤、`Makefile` | YAML 解析 + 镜像 label 内省 | **标准 Dockerfile 构建成功**，revision 与 commit 一致 |
| ignore 规则补漏 | `.gitignore` / `.dockerignore` 的 `.env*`、`.g7*`、`tmp/` | 构建上下文仿真 | 逐项 EXCLUDED 核对 |

## G7.2 本轮修掉的真实缺陷

见 `VALIDATION_RESULTS.md` §V14.2（9 条）。要点：幽灵 owner、失败上传私有化共享文档、
提交失败不回滚、读谓词忽略 owner（含 4 处调用点抑制）、ACL seal 静默失败、
快照空集合短路、`consistency` 被覆写、`UnboundLocalError`、smoke 命名残留。

## G7.3 仍未做（与 G7_RESULTS §6 一致）

- SSH 只读 staging、真实 TCP 断线、CUDA torch 替换、PyMilvus ORM 迁移、
  GitHub CI docker job 实跑、Linux flock、真实 IME/读屏器：均未验证。
- **`--pull` 未执行**（registry 直连超时）；本轮是 `--no-cache` + 本机已有基础镜像。
- attu 未启动（本机 8000 被无关进程占用）。
- **双模式对比（P2）未执行**：需要一次真实的本地 QLoRA + cloud_multi 同批任务运行。
- 单 API Key：多租户验证均以测试内不同 `principal_id` 构造。
- 聊天事件跨进程追加仍只适合单 app 进程。

---

# G8 增量状态（2026-10-29）

## G8.1 本轮改动与验证手段

| 项 | 改动文件 | 验证手段 | 结果 |
|---|---|---|---|
| 构建上下文凭据排除 | `.dockerignore`（敏感模式全部 `**/` 前缀 + 补 ppk/id_rsa/p12/pfx） | `tests/test_docker_context_hygiene.py`（24 条）+ 探针镜像内 `ls -R` | 修复前该测试失败 6 条，修复后全过；新镜像内已无私钥、无项目 `.pyc` |
| 云端 schema 回显 | `app/agent/aiops/cloud_multi.py`（`_looks_like_schema_echo` + 专门重试指令） | `tests/test_json_extraction.py` 新增 3 条 + 真实模型连跑 8 次 | 5/8 → **8/8** |
| compose 采用新镜像 | `docker-compose.yml`（`image: ${APP_IMAGE:-...}`） | `docker compose config --images` + 容器 `.Image` 内省 | 容器实际运行 `sha256:4791e2f4…` |
| 评测 gold 可执行性 | `eval/scenarios/*.yaml`（4 个）、`eval/README.md` | `scripts/validate_eval.py` + `bind_tool_args` 实测 | 原 gold 会在唯一目标上被拒绝，已改为 `search_service_logs` |
| 双模式基准 | `scripts/mode_benchmark.py`（新）、`eval/mode_benchmark/tasks.jsonl`（新） | 两种模式各跑 18 条 | 各 18/18 complete |
| 模式取舍可见 | `app/api/aiops.py`、`web/src/api.ts`、`App.tsx`、`styles.css` | `tests/test_execution_modes_api.py`（形状锁定）+ 前端 75 passed | 401/401/200 且新字段生效 |
| 本地失败文案 | `app/agent/aiops/local_runtime.py`、`web/src/view.ts` | `web/src/view.test.ts` 新增 1 条 | 只对 local 模式显示"已停止" |

## G8.2 本轮修掉的 P0

1. `.dockerignore` 裸 glob 不跨目录 → 未加密 RSA 私钥真实存在于镜像层
   `servers/AgentTest.pem`（755 / 1674 B，sha256 与宿主一致）。
2. cloud_multi Supervisor 把 JSON Schema 文档当答案抄回，约 1/3 运行直接失败。
3. `app:` 无 `image:` → `up --force-recreate` 复用旧镜像，验收跑的不是新构建。
4. 4/5 评测场景把 `search_log` 当 gold，而唯一目标的 topic 为空会被正确拒绝。

## G8.3 仍未做

与 `G8_RESULTS_2026-10-29.md` §8 一致（13 项）。要点：SSH staging、真实 TCP 断线、
CUDA torch 替换、PyMilvus ORM、CI docker job 实跑、Linux flock、IME/读屏器、
`--pull`、attu 启动、多副本、真实多用户、fp16 vs 4-bit 同题对照、
`search_log` 对真实 topic 的成功路径。

**P2 双模式对比本轮已完成**（G7 遗留项）：18 条固定任务、两条链路各 18/18 完成。
