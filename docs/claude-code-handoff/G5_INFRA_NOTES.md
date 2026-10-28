# G5 工程维护实测记录（Pydantic / Ruff / Docker / CI / MCP 同名实例）

本文件只记录**本轮真实执行过**的命令与结果。没有跑过的（例如 `--pull --no-cache`
这一具体参数组合、GitHub Actions 上的 job、真实浏览器点选）显式标注“未实测”，
不写估计值冒充测量值。

- 工作区：`E:/On_Call_Agent/on_call_agent`，Windows 11，Git Bash。
- 解释器：`./.venv/Scripts/python.exe`（Python 3.13.5，pydantic 2.12.5 → 期间被并行
  进程改动，见第 6 节）、ruff 0.17.1、Docker 29.8.2（desktop-linux，BuildKit 代理
  `http.docker.internal:3128`）、Docker Compose v5.6.0。
- 已核对基线镜像：`on_call_agent-app:latest` = `sha256:7788efe302394b1225c96aee1bf804da8cde7399e8a781d6d2f13dde090a4eb9`，
  未压缩 `18868738945` 字节（18.9GB），即 `DOCKER_ACCEPTANCE_2026-10-27.md` 记录的那个。

---

## 1. Pydantic：`class Config` → `ConfigDict`

改动文件：`app/models/aiops.py`（`AIOpsRequest`、`DiagnosisResponse`）。

迁移前实测（`-W default`）：

```
app\models\aiops.py:56  PydanticDeprecatedSince20: Support for class-based `config` is deprecated ...
app\models\aiops.py:120 PydanticDeprecatedSince20: Support for class-based `config` is deprecated ...
```

迁移后语义等价性验证（把 HEAD 版本导出到临时目录，与新版本在同一进程里比对）：

```
AIOpsRequest schema identical: True
  old cfg: {'json_schema_extra': {'example': {'session_id': 'session-123'}}}
  new cfg: {'json_schema_extra': {'example': {'session_id': 'session-123'}}}
DiagnosisResponse schema identical: True
  old cfg == new cfg（示例 JSON 逐字一致）
```

`model_json_schema()` 的键集合也一致（`$defs/description/example/properties/title/type`），
说明 `json_schema_extra` 仍然按原样注入 OpenAPI 的 `example` 字段。

回归（`-W default`，迁移后）：

```
./.venv/Scripts/python.exe -m pytest tests/test_typed_plan.py tests/test_aiops_target_api.py \
    tests/test_p0_cloud_pipeline.py -q -o addopts="" -W default
35 passed, 1 warning in 9.79s
```

migration 后该警告消失；剩下的 1 条是第三方 `langgraph`
（`LangChainPendingDeprecationWarning: allowed_objects`）与一条 `ResourceWarning`，
**不是我们的代码**。仓库里没有其它 `class Config`（`grep -rn "\.Config\b"` 无命中）。

---

## 2. Ruff 配置迁移

`pyproject.toml` 的变化只在**位置**上：`select/ignore` → `[tool.ruff.lint]`，
`per-file-ignores` → `[tool.ruff.lint.per-file-ignores]`，`isort` → `[tool.ruff.lint.isort]`。
`exclude` 属于文件发现，留在 `[tool.ruff]`。规则集合与忽略项逐字未变。

迁移前后的对比（用 `git show HEAD:pyproject.toml` 作为 `--config` 跑同一批文件）。

⚠️ 工作区在测量期间被其它并行 agent 持续修改，**绝对条数会漂移**；验收依据是
“同一时刻前后两次结果完全相同”，不是某个固定数字。两次独立测量：

| 时刻 | 命令 | 迁移前（HEAD 配置） | 迁移后（新配置） |
|---|---|---|---|
| 23:5x | `--select E,F,I` | Found **141** errors | Found **141** errors |
| 02:1x | `--select E,F,I` | Found **140** errors | Found **140** errors |
| 02:1x | `--select F821,F811,F841` | All checks passed | All checks passed |
| 23:5x / 02:1x | 配置默认 | Found **356** / **353** errors | Found **356** / **353** errors |

