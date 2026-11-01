# ClaudeCode 下一阶段任务书：AutoDL RAG 收尾、真实双模式验收与 Docker 空间治理

日期：2026-10-30  
项目：`E:\On_Call_Agent\on_call_agent`  
目标分支：`g6-permissions-chat-consistency`

## 0. 本轮执行目标

本轮只解决任务书上一轮仍未完成的核心交付：

1. 将 AutoDL 官方文档真正抓取、规范化并写入 RAG；
2. 完成 AutoDL SSH 目标的产品级配置和本地 adapter 路由；
3. 用真实 AutoDL 目标完成本地 adapter 与 `cloud_multi` 的同题对比；
4. 修复容器环境下的观测口径问题；
5. 建立严格的 Docker 构建、验收、清理流程，立刻释放不再使用的本项目资源。

上一轮的真实结论必须保留：

- SSH 只读连接和工具执行已经真实跑通；
- 代码修复和回归测试已经通过；
- AutoDL 官方文档尚未入库；
- 本地/云端 AutoDL 同题对比尚未执行；
- 旧的 16 条专项评估存在输出泄漏，`Tool F1=0.766` 不能作为泛化证据。

## 1. 强制执行方式

本轮不要限制 token、上下文长度或执行时间。ClaudeCode 主 Agent 必须建立 workflow 并并行使用子 Agent，主 Agent 只负责依赖排序、冲突合并和最终验收。

推荐分工：

| 子 Agent | 范围 | 输出 |
|---|---|---|
| `autodl_rag_ingest` | 官方文档抓取、正文抽取、许可记录、manifest、Milvus 导入 | `AUTODL_CORPUS_REPORT.md`、`AUTODL_RAG_REPORT.md` |
| `autodl_target_config` | `services.json`、端口、product、运行配置、目标路由 | `AUTODL_TARGET_REPORT.md` |
| `autodl_runtime_metrics` | cgroup 指标、磁盘路径、journal 日志能力、工具结果口径 | `AUTODL_RUNTIME_REPORT.md` |
| `autodl_benchmark` | 固定题集、本地 adapter、cloud_multi、真实 SSH 同题对比 | `AUTODL_LIVE_BENCHMARK_REPORT.md` |
| `docker_space_guard` | 镜像归属、构建策略、清理记录、镜像 digest | `DOCKER_SPACE_REPORT.md` |
| `security_claims` | 凭据、日志、截图、README 和对外表述审查 | `AUTODL_SECURITY_CLAIMS_AUDIT.md` |

共享代码文件只能由 integration Agent 最终修改。子 Agent 先提交报告、补丁或可复现命令，避免同时编辑同一文件。

## 2. Docker 空间治理：最高优先级，必须立即执行

### 2.1 已知当前资源状态

本轮开始时已核对：

- `oncall-agent` 正在使用约 `10.7 GB` 的项目镜像，不能删除；
- `g6-ignore-probe-current:latest` 是本项目临时探针镜像，已删除，释放约 `186 MB`；
- `mact-*`、`personal_design-*`、Milvus、MinIO、etcd 资源不属于本轮清理范围；
- Docker build cache 当前为 `0 B`。

后续每次开始工作前都重新执行：

```powershell
docker system df -v
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Labels}}"
docker images --format "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}"
```

### 2.2 代码修改期间禁止构建镜像

严格按以下顺序：

1. 先完成代码、配置、测试和文档修改；
2. 修改期间不得为了验证一个小改动反复 `docker build`；
3. Python/前端/配置检查优先使用本机环境；
4. 所有代码修改完成后只进行一次候选镜像构建；
5. 构建后只启动候选镜像进行真实依赖验收；
6. 验收结束立即删除旧的、不再使用的本项目镜像、探针容器和临时容器。

不要在代码尚未稳定时构建 10 GB 级镜像。不要为了绕过构建错误创建长期保留的 Dockerfile 变体或临时镜像 tag。

### 2.3 镜像命名和构建要求

候选镜像必须使用可追溯 tag，例如：

```text
on_call_agent-app:<git-short-sha>
```

Compose 必须通过 `APP_IMAGE` 显式使用候选镜像，并记录：

- Git revision；
- image ID/digest；
- Dockerfile hash；
- 构建开始和结束时间；
- 是否使用 `--pull`；
- 是否使用外部代理；
- 构建是否为标准仓库 Dockerfile。

未完成代码修改前禁止执行以下动作：

- `docker compose build`；
- `docker build`；
- `docker buildx build`；
- 任何会产生完整应用镜像的探针构建。

### 2.4 清理规则

