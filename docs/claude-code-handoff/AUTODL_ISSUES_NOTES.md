# AutoDL / RAG E2E 优化过程踩坑记录

> 用途：给作者本人复盘、重新手操一遍。按"现象 → 原因 → 怎么确认的 → 怎么解"组织。
> 每条都标注是否已修、在哪修的。**没有实际确认过的不会写在这里。**

## A. 环境与工具链

### A1. `nohup ... &` 在 `wsl -d Ubuntu -- bash -c '...'` 里活不下来

- **现象**：`wsl -d Ubuntu -- bash -c 'nohup bash serve.sh > log 2>&1 &'` 返回成功，但几秒后进程没了，日志文件都没创建。
- **原因**：WSL 的非交互 `bash -c` 在命令结束时会把整个会话的进程组收掉，`nohup` 只挡 SIGHUP，挡不住这个。
- **怎么确认**：`ls ~/g8/vllm.log` 报 No such file；`pgrep -f "vllm serve"` 为 0。
- **怎么解**：用 `setsid` 脱离会话：
  ```bash
  wsl -d Ubuntu -- bash -c 'setsid nohup bash ~/g8/serve.sh > ~/g8/vllm.log 2>&1 < /dev/null & disown; sleep 3; echo ok'
  ```
- **备注**：这条对任何"从 Windows 侧拉起 WSL 常驻进程"都适用。

### A2. Windows 路径 `/tmp/xxx` 在 Git Bash 里不可写

- **现象**：`curl -o /tmp/probe.html` 返回 200 且 size 正确，紧接着 Python `open('/tmp/probe.html')` 报 `FileNotFoundError`。
- **原因**：Git Bash 的 `/tmp` 与 Python（Windows 原生）看到的路径不是同一个；curl 是 MSYS 程序，写的是 MSYS 的 tmp，Python 按 Windows 语义找不到。
- **怎么解**：临时文件一律写到 `E:/On_Call_Agent/.tmp_xxx/`，两边都能访问；用完删。
- **备注**：项目约束本来就是"只在 E 盘操作"，这条正好一致。

### A3. `rm -rf` 被安全检查拦下（工作目录相关）

- **现象**：`rm -rf /e/On_Call_Agent/.tmp_probe` 被内置安全策略拒绝，提示"would remove a workspace directory"。
- **原因**：当前 shell 的 cwd 在 `E:/On_Call_Agent` 下（或曾经是），删除其子目录被视为删除工作区的一部分。
- **怎么解**（任选，都被安全检查认可）：
  - 先 `cd` 到别处（如 `cd /e/On_Call_Agent/on_call_agent`）再删；
  - 或 `find dir -type f -delete && find dir -depth -type d -exec rmdir {} +`；
  - 目录里的进程/句柄没释放时会 `Device or resource busy`，此时**退出该目录**再删即可。
- **不要**：拆分命令绕过检查——那等于绕过安全策略。

### A4. `docker build` 时 apt 间歇 502

- **现象**：`apt-get update` 报 `E: Failed to fetch http://deb.debian.org/.../InRelease 502 Bad Gateway`，构建失败 code 100。
- **原因**：走本机代理（`host.docker.internal:7897`）时 edge 节点偶发 502，不是 Dockerfile 问题。
- **怎么确认**：宿主上 `curl -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}' http://deb.debian.org/debian/dists/trixie/InRelease` → 200，说明代理本身通，刚才是瞬时故障。
- **怎么解**：原样重跑同一条 `docker build`。**不改 Dockerfile，不加 `--network host` 变体**——那类产物不能当标准构建证据。
- **教训**：先区分"环境瞬时故障"和"Dockerfile 有错"，否则会去改本来正确的文件。

### A5. 构建遗留的中间容器要自己清

