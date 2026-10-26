# 实施状态（逐项）

日期：2026-10-26。对象：**当前工作区**（HEAD `fca7f1c` + 未提交改动），不是某个干净提交。
原始问题定义见 [ENTERPRISE_REVIEW.md](ENTERPRISE_REVIEW.md)，上轮复核见
[COMPLETION_AUDIT_2026-10-26.md](COMPLETION_AUDIT_2026-10-26.md)。

三列含义严格区分，不允许互相替代：

- **已实现**：代码在当前工作区存在且被生产路径调用（不是只被测试调用）。
- **已单测**：有针对该行为的自动化测试，且本轮真实执行通过。
- **已验证**：在真实依赖（真实 Milvus / 真实云 API / 真实浏览器 / 真实镜像）上跑过。
  本轮只有浏览器一项进入了这个等级，且仅限 Milvus 不可用时的降级路径。

## 1. 结论摘要

| 维度 | 状态 |
|---|---|
| 后端测试 | 528 passed, 1 skipped（`-o addopts=""`，本轮最后一次 2026-10-26） |
| 前端 | `npm run build` / `npm run lint`（仅 0 warning）/ `npm test` 44 passed |
| 静态检查 | `ruff --select F821,F811,F841` 全通过；`bash -n docker-entrypoint.sh` 通过 |
| 真实浏览器 | 已跑（见 [BROWSER_CHECK.md](../BROWSER_CHECK.md)），只覆盖 Milvus 不可用场景 |
| 真实 Milvus | **未验证**（本机 19530 未启动，全部用 fake store / 内存 cosine） |
| 真实云 API 诊断 | 部分（一次 cloud_multi 真实运行，模型返回非 JSON 导致角色失败，见 BROWSER_CHECK D12） |
| Docker 镜像 | **未验证**（Docker Desktop 守护进程未运行，只有静态检查） |

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
| FE-03 | 部分 | 无 | 浏览器步骤 1 | 模式卡片来自 `/api/execution-modes`，刷新按钮在 `modeError` 或未配置时出现；**仍没有**对模型端点的真实连通性探测 |
| FE-04 | 是 | `web/src/sse.test.ts`、`chat.test.ts` | 浏览器步骤 5/7 | 诊断与聊天都走 `sse.ts` 的 `SseBuffer`+`applyBlock`+`diagnose`；EOF 无终态判 `truncated` |
| FE-05 | 部分 | `chat.test.ts` | 浏览器步骤 9 | ChatPanel 有请求版本隔离、卸载 abort、服务端历史回填；**TAB 关闭后重开**的历史一致性未验证 |
| FE-06 | 是（含本轮修复） | `test_knowledge_api.py`（14 项） | 浏览器步骤 2/3/11 | 文档/作业/版本/撤销面板；本轮修掉“失败 revision 可被 activate” |
| FE-07 | 是 | `view.test.ts` + 浏览器 | 浏览器步骤 7 | 引用卡片渲染 citations（title/locator/source_uri/snippet/score）；检索状态五态独立标签 |
| FE-08 | 是 | `test_runs_api.py` + `test_run_lifecycle_wiring.py` | 否 | 运行列表与评测分开加载；`?run=` 深链；`after_seq` 续读改为按位置（无 seq 的终态不再丢） |
| FE-09 | **否** | — | 浏览器步骤 9/10 记录为失败 | IME 组合态本轮补了 `isComposing`/`keyCode===229` 判断（`ChatPanel.tsx`）；**仍缺**：`aria-live` 播报、≤1180px 证据 inspector 替代入口 |
| target_required 候选选择 | 是 | `test_target_required_flow.py`、`view.test.ts` | 浏览器步骤 8 | 候选卡片点击后带 `target_choice` 重发一次；回放时卡片因未选模式而禁用（D5） |

### 后端

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| BE-01 | 是（本轮接通渲染） | `test_claims.py`、`test_cloud_multi_quality.py` | 否 | `quality.render_report` 现在同时渲染 `claim` 与服务端 `rendered`；数值正则改为只排除 ASCII 前缀（中文紧邻数值可识别）；新增**指标族**校验（CPU 证据不能支撑“内存 12%”） |
| BE-02 | 是（本轮修复） | `test_cls_wrapper_status.py`（9 项） | 否 | CLS 包装层透传 ok/status/source/demo/partial，AccessDenied 不再变成“0 条成功” |
| BE-03 | 是 | `test_target_resolution.py`（本轮新增 3 条同名多实例用例） | 浏览器步骤 8 | 目标目录按**完整目标**去重（服务+实例+地域+主题+SSH），同名多实例进入 `target_required` |
| BE-04 | 是 | `test_runtime_target_binding.py` | 否 | `get_topic_info_by_name` 移出发现工具；`TOPIC_KEYS` 在目标未声明 topics 时一律拒绝 |
| BE-05 | 是 | `test_cloud_multi_quality.py`、`test_run_lifecycle_wiring.py` | 浏览器步骤 6 | 本地模式 `complete` 现在带 lifecycle/quality/status/gaps/tools；云端 `agent_result` 落 observations/hypotheses/gaps（追问依赖它） |
| BE-06 | 是（本轮接入运行链路） | `test_run_lifecycle_wiring.py::test_budget_is_shared_with_runtime` | 否 | `AdmissionController` 在 `run_diagnostic` 用 `async with` 包裹；`RequestBudget` 从 runtime 传到两个运行器，工具/LLM/prompt 都 `reserve`；超支记成失败证据 |
| BE-07 | 是（本轮接地） | 同上 | 否 | `deadline` 从 `run_diagnostic` 传到 `RunTools`（每次工具调用取 `min(budget, remaining)`）；流提前关闭时 `RunTools.cancel()` 并落 `run_cancelled` trace |
| BE-08 | 部分 | `test_runs_api.py` | 否 | `/api/runs` 增 `degraded` 布尔；续读游标用本页最大 seq。**未做**：持久卷、retention、abandoned 恢复、访问归属 |

