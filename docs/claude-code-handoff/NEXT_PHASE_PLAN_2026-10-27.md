# 下一阶段优化与验收计划

日期：2026-10-27。对象：当前工作区及已完成的 A01–A13 离线整改。上一阶段的三个反例探针均已恢复为 `bug_reproduced=false`，但这只证明测试替身边界正确，不能替代真实 Milvus、云模型、SSH、浏览器和容器验收。

## 当前基线

- 后端：621 passed、1 skipped、2 warnings。
- 前端：53 passed，`npm run build`、`npm run lint` 通过。
- Python：`ruff check app servers scripts --select F821,F811,F841` 通过。
- 11 个 core 来源的 raw/normalized/notices 已生成，manifest 哈希一致；仍有 3 个来源许可状态 `needs_review`。
- 三个离线探针已通过：active revision/失败残片、严格本地 embedding、目标绑定/预算/取消/分页/评测格式等反例均已修复。
- 当前工作区已经有本地提交；交给 Claude Code 前先记录新的 `git status` 与 `git log`，不要把不同时间的测试数字混写。
- Docker daemon 可用，但本次 `docker build` 在 Dockerfile frontend syntax 镜像阶段因无法从 Docker Hub 获取 OAuth token 失败。需要网络/代理恢复后再重试，不能把此结果写成“构建成功”或“代码构建失败”。

## 阶段 G1：真实依赖验收（最高优先级）

### G1.1 Docker 与 Compose

在隔离测试环境执行，不连接生产服务器：

```powershell
docker compose config --quiet
docker build --progress=plain -t oncall-agent-acceptance .
docker compose up -d milvus
docker compose up -d app
docker compose ps
docker compose logs --no-color --tail=200 app
```

如果第一步构建仍在 `auth.docker.io` 失败，记录 Docker Desktop 登录/代理/registry mirror 的环境原因；不要修改 Dockerfile 来绕过认证，也不要把缓存镜像当成干净构建。

验收：多阶段镜像严格使用 npm ci 和 uv sync --frozen --no-dev；entrypoint 等待 MCP 就绪并监督子进程；Milvus 未就绪时 app 仍启动，/health 只把 RAG 标为 unavailable；测试 SIGTERM、MCP 子进程异常退出、app 容器重启，以及四个命名卷重建后的 uploads/traces/eval/data 保留。记录镜像 digest、Compose 配置、容器日志和健康检查结果。

### G1.2 真实 Milvus 与知识库

使用 staging Milvus，先导入 11 个 core 文档中的小批量，再逐步扩大。保留 collection 名称、embedding descriptor、metric、实体数和版本 manifest。

验收顺序：

