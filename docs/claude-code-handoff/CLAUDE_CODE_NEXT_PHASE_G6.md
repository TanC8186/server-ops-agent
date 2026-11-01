# G6 下一阶段任务书：权限与聊天流一致性收尾

日期：2026-10-28  
基线：G5 工作区（`ed13769` + 未提交改动）

## 先读约束

G5 已完成的真实 Milvus、真实 LLM、浏览器闭环、镜像瘦身、跨进程 trace 锁、校准集和前端能力不要重复实现。不要新增泛化防御层、统一兜底框架、无业务目标的重试/清洗逻辑，也不要新增或宣传“冒烟测试”。本轮只修下面列出的具体业务一致性问题，并用对应的直接用户流程或数据场景验证。

## P0：知识库作业权限闭环

文件：`app/api/knowledge.py`，必要时只调用现有 `app.core.principal` 和 `_enforce_document`。

1. `/api/knowledge/jobs`、`/api/knowledge/jobs/{job_id}`、`/api/knowledge/jobs/{job_id}/retry` 接收 `X-API-Key`，解析 principal。
2. `get_job` 与 `retry_job` 先取作业对应的 `document_id`，复用现有文档 owner/ACL 规则后再返回或重试；不得仅因为 job_id 存在就暴露 `source_uri`、错误信息或创建新作业。
3. `list_jobs` 只返回当前 principal 可见文档的作业。过滤必须发生在返回分页结果之前，不能让别人的作业消耗当前用户的 `limit`。
4. 同样修正 `list_documents` 的可见分页：隐藏文档不能消耗当前用户的 `limit`，不能因为先全局分页再过滤而出现空页或漏掉后续可见文档。
5. 保持单租户 `dev` 的现有行为；只补 multi/不同 principal 的真实资源边界。

直接验收：principal A 上传并制造一个 failed job；principal B 列表看不到、详情为 403/404、retry 不能创建新 job；A 可以正常查看并 retry。不要新增权限抽象。

## P0：按当前 Compose 重建并核对运行配置

当前运行容器仍是 `AUTH_MODE=dev`，而 compose 的默认容器配置是 `deployment`；同时当前 `docker compose ps -a` 中 app 仍在运行，但 etcd/MinIO/Milvus 已退出。`/health` 的顶层 healthy 只代表进程存活，当前能力状态已经明确显示 `rag=unavailable`。

1. 依据当前工作区重新构建并 `docker compose up -d --force-recreate app`，不要复用旧容器/旧 tag。
2. 用 `docker compose config` 和容器内非敏感配置字段核对 `AUTH_MODE`、Milvus 地址、MCP 地址；任何命令输出都不得包含密钥。
3. 让 deployment 模式下无 key 得到 401；配置 key 后再做一次受保护接口的直接访问。Milvus 依赖恢复后再记录 RAG readiness；不要把进程 liveness 写成全栈可用。

这项是部署状态校正，不是新增健康检查层，也不要求写独立冒烟脚本。

## P0：本轮验收中出现的凭据暴露处理

检查 Compose/容器环境时发现真实云模型、腾讯云和 SSH 凭据可能出现在命令输出中。不要把它们复制到报告、截图、测试产物或 Git。由凭据持有人轮换受影响的 key，并检查本轮日志、截图、未提交文件和历史记录是否含有明文；任务书和最终报告只记录“已轮换/待轮换”状态，不记录值。

## P0：拆开诊断上下文 run 与聊天流 run

文件：`app/api/chat.py`、`app/services/chat_events.py`，相关测试放在现有聊天路由测试文件。

`ChatRequest.run_id` 是“追问所引用的诊断 run”，不能再作为聊天事件文件名或 `event_id` 前缀。每次 `/api/chat_stream` 都生成独立的 `chat_run_id`，并把它传给 `knowledge_service.answer_question`；请求里的 `run_id` 只用于 `_load_context_async`。事件记录的 `session_id` 必须来自服务端当前请求。

聊天事件续读时，除已有 session owner 检查外，确认落盘事件的 `session_id` 与查询参数一致；不同 session 不得借助已知 chat run 读取或污染事件文件。