- **现象**：`docker ps -a` 出现 `angry_lichterman` 之类随机名容器。
- **原因**：`docker build` 在 apt 步骤失败时，中间容器可能没被回收。
- **怎么确认**：`docker inspect <name> --format '{{.Config.Cmd}}'` 看到是 `apt-get update ...`，确认是自己的构建残留（不是其他项目的）。
- **怎么解**：`docker rm -f <name>`。**删之前一定先看 Cmd/Image 确认归属**，`mact-*` / `personal_design` 一律不动。

### A6. WSL 里 GPU 显存只够一个模型

- **现象**：想在 8002 起 AutoDL 模型时，先前的模型还占着 6GB，8GB 卡放不下第二个。
- **怎么确认**：`nvidia-smi --query-gpu=memory.used --format=csv,noheader`。
- **怎么解**：先 `pkill -f "vllm serve"` 再起新的；或者用 `--gpu-memory-utilization` 压小。
- **备注**：fp16 merged 模型 2.9GB，加载时 `--gpu-memory-utilization 0.45` 够用。

## B. SSH / 目标绑定

### B1. `SSH_KEY_FILE` 一个变量承担两种语义，容器内必然 ENOENT

- **现象**：`.env` 写 `SSH_KEY_FILE=E:/On_Call_Agent/.tmp_g8_keys/xxx`，容器里 SSH 工具报
  `[Errno 2] No such file or directory: 'E:/On_Call_Agent/.tmp_g8_keys/xxx'`。
- **原因**：这个变量同时被当成
  1. compose `volumes` 的**宿主侧源路径**（compose 在宿主执行，需要 Windows 路径）；
  2. 容器内进程读的**目标路径**（需要 Linux 路径）。
  旧 compose 写法 `- SSH_KEY_FILE=${SSH_KEY_FILE:-/app/ssh_key.pem}` 把宿主路径原样传进容器。
- **怎么确认**：`docker exec oncall-agent printenv SSH_KEY_FILE` 得到 Windows 路径；
  `docker exec oncall-agent ls -la /app/ssh_key.pem` 却存在（说明挂载成功，只是进程去读了错路径）。
- **怎么解**：compose 里把两者分开，容器内固定成挂载目标：
  ```yaml
  environment:
    - SSH_KEY_FILE=/app/ssh_key.pem        # 容器内，固定
  volumes:
    - ${SSH_KEY_FILE:-/dev/null}:/app/ssh_key.pem:ro   # 宿主侧仍读同一个变量
  ```
- **状态**：已修（`docker-compose.yml`）。

### B2. `services.json` 的 `type: ssh` 没有任何代码消费

- **现象**：把目标从腾讯云换成 AutoDL SSH 主机后，本地模型仍然规划
  `query_cpu_metrics` / `query_memory_metrics` / `search_service_logs`，全部失败。
  用户原话："怎么还有腾讯云的东西！"
- **原因**：两层叠加
  1. `services.json` 里写的 `"type": "ssh"` 在 `app/core/targets.py` 的 `Target` 里**没有对应字段**，
     `catalog_targets()` 直接丢弃 → "目标是 SSH 主机"这个事实在运行时不存在；
  2. `create_run_tools` 把全部 22 个 `READ_ONLY_TOOLS` 一次性交给 planner，
     `try_local_plan` 把整个目录编进 system prompt。模型只能从目录里选。
- **怎么确认**：
  - `grep -rn 'row.get("type")' app/core/targets.py` → 空；
  - 实测计划里出现 `query_cpu_metrics`，其错误是 `X-TC-Region 取值错误`（说明确实调了腾讯云）。
- **怎么解**：
  - `Target` 增加 `kind`（`cloud` 默认 / `ssh`），由 `catalog_targets` 从 `type` 字段填充；
    声明 `type=ssh` 但缺 `ssh_host` 时退回 `cloud`（避免"只读 SSH 但不知道连哪"）；
  - `RunTools.tools_for_target()`：ssh 目标只给 `SSH_TOOLS ∪ TARGET_AGNOSTIC_TOOLS`；
    cloud 目标保持全量（不改已有语义）；
  - `cloud_multi` 专科目录同样收窄，且"收窄后某专科无工具"时明确失败，不派空任务。
