# AutoDL 下一阶段任务书：SSH 目标、RAG 语料与端侧 Planner 适配

日期：2026-10-29  
基线分支：g6-permissions-chat-consistency  
当前代码 HEAD：5c8a335  
当前目标目录：services.json 中的 AutoDL SSH 主机  
本轮目标：让项目在真实 AutoDL Linux 服务器上完成“目标绑定 → SSH 只读采集 → AutoDL 运维知识检索 → 本地 QLoRA Planner 规划”的闭环。

## 一、先明确技术结论

当前 G8 修复已经解决了第一个问题：

- services.json 的 SSH 类型现在会被 Target.kind 消费；
- SSH 目标的 planner 工具目录被收窄为 SSH 工具、发现、时间和知识检索；
- 云端模式可以使用 SSH 工具完成真实目标诊断。

但第二个问题仍然存在：

- 端侧 QLoRA 训练集里的 CPU/memory 样本几乎全部绑定 query_cpu_metrics/query_memory_metrics；
- AutoDL SSH 目标没有这些云监控工具；
- 工具目录收窄后，本地模型会退回 get_current_time/retrieve_knowledge，而不是稳定选择 get_system_info、get_process_list、check_disk_usage、search_system_logs；
- RAG 能提供 AutoDL、Linux、CUDA、Docker、vLLM 的处置知识，但不能自动改变 Planner 已经学到的“CPU → 云监控工具”映射。

因此本轮必须把三件事分开验证：

1. SSH 目标和工具能力是否正确；
2. RAG 是否能返回 AutoDL/Linux 运维知识；
3. 端侧模型是否能根据 SSH 目标和 Linux 症状生成正确计划。

## 二、执行方式

本轮不要限制 token、上下文长度或执行时长。主 Agent 必须创建并行子 Agent，避免自己独立承担连接、语料、训练、评测和文档工作。

| 子 Agent | 工作范围 | 输出 |
|---|---|---|
| autodl_connection | AutoDL SSH 配置、key 登录、只读工具真实联调 | AUTODL_CONNECTION_REPORT.md |
| autodl_corpus | AutoDL/Linux/GPU/Docker/vLLM 公开手册收集、许可、规范化 | AUTODL_CORPUS_REPORT.md |
| planner_dataset | AutoDL/SSH 训练数据生成、工具名和参数校验 | AUTODL_DATASET_REPORT.md |
| local_adapter | QLoRA adapter 训练、量化和端侧服务 | AUTODL_ADAPTER_REPORT.md |
| rag_integration | RAG metadata、检索过滤、RAG-assisted planning 方案 | AUTODL_RAG_REPORT.md |
| benchmark | baseline/filtered/RAG/local/cloud 对照评测 | AUTODL_BENCHMARK_REPORT.md |
| security_docs | key、密码、日志、README 和简历口径 | AUTODL_SECURITY_RESUME.md |

主 Agent 的职责：

1. 先收集所有子 Agent 的只读审查结果；
2. 先完成配置和语料，再修改训练和应用接线；
3. 共享文件只允许一个 integration Agent 修改；
4. 最后统一执行真实 AutoDL 场景、RAG 评测和本地模型对比；
5. 保存每个阶段的输入题集、模型版本、adapter、量化方式和结果 hash。

## 三、硬性约束

1. 不恢复任何腾讯云监控工具作为 AutoDL 目标的可用能力。
2. 不为了让测试通过把 query_cpu_metrics/query_memory_metrics 伪装成 SSH 工具。
3. 不把 RAG 文档中的示例命令当作已经执行过的观测结果。
4. 不把合成 demo 数据包装成真实 AutoDL 状态。
5. 不把 prompt 里简单添加一句“请使用 SSH”当作训练问题已经解决。
6. 不新增通用防御层或无限兜底逻辑；能力边界通过目标目录、工具 schema、训练数据和 RAG metadata 明确表达。
7. 真实 SSH 只执行现有只读工具，禁止执行写入、安装、重启、删除和修改配置命令。
8. 所有 key、密码、代理地址、主机敏感信息和完整环境变量不进入报告、截图、JSON 或 Git。

