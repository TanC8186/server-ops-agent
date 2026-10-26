# 实施状态（逐项）

日期：2026-10-27（第二轮整改）。对象：**当前工作区**，基线 `fca7f1c` + 两轮未提交实现
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
| 后端测试 | **577 passed, 1 skipped**（`-o addopts=""`，2026-10-27 本轮实测） |
| 前端 | `npm run build` 通过 / `npm run lint` 0 warning / `npm test` **48 passed** |
| 静态检查 | `ruff --select F821,F811,F841` 全通过；`docker compose config --quiet` 通过 |
| 三个离线验收探针 | 全部 `bug_reproduced=false`（2026-10-27 修复后复跑，见 §6） |
| 真实浏览器 | 旧记录见 [BROWSER_CHECK.md](../BROWSER_CHECK.md)；**当前构建的浏览器旅程未重跑**（A12 待办） |
| 真实 Milvus | **未验证**（本机 19530 未启动，全部用 fake store / 内存向量） |
| 真实云 API 诊断 | 部分（一次 cloud_multi 真实运行，模型返回非 JSON 导致角色失败，见 BROWSER_CHECK D12） |
| Docker 镜像 | **未验证**：Docker daemon 可用（29.8.2），但本轮未 build/up，无镜像与恢复验证 |

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
| FE-09 | 部分（A12 收窄） | `view.test.ts`（新增 4 条） | **否** | 已补：`aria-live` 播报区（`role=status`+`.sr-only`）、≤1180px 证据 inspector 替代入口（`aria-expanded` 切换按钮）、IME 组合态判断。**仍缺**：真实 IME/读屏器与当前构建的浏览器旅程实测 |
| target_required 候选选择 | 是（A04 修复） | `test_target_required_flow.py`、`view.test.ts`、`test_acceptance_targets_budget_paging.py` | 否 | 服务端签发稳定候选 `target_id`；前端按完整身份去重、点击后带 `target_candidate_id` 重发一次，服务端在当前目录重新校验并绑定到具体实例/地域 |

### 后端

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| BE-01 | 是（A06 加严） | `test_claims.py`、`test_cloud_multi_quality.py`、`test_claim_boundaries.py` | 否 | `quality.render_report` 同时渲染 `claim` 与服务端 `rendered`；A06 起按 **(数值, 单位)** 逐对核对被引用证据、并检测声明里的**别的目标**：`999ms`（同族数值不符）、`12%`（percent vs ms 单位冲突）、`12ms，svc-b…`（目标不一致）全部降为假设；查询窗口“最近 10 分钟”不误判 |
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
| RAG-04 | 是（A03 收紧） | `test_embedding_descriptor.py`、`test_acceptance_boundaries.py` | **否** | 建库 metric 取自描述符；初始化读真实 `metric_type` 比对；缺描述符而 collection 非空 → `invalid_index`。**A03**：`strict_local=True` 不再受远端 provider 影响，返回按需加载的本地 embedding，远端工厂调用为 0。真实 Milvus 未验证 |
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
| A03 | strict_local 真正约束 provider；远端 factory/HTTP 次数为 0 | 是 | 同上（2 条）+ RAG 探针 | 否 |
| A04 | 服务端候选 ID、保留同名多实例、点击绑定具体实例 | 是 | `test_acceptance_targets_budget_paging.py`、`view.test.ts` | 否（未在真实浏览器点过） |
| A05 | 补全只用 schema 接受字段；合法 topic 成功、越界拒绝 | 是 | 同上 | 否 |
| A06 | 按 metric/value/unit/target/time 核对；三反例降级、正确对照保留 | 是 | `test_claim_boundaries.py`（11 条） | 否 |
| A07 | 日志参数收敛 + 证据字节预算真实生效 | 是 | 同上 | 否 |
| A08 | transient 锁内有限退避恢复；permanent 不重试；并发不重复初始化 | 是 | `test_acceptance_boundaries.py`（3 条） | 否 |
| A09 | 混合 seq/无 seq 的分页必定读到终态 | 是 | `test_acceptance_targets_budget_paging.py` + `test_runs_api.py` | 否 |
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
