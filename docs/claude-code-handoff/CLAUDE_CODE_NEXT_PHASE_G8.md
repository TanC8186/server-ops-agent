# G8 下一阶段任务书：双模式基准、最终发布与简历证据闭环

日期：2026-10-29  
基线分支：\`g6-permissions-chat-consistency\`  
当前 HEAD：\`69f4bf5\`  
上一阶段：G7（上传归属一致性、评测快照边界、镜像 provenance）

本轮目标：把项目从“功能和权限基本完成”推进到“可以作为企业软件开发/AI 应用工程岗位简历项目展示”的最终版本。重点补齐本地 QLoRA Planner 与在线 cloud_multi 的同题对比、最终镜像可追溯性、凭据清理、运行文档和简历证据。

## 一、执行方式：主 Agent 编排，子 Agent 并行，统一集成

### 1. 不限制 token 和上下文使用

本轮不要为了节省 token、缩短回复或提前结束而压缩检查范围。允许 ClaudeCode 长时间运行、读取完整代码和历史文档、重复运行必要的构建与直接业务验证。优先保证证据完整、任务闭环和最终结果可复现。

### 2. 必须由主 Agent 创建并管理子 Agent

主 Agent 不应独自承担所有代码阅读、训练评测、构建验证和文档整理。启动阶段先建立任务 DAG，再按依赖并行派发以下子任务：

| 子 Agent | 负责范围 | 输出物 |
|---|---|---|
| \`release_provenance\` | 最终 HEAD 镜像重建、OCI revision、Compose、鉴权、RAG readiness | \`G8_RELEASE_REPORT.md\` 草稿、镜像 digest、配置状态 |
| \`secret_hygiene\` | 凭据轮换状态、仓库/日志/TEMP/构建上下文扫描、ignore 规则 | \`G8_SECRET_AUDIT.md\`，只输出状态/计数，不输出值 |
| \`mode_benchmark\` | 本地 QLoRA 与 cloud_multi 同题对比、延迟/资源/token/工具结果 | \`G8_MODE_BENCHMARK.md\` 与结构化 JSON 结果 |
| \`local_model_quality\` | QLoRA 数据覆盖、工具名不一致、端侧模型计划质量和失败边界 | \`G8_LOCAL_MODEL_REVIEW.md\`，必要时提出最小训练数据修正 |
| \`rag_legacy_acl\` | 旧 LangChain 知识工具路径、principal 传播、单进程/多进程边界 | \`G8_RAG_LEGACY_REVIEW.md\` |
| \`frontend_resume\` | 首页模式选择、状态文案、错误路径、截图、README 简历证据 | \`G8_FRONTEND_REVIEW.md\` 与截图清单 |

主 Agent 负责：

1. 给每个子 Agent 完整上下文和明确文件范围；
2. 允许互不依赖的审查并行执行；
3. 收集所有结果后建立冲突清单；
4. 只让一个集成 Agent 修改同一组共享文件，避免并发编辑冲突；
5. 最后由主 Agent 统一运行回归、构建、Compose 和文档审计。

### 3. 推荐工作流

\`\`\`text
Phase A  并行审查
  ├─ release_provenance
  ├─ secret_hygiene
  ├─ mode_benchmark
  ├─ local_model_quality
  ├─ rag_legacy_acl
  └─ frontend_resume

Phase B  并行准备
  ├─ release_provenance：准备最终构建流程
  ├─ mode_benchmark：准备固定题集和结果 schema
  ├─ local_model_quality：准备端侧运行配置和数据修正建议
  └─ frontend_resume：准备最终截图和简历表述

Phase C  集成修改
  └─ 单一 integration Agent 按依赖顺序修改代码、脚本和文档

Phase D  真实直接验收
  ├─ 目标场景回归
  ├─ 本地/云端同题运行
  ├─ 标准 Dockerfile 构建
  ├─ Compose + 鉴权 + RAG readiness
  └─ 工作区、敏感文件、提交身份和简历口径审计
\`\`\`

不要让多个 Agent 同时编辑 \`README.md\`、\`Dockerfile\`、\`G8_RESULTS\` 或同一测试文件。审查阶段可以并行，写入阶段必须有唯一 owner。

## 二、硬性范围约束

1. 不重复实现 G7 已完成的上传 owner 回滚、owner OR ACL、作业权限、聊天流 run/session 隔离和评测快照修复。
2. 不新增通用防御层、统一兜底框架、无业务目标的重试/清洗逻辑或新的权限抽象。
3. 不新增独立的开发取样流程。模式对比必须使用固定题集和明确分母，结果用于工程比较，不包装成模型准确率。
4. 不把“配置存在”写成“模型已连通”，不把 app liveness 写成所有依赖 ready，不把 MCP 首次加载前的 degraded 写成 ready。
5. 所有凭据、临时 key、SSH 私钥、代理地址和环境变量值不得进入报告、截图、JSON、镜像 label 或 Git。
6. SSH staging、真实 TCP 断线、CUDA torch 替换、PyMilvus ORM 迁移、GitHub CI 实跑、Linux flock、真实 IME/读屏器仍属于未验证边界，除非本轮实际完成，不得写成已完成。

## 三、P0：修复最终镜像 provenance

相关文件：\`Dockerfile\`、\`Makefile\`、\`docker-compose.yml\`、\`.github/workflows/ci.yml\`。

### 当前问题

G7 当前运行镜像包含 G7 代码，但镜像 label 的 revision 仍是 \`b27f9f4\`；最终代码 HEAD 已是 \`69f4bf5\`。镜像功能可用，但无法证明它对应最终提交。

### 实施要求

1. 在干净工作区确认最终 HEAD。
2. 使用仓库原始 \`Dockerfile\` 重建镜像，传入：

\`\`\`powershell
$rev = git rev-parse HEAD
docker build --no-cache \`
  --build-arg GIT_REVISION=$rev \`
  -t oncall-agent:g8-final .
\`\`\`

3. \`docker image inspect\` 的 \`org.opencontainers.image.revision\` 必须逐字等于当前 HEAD。
4. 如果 \`--pull\` 因 registry 网络失败，必须如实记录“未执行 \`--pull\`”；不能把本机已有基础镜像写成全新拉取结果。
5. 构建时需要代理只能通过构建期参数传递，不能写入 Dockerfile 的 \`ENV\`、label、层文件或报告。
6. 使用该 digest 执行 \`docker compose up -d --force-recreate\`，不能继续使用旧 tag 对应的旧容器。
7. 验收结束后记录：HEAD、Dockerfile hash、镜像 digest、镜像大小、revision label、Compose 状态。

### 直接验收

- 镜像内存在 G7 代码；
- label revision = 最终 HEAD；
- app/etcd/MinIO/Milvus healthy；
- \`AUTH_MODE=deployment\`；
- 无 key/错 key/正确 key 为 401/401/200；
- \`/health\` 分开记录 liveness、Milvus、RAG、MCP；
- MCP 尚未首次加载时只能写 degraded，不能写 ready。

## 四、P0：凭据轮换与发布前敏感信息清理

相关范围：\`.env\`、\`logs/\`、\`%TEMP%\`、\`%LOCALAPPDATA%\`、构建上下文、截图、Git 历史、Docker image history。

1. 按 G7 清单完成真实 LLM/API、云厂商、SSH 私钥和其他曾暴露凭据的轮换。
2. 扫描仓库和运行目录时只输出：文件路径、命中类型、数量、是否已脱敏；禁止打印匹配内容。
3. 检查临时目录、探针目录、临时复制目录、Docker build context、镜像 history 和容器环境。
4. 确认 \`.env*\`、\`.pem\`、\`.key\`、\`.g8*\`、临时目录不会进入 Git 或 Docker context；\`.env.example\` 必须保留。
5. 对已经删除的明文凭据仍要求轮换，因为删除文件不等于凭据失效。
6. 报告只写 \`rotated/pending/absent\`，不写 key、token、路径型 secret 或代理 URL。

直接验收：工作区不含明文凭据；容器配置检查只输出 \`set/unset\`、长度或 hash；报告中没有密钥值。

## 五、P1：本地 QLoRA 与 cloud_multi 同题工程对比

这是本轮的核心项目价值，不是泛化模型评测。由 \`mode_benchmark\` 子 Agent 负责数据和执行，主 Agent 负责最终口径。

### 1. 固定题集

准备 10–20 条固定任务，至少覆盖：

- CPU、内存、磁盘、网络、服务异常；
- 日志检索和知识库问答；
- 目标服务存在同名多实例；
- 目标不明确，需要用户选择；
- 知识库无答案；
- 工具参数边界；
- 本地模型计划无法通过校验。

题集必须保存 sha256，运行前后不可偷偷修改。每条记录输入症状、目标服务、期望工具集合、是否允许拒答、是否需要引用和运行模式。

### 2. 统一运行条件

本地 Q4/AWQ 与 cloud_multi 必须使用：

- 相同输入；
- 相同 MCP 工具目录；
- 相同服务目标和候选目录；
- 相同只读工具白名单；
- 相同结果 schema 和证据渲染规则；
- 明确的并发数和超时。

### 3. 必须记录的指标

分别记录本地和云端，不合成一个总分：

- 计划 schema 合法率；
- 计划工具集合召回和工具 F1；
- 工具调用成功率；
- 目标绑定成功率；
- 端到端延迟 p50/p95；
- LLM 输入/输出 token；
- GPU 显存峰值或 CPU 内存峰值；
- 本地停止、云端拒答、知识库无答案的数量与分母；
- 证据和引用是否能回溯到服务端结果；
- 本地失败是否按设计停止，禁止暗中切云端。

### 4. 结果口径

可以写：

- “在固定 15 条运维任务上，本地 Planner 的工具调用成功率、延迟和资源占用与 cloud_multi 的对比”；
- “本地模式在无云凭证时可独立运行，并在计划不合法时停止”；
- “云端模式使用 Supervisor、metrics、logs、Reviewer 的多智能体编排”。

不能写：

- “本地模型准确率 X%”；
- “多智能体准确率 X%”；
- “本地模型全面优于云端”；
- “4-bit 无损”而没有同版本、同题集、同硬件证据。

## 六、P1：端侧 QLoRA 训练与数据覆盖优化

由 \`local_model_quality\` 子 Agent 负责，只有在不破坏 G7 运行链路时才修改训练数据或训练脚本。

1. 检查训练集中的工具名与生产工具名是否一致，重点处理 \`search_log\`、\`search_service_logs\`、\`search_system_logs\` 的差异。
2. 将目标歧义、参数缺失、日志查询、否定句、OOM 边界、中文改写和未授权工具作为独立数据桶统计。
3. 训练集、验证集、测试集按题目模板和故障类型去重，避免同一模板变体泄漏到多个集合。
4. 保持输出 schema、工具白名单和参数校验与生产代码一致；训练脚本不能偷偷放宽生产校验。
5. 在 RTX 4060 Laptop 8GB 上记录：模型格式、量化方式、上下文长度、显存峰值、TTFT、TPOT、并发数和失败原因。
6. 如果重新训练，只做一轮有明确假设的改动，保留旧权重、旧指标和新权重、新指标的对应关系；不以单次测试集数字宣称泛化能力。
7. 0.5B 若仍不稳定，记录为实验结果，不要为了完成最低参数目标而改变验收口径。

## 七、P1：遗留 RAG 路径和部署边界

由 \`rag_legacy_acl\` 子 Agent 先审查，主 Agent 决定是否纳入本轮代码范围。

1. 检查 \`app/tools/knowledge_tool.py\` 和 \`app/services/rag_agent_service.py\` 是否仍可被实际 API 或诊断入口调用。
2. 如果仍可达，接入与 \`/api/chat\`、双模式诊断一致的 principal/ACL 过滤；如果不可达，明确标注 deprecated 并禁止新代码引用。
3. 不为历史不可达代码建立第二套权限系统。
4. 明确单 API key 的真实限制：当前 principal 隔离是测试和资源归属验证，不是完整账号、组织和角色体系。
5. 明确聊天事件、会话历史和 JSON 文件存储只适合单 app 进程；除非明确要支持多副本，否则不引入 Redis/数据库迁移。

## 八、P1：前端求职展示和用户体验收尾

由 \`frontend_resume\` 子 Agent 负责，重点是可见行为，不增加复杂 UI。

1. 首页模式选择必须清楚显示：本地端侧模型与在线多智能体各自的模型来源、是否需要云凭证、失败时行为和资源代价。
2. 运行中显示当前模式、模型名、目标服务、目标实例/地域、RAG 状态和证据来源。
3. 本地模式失败时明确显示“本地模式已停止”，不能出现用户误以为自动切云端的文案。
4. 云端模式显示 Supervisor、metrics、logs、Reviewer 的阶段状态，并能回看 trace。
5. 继续保持引用卡片、目标绑定、RAG unavailable、作业失败/重试和流恢复的现有行为。
6. 截图只保留无密钥、无真实服务敏感数据的演示数据；记录视口和构建 digest。
7. README 新增一段“为什么同时支持本地与云端”，内容围绕算力限制、隐私、延迟和能力差异，不写未经验证的效果。

## 九、最终提交和发布材料

1. 由主 Agent 统一生成 \`G8_RESULTS_2026-10-29.md\`，记录：
   - 最终 HEAD 和分支；
   - G8 代码提交与文档提交；
   - 镜像 digest、大小、revision label；
   - 固定模式对比题集 sha256；
   - 本地/cloud 指标及分母；
   - 凭据轮换状态；
   - 未验证边界。
2. 更新 \`README.md\`、\`IMPLEMENTATION_STATUS.md\`、\`VALIDATION_RESULTS.md\`，不要覆盖历史 V1–V14 证据。
3. 生成最终简历项目描述，分成“已验证事实”和“明确限制”两组。
4. 当前 G7 分支完成 G8 后形成独立提交，不要把临时评测结果和敏感日志加入 Git。
5. 最终再决定是否合并到 \`master\`；合并前检查主分支没有旧代码覆盖 G6/G7/G8。

## 十、完成标准

### 必须完成

- 最终镜像 revision label 与最终 HEAD 完全一致。
- 四个核心 Compose 服务状态、鉴权结果、Milvus/RAG readiness 可复现。
- 所有已暴露凭据完成轮换或有明确 owner 和 deadline；报告不含值。
- 本地 QLoRA 与 cloud_multi 使用同一固定题集完成工程指标对比。
- 端侧训练数据中的生产工具名、schema 和参数校验一致。
- 工作区 clean，临时 key、日志、缓存、探针和代理配置不入库。

### 可以保留为未验证

- SSH staging；
- 真实 TCP 断线时后端是否继续执行；
- CUDA torch 替换；
- PyMilvus ORM 迁移；
- GitHub CI Docker job 实跑；
- Linux flock；
- 真实 IME/读屏器；
- 多副本共享会话与跨进程聊天事件一致性。

## 十一、推荐命令

\`\`\`powershell
# 当前身份
git status --short
git rev-parse HEAD

# G7 回归基线
./.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider

# 前端
cd web
npm test
npx tsc -b
npm run lint
npm run build
cd ..

# 最终镜像 provenance
$rev = git rev-parse HEAD
docker build --no-cache --build-arg GIT_REVISION=$rev -t oncall-agent:g8-final .
docker image inspect oncall-agent:g8-final --format '{{json .Config.Labels}}'

# Compose 直接验收
docker compose up -d --force-recreate
docker compose ps -a

# 最终检查
git diff --check
git status --short
\`\`\`

如果标准 Dockerfile 构建只能通过临时网络绕行，必须把代理只用于构建期，并在结果中明确 \`--pull\` 是否执行；不能把临时构建结果写成完全可复现的 CI 证据。