`--output-format concise` 的逐行 diff 也完全相同（`sort` 后 `diff` 无输出）：

```
142 before.txt
142 after.txt
IDENTICAL E,F,I output
diff lines: 0
```

`top-level linter settings are deprecated` 警告消失：

```
./.venv/Scripts/python.exe -m ruff check app --statistics 2>&1 | grep -c "top-level linter settings are deprecated"
0
```

### 关于 `I`（isort）规则

`I` 在迁移前后都处于**启用**状态（原本就在 `select` 里），迁移只是位置变化；
既有代码本来就有 23 条 `I001`（未排序 import，`--select I --statistics` 实测），
所以“启用 I”不是本轮新增的负担。
本轮**没有**跑 `ruff check --fix` 重排 import：

- 那会产生 23 处与本任务无关的 diff，混进并行 agent 的改动里无法审阅；
- 排序会改动 `app/models/aiops.py`、`servers/monitor_server.py` 等我负责的文件，
  但也会改动别人的文件，违反“只改自己的文件”。

结论：**启用 I 的“真正格式化”需要单独一轮**，命令是
`ruff check app servers scripts tests --select I --fix`，建议独立提交。
本轮保留现状，只做配置迁移（验收标准是“不比迁移前更差”，实测完全一致）。

我改动的两个 Python 文件在迁移前后 ruff 结果也一致：`app/models/aiops.py` 始终是
`I001 + F401(typing.List)`（既有问题，未新增），`servers/monitor_server.py` 始终是
17 条（`I001×2 + F541×5 + E501×10`，行号因新增代码整体后移，类别与数量不变）。

---

## 3. Docker / CI：防止验收陈旧镜像

### 3.1 CI（`.github/workflows/ci.yml`）

新增 `docker` job：

- 触发条件：`push` 到默认分支，**或** `workflow_dispatch` 且手动勾选 `build_image`。
  PR 一律不跑（18GB 级构建每个 PR 跑一次没有收益）。
- 构建命令固定为 `docker build --pull --no-cache -t oncall-agent:${GITHUB_SHA} .`：
  `--pull` 拉基础镜像最新、`--no-cache` 不复用任何层、tag 用 commit sha 而不是 `latest`。
- 构建后用 `docker image inspect` 把 `Id` / `Size` / `RepoDigests` 写进
  `$GITHUB_STEP_SUMMARY`，作为“本次提交到底产出哪个镜像”的证据。

YAML 合法性（本地真实解析）：

```
./.venv/Scripts/python.exe -c "import yaml; d=yaml.safe_load(open('.github/workflows/ci.yml',encoding='utf-8')); print(list(d['jobs']))"
['test', 'docker']
```

`docker image inspect` 的三个模板串在**本机真实镜像**上验证过输出：

```
./.venv/Scripts/python.exe / bash 实际执行:
  镜像已构建: on_call_agent-app:latest
    image id : sha256:7788efe302394b1225c96aee1bf804da8cde7399e8a781d6d2f13dde090a4eb9
    size     : 18868738945 bytes
    digest   : on_call_agent-app@sha256:7788…,oncall-agent-acceptance@sha256:7788…
```

`{{join .RepoDigests ","}}` 这个模板串在本机 docker 29.8.2 可用。

**未实测**：GitHub Actions 上真实跑这个 job（需要 push 到远端；本任务不 commit 不 push）。

### 3.2 Makefile：`docker-build-fresh`

新增目标（已加入 `.PHONY`）：

```
make docker-build-fresh
# 实际展开：docker build --pull --no-cache -t oncall-agent:$(date +%Y%m%d-%H%M%S) .
# 随后打印 image id / size / digest，构建失败直接 exit 1，不留半成品 tag
```

