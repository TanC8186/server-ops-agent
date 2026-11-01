# G7 下一阶段任务书：上传归属一致性、评测快照与最终发布收尾

日期：2026-10-29  
基线分支：`g6-permissions-chat-consistency`  
基线 HEAD：`b27f9f4`  
代码提交：`de9d36e`  
基线提交：`ed13769`

本轮目标是修复 G6 复核中确认的两个数据一致性问题，收紧评测脚本的快照边界，整理镜像构建和验收证据，形成可以合并到最终主分支的发布版本。

## 必须遵守的范围约束

1. 不重复实现 G6 已完成的作业 ACL、聊天流 run/session 拆分、诊断 principal 传播、聊天事件清理和网络异常续读。
2. 不新增通用防御层、统一兜底框架、无业务目标的重试/清洗逻辑或新的权限抽象。
3. 不新增独立的开发取样验证流程。已有评测脚本只做必要的命名和数据一致性修正。
4. 所有凭据、测试 key、SSH 私钥和环境变量值不得进入报告、截图、测试产物或 Git。命令输出只允许打印状态、长度、hash 或布尔值。
5. 历史验收文件中的旧数字和历史文件名可以保留，但新增结论必须标注时间、commit、镜像 digest、题集 sha256 和分母。

## P0-1：修复上传归属与文档提交的一致性

相关文件：

- `app/api/knowledge.py`
- `app/core/principal.py`
- `app/services/knowledge_store.py`
- 现有知识库 API 测试文件；可新增 `tests/test_knowledge_upload_consistency.py`

### 已确认的问题

当前上传路径先写入 principal owner，再调用 `submit_document`。当 metadata 非法或提交过程失败时，文档没有创建，但 owner 侧表留下了记录，下一次同名上传会被错误拒绝。

另一个问题是：索引提交成功后，`_seal_uploaded_document()` 写 ACL 失败时，接口仍返回 202。owner 已记录，但文档 ACL 中可能只有其他成员；当前 `_owner_acl_visible()` 在 ACL 非空时忽略 owner，导致上传者看不到自己的文档和作业。

### 实施要求

1. 在写 owner 之前完成所有不会产生持久化副作用的校验：文件内容、source URI、metadata、日期、principal 列表、同名文档写权限。
2. owner claim 与 `submit_document` 之间必须有最小的状态一致性处理：
   - 新文档首次 claim 成功后，如果提交失败，清理**本次新建的 owner 记录**；
   - 已有文档且 owner 属于当前 principal 时，提交失败不得清理原有 owner；
   - 不要新增通用事务框架，直接在上传路径记录 `owner_before`/`claimed_by_request` 并按结果回滚。
3. 明确 owner 与 ACL 的读取关系：owner 始终拥有读取权限，ACL 成员拥有共享读取权限；写权限仍保持 G6 规则，即有 owner 时只有 owner 能 activate/delete/retry/覆盖上传。
4. ACL 补写失败时不得让 owner 丢失可见性。可以采用“owner OR ACL”的读取谓词，或采用明确的补偿写入；不要返回一个上传者无法看到的成功结果。
5. 不改变 G6 已确认的权限语义：
   - 只读 ACL 成员不能 activate/delete/retry；
   - 同名 source URI 不得被其他 owner 覆盖；
   - 删除后的历史作业不可重新暴露；
   - 作业列表隐藏项不消耗分页配额。

### 直接验收场景

至少覆盖以下场景，测试必须验证存储状态而不只验证 HTTP 状态：

1. 上传带非法 `valid_from`：返回 400；文档不存在；owner 侧表没有幽灵记录；随后同名合法上传可以成功。
2. 模拟 `submit_document` 在 claim 后失败：owner 记录按本次 claim 回滚；已有同名文档的 owner 不被误删。
3. 模拟 ACL seal 写入失败：上传结果与文档状态的契约明确；若仍返回 202，当前 owner 必须能列出文档、查看 job、检索该文档。
4. owner、ACL 只读成员、陌生 principal 三者分别执行 list/get/activate/delete/retry，权限结果与 G6 规则一致。

## P0-2：凭据处理与发布前清理

相关文件和目录：`.env`、`logs/cuda_cfg.log`、`docs/claude-code-handoff/`、截图目录、Git 历史。

1. 按 G6 报告中的轮换清单完成真实 LLM/API、云厂商和其他已暴露凭据的轮换；报告只写“已轮换/待轮换”，不写值。
2. 对日志、截图、评测 JSON、Markdown 和 Git 跟踪内容做一次不回显值的扫描，只输出命中文件名和计数。
3. 删除临时测试 key、临时 Dockerfile、探针目录、临时样本文档和一次性缓存；确认它们没有进入镜像上下文或 Git。
4. `.env`、私钥和运行时 key 继续保持未跟踪；不要为了“方便复现”把密钥写入 `.g6*` 或其它工作区文件。