- **状态**：已修（`app/core/targets.py`、`app/agent/aiops/runtime_tools.py`、`cloud_multi.py`），
  新增 `tests/test_ssh_target_tool_scope.py`（13 条）。**已验证**：把收窄逻辑临时改回恒返回全量，
  该测试文件会失败——说明不是空测试。

### B3. 收窄工具目录必须保留 `list_all_services`

- **现象（差点踩到）**：第一版收窄时只保留 `SSH_TOOLS`，结果 planner 第一步
  （约定必须是 `list_all_services`）无法满足，本地计划永远过不了校验。
- **原因**：`_plan_is_valid()` 要求第一个 tool 步骤是 `list_all_services`。
- **怎么解**：新增 `TARGET_AGNOSTIC_TOOLS = {list_all_services, get_current_time,
  get_current_timestamp, retrieve_knowledge}`，收窄时并集保留。
- **状态**：已修，并有测试 `test_narrowed_catalog_still_contains_first_step_tool` 锁定。

### B4. `runtime.target` / `tools_for_target` 在新代码里被无条件访问

- **现象**：改完收窄逻辑后 16 个 cloud_multi 测试从 `complete` 变成 `error`，
  报 `AttributeError: 'FakeRuntime' object has no attribute 'target'`。
- **原因**：测试替身 `FakeRuntime` 只有 `by_name`，没有 `target` / `tools_for_target`。
  生产代码假设这些一定存在。
- **怎么解**：加两处防御，且**不改测试替身**（替身本来就该最小）：
  - `_allowed_tool_names(runtime)`：有 `tools_for_target` 就调，没有就退回 `by_name`；
  - `getattr(runtime, "target", None)` + `getattr(target, "ssh_only", False)`。
- **教训**：给生产代码加新方法时，凡是"运行器对象"都可能是替身 —— 用 getattr 探测，
  而不是新增一层"兼容适配器"。

### B5. `target_kind` 新形参把 6 个测试打成 error

- **现象**：给 `try_local_plan` 加 `target_kind` 后，
  `test_state_isolation` / `test_target_required_flow` 共 6 个用例从 `complete` 变 `error`。
- **原因**：测试替身 `fake_plan(user_input, target, tools, run_id, *, allow_cloud_fallback=False)`
  没有 `target_kind`，`await try_local_plan(..., target_kind=...)` 直接 `TypeError`。
- **怎么解**：沿用仓库已有的 `_accepts_keyword(func, name)` 惯例，按签名裁剪：
  ```python
  plan_kwargs = {"allow_cloud_fallback": False}
  if _accepts_keyword(try_local_plan, "target_kind"):
      plan_kwargs["target_kind"] = getattr(tools.target, "kind", "")
  result = await try_local_plan(user_input, target, tools.tools_for_target(), run_id, **plan_kwargs)
  ```
- **状态**：已修，916 passed。
- **教训**：这个仓库反复踩同一个坑（`create_tools_compat`、`discover_service_compat` 都是为它而存在）。
  **加可选形参时默认就要走 `_accepts_keyword`**。

### B6. AutoDL 主机是 CPU 容器，`free` 显示 1TB 是宿主机的

- **现象**：`nproc=1`、`free -g` 显示 `MemTotal 1007`、`load average 22`，看起来配置很怪。
- **原因**：容器视角读的是宿主机的 `/proc/meminfo`，真实限制在 cgroup 里。
- **怎么确认**：
  ```bash
  cat /sys/fs/cgroup/cpu.max      # → 50000 100000  即 0.5 CPU
  cat /sys/fs/cgroup/memory.max   # → 2147483648    即 2 GiB
  ```
  `nvidia-smi` 不存在 → 这是 CPU 容器，不是 GPU 实例。
- **教训**：**永远以 cgroup 为准**，不要用 `free` / `nproc` 判断容器配额。
  `load average` 20+ 是宿主机上**别人的**任务（已跑 553 天未重启）。

### B7. 这台机器没有 `/var/log/syslog`