本机没有 `make`（`make: command not found`），所以**不能**直接跑这个目标；我把
recipe 的 shell 体原样抽出来用 `bash -c` 执行过一遍，引号与 `{{...}}` 模板都正常
（输出见 3.1 末）。构建前端产物需要 npm registry 与基础镜像可达，构建失败会让目标
非 0 退出——这是刻意的，不允许“打印一个旧 tag 当成功”。

### 3.3 docker-compose.yml

只在 `app` 服务定义**上方**加了注释，说明：
`docker compose up` 会复用同名旧镜像；重新验收要 `make docker-build-fresh` +
`--force-recreate`，或按 digest 部署；不要用 `latest` 作为验收对象。
**没有**添加 `image:` 字段（保持默认行为不变）。

合法性验证：

```
docker compose config --quiet
exit=0

./.venv/Scripts/python.exe -c "import yaml; d=yaml.safe_load(open('docker-compose.yml',encoding='utf-8')); print(list(d['services']))"
['etcd', 'minio', 'milvus', 'attu', 'app']
app build: {'context': '.', 'dockerfile': 'Dockerfile'}
volumes: ['oncall-uploads', 'oncall-logs', 'oncall-eval-results', 'oncall-data']
```

---

## 4. 镜像体积：实测构成（基线镜像）

用 `docker run --rm --entrypoint sh on_call_agent-app:latest -c 'du -sm ...'` 在
**已存在的基线镜像**里测量（真实数字，非估计）：

### 4.1 `/app` 顶层

| 路径 | 大小 |
|---|---|
| `/app/.venv` | **6673 MB** |
| `/app/.venv-train` | **5139 MB** |
| 其余（knowledge、docs、web、app、tests…） | < 20 MB |

`/app` 合计 `11831 MB`（`du` 口径）。

### 4.2 `.venv` 里前 10 大依赖

| 包 | MB |
|---|---|
| nvidia | **3196** |
| torch | **1182** |
| triton | **897** |
| tencentcloud | 390 |
| transformers | 122 |
| scipy | 113 |
| sympy | 80 |
| pandas | 76 |
| sklearn | 51 |
| numpy | 45 |

nvidia 子目录：`cu13` 1732 / `cudnn` 922 / `nccl` 242 / `cusparselt` 224 / `nvshmem` 78。
torch 的 `libtorch_cuda.so` 单文件 480MB、`libtorch_cpu.so` 426MB。

判定：**镜像里的 torch 是 CUDA 版**（`torch 2.14.1+cu130`，`torch.version.cuda == 13.0`），
并且 `uv.lock` 里 `torch` 的 linux 依赖带 `cuda-bindings` / `cuda-toolkit[...]` /
43 个 `nvidia-*` 以及 `triton`。应用代码只用 CPU（
`app/services/vector_embedding_service.py` 的 `model_kwargs={"device": "cpu"}`，
全仓库没有 `device="cuda"`），所以 CUDA 运行时在镜像里是纯负担。

### 4.3 两个真实可优化项（本轮已做/可执行的）

**(a) `.venv-train` 被 `COPY . .` 打进了镜像——本轮已修，并实测。**

`/app/.venv-train/pyvenv.cfg` 内容 `home = E:\anaconda3`、只有 `Lib/` 和 `Scripts/`，
是**宿主 Windows 的 venv**，在 Linux 容器里既跑不起来也用不到；`.dockerignore` 之前
只排除了 `.venv/`，没排除它。已在 `.dockerignore` 加 `.venv-train/`。

实测（不重建整个镜像，只量 `COPY . .` 这一层）：

```
# 改前（git HEAD 的 .dockerignore）
Sending build context to Docker daemon  5.326GB
（COPY 层：构建在 500s 超时内没完成，仅用于量 context）

# 改后
Sending build context to Docker daemon  18.53MB   ← `docker build` 真实输出
docker build -f <只含 COPY . . 的探针 Dockerfile> -t copy-probe:new .
docker history copy-probe:new
20.4MB  /bin/sh -c #(nop) COPY dir:…
```