直接验收：工作区不含明文凭据；容器配置检查只输出 `set/unset`、长度或 hash；报告中没有密钥值。

## P1-1：评测快照的一致性边界

相关文件：

- `scripts/eval_generation.py`
- `scripts/eval_calibration.py`
- `scripts/eval_blind.py`
- `eval/rag/README.md`
- `tests/test_calibration_eval.py`

### 必须修复的问题

1. `_verify_profile_consistency()` 中，profile 记录了非空 `store_document_ids`，而当前 store 的 documents 为空时，必须判定为 mismatch 并停止；不能因为 `current_ids=[]` 而只记 partial 后继续使用旧 profile。
2. profile 记录了 descriptor hash，而当前 store descriptor 集合为空时，同样判定为 mismatch。
3. `eval_calibration._store_evidence()` 读取存在但损坏的 JSON store 时，必须产生明确的数据错误或结构化失败，不能抛 `UnboundLocalError`。
4. 快照 mismatch 与 evidence 缺失分开：
   - mismatch：题集、manifest、descriptor、store 路径或文档集合明确不同，停止本次评测；
   - evidence 缺失：历史产物可标记 partial，但不能被报告写成“完整一致”；是否允许继续必须在命令输出和结果 JSON 中明确。
5. 保持现有指标分开：Recall、MRR、门控召回/误答权衡、引用支持、judge、确定性要点覆盖、拒答和误答分别保留分母，不合成一个“问答准确率”。

### 清理旧的开发取样命名

`eval_blind.py` 仍有旧的开发取样帮助文本、kind 和文件名前缀。统一改成 `sampled`/“抽样检索评测”语义：

- `--limit` 只表示本次处理的题目数量；
- 新产物使用稳定的 sampled kind 和 sampled 前缀；
- 历史 JSON 文件不重写，保持原文件名和 provenance；
- `eval_generation.py` 的注释中也不要保留旧的该类命名。

### 直接验收场景

1. 使用当前真实 profile/store：一致性结果为 0 mismatch。
2. 删除当前 store 中全部 documents：评测在调用模型前停止，并给出明确 mismatch。
3. 写入损坏 JSON store：校准脚本给出明确失败，不出现 Python 未初始化变量异常。
4. 旧历史产物仍可被读取并被标记为历史/partial，不被伪装成当前完整验收结果。

## P1-2：镜像构建 provenance 与 Compose 验收

相关文件：`Dockerfile`、`.github/workflows/ci.yml`、`docker-compose.yml`、`Makefile`。

G6 镜像功能上可用，但最终镜像使用了临时 `.g6-local.Dockerfile` 和 `--network host`，仓库原始 Dockerfile 没有在同一环境中完成构建。下一轮必须把这件事写清楚或解决，不能把临时变体包装成标准 Dockerfile 构建证据。

1. 优先在可复现网络环境或 CI 上用仓库原始 `Dockerfile` 完成一次 `--pull --no-cache` 构建；记录完整 commit、镜像 digest、大小和构建命令。
2. 如果本机 apt 边缘节点仍然 502，不要修改业务代码绕过；将本机网络限制标记为环境问题，并把标准 Dockerfile 的真实 CI 结果作为发布证据。
3. 为镜像增加 OCI revision 元数据（例如 `org.opencontainers.image.revision`），构建时传入当前 Git commit；不得把密钥写入 label 或 build args。
4. 用当前镜像执行完整 Compose 启动，不使用旧 tag 复用；验证：
   - app、etcd、MinIO、Milvus 状态；
   - `AUTH_MODE=deployment`；
   - 无 key/错误 key 为 401，正确 key 为 200；
   - `/health` 的 liveness、Milvus readiness、RAG capability、MCP capability 分开记录；
   - MCP 尚未首次加载时只能写 degraded，不能写 ready。
5. 验收结束后只停止本项目 Compose 资源，不触碰 `mact-*` 和 `personal_design-*` 资源。

## P1-3：发布文档与提交身份整理

相关文件：

- `docs/claude-code-handoff/G6_RESULTS_2026-10-28.md`
- `docs/claude-code-handoff/IMPLEMENTATION_STATUS.md`
- `docs/claude-code-handoff/VALIDATION_RESULTS.md`
- `README.md`