## 四、P0：AutoDL 目标配置和 SSH 连接闭环

相关文件：

- services.json
- app/core/targets.py
- servers/ssh_server.py
- .env.example
- docs/ENVIRONMENT.md
- tests/test_runtime_target_binding.py
- tests/test_server_env_isolation.py
- tests/test_ssh_safety.py

### 1. 统一 AutoDL 连接参数

当前 docs/ENVIRONMENT.md 记录 AutoDL 使用非默认 SSH 端口 12792，而 services.json 只有 ssh_host，Target 也没有 ssh_port 字段。必须明确端口来源，避免目标目录和全局 SSH 配置指向不同主机。

推荐改法：

- Target 增加 ssh_port；
- services.json 记录真实 AutoDL SSH 端口；
- SSH MCP 使用已验证的 host、port、user、key；
- 目标绑定同时校验 ssh_host 和 ssh_port；
- key 路径只来自运行时环境，不写入 services.json。

如果决定继续用全局 SSH_PORT，也必须在 services.json 校验时明确证明它和目标配置一致，并在启动日志中只记录 host/port，不记录 key 或密码。

### 2. 连接配置

- 使用专用 RSA key；
- SSH_PASSWORD 不配置或明确为空；
- 不再依赖密码登录；
- key 文件权限符合 Paramiko/OpenSSH 要求；
- SSH_SERVER_TYPE=linux；
- AutoDL 主机的 host/port/user 与 services.json 一致；
- 配置检查只输出 set/unset、端口和 hash，不输出 key 内容。

### 3. 真实直接验收

在获得授权的 AutoDL 主机上执行一次完整只读流程：

1. list_all_services；
2. 绑定唯一 autodl-ssh 目标；
3. get_system_info；
4. get_process_list；
5. check_disk_usage；
6. search_system_logs；
7. read_log_file（只读且路径在允许范围内）；
8. retrieve_knowledge。

每个结果必须记录：

- status；
- source/source_kind=ssh 或 knowledge；
- live；
- target；
- duration；
- 是否为真实 AutoDL 返回。

禁止把 SSH 失败写成“服务器异常”而不区分认证失败、端口错误、权限不足、日志路径不存在和远端工具不可用。

## 五、P0：AutoDL RAG 语料库

### 1. 语料主题

建立独立的 AutoDL/Linux 语料 manifest，不要把 AutoDL 文档直接混进原腾讯云主题而不加 metadata。至少覆盖：

- AutoDL/SeetaCloud SSH 连接和实例运行说明；
- Ubuntu 22.04 系统管理；
- systemd/journalctl；
- NVIDIA 驱动、nvidia-smi、CUDA compatibility；
- MUSA/摩尔线程运行时（如果当前机器实际使用 MUSA）；
- PyTorch CUDA/MUSA 环境排查；
- Docker、容器磁盘、镜像和 GPU runtime；
- vLLM、llama.cpp、模型服务；
- GPU 显存不足、OOM、进程占用；
- 磁盘满、inode、日志增长；
- SSH、端口、密钥和网络排查。

### 2. 来源和许可

- 优先使用 AutoDL 官方公开文档、Ubuntu 官方手册、NVIDIA 官方文档、PyTorch 官方文档、Docker 官方文档、vLLM 官方文档；
- 每个来源记录 URL、抓取日期、license/usage 状态和原文 hash；
- needs_review 的来源不能写成已完成商业发布语料；
- 保留 raw、normalized、notices 和 manifest；
- 页面过短、抽取失败或动态渲染内容单独标记，不默认为完整手册。

### 3. Metadata

每个文档必须带：

- product=autodl 或 product=linux/gpu/docker/vllm；
- os_family=linux；
- product_version 或环境版本；
- target_kind=ssh；
- source_kind=manual；
- valid_from/valid_until；
- source_uri；
- license_id；
- document_id/revision/chunk_id。