即：**构建上下文从 5.33GB 降到 ~19MB，镜像里 `COPY . .` 这一层从 5.41GB 降到 20–23MB**。
基线镜像的 `docker history` 也印证了这 5.41GB 就是 `COPY dir:… in .` 那一层；
4.4 节完整重建出来的镜像里该层是 **22.9MB**（略大于探针，因为探针构建时还没加
`services.staging.json` 与本文件）。

**(b) CUDA 版 torch（约 3.2GB nvidia + 0.9GB triton + 0.5GB libtorch_cuda）**
**未实做**（不改 `uv.lock`）。给出的是**在临时工程里实测过的**可行配方：

- `uv` 0.12.7 的 `UV_TORCH_BACKEND=cpu` **只对 `uv pip install` 生效**
  （`uv pip install --help` 有 `--torch-backend`，`uv sync --help` 没有）。
  实测：`UV_TORCH_BACKEND=cpu uv sync --dry-run` 与不设置时输出完全一样。
- `[tool.uv] torch-backend = "cpu"` 对本仓库**无效**：`uv.lock` 是 `--frozen` 使用的，
  而且 torch 不是直接依赖，`torch` 的解析结果没有变化（实测 nvidia 仍是 43 条）。
- **实测有效的做法**（在临时目录复制 `pyproject.toml`+`uv.lock` 后验证）：
  1. 把 `torch==2.14.1` 写成**直接依赖**（只有直接依赖能被 `[tool.uv.sources]` 重定向）；
  2. 加 `[[tool.uv.index]] name="pytorch-cpu" url="https://download.pytorch.org/whl/cpu" explicit=true`
     和 `[tool.uv.sources] torch = { index = "pytorch-cpu" }`；
  3. `uv lock`。

  结果：`torch-2.14.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl`，
  `nvidia-*` 从 43 条降到 **0**，`triton` 从 2 条降到 **0**，包总数 241 → 209。
  CPU wheel 实测 `Content-Length: 196227172`（187MB），对比 CUDA wheel 的 554581699（529MB）。

  预计节省 ≈ 3196(nvidia) + 897(triton) + (1182−~250 的 torch 差额) ≈ **4.9GB**。
  但这会让 `uv.lock` 大幅变化、影响所有平台的解析结果，属于跨文件/跨平台决策，
  本轮**没有**改 `uv.lock`（不在文件所有权内），列入 cross_file_requests。

**(c) 离线能力**：镜像里**没有**烘焙任何模型权重（
`find /app -name '*.safetensors' -o -name 'pytorch_model.bin'` 无结果），
embedding 模型靠 `docker-compose.yml` 的
`${HF_CACHE_DIR:-~/.cache/huggingface}:/root/.cache/huggingface` 挂载提供，
`LazyLocalEmbeddings` 用 `local_files_only=True` fail-closed。
所以 (a)(b) 两项都**不削减离线能力**：模型仍在宿主机缓存里，第一次 embedding
不联网（若缓存缺模型会显式报错，不会偷偷下载）。

### 4.4 完整重建验证（实测，最终成功）

本轮共尝试 4 次 `DOCKER_BUILDKIT=0 docker build -t oncall-agent:g5-dockerignore .`
（含 `web` 阶段的 `npm ci`/`npm run build`）：

- 第 1 次：前端 `tsc -b` 在我构建时失败（`web/src/chat.test.ts:348/377` 的
  `fetch` mock 类型不兼容），随后并行 agent 修好了该文件，之后前端阶段通过；
- 第 2 次：daemon 报 `failed to write compressed diff: lstat .../site-packages/tencentcloud/lke: no such file or directory`（中间镜像层抓取竞态）；
- 第 3 次：`uv sync` 被 SIGKILL（exit 137）。本机当时只剩 ~2GB 可用物理内存，
  Docker VM 上限 7.65GiB，同时有其它 agent 的 pytest/构建在跑；
- 第 4 次：**成功**。

