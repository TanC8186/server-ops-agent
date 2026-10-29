# G9 下一阶段任务书：最终发布冻结、凭据轮换与简历交付

日期：2026-10-29  
基线分支：g6-permissions-chat-consistency  
当前 HEAD：0b2b0cd  
G8 代码提交：35c7bfa  
G8 构建提交：63d1a41  
G8 镜像：sha256:6a7ff2b7f04f…

本轮不再堆叠功能，目标是完成最终发布候选版本：凭据轮换、镜像严格对应 release commit、云端工具依赖边界确认、README/简历/面试演示材料闭环。

## 一、执行方式

### 不限制 token 和执行时长

不要因为 token、上下文、回复长度或单轮耗时而压缩任务。允许 ClaudeCode 完整读取 G1–G8 文档、执行多轮构建和直接业务验证。每个结论必须有命令、产物或可复核状态。

### 主 Agent 编排子 Agent

主 Agent 先建立 DAG，再并行派发：

| Agent | 范围 | 输出 |
|---|---|---|
| release_freeze | release commit、Dockerfile、revision、digest、Compose | G9_RELEASE_FREEZE.md |
| credential_rotation | key 轮换、日志/TEMP/Git/镜像扫描 | G9_SECRET_STATUS.md |
| cloud_account | CLS/QCE/SSH 账户状态 | G9_CLOUD_DEPENDENCY.md |
| resume_packaging | README、简历、面试演示 | G9_RESUME_PACKAGE.md |
| legacy_boundary | 遗留 RAG、单 key、多副本边界 | G9_BOUNDARY_REVIEW.md |

审查阶段允许并行；共享文件只能由一个 integration Agent 修改。主 Agent 统一解决冲突并执行最终验收。

## 二、硬性约束

1. 不重复实现 G8 已完成的私钥排除、schema echo、Compose 镜像选择和 gold 工具修复。
2. 不新增通用防御层、统一兜底框架、无业务目标的重试/清洗逻辑或新的权限抽象。
3. 不新增独立开发取样流程；模式对比、账户依赖确认和面试演示必须使用固定题集或实际用户流程。
4. 不把配置存在写成服务连通，不把 app liveness 写成所有能力 ready。
5. 不把凭据、临时 key、私钥、代理地址或环境变量值写入报告、截图、JSON、镜像 label 或 Git。
6. SSH、真实 TCP 断线、CUDA torch、PyMilvus、GitHub CI、Linux flock、IME/读屏器、多副本和真实多用户，除非本轮真实完成，否则继续标记未验证。

## 三、P0：凭据轮换

G8 已证实未加密 RSA 私钥曾进入镜像层，多个真实凭据曾出现在日志或临时 env 转储中。删除文件不能代替轮换。

1. 轮换 LLM API、腾讯云 Secret、SSH key pair 和 G7/G8 清单中的所有曾暴露凭据。
2. 轮换过程不把新值写入仓库、任务书、截图、JSON 或镜像。
3. 验证旧值失效，只输出状态码、布尔值、长度或 hash。
4. 扫描 Git tracked 内容和历史、.env*、.pem、.key、id_rsa*、logs、TEMP、Docker context、image history、Compose 渲染和容器环境，只输出文件名与计数。
5. 确认 .env*、.pem、.key、.g9*、临时目录被排除，.env.example 保留。
6. 报告状态只允许 rotated、pending、absent。

完成条件：真实凭据全部 rotated，或有明确 owner、阻塞原因和 deadline；否则不得称最终发布完成。

## 四、P0：冻结 release commit 并重建镜像

G8 镜像 revision 指向 63d1a41，最终 HEAD 后来变成文档提交。G9 必须建立冻结顺序：

1. 先完成所有代码、README、状态表和结果文档。
2. 创建唯一 release commit。
3. release commit 后不再修改代码或发布文档；任何修改都必须重新生成 release commit。
4. 在 release commit 上执行标准 Dockerfile 构建：

~~~powershell
$rev = git rev-parse HEAD
docker build --no-cache --build-arg GIT_REVISION=$rev -t oncall-agent:g9-release .
docker image inspect oncall-agent:g9-release --format '{{json .Config.Labels}}'
~~~

5. org.opencontainers.image.revision 必须逐字等于 release commit。
6. --pull 若因 registry 失败未执行，必须如实记录；不能把本机基础镜像写成新拉取。
7. 代理只能作为构建期参数，不能进入 ENV、label、层文件或报告。
8. 用 digest 启动 Compose，不用旧 tag：

~~~powershell
$env:APP_IMAGE = "oncall-agent@sha256:<digest>"
docker compose up -d --force-recreate app
docker inspect oncall-agent --format '{{.Image}}'
docker compose ps -a
~~~

9. 报告记录 HEAD、Dockerfile hash、digest、大小、label、实际容器 Image digest。

直接验收：

- app、etcd、MinIO、Milvus healthy；
- attu 未起时单独记录，不把它算作核心四服务；
- deployment 无 key/错 key/正确 key 为 401/401/200；
- liveness healthy、Milvus connected、RAG ready、MCP 未首次加载时 degraded；
- 镜像无项目私钥、.env、临时 key、项目 pyc；
- image label 与 release commit 一致；
- Compose 实际 digest 与报告一致。