- **现象**：`search_system_logs` 返回 `count: 1, entries: [{"message": "(empty)"}]`。
- **怎么确认**：`test -f /var/log/syslog` → NO；`test -d /var/log/journal` → yes。
- **原因**：AutoDL 容器不用 systemd，日志走 journal 目录 + apt/dpkg 文件，没有传统 syslog。
- **含义**：SSH 日志工具在这台机器上**只能返回空**，这是环境事实不是 bug。
  写报告时必须说"日志路径不存在"，不能写成"服务器无异常日志"。

## C. 模型与推理

### C1. 纯文本成功返回被判成工具失败

- **现象**：证据表里出现 `get_current_time · 不可用 · unavailable`，但它的返回值其实是
  `2026-10-29 18:56:59`（正常时间戳）。
- **原因**：`app/core/tool_contract.py::classify_payload` 把所有**非空字符串**都判成
  `status=error`（旧逻辑是为 CLS 的文本型错误返回设计的）。
- **怎么解**：改成"简短 + 带失败标记"才算失败：
  - 前缀命中（`获取时间失败` / `error` / `Error` / `ERROR`）或
  - 短文本里含 `失败:` / `错误:` / `异常:` / `refused` / `timeout`，且长度 ≤200。
  长正文里偶现"失败"二字**不算** —— 误判成功为失败正是要修的问题。
- **状态**：已修，新增 3 条测试（时间戳=ok、三种文本失败=error、长正文不误判）。

### C2. 本地通用模型不会为 SSH 目标选 SSH 工具（训练数据缺口）

- **现象**：收窄目录后，通用 v9 模型面对"CPU 负载高"仍选 `get_current_time` + `retrieve_knowledge`。
- **怎么确认**（关键，别猜）：直接把目录收窄到只有 SSH 工具，对模型单发一次：
  计划依然是 `[list_all_services, get_current_time, retrieve_knowledge]`。
  再统计训练集每类的 gold 工具：
  | category | 主要 gold 工具 |
  |---|---|
  | cpu（300 条） | `query_cpu_metrics` 300 次；`get_system_info` **0 次** |
  | memory（390 条） | `query_memory_metrics` 390 次 |
  → 模型学到"CPU 慢 → query_cpu_metrics"，该工具不在目录时退回兜底。
- **含义**：这是训练分布问题，不是接线问题，也不是"模型笨"。
- **怎么解**：用 AutoDL 专用 adapter（见 C3）。**不要**靠改 prompt 硬拗。

### C3. AutoDL adapter 确实解决了选工具问题（已实测）

- **做法**：merged fp16 模型（`training/out/qwen15-autodl-ssh-v1/merged`，2.9GB）
  用 WSL vLLM 起在 `:8002`，`--served-model-name qwen-autodl`。
- **实测结果**（同样收窄目录 + `target_context_for("ssh")`）：
  | 症状 | 结果 |
  |---|---|
  | CPU 负载高 | `P2/cpu` → `get_system_info, get_process_list, check_disk_usage` |
  | 内存 95% OOM | `P1/memory` → `get_system_info, get_process_list, search_system_logs` |
  | 查错误日志 | `P4/healthy` → `get_system_info, search_system_logs` |
  | 指标正常 | `P4/healthy` → 全部 SSH 工具 |
  → **零腾讯云工具**，正是想要的效果。
- **train/serve 一致性已确认**：训练数据的 system prompt 里确实含
  `采集方式：SSH 只读` / `平台：Linux` / `不能选择 query_*_metrics`，
  与 `target_context_for("ssh")` 一致 —— 不是 skew。

### C4. `max_tokens=768` 对 AutoDL 模型不够，磁盘场景会无限重复

- **现象**：磁盘场景 `finish_reason=length`，JSON 不闭合；加到 1536 **仍然截断**。
- **原因**：该模型在磁盘症状上会进入重复生成循环（反复输出 `success_criteria`），
  不是单纯的"输出太长"。