```
Successfully built 5ee53cccceda
Successfully tagged oncall-agent:g5-dockerignore
exit=0

docker image inspect oncall-agent:g5-dockerignore --format '{{.Id}} {{.Size}}'
sha256:5ee53cccceda6e350ecafd4d90ab394207415d8b28d39173d194c585aba82b78 10679031916

docker system df -v | grep g5-dockerignore
oncall-agent  g5-dockerignore  5ee53cccceda  10.7GB  10.68GB  57.7kB  0
```

| 项 | 基线 `on_call_agent-app:latest` | 本次 `oncall-agent:g5-dockerignore` |
|---|---|---|
| image id | `sha256:7788efe30239…` | `sha256:5ee53cccceda…` |
| `docker images` 大小 | 18.9GB | **10.7GB** |
| `.Size`（字节） | 18868738945 | **10679031916** |
| 层数 | 12 | 12 |
| 层大小合计（history） | 12.61 GB | 7.21 GB |
| `COPY . .` 层 | 5.41GB | **22.9MB** |
| `/app`（容器内 du） | 11831MB | **23MB**（.venv 单独 6665MB，已不再有 `.venv-train`） |

`.Size` 字节数差 **−8.19GB（−43.4%）**；history 层大小合计差 −5.40GB
（两个口径的差来自 `docker images` 把未压缩的共享基础层也计入，以及历史层压缩/删除
在不同 daemon 版本上的统计方式；这里两个数字都按原样给出，不做归一）。

容器内验证（真实执行，非推断）：

```
docker run --rm --entrypoint sh oncall-agent:g5-dockerignore -c 'cd /app && AIOPS_SERVICES_FILE=services.staging.json python -c "..."'
catalog: /app/services.staging.json
rows: ['agent-test-server', 'staging-payment-api', 'staging-payment-api']

python -c "with warnings.catch_warnings(): warnings.simplefilter('error'); AIOpsRequest(); DiagnosisResponse(data={})"
models ok, no deprecation warning

ls -d /app/.venv-train
ls: cannot access '/app/.venv-train': No such file or directory
```

即新镜像同时验证了三件事：`.venv-train` 已不在镜像里、staging 目录切换在容器内可用、
Pydantic 弃用警告在镜像内的运行时也消失。

**仍未验证**：`--pull --no-cache`（本轮为节省 20 分钟与内存用了层缓存，`COPY`/`uv sync`
两层是真实重跑的）；`docker compose up` 端到端起容器后的 /health。
`--pull --no-cache` 的等价命令已固化进 CI 与 `make docker-build-fresh`，但本轮没有实际执行
那条 18GB 全量重建。

---

## 5. MCP 同名双实例目录

### 5.1 改动

`servers/monitor_server.py`：

- 保留 `SERVICES_FILE`（默认仓库根 `services.json`）作为**默认值**；
- 新增 `SERVICES_FILE_ENV = "AIOPS_SERVICES_FILE"` 和 `services_file_path()`：
  `read_env("AIOPS_SERVICES_FILE", "")` 覆盖，相对路径按**仓库根**解析而不是 CWD
  （MCP server 由 entrypoint 从不同目录拉起，CWD 不可依赖）；
- `_get_service_list()` 改为每次调用 `services_file_path()`，日志里打印真实路径。
- 每次调用重新读，而不是 import 时固化：`read_env` 不写 `os.environ`，
  进程起来后仍可切换目录做演练。

默认行为不变（实测）：

```
./.venv/Scripts/python.exe -c "from servers import monitor_server as m; print(m.services_file_path())"
E:\On_Call_Agent\on_call_agent\services.json
list_all_services().fn() -> total: 1 ['agent-test-server']
```

### 5.2 `services.staging.json`（新增，未改 `services.json`）

| service_name | instance_id | region | type |
|---|---|---|---|
| `agent-test-server` | `lhins-nst2h9mv` | `ap-guangzhou` | lighthouse（原有那一条） |
| `staging-payment-api` | `ins-staging-a-0001` | `ap-guangzhou` | cvm（staging 夹具） |
| `staging-payment-api` | `ins-staging-b-0002` | `ap-shanghai` | cvm（staging 夹具） |