每次验收结束必须输出本项目资源清单，并只删除已确认属于本项目且没有容器引用的资源：

```powershell
docker ps -a --filter label=com.docker.compose.project=on_call_agent
docker images --no-trunc
docker system df -v
```

允许删除：

- 本项目已停止且不再使用的临时容器；
- 本项目无容器引用的旧候选镜像；
- 本项目临时探针镜像；
- 本项目构建失败留下的中间资源，且已核对 image/container 归属。

禁止执行：

- `docker system prune -a`；
- `docker image prune -a`；
- `docker volume prune`；
- 未核对归属的批量删除；
- 删除 `mact-*`、`personal_design-*` 或其他项目资源；
- 删除仍被健康容器使用的当前镜像；
- 为了节省空间删除 Milvus 数据卷、项目上传卷、日志卷或评测结果卷。

清理完成后记录清理前后 `docker system df`，并说明释放空间大小。删除动作必须先列清单，再执行删除，再复核容器健康状态。

## 3. P0：AutoDL 官方文档真正入库

### 3.1 固定官方来源

只抓取以下来源，不递归站点，不接受用户输入 URL：

- `https://www.autodl.com/docs/env/`
- `https://www.autodl.com/docs/quick_start/`
- `https://www.autodl.com/docs/image/`
- `https://www.autodl.com/docs/instance_data/`
- `https://api.autodl.com/docs/gpu/`
- `https://www.autodl.com/docs/scp/`
- `https://www.autodl.com/docs/qa/`
- `https://www.autodl.com/docs/fs/`
- `https://www.autodl.com/docs/instance_pro_api/`

### 3.2 先修代码阻塞，再抓取

必须修复并加直接回归：

1. `scripts/fetch_corpus.py` 白名单加入 AutoDL 官方域名，并继续校验重定向目标；
2. manifest 路径不再硬编码旧日期，normalize 必须读取实际本轮 manifest；
3. metadata 允许保存 `target_kind`、`source_kind`、`document_id`、`revision`、`chunk_id`；
4. `RetrievalFilters` 增加 `target_kind`，并贯通到 Milvus 检索表达式；
5. 过滤条件缺失或非法时返回明确错误，不退化为不过滤；
6. AutoDL 语料与旧腾讯云语料保留独立 product/revision。

### 3.3 语料产物

目录至少包含：

```text
knowledge/sources/autodl/
  raw/
  normalized/
  notices/
  manifest.json
```

每个来源保存 URL、抓取时间、HTTP 状态、content type、字节数、原文 SHA-256、revision、license 状态和正文抽取说明。正文必须从 `<article>` 或等价正文容器提取，不能把侧边栏导航当作知识正文。

每个 chunk 至少包含：

```text
product=autodl
os_family=linux
target_kind=ssh
source_kind=manual
product_version
source_uri
document_id
revision
chunk_id
valid_from
valid_until
license_id
```

### 3.4 RAG 真实验收

导入 Milvus 后核对：

- active revision；
- document 数量；
- chunk 数量；
- collection 维度和 descriptor；
- 更新失败零残留；
- 回滚后 active revision 恢复；
- `target_kind=ssh` 过滤只返回 AutoDL/Linux 相关文档；
- 引用卡片包含 revision、locator 和 source URL。

文档知识只能解释平台操作和处置方法，不能代替当前实例的实时 CPU、内存、磁盘、GPU 或日志证据。

## 4. P0：目标配置和本地 adapter 路由

### 4.1 目标字段

`services.json` 的 AutoDL 条目必须明确：

- `service_name=autodl-ssh`；
- `type=ssh`；
- `product=autodl`；
- `ssh_host`；
- `ssh_port`；
- `region`；
- 不包含 key、密码、token 或本机绝对密钥路径。

`Target` 必须消费 `kind`、`product`、`ssh_host`、`ssh_port`。目标绑定后，工具参数不能覆盖这些字段。

### 4.2 运行配置

在本机 `.env` 或部署环境中配置并只记录 set/unset 状态：

- `LOCAL_LLM_BASE_URL`；
- `LOCAL_LLM_MODEL`；
- `LOCAL_LLM_MODEL_SSH=qwen-autodl`；
- `SSH_HOST`；
- `SSH_PORT`；
- `SSH_USER`；
- `SSH_KEY_FILE`。

AutoDL SSH 目标必须路由到 `qwen15-autodl-ssh-v1`，其他目标继续使用通用 adapter。未配置专用模型时应明确显示“AutoDL 专用模型未配置”，不能默默宣称已经使用专用 adapter。

## 5. P1：修复 AutoDL 容器观测口径