## 五、P1：云端账户与模式对比口径

G8 的 18 条对比有效，但工具成功率受 CLS 未开通、QCE namespace/metric 无效和实例状态影响。

### 账户确认

cloud_account Agent 只读确认：

- CLS 是否开通、测试日志主题是否存在；
- QCE namespace 和 metricName 是否有效；
- 测试实例是否存在且 key 有权限；
- SSH staging 是否有授权。

只输出 ready、unavailable、unauthorized、not_found 和错误类别，不输出凭据。

### 如果账户和资源已授权

1. 保持 G8 18 条固定题集和 sha256；
2. 不改模型、工具白名单或评分函数；
3. 重新运行本地和 cloud_multi；
4. 将结果标为“依赖恢复后的运行”，保留 G8 原始结果。

### 如果账户仍不可用

1. 不伪造成功；
2. 不删除失败行、不修改 gold；
3. 保留计划召回/F1、延迟、token、拒答边界；
4. 工具成功率单列为账户依赖，不用于比较模型优劣；
5. 简历只写计划层、延迟和 token 对比。

禁止写本地模型准确率、多智能体准确率、本地全面优于云端、4-bit 无损或真实工具全部成功。

## 六、P1：端侧模型数据和生产一致性

1. 检查训练工具名和生产注册表，尤其是 search_log 与 search_service_logs。
2. 不通过修改训练集掩盖生产工具不可执行问题。
3. 训练/验证/测试按模板、故障类型和工具名去重。
4. 统计目标歧义、参数缺失、日志查询、否定句、OOM 边界、未授权工具。
5. 保持训练 schema、生产 schema、白名单和参数校验一致。
6. 没有明确新假设时不重新训练。
7. 0.5B、CUDA torch、fp16 vs 4-bit 继续独立记录，不混入发布结论。

## 七、P1：简历和面试材料

README、简历和演示必须统一口径。

可以写：

- 本地 QLoRA Planner 与 cloud_multi 双模式；
- 固定 18 条任务上的延迟、token、计划召回/F1；
- 本地失败明确停止、不暗中切云端；
- Supervisor、metrics/logs 专科、Reviewer 编排；
- 真实 Milvus、版本化 RAG、引用和 ACL；
- 私钥入镜像、schema echo、陈旧镜像、不可执行 gold 工具等真实缺陷修复；
- 900 后端测试、75 前端测试、标准 Dockerfile 构建证据。

不能写：

- 问答准确率或模型总准确率；
- 完整多用户账号体系；
- SSH 已联调；
- 真实 TCP 断线恢复；
- CI Docker job 已通过；
- --pull 构建已完成；
- 工具成功率代表模型能力；
- 4-bit 相比 fp16 无损。

面试演示顺序：

1. 首页选择 local 或 cloud_multi；
2. 展示模型来源、凭据需求、失败行为和资源代价；
3. 输入症状并选择目标实例；
4. 展示计划、工具阶段、证据和引用；
5. 展示 RAG 状态、trace 和报告；
6. 展示 18 条同题模式对比；
7. 说明账户依赖、单 key、单进程存储和未验证边界。

截图不得含密钥或真实敏感服务数据，并记录构建 digest。

## 八、结果归档

创建 docs/claude-code-handoff/G9_RESULTS_2026-10-29.md，记录：

- release commit、分支、HEAD；
- image digest、大小、revision、Dockerfile hash；
- Compose 使用 digest 和四核心服务状态；
- 凭据轮换状态；
- G8 题集 sha256；
- local/cloud 指标及分母；
- 900/75 回归；
- 未验证边界；
- 可写入简历的事实和禁止表述。

保留 G1–G8 历史结果，不覆盖历史数字。

## 九、完成标准

### P0

- 暴露凭据全部轮换，或有明确 owner 和 deadline；
- release commit 冻结后镜像 label 与 release commit 一致；
- Compose 实际 digest 与报告一致；
- 镜像无项目私钥、env、临时 key 和项目 pyc；
- deployment 鉴权和 liveness/RAG/MCP 分层可复现。

### P1

- 18 条模式对比保留原始数据，账户错误与模型指标分开；
- README、状态表、G9 结果和简历口径一致；
- 本地失败行为、云端多智能体阶段和 RAG 引用演示可复现；
- 工作区 clean，临时资源清理完成。

## 十、推荐命令

~~~powershell
git status --short
git rev-parse HEAD
./.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider

cd web
npm test
npx tsc -b
npm run lint
npm run build
cd ..

$rev = git rev-parse HEAD
docker build --no-cache --build-arg GIT_REVISION=$rev -t oncall-agent:g9-release .
docker image inspect oncall-agent:g9-release --format '{{json .Config.Labels}}'

$env:APP_IMAGE = "oncall-agent@sha256:<digest>"
docker compose up -d --force-recreate app
docker compose ps -a

Invoke-RestMethod http://127.0.0.1:9900/health
Invoke-WebRequest http://127.0.0.1:9900/api/knowledge/documents -SkipHttpErrorCheck

git diff --check
git status --short
~~~

如果本机只能通过构建期代理完成构建，必须记录 --pull 是否执行；不能把代理绕行后的本机构建写成 CI 可复现证据。