`_comment` 里写明：这是演练夹具，instance_id 不对应真实云资源，拿它查腾讯云只会
得到 API 报错或 `unavailable`，不会产生伪造监控数据。

### 5.3 真实执行验证

```
$ AIOPS_SERVICES_FILE=services.staging.json ./.venv/Scripts/python.exe -c "..."
path: E:\On_Call_Agent\on_call_agent\services.staging.json
count: 3
list_all_services().fn() -> staging total: 3
   agent-test-server lhins-nst2h9mv ap-guangzhou
   staging-payment-api ins-staging-a-0001 ap-guangzhou
   staging-payment-api ins-staging-b-0002 ap-shanghai
region filter ap-shanghai: 1
```

目标解析（复用 `app/core/targets.py`，只读，不改别人的文件）：

```
resolve_target(rows, 'staging-payment-api', '')
status: target_required
  candidate: staging-payment-api ins-staging-a-0001 ap-guangzhou tgt-ee0bb5356eafba8a
  candidate: staging-payment-api ins-staging-b-0002 ap-shanghai  tgt-91643f7c27919806
resolve_target(rows, None, '', candidate='tgt-91643f7c27919806')
bound: bound ins-staging-b-0002 ap-shanghai
```

即：**同名双实例会进 `target_required`，点击第二个候选后精确绑定到
`ins-staging-b-0002 / ap-shanghai`**，不会回退到服务名。

CWD 无关性（在 `/tmp` 下用绝对 PYTHONPATH 启动）：

```
cwd-independent path: E:\On_Call_Agent\on_call_agent\services.staging.json
rows: ['agent-test-server', 'staging-payment-api', 'staging-payment-api']
```

不存在的文件（fail-soft，返回空列表而非伪造）：

```
AIOPS_SERVICES_FILE=/nonexistent/services.json
path: C:\Program Files\Git\nonexistent\services.json   # Git Bash 把 /… 当路径转换，属宿主 shell 行为
rows: []
```

（这里是 Git Bash 的 MSYS 路径转换；在容器/Linux 下 `/nonexistent/...` 会原样保留。
两种情况都返回空列表并记 warning，行为符合预期。）

### 5.4 一个真实发现：`planner._guess_service` 不吃这个环境变量

`app/agent/aiops/planner.py:96` 的 `_guess_service` 自己拼路径读仓库根 `services.json`
（`parents[3] / "services.json"`），**不经过** `services_file_path()`。实测：

```
AIOPS_SERVICES_FILE=services.staging.json
_guess_service('staging-payment-api cpu 高')  ->  'unknown'
_guess_service('agent-test-server 异常')      ->  'agent-test-server'
```

影响：本地小模型快路径（`try_local_plan`）拿到的 service 提示对 staging 服务是
`unknown`。这只影响**本地模式**的服务名先验，不影响 `list_all_services` 返回的目录，
也不影响 `app/core/targets.py` 的候选解析（后者用的是 MCP 工具返回的行）——
浏览器验证走 `cloud_multi` 时路径不经过它。为不改别人的文件，这里只记录并列入
cross_file_requests：若要本地模式也支持 staging，应把该处改为复用同一个
`services_file_path()`（或直接读 MCP 的 `list_all_services`）。

### 5.5 需要补的单测（不在我的文件所有权内）

`tests/test_server_env_isolation.py` 建议追加（列入 cross_file_requests）：
`AIOPS_SERVICES_FILE` 覆盖后 `services_file_path()` 指向新文件、`_get_service_list()`
返回 3 条；未设置时仍是仓库根 `services.json` 且只有 1 条。

---

## 6. 环境干扰（影响上述部分数据的强度，必须记录）

测量期间有**其它并行 agent 在写 `.venv`**：