检索 AutoDL 目标时优先使用 product、os_family、target_kind 和版本过滤。不要让 AutoDL 目标默认命中腾讯云 API 文档。

### 4. RAG 直接验收

准备至少 30 条固定问题，覆盖：

- SSH 连通和端口；
- CPU/内存/磁盘；
- GPU/显存/CUDA；
- Docker/vLLM；
- systemd/journalctl；
- 无答案和版本冲突。

记录：

- Recall@k、MRR；
- 有答案/无答案分母；
- no-answer 误答；
- 引用支持；
- 文档版本、revision 和 source URI；
- 目标过滤前后候选文档集合。

不能写“RAG 让模型学会了 AutoDL”，只能写“RAG 为 AutoDL/Linux 处置提供版本化知识和引用”。

## 六、P1：本地 Planner 的 AutoDL/SSH 数据适配

### 1. 数据设计

新建独立数据集或 adapter，不覆盖原腾讯云版本。建议命名：

- qwen15-autodl-ssh-v1；
- eval/autodl_ssh_tasks.jsonl；
- training/data/autodl_ssh_train.jsonl；
- training/data/autodl_ssh_val.jsonl；
- training/data/autodl_ssh_test.jsonl。

每条训练输入必须显式包含：

- 平台：Linux；
- 采集方式：SSH；
- 目标服务；
- 可用工具目录；
- 症状；
- 输出 schema。

建议至少 120 条训练样本、40 条验证样本、40 条独立测试样本，按模板分组切分，避免同模板泄漏。

### 2. 样本分布

至少覆盖：

- CPU 高、CPU 正常、进程占用；
- memory 高、OOM、swap；
- disk 满、inode、日志增长；
- GPU 显存、nvidia-smi、CUDA/MUSA；
- Docker 容器异常、镜像占满磁盘；
- vLLM 启动失败、模型加载、显存不足；
- systemd/journalctl；
- SSH 无权限、日志文件不存在、工具不可用；
- target unclear；
- knowledge no-answer；
- unauthorized tool 请求。

目标工具只能使用真实 SSH 目录中的：

- list_all_services；
- get_current_time/get_current_timestamp；
- retrieve_knowledge；
- get_system_info；
- get_process_list；
- check_disk_usage；
- search_system_logs；
- read_log_file。

训练样本中不得出现 AutoDL 目标选择 query_cpu_metrics/query_memory_metrics。

### 3. Prompt 和训练一致性

不能只在运行时临时加一句提示。训练和服务 prompt 必须使用同样的字段和顺序：

~~~text
平台：Linux
采集方式：SSH
目标：autodl-ssh
工具能力：get_system_info, get_process_list, check_disk_usage,
search_system_logs, read_log_file, retrieve_knowledge
告警文本：...
~~~

工具目录、参数 schema、category 和输出格式必须与生产服务一致。修改后重新导出 tool_catalog 和 triage schema。

### 4. Adapter 策略

优先训练独立 AutoDL adapter，不覆盖现有 qwen15-v9：

- 保留原始 Tencent/cloud adapter；
- 新增 AutoDL SSH adapter；
- 运行时根据 target.kind 或 LOCAL_LLM_ADAPTER 选择；
- trace 记录 adapter、base model、quantization；
- 失败时 local 模式停止，不暗中切 cloud；
- 比较 base v9、AutoDL adapter 和 prompt-only 三种结果。

如果显存和训练环境不足，先做 prompt-only 基线并明确它不是最终解决方案。

## 七、P1：RAG 与 Planner 的接法

必须做消融，区分 RAG 负责什么：

### A. RAG 后置知识模式

流程：

1. local Planner 先生成 SSH 计划；
2. SSH 工具获取真实观测；
3. retrieve_knowledge 根据症状、观测和平台检索处置手册；
4. 报告中输出知识引用和观测证据。

这是默认安全路径，RAG 不直接决定工具调用。

### B. RAG 辅助规划模式