### RAG

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| RAG-01 | 是（本轮换入口） | `test_knowledge_pipeline.py`、`test_run_context_and_chat.py` | 否 | `/api/chat` 与 `/api/chat_stream` 走 `knowledge_service.answer_question`（retrieve→gate→answer），不再走旧 `RagAgentService` 的工具循环 |
| RAG-02 | 是 | `test_retrieval_contract.py` | 否 | `retrieval`/`content`/`done` 事件；前端 `sse.ts`+`view.ts` 消费 |
| RAG-03 | 是（本轮修复） | 单测覆盖 fake store | 否 | `VectorStoreManager` 只在成功后才置 `_initialized`；失败保留异常并继续抛；不再把查询异常吞成 `[]` |
| RAG-04 | 是（本轮接通 I/O 边界） | `test_embedding_descriptor.py` | **否** | 建库 metric 取自 `current_descriptor().distance`；初始化时读 collection 真实 `metric_type` 并与描述符比对；缺描述符而 collection 非空 → `invalid_index`。真实 Milvus 未验证 |
| RAG-05 | 是 | `test_chunking.py`、`test_token_chunking.py` | 实测语料 | 超长单行代码按 token 二分硬切（实测 951 个 chunk，**0 个超预算**） |
| RAG-06 | 是（本轮修复） | 单测 | **否** | 删掉不存在的 `similarity_search_with_score` 回退；分数按 metric 解释（COSINE/IP 相似度、L2 转距离），metric 未知时全部记 0 |
| RAG-07 | 是 | `test_knowledge_api.py` | 否 | 有界后台 worker（固定线程池）、job 状态机、retry、重启把残留 running 标 failed |
| RAG-08 | 是 | `test_knowledge_api.py` | 否 | `document_id` 由规范化 source_uri 派生（`doc-<12hex>`），同名不同目录不复用 |
| RAG-09 | 是（本轮重写） | `test_run_context_and_chat.py` | 否 | 新增 `app/services/chat_history.py`：JSON 原子写、TTL、条数与字符裁剪、session_id 清洗；不再读 checkpoint 内部结构 |
| RAG-10 | 部分 | `test_run_context_and_chat.py`、`test_run_lifecycle_wiring.py` | 否 | 诊断白名单里加了 `retrieve_knowledge`（`kind=knowledge`，永不进“当前观测”）；追问 run_id 校验同会话归属。**缺**：知识片段未在报告里与观测交叉引用 |

### OPS

| ID | 已实现 | 已单测 | 已验证 | 说明与缺口 |
|---|---|---|---|---|
| OPS-01 | 是 | `test_health_capabilities.py` | **否** | entrypoint 不再 `sleep` 当就绪：`docker/mcp-healthcheck.py` 逐端口探测 + 监督循环；`/health` capabilities 四类（local/cloud/rag/tracing），Milvus 故障只降级 rag |
| OPS-02 | 是 | — | **否** | 多阶段 Dockerfile：`npm ci` 不回退、`uv sync --frozen --no-dev`。**镜像从未构建过**（Docker Desktop 未运行） |
| OPS-03 | 是（本轮加运行条件） | `test_eval_scoring.py` | **否** | `scripts/run_eval.py` 输出 manifest（模式/模型/adapter 形状/场景列表/commit/dirty）；`scripts/eval_retrieval.py` 给出离线 dense 基线（recall@3=1.0, MRR=1.0, 3 条无答案全拒答，阈值在 dev 集上拟合） |

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

## 5. 未完成清单与原因

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
7. **FE-09 剩余项**：`aria-live` 播报、窄屏 inspector 替代入口、真实 IME 测试（本轮只用合成事件）。
8. **BE-08 剩余项**：应用持久卷已有 compose 卷，但 trace 的 retention/abandoned 恢复/访问归属未实现。
9. **RAG-10 交叉引用**：知识片段目前只出现在报告“知识依据”一节，没有逐条挂到具体观测上。