- **含义**：这是**模型质量缺陷**，必须在报告里如实记录，不能只调大 token 掩盖。
- **待办**：要么在训练集里针对该模板补样本，要么在 planner 侧对"同一字段重复"做检测并判失败
  （宁可明确失败，也不要输出半截 JSON 当证据）。

### C5. fp16 模型在 9P 文件系统上加载很慢

- **现象**：AutoDL merged 模型加载耗时 ~2 分钟。
- **原因**：`/mnt/e/...` 是 9P 挂载，vLLM 日志明确写了
  `Auto-prefetch is disabled because the filesystem (9P) is not a recognized network FS`。
- **影响**：只影响启动时间，不影响推理正确性。

## D. RAG 语料

### D1. AutoDL 官方文档可以直接抓（服务端渲染）

- **验证方式**：`curl -s -L -o x.html -w '%{http_code} %{size_download}'` 得到 200 + 48–55KB。
- **注意**：直接 `re.sub` 去标签得到的大半是**侧边栏导航**，正文不在最外层。
- **正确做法**：定位 `<article>` 标签（或正文容器）再取文本。实测 `docs/env` 的
  `<article>` 里有真正正文（系统盘/数据盘、`/root/autodl-tmp`、容器内不支持 Docker 等）。
- **教训**：**抓取成功 ≠ 语料可用**。必须检查"可见字符数是正文还是导航"。

### D2. 知识库元数据只允许 4 个自由文本字段

- **现状**：`app/services/knowledge_store.py::METADATA_TEXT_FIELDS`
  = `os_family` / `product` / `product_version` / `license_id`。
- **任务书要求**还要有 `target_kind` / `source_kind` / `product_version` / `chunk_id` 等。
- **含义**：RAG 的 metadata 合同**需要扩展代码**，不是配一下就行。

### D3. `RetrievalFilters` 没有 `target_kind`

- **现状**：`app/services/knowledge_service.py::RetrievalFilters` 只有
  `os_family` / `product` / `product_version` / `at_date`。
- **任务书 §3.4 要求**按 `target_kind=ssh` 过滤。
- **含义**：需要新增该过滤维度（且要一路贯通到 Milvus 的 `expr`）。

### D4. AutoDL 数据集的 held-out 是真的（已核实）

- **验证**：按 `(family, template_index)` 分组，train 136 组 / test 16 组，
  **重叠 0 组**，test 的 16 行全部来自不重叠的组。
- **含义**：那 16 条的离线结果有效，但**样本极小**，不能当泛化能力。

## E. 流程与工程习惯

### E1. 改共享文件前先看有没有未提交改动

- **踩点**：任务书说"已有 adapter"，先验证发现工作树有 6 个文件未提交（含我上一轮修的
  `local_runtime.py`）。如果不先看就改，很容易覆盖别人的工作。
- **做法**：`git status --short` + `git diff <file>` 确认，再动手。

### E2. 测试替身与生产签名不一致是这个仓库的高频坑

已经出现过 **3 次**：
- `create_tools_compat`（deadline/budget/owner）
- `discover_service_compat`（candidate）
- `_accepts_keyword` for `target_kind`（本次）

**结论**：给运行时函数加可选参数时，**默认就用 `_accepts_keyword` 裁剪**，不要直接传。

### E3. 症状 ≠ 根因（本次最重要的教训）

用户报"怎么还有腾讯云的东西"，最直觉的解释是"模型选错了"。
实际根因是**配置字段没人消费 + 工具目录没按目标收窄**——两层代码缺陷。
如果直接去微调模型/prompt，会花很久且修不好。

**方法**：先问"这个信息在运行时存在吗？"，再问"模型能看见什么？"。
本次就是靠这两问定位的。

### E4. 偶发失败要如实记录，不要假装没发生

`test_acl_seal_failure_keeps_uploader_visibility` 在一次全量运行中失败 1 次，
单独跑 4 次 + 全量跑 2 次都通过。该测试与其覆盖代码自 G7 起未改动。
结论：与本次无关的既有序敏感问题，**但要写进报告**，不能因为"重跑就过"而抹掉。