1. 上传 → queued → indexing → ready；HTTP 202 不得被显示成索引成功。
2. OLD → NEW 更新后，查询只返回 active revision；撤销/回滚恢复正确版本。
3. 每批写入一行后注入失败，确认失败 revision 无残片、旧 active 仍可检索。
4. 修改 embedding/metric/维度后，状态为 invalid_index 并要求显式重建。
5. 停止 Milvus 后重新启动，同一 app 进程能在退避重试后恢复；不需要手工 reset。
6. 真实 HTTP /api/knowledge/* → knowledge_service → VectorStoreRetriever → Milvus → SSE 引用，不能只测 fake store。

输出 collection 描述、11 个文档的 active revision、chunk 数、查询示例、失败恢复日志和清理结果。不要把离线 cosine 结果写成 Milvus 结果。

### G1.3 当前浏览器构建

用当前 npm run build 产物重新执行，不复用旧构建截图：

- 1440：本地/在线模式选择、诊断计划、报告、引用。
- 1100/1024/768：inline inspector、aria-live、目标候选选择与状态更新。
- 390/375：输入框、知识库文档列表、作业失败/重试、引用卡片。
- 同名服务的两个实例必须显示两张卡，点击第二张后请求体包含其 target_candidate_id，服务端绑定第二个实例。
- 实际中文输入法组合态、停止/再发送、断线恢复、历史分页和 tab 切换。

每个旅程保存 URL、HTTP 状态、截图、浏览器版本、构建 hash 和未通过原因。合成 DOM/纯函数测试只能作为补充。

### G1.4 云模型、监控与 SSH

只用明确配置的 staging 凭证和只读权限：

- 在线 Supervisor/metrics/logs/Reviewer 用真实模型跑至少 3 个 JSON 输出正常、1 个非 JSON、1 个超时场景；确认 JSON 提取/重试/降级报告行为。
- 不连接真实告警写入或生产日志，记录 token、延迟、费用和失败的分母。
- SSH 只读连接先执行健康检查 fixture；核对目标绑定、命令超时、取消、远端进程仍可能存活的边界。不得把“本地停止等待”描述成“远端命令已杀死”。

## 阶段 G2：RAG 质量与数据治理

### G2.1 语料完整性

- 逐项人工核对 3 个 needs_review 许可状态，补 license basis、版权与 notice；没有依据的来源保持 pending。
- 检查 DK-03 normalized 正文长度和章节完整性，确认不是只提取了选项表。
- 把滚动 latest/main 固定成抓取时间、commit/tag、canonical URL 和 hash。
- 保留原文、规范化 Markdown、分片 manifest、descriptor 和导入 job 的可追溯关系。

### G2.2 评测集

建立与 dev 分离的 blind 集，建议首批 50–100 条，覆盖中文问题检索英文手册、精确命令/参数/错误码、Linux/Windows 和版本冲突、无答案/过期版本/相似但不支持结论的文档、代码/YAML/完整命令、提示注入、active revision/撤销/ACL/索引不可用。

只在 dev 拟合 no-answer 阈值，在 blind 上报告 Recall@k、MRR/nDCG、引用支持率、无答案误答率、p50/p95、CPU/RAM/VRAM。不要把 14 条 dev 的满分写成泛化结论。

### G2.3 Parent、引用和权限

- child 命中时回答上下文使用有界 parent，并在引用卡片保留 child locator；超长 parent 必须可解释截断。
- 观测型 evidence 与 knowledge 型 evidence 分开，知识片段逐条挂到相关观测或明确显示“仅为背景依据”。
- 在查询候选阶段执行服务端推导的 ACL/OS/版本/有效期过滤，未授权片段不能进入 reranker、模型或日志。
- 同一 session 的历史恢复不等于多轮回答上下文；设计有界历史窗口后再测试省略追问。

## 阶段 G3：运行可靠性与运维闭环

1. `purge_expired_traces()` 目前有实现但没有明显的调度调用；增加明确的 cron/启动后后台任务或运维命令，记录删除数量、失败数和 dry-run。
2. 保证 abandoned 运行和 retention 清理与正在追加的 trace 并发时不丢事件。
3. 让 run/event 的权限归属从共享 session_id 升级为服务端 principal/资源授权；跨用户读取、知识删除、引用回看都复用同一权限边界。
4. 将 trace、job、active revision、pending cleanup 的状态指标接入 /health 或 metrics，避免只写日志。
5. 把 Pydantic 弃用 warning、Ruff 配置迁移 warning 和 Node 实验性 strip-types 测试方式纳入依赖升级计划。

## 阶段 G4：求职项目呈现

README 和简历只写可追溯结果：

- 已完成：双模式、本地严格离线路径、在线多智能体图、工具/目标/证据约束、版本化 RAG job、引用与状态协议。
- 已测但非真实依赖：621 个后端测试、53 个前端逻辑测试、离线 RAG dev baseline。
- 待验证：真实 Milvus/云 API/SSH/容器/当前浏览器完整旅程。
- QLoRA 对比必须固定 base/adapter、量化配置、数据划分、工具 fixture 和评测集；没有盲测和统一条件时不写“准确率提升”。

## Definition of Done

下一阶段只有同时满足以下条件，才能把项目称为“可演示完成”：当前构建浏览器旅程通过；Docker 镜像可在干净环境构建并启动；真实 Milvus 完成一轮导入/检索/更新失败/恢复；cloud API、只读 SSH 和取消边界各有 staging 记录；blind RAG 结果与资源指标有固定脚本和 manifest；retention、权限、parent/观测关联的剩余边界已实现或明确写在 README；IMPLEMENTATION_STATUS.md 与 VALIDATION_RESULTS.md 使用同一日期、同一 commit、同一测试数字。