- 01:08–01:38 之间 `.venv/Lib/site-packages` 被反复改写：`fastmcp` 从 2.14.5
  升到 **3.4.0**（`fastmcp-3.4.0.dist-info` + `fastmcp_slim-3.4.0.dist-info`），
  `pydantic` 从 2.13.4 变成 **2.12.5**；
- 期间出现过 `ImportError: cannot import name 'PrivateKeyJWTClientAuthenticator'`
  与 `ImportError: cannot import name '__version__' from 'pydantic_core'`，
  都是“包装到一半”的中间态，不是我的改动导致；
- 环境稳定后重跑的测试全部通过（第 1、5 节的输出即稳定后结果）。

`uv.lock` 里 `fastmcp` 仍是 2.14.5；venv 与锁文件此刻不一致，属于并行进程的副作用，
不在本任务范围内，但会干扰“按锁文件复现”的结论，故记录。

---

## 7. 主 agent 后续真实浏览器验证所需命令

目标：在两个同名实例上真实点选第二个候选，验证 `target_candidate_id` 绑定。

```bash
cd E:/On_Call_Agent/on_call_agent

# 0) 确认当前没有旧进程占端口（8003/8004/8005/9900）
netstat -ano | grep -E ':(8003|8004|8005|9900)\s' || echo "ports free"

# 1) 先构建前端（app/main.py 只在 web/dist/assets 存在时挂载静态资源）
cd web && npm ci && npm run build && cd ..

# 2) 用 staging 目录启动 Monitor MCP（同名双实例就在这个文件里）
AIOPS_SERVICES_FILE=services.staging.json \
  ./.venv/Scripts/python.exe servers/monitor_server.py &
# 期望日志：监听 127.0.0.1:8004/mcp；list_all_services 返回 3 条

# 3) CLS / SSH MCP（与 monitor 同样的启动方式，端口 8003 / 8005）
./.venv/Scripts/python.exe servers/cls_server.py &
./.venv/Scripts/python.exe servers/ssh_server.py &

# 4) 主应用
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9900
# 或：./.venv/Scripts/python.exe servers/web_server.py

# 5) 浏览器
#    http://127.0.0.1:9900/
```

浏览器里要做的操作（这是验收点，不是“页面能打开”就算过）：

1. 模式选“在线 API 多智能体”（`cloud_multi`），输入症状里**只提服务名**
   `staging-payment-api`（不提实例/地域）；
2. 期望出现 `target_required` 候选卡片，**两张**，分别显示
   `实例：ins-staging-a-0001 / 地域：ap-guangzhou` 与
   `实例：ins-staging-b-0002 / 地域：ap-shanghai`；
3. 点**第二张**（`ins-staging-b-0002` / `ap-shanghai`）；
4. 期望：请求体里带 `target_candidate_id=tgt-91643f7c27919806`，运行结果的目标
   是 `ins-staging-b-0002 / ap-shanghai`，且后续工具调用的 `instance_id`/`region`
   都被绑定成该实例（不是列表里第一个）。

注意：这两个实例是 staging 夹具，真实腾讯云监控查询会 **失败/返回 unavailable**，
这是预期的（证明没有伪造数据）。要看“选对了目标”只需看目标绑定与工具参数。

若要回到默认单实例目录：不设置 `AIOPS_SERVICES_FILE` 重启 monitor 即可，
`services.json` 未被修改（`git status` 里没有它的变更）。

---

## 8. 本轮未做/未测（明确清单）