必须针对真实容器验证并修复：

1. `get_system_info` 的 CPU、内存、进程限制优先读取 cgroup；
2. `free` 显示的宿主机内存不能作为容器可用内存；
3. `check_disk_usage` 的默认路径必须适配 Linux，不能默认 `C:/`；
4. 真实数据盘 `/root/autodl-tmp` 应作为 AutoDL 目标可选路径；
5. `search_system_logs` 支持当前环境的 journal 目录或明确返回“不支持/路径不存在”；
6. `/var/log/syslog`、`/var/log/auth.log` 不存在时必须返回 unavailable，不能生成假日志；
7. GPU 不存在时明确返回 unavailable，不能把 CPU 容器写成 GPU 实例；
8. 观测证据标记 `source=ssh`、`live=true`、目标 host/port 摘要和 duration。

## 6. P1：真实 AutoDL 双模式同题评测

必须先完成第 3、4、5 节，再进行对比。固定同一批题目和同一目标：

- 本地：`qwen15-autodl-ssh-v1`；
- 在线：`cloud_multi`；
- 目标：当前授权 AutoDL SSH 主机；
- 操作：只读工具；
- 题集：单独命名、固定 SHA-256、训练/测试按模板隔离。

至少覆盖：CPU 高、内存/OOM、磁盘满、进程异常、日志查询、系统信息、知识库问答、无答案问题、GPU 不可用。

分别记录：

- p50/p95 延迟；
- 输入/输出 token；
- plan tool recall、tool F1、参数 exact；
- 真实 SSH 工具成功率；
- RAG 引用支持率；
- 任务完成率；
- 失败原因。

模型规划错误、SSH 权限错误、路径不存在、RAG 无答案和 AutoDL 资源不可用必须分开统计，不能混成一个失败率。

## 7. 最终验收门槛

本轮只有满足以下条件才可写“任务完成”：

- AutoDL 官方文档已实际写入独立 RAG revision；
- `target_kind=ssh` 过滤在真实 Milvus 上生效；
- `services.json` 有明确 port/product；
- `.env`/部署配置实际选择 AutoDL adapter；
- 真实 AutoDL SSH 只读链路可复现；
- 本地和 cloud_multi 已对同一批 AutoDL 题集完成对比；
- 观测数据使用 cgroup 和实际 Linux 日志能力；
- Docker 构建只发生在代码修改完成后；
- 旧镜像、探针和临时容器已按归属清理；
- 清理前后 Docker 空间有记录；
- 所有报告与 Git revision、数据 hash、镜像 digest 一致。

以下情况只能标记 `ANALYZED` 或 `UNVERIFIED`：

- 只抓到网页但没有入 Milvus；
- 只有离线合成题集，没有真实 AutoDL 同题；
- 只有模型输出截图，没有工具执行证据；
- 只配置了路由代码，没有实际模型服务配置；
- 用旧腾讯云 benchmark 代表 AutoDL；
- 只运行测试替身，没有真实依赖验收。

## 8. 禁止事项

1. 不恢复腾讯云监控工具作为 AutoDL 能力；
2. 不把手册命令当成已执行的服务器观测；
3. 不把泄漏题集或常数基线不占优的分数写成模型泛化能力；
4. 不使用无限重试或大规模防御性编程掩盖根因；
5. 不在代码修改期间反复构建 10 GB 级镜像；
6. 不运行全局 Docker prune；
7. 不删除 `mact-*`、`personal_design-*`、Milvus 或其他项目资源；
8. 不删除仍被运行容器使用的镜像或持久化卷；
9. 不把真实密钥、密码、代理、host 配置或完整环境变量写入报告；
10. 未获授权时不执行远程安装、重启、删除、写配置等操作。

## 9. 最终交付文件

```text
docs/claude-code-handoff/AUTODL_CORPUS_REPORT.md
docs/claude-code-handoff/AUTODL_RAG_REPORT.md
docs/claude-code-handoff/AUTODL_TARGET_REPORT.md
docs/claude-code-handoff/AUTODL_RUNTIME_REPORT.md
docs/claude-code-handoff/AUTODL_LIVE_BENCHMARK_REPORT.md
docs/claude-code-handoff/DOCKER_SPACE_REPORT.md
docs/claude-code-handoff/AUTODL_SECURITY_CLAIMS_AUDIT.md
docs/claude-code-handoff/AUTODL_PHASE_RESULT_YYYY-MM-DD.md
```

最终总报告必须明确写出：已完成、未完成、证据路径、复现命令、Docker 清理前后空间，以及哪些结论不得作为对外主张。