如果要让 RAG 参与 Planner：

1. 先用 target_kind/product/os/version 做一次有界检索；
2. 只取 2–3 个短片段；
3. 以固定格式注入 Planner prompt；
4. 训练数据必须包含同样的知识片段字段；
5. 对比无 RAG 与有 RAG 的工具召回、F1、schema 合法率、延迟和 token；
6. 如果 prompt 变长导致端侧模型退化，保留 A 路径，不强行把 RAG 塞进 Planner。

不得因为 RAG 检索命中了某个命令，就直接执行该命令。命令仍必须来自工具白名单和现有参数校验。

## 八、P1：AutoDL 同题评测

准备固定 40 条任务，保存 sha256，分为：

- 20 条 SSH 采集计划题；
- 10 条 AutoDL RAG 知识问答；
- 5 条无答案/版本冲突题；
- 5 条工具不可用/权限失败题。

比较：

1. 原始本地模型 + SSH 工具目录；
2. prompt-only + SSH 工具目录；
3. AutoDL adapter + SSH 工具目录；
4. AutoDL adapter + RAG 后置；
5. AutoDL adapter + RAG 辅助规划（如果实现）。

指标分别记录：

- schema 合法率；
- tool recall；
- tool precision/F1；
- arg exact；
- target binding；
- no-answer refusal；
- citation support；
- p50/p95 latency；
- input/output token；
- GPU memory；
- 失败类型分布。

不能合成“AutoDL 运维准确率”。必须说明 RAG、模型、SSH 工具平面和真实账户错误各自的影响。

## 九、P1：前端和报告展示

前端必须显示：

- 当前目标：autodl-ssh；
- platform=Linux；
- collection method=SSH；
- host/port 只显示非敏感脱敏信息；
- local/cloud 模式；
- 当前 adapter；
- RAG 状态；
- SSH 证据来源；
- 观测与知识引用的区别。

报告中：

- SSH 真实观测标记 source_kind=ssh、live=true；
- RAG 标记 kind=knowledge、live=false；
- demo/unknown 不得作为 AutoDL 真实状态；
- 不能展示 key、密码、完整 host token 或本机敏感路径。

## 十、完成标准

### 必须完成

- AutoDL host/port/user/key 配置一致；
- 真实 SSH 只读流程至少成功完成一次，或明确记录授权/连接阻塞；
- AutoDL 语料有 manifest、来源、hash、license 和 metadata；
- 至少 40 条 AutoDL 固定评测题；
- AutoDL adapter 或 prompt-only 基线完成并有独立指标；
- RAG 后置路径完成；RAG 辅助规划若效果变差则如实保留为失败实验；
- 原 Tencent/cloud 版本不被覆盖；
- 工作区无 key、密码、探针和临时文件。

### 可以保持未验证

- AutoDL GPU/MUSA 真实加速，如果当前实例驱动不可用；
- 真实 CLS/QCE 云监控；
- 写入、重启、安装、扩容等非只读操作；
- 多副本 SSH/RAG 共享状态；
- 端侧模型泛化到其他云厂商。

## 十一、推荐复现命令

~~~powershell
git status --short
git rev-parse HEAD

# 目标和环境静态检查
./.venv/Scripts/python.exe -m pytest tests/test_runtime_target_binding.py tests/test_ssh_safety.py tests/test_server_env_isolation.py -q -o addopts=

# 端侧模型训练和评测
.venv-train/Scripts/python.exe training/train_lora.py --help
.venv-train/Scripts/python.exe training/eval_lora.py --help

# AutoDL RAG 题集和本地评测
./.venv/Scripts/python.exe scripts/eval_retrieval.py --help
./.venv/Scripts/python.exe scripts/mode_benchmark.py --help

# 完整回归
./.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider

# 最终状态
git diff --check
git status --short
~~~

如果没有获得 AutoDL SSH 授权或当前 GPU 驱动不可用，必须停止在“真实连接阻塞”状态，不要使用 demo 结果替代真实服务器证据。