| 项 | 状态 | 原因 |
|---|---|---|
| 完整重建（第 4 次，成功） | **已实测** | 10.7GB / `sha256:5ee53cccceda…`；见 4.4 |
| `--pull --no-cache` 这一具体参数组合 | **未实测** | 本轮为省 20 分钟与内存用了层缓存（`COPY`/`uv sync` 两层仍是真实重跑）；等价命令已固化进 CI 与 `make docker-build-fresh` |
| `uv.lock` 改为 CPU torch | **未做** | 需要改锁文件（不在所有权内），且影响全平台解析；配方已实测可行（第 4.3b） |
| GitHub Actions 上真实跑 docker job | **未实测** | 不 commit / 不 push |
| `make docker-build-fresh` 直接执行 | **未实测** | 本机没有 `make`；recipe 的 shell 体已单独执行验证 |
| `ruff --select I --fix` 全局重排 import | **未做** | 会产生 23 处无关 diff，应单独一轮 |
| `tests/test_server_env_isolation.py` 追加断言 | **未做** | 文件不在所有权内，列入 cross_file_requests |
| 真实浏览器点选 | **未做** | 属于主 agent 的 G5-3 任务；第 7 节给了可执行命令与期望值 |
| `docker compose up` 起容器后的 /health | **未实测** | 新镜像只做了 `docker run --rm` 的一次性 smoke（第 4.4）；起容器会占端口并影响并行 agent |

---

## 9. 主 agent 收尾补记（2026-10-28）

以下是本文件写完之后、由主 agent 在本轮内完成的两件事。**第 1–8 节的原始记录保留**，
包括它们标注为“未做/未实测”的项——这里只补新增证据，不改写历史结论。

### 9.1 最终镜像（从**最终工作区**重建）

第 4.4 节那次成功重建对应的工作区还缺少后续的 chat 事件接线/串口改动，
镜像是 `sha256:5ee53cccceda…`。收尾时从最终工作区又完整重建了一次：

| 项 | 值 |
|---|---|
| 命令 | `DOCKER_BUILDKIT=0 docker build -t oncall-agent:g5-final .` |
| 结果 | `Successfully built d93d673ee7d2`，28 步，`exit=0` |
| image id | `sha256:d93d673ee7d22b9048a4285c0afcd2ed9ae7eb3b8c8ed5772ad263dbaf777bf6` |
| 大小 | `10 680 648 987` 字节（10.7 GB；基线 18.9 GB，−43.4%） |
| 容器内核对 | `/app/.venv-train` **不存在**；`/app/.venv` 6665 MB；`chat_events` / `visible_documents` / `AIOPS_SERVICES_FILE` 均在镜像代码里 |
| 起容器 | `CONTAINER_AUTH_MODE=dev docker compose up -d --force-recreate app` → `healthy`，`/health` 200；`GET /api/chat/events/<run>?session_id=…` 在容器内返回 `seq [1,2]` |

第 8 节“`docker compose up` 起容器后的 /health：未实测”这一行，**至此已实测**（同上）。
`--pull --no-cache` 仍未在本机实测（用了层缓存），CI 与 `make docker-build-fresh` 里的等价命令
也仍未在 GitHub/`make` 上跑过（本机无 `make`）。

### 9.2 真实浏览器点选（第 7 节给出的期望值，已实测）

用 `AIOPS_SERVICES_FILE=services.staging.json` 起 monitor（8004）、`DEMO_MODE=1` 起 cls/ssh，
后端用上面的新镜像起容器，浏览器 Playwright Chromium 访问 `http://127.0.0.1:9900/`：

| 检查 | 结果 |
|---|---|
| `target_required` 候选 | 3 张（`agent-test-server`、两个 `staging-payment-api`） |
| 卡片 DOM 钩子 | `data-testid="target-card"` + `data-target-id/-instance-id/-region/-index` |
| 点第 3 张（同名第 2 实例） | trace 里 `status/target_bound` = `ins-staging-b-0002 / ap-shanghai` |
| 页面“目标”行 | `staging-payment-api 实例 ins-staging-b-0002 地域 ap-shanghai`（绑定前为“未绑定”） |

截图：`screenshots/g5-target-bound-2026-10-28.png`。

### 9.3 仍然未做的（与第 8 节一致）

- CPU torch / `uv.lock` 重解析（配方已实测，未落地）；
- `ruff --select I --fix` 全局重排；
- `tests/test_server_env_isolation.py` 的追加断言（`AIOPS_SERVICES_FILE` 覆盖生效只做了
  `python -c` 手工验证与真实 MCP 进程验证）；
- GitHub Actions docker job 真实执行。