直接验收：同一诊断 run 连续追问两次，得到两个不同 chat run，两个文件的 seq 都从 1 连续；跨 session 传入已知 run_id 时，原文件内容不增加，续读返回拒绝或 404。不要通过“捕获所有异常后重试”实现。

## P1：聊天流生命周期的三个确定问题

文件：`app/services/chat_events.py`、`web/src/chat.ts`、`web/src/ChatPanel.tsx`。

1. `chat_events.purge_expired()` 当前没有调用点。接入已有生命周期/清理调度，或删掉“自动 TTL”表述并明确由运维任务调用；二者选一个，不增加后台线程。
2. `fetch/read()` 网络异常且已有 `run_id` 时，按 EOF 的同一条续读路径尝试一次有界恢复；没有 run_id 才直接报错。
3. 用户主动点击“停止”属于 cancelled：清掉该次 `CHAT_PENDING_KEY`，不要在下次打开时把主动取消误当作待恢复流。
4. 明确多标签页策略。若产品只支持一个活动聊天标签页，在界面与 README 写清楚；若支持多会话，再把 pending key 按 session/run 分开。不要为了假设的场景增加通用同步层。

直接验收：过期文件可在既有清理入口删除；网络读取失败后能续读已落盘终态；点击停止后刷新不会再次恢复该流。

## P1：诊断路径使用同一 principal 的 RAG 可见性

文件：`app/agent/aiops/knowledge_wiring.py`、`app/agent/aiops/runtime_tools.py` 及其调用链。把已解析的 principal/过滤条件传入诊断模式的 `retrieve_knowledge`，与 `/api/chat` 使用同一 `VectorStoreRetriever` 可见性规则。不能让诊断路径绕过文档 ACL；不要再造第二套过滤器。

直接验收：multi 模式下 A 的文档在 A 的诊断与问答中可见，B 的诊断与问答均不可见；single/dev 行为保持不变。

## P1：修正验收口径与评测快照

1. 当前 `docker compose ps -a` 显示 app healthy、Milvus 依赖退出；将 README/验收报告拆成进程 liveness 与 RAG readiness/能力状态，历史“全栈 healthy”保留时间点和镜像 digest，不把 app healthy 写成 Milvus healthy。
2. G5 浏览器续读证据是“客户端截断、服务端已落盘后按 seq 回放”，不要写成真实 TCP 断线时后端仍持续执行。若要声称断线恢复，必须验证真实中断路径；否则保留当前准确表述。
3. 校准 profile 读取时核对题集 sha256、语料 manifest、embedding descriptor 与当前 store；不一致直接停止，避免换 collection 后静默复用旧阈值。这是数据一致性检查，不是通用防御层。
4. 评测产物仍保留检索 Recall、门控权衡、引用支持、judge、确定性要点覆盖、拒答/误答各自分母；不要合成“问答准确率”。如需展示本地 QLoRA 与 cloud_multi，使用同一小批任务比较工具成功率、延迟、token/显存和拒答边界，不新增 smoke 名称。
5. 把现有 `eval/rag/README.md`、`scripts/eval_generation.py` 中面向开发取样的“冒烟”措辞改成“抽样生成评测”；相应产物 kind 使用 `generation_eval_llm_sampled`。历史证据文件名可以保留以维持 provenance，但新报告不再使用该称呼。
6. `VectorStoreRetriever` 对带 `expr` 的调用不要捕获所有 `TypeError` 后无条件去掉过滤；只在适配器明确不支持该参数时走兼容分支，真实查询错误应保持失败状态，避免静默降低候选范围。

## P2：明确记录而不是扩大范围

- SSH 只读 staging 未获授权，继续标为未验证，不伪造联调。
- CUDA torch、PyMilvus ORM 迁移、GitHub CI 实跑、Linux `flock` 分支、真实 IME/读屏器留作独立任务，不和权限/聊天流修复混在一次改动中。
- 完成本轮后提交一个可审计 commit，附当前代码 commit、镜像 digest、题集/manifest/descriptor sha256；清理不应入库的临时评测文件。

## 完成标准

- 上述 P0/P1 直接验收场景全部通过；现有 G5 结果不回退。
- 后端、前端既有回归命令通过；报告只写本轮实际执行的场景和分母。
- 工作区不再以“未提交 40 个业务文件 + 多个临时产物”的状态作为最终发布快照。