1. 将 G6 身份统一成三个字段：`base=ed13769`、`code=de9d36e`、`HEAD=b27f9f4`；删除或标注“dirty/本轮未提交”的过时表述。
2. 历史 V1–V12 证据保留，但新增 G7 段落只引用当前提交和当前产物，不覆盖历史数字。
3. 明确当前部署限制：单 API key 只能验证构造出的 principal 隔离，不等于真实多用户账号体系；聊天事件跨进程追加仍只适合单 app 进程。
4. 保留真实未验证边界：SSH staging、真实 TCP 断线、CUDA torch 替换、PyMilvus ORM 迁移、GitHub CI Docker job、Linux flock、真实 IME/读屏器。
5. G7 完成后在当前分支形成单独提交；不要在未完成验收时直接改写主分支历史。完成后准备合并到项目最终主分支。

## P2：双模式证据补强（独立于权限修复）

项目的核心卖点是本地 QLoRA Planner 与在线 cloud_multi 编排并存。权限和数据一致性修复完成后，再用同一批代表性运维任务做模式对比，不扩大成泛化模型评测：

1. 固定 10–20 条任务，覆盖 CPU、内存、磁盘、日志、服务异常、目标歧义和知识库无答案。
2. 本地 Q4/AWQ 与 cloud_multi 使用相同输入、相同工具目录、相同目标绑定规则。
3. 分别记录：
   - 计划 schema 合法率；
   - 工具调用成功率和工具 F1；
   - 端到端延迟 p50/p95；
   - 输入/输出 token；
   - GPU 显存或 CPU 内存；
   - 本地模型停止/云端拒答/知识库无答案边界；
   - 报告中的证据引用是否可回溯。
4. 结果只写成模式对比，不写“本地模型准确率”或“多智能体准确率”。
5. 记录硬件、模型版本、量化方式、上下文长度和并发数；现有 v8/v9 性能表未重测前，不要把它写成 v9 新结果。

## 建议执行顺序

1. 先处理 P0-1 上传归属一致性。
2. 同时完成 P0-2 凭据轮换与敏感产物清理。
3. 修复 P1-1 评测快照空集合/损坏 store 边界，清理旧取样命名。
4. 用标准 Dockerfile 或 CI 完成 P1-2 构建 provenance 和 Compose 验收。
5. 修正文档身份、README 限制和 G6 历史说明。
6. 最后再做双模式对比，避免在权限和评测快照未稳定前生成新的模型数字。

## 完成标准

### 代码与数据

- 非法 metadata、提交失败、ACL seal 失败三个上传一致性场景均有明确结果，且无幽灵 owner、无上传者丢失可见性。
- G6 作业 ACL、聊天流 run/session 隔离、诊断 principal 传播保持通过。
- 评测 profile 在当前 store 为空或文档集合变化时不会继续复用旧阈值。
- 损坏 store 产生明确失败，不出现未初始化变量异常。

### 运行与构建

- 标准 Dockerfile 或真实 CI 构建成功，镜像 revision、digest、大小可追溯。
- Compose 四个核心服务的状态与 app liveness/RAG readiness 分开记录。
- deployment 鉴权的 401/200 结果可复现，命令输出不含凭据。

### 文档与发布

- `G7_RESULTS_2026-10-29.md` 记录实际执行命令、commit、digest、题集/manifest/descriptor hash 和分母。
- README、状态表和验收报告不再有过时的 dirty commit 身份。
- 工作区 clean，临时文件、测试 key、敏感日志不入库。
- 对外只写已验证的双模式能力、RAG 检索指标和真实部署证据；不写未验证 SSH、真实 TCP 断线、完整多用户账号体系或生成式问答总准确率。

## 推荐复现命令

```powershell
# G7 直接业务回归
./.venv/Scripts/python.exe -m pytest `
  tests/test_knowledge_jobs_acl.py `
  tests/test_knowledge_upload_consistency.py `
  tests/test_diagnosis_knowledge_principal.py `
  tests/test_chat_events.py `
  tests/test_run_context_and_chat.py `
  tests/test_calibration_eval.py `
  -q -o addopts= -p no:cacheprovider

# 后端完整回归
./.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider

# 前端
cd web
npm test
npx tsc -b
npm run lint
npm run build
cd ..

# 评测快照一致性
./.venv/Scripts/python.exe scripts/eval_generation.py --dry-run

# 构建与 Compose（不得输出密钥）
docker build --pull --no-cache -t oncall-agent:g7-final .
docker compose up -d --build --force-recreate
docker compose ps -a
docker compose down

# 最终状态
git diff --check
git status --short
```

如果本机网络导致标准 Dockerfile 无法构建，只记录环境失败原因并停止，不要用临时 Dockerfile 的 digest 冒充标准构建结果。
