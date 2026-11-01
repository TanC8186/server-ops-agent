# On-Call Agent 项目审查与技术优化建议

审查日期：2026-10-26（Asia/Taipei）。代码快照：`fca7f1c`。改进方向：大模型应用 / Agent 开发，以及大模型算法 / 微调与推理优化。

后续已实现请求级双模式及相关执行边界，详见 [双模式实现说明](DUAL_MODE.md)。本报告保留上述提交的审查记录；当前修复范围应结合后续实现与测试判断。

## Material Passport

- Origin Skill: experiment-skill
- Origin Mode: validate（训练与评测部分）；其余为工程代码审查
- Origin Date: 2026-10-26
- Verification Status: ANALYZED
- Version Label: project_review_v1
- 范围：Agent 运行链路、MCP/SSH、数据与微调、评测与性能统计、API/前端、部署配置。
- 验证边界：执行了现有测试、静态校验、前端构建及隔离反例；未重新训练、重新跑模型精度基准或真实故障演练。未执行 SSH 命令或主动调用云端 LLM。
- 本报告中的确定性反例已独立复现；历史模型效果仍属于历史产物分析，不升级为本轮重新验证的模型效果。

## 总体判断

项目已有可展示的工程骨架：LangGraph 编排、typed plan 直接执行、MCP 工具平面、运行 trace、本地小模型规划、训练和量化脚本。最值得强化的主线是：**资源受限条件下，能够校验执行结果、在证据不足时降级的运维诊断 Agent**。

目前最需要补的是证据质量、执行边界和完整本地运行能力。继续增加框架、堆更多工具或先压到 0.5B，优先级较低。

## 优先修复：影响现有结论或实际行为的问题

### 1. 主训练路径是 LoRA SFT，尚无完整 QLoRA 训练证据

证据：`training/train_lora.py:102–107` 直接以 `torch.bfloat16` 加载模型；`:112–132` 添加 LoRA。主路径没有 4-bit 基座加载、`BitsAndBytesConfig` 或 k-bit 训练准备。`training/probe_qlora.py` 有 NF4 探针，但单样本 forward/backward 不等于完成训练、验证与保存。

因此，当前仓库能支持的说法是“Qwen2.5-1.5B LoRA SFT + 训练后 4-bit 量化部署”。QLoRA 的核心是训练期间通过冻结的 4-bit 基座向 LoRA 适配器反向传播，见 [QLoRA 原论文](https://arxiv.org/abs/2305.14314)。

建议：先统一真实技术表述。如果确实要补 QLoRA，增加独立完整训练入口，记录基座量化配置、实际 dtype、可训练参数量、峰值显存、数据哈希、命令、适配器和评测输出，并与相同数据和预算的 LoRA 做对照。若另有未入库的完整训练产物，应补其来源与复现说明。

另外两处配置需要修正：`--fp16` 只切换 Trainer 配置，模型加载仍硬编码 bf16；`--no-grad-checkpointing` 没有关闭 `TrainingArguments` 中固定为 True 的 checkpointing。位置：`train_lora.py:102–109,153–159`。

### 2. 当前训练/测试数据已有交叉，不能继续称独立 held-out

证据：`training/gen_dataset.py:54–58,70–73` 将测试模板原句重复添加到新索引，明确意图让其进入 train；`training/RESULTS.md:146–149` 也记录将 test 措辞追加进训练。

当前落盘数据为 train/val/test = **1110/150/178**。统计得到：

- train 与 test 有 **23 种完全相同的 user 文本**，命中 **37 条 test 样本**。
- 仅将四个服务名和数字归一化后，**88/178 条 test** 的句型已出现在 train。此项是句型重叠，不能与逐字重复混为一谈。
- 当前数据污染不自动证明历史 v9 使用的数据也有相同污染；历史版本需要各自的数据快照。

建议：把已用于定位错误、补数据的旧 test 转为开发/回归集；按去重后的模板族、来源、服务或时间分组重新留出测试集，改写版本跟随原样本进入同一分组。随后冻结新测试集和判分代码。修复集上的提高可以证明已知错误得到改善，不能直接证明未知工单泛化。

验收：跨 split 原句重复为零；分组不跨 split；所有目标类别在新测试集中覆盖；样本带来源、组 ID 与数据版本；调参只看开发集。

### 3. 任务成功率与 groundedness 有可复现的假阳性

位置：`scripts/run_eval.py:97–118,122–177`。

隔离复现结果：

- gold 要查询 server-a，trace 查询 server-b，报告为空，执行两步但预算为一步，仍得到 `task_success=True`。
- `{"cpu":{"value":20},"disk":{"value":99}}` 被判为 CPU > 90。
- 报告里的 `95` 可以匹配预览中的 `195`，得到 groundedness=1。

原因是事实匹配在整个结果内遍历数字，没有绑定指标路径和目标实例；成功条件未检查报告内容、实例、预算；groundedness 使用数字子串匹配，且未进入成功条件。

建议：保存完整结构化工具结果，使用 `evidence_id / service / instance / metric / value / unit / time_window / source`；报告以结构化字段引用证据。按实体、指标、单位和时间窗校验，再渲染文本。预览只用于 UI 展示，不承担真值存储。

`search_log` 与具体日志工具的差异也需要按数据范围、过滤条件和证据完整性确认，不能仅为提高分数设置别名。主指标宜是“是否获得所需证据并正确诊断”，工具名匹配作为辅助指标。

### 4. MCP 的 unavailable 可能通过 Verifier，错误恢复链路失效

位置：`app/agent/aiops/executor.py:78–92`；`app/agent/aiops/verifier.py:19–36`。

Executor 将工具返回的 content block 列表序列化为 JSON；Verifier 只检查顶层 dict。下面的真实 MCP 结果形式会被判为可用：

```json
[{"type":"text","text":"{\"ok\":false,\"source\":\"unavailable\"}"}]
```

安装环境中的 MCP adapter 确实将 TextContent 转换为 block 列表，因此不只是理论输入。

建议：在 MCP 边界统一解包并验证类型，内部传递统一 `ToolResult`，保留 status、source、data、error 和 evidence。空结果、超时、错误、不可用分别有明确语义。为真实 adapter 返回形态加入回归测试。

### 5. SSH 磁盘查询存在命令注入入口

位置：`servers/ssh_server.py:320–333`。Linux 分支直接构造 `df -h {path}`，绕过现有路径辅助校验。

通过抽取原函数、替换 `_exec_readonly` 为捕获函数，输入 `/; printf READONLY_BOUNDARY_BYPASS`，得到命令 `df -h /; printf READONLY_BOUNDARY_BYPASS`。**没有实际执行该命令或连接 SSH。**

建议：对工具入口做路径类型与范围校验，对 shell 参数使用可靠引用；长期以结构化、白名单命令构造器统一处理。测试应覆盖每个公开工具入口，不能只验证安全辅助函数。远端账号同时限制权限。

### 6. 本地计划校验会放过已标记无效的步骤

位置：`app/agent/aiops/local_planner.py:117–125,138–158`。

缺必填参数的步骤被标为 `_invalid=True`，但 `_plan_is_valid` 直接跳过它，之后仍可能返回 True；同时只检查参数名，没有完整验证 required、类型、枚举和边界。`training/schema.py:49–58` 也不验证参数类型，且“发现服务”只检查出现，不检查先后依赖。

建议：训练验证、在线接受和执行前校验共用工具的完整 JSON Schema/Pydantic 约束；任一步 invalid 就拒绝整份计划或显式删除后重新校验。未知服务必须先发现再绑定，不能从描述里猜到一个名字就当成已授权目标。

`training/scoring.py:203–230` 的 `plan_ok` 主要是关键工具集合覆盖，不要求严重性、类别或参数全对。因此应明确命名为工具覆盖通过率，并另报参数可执行率、依赖合法率与端到端任务成功率。保留纠错前/纠错后两套指标，区分模型与修复规则的贡献。

### 7. 同一 session 并发可能串报告

位置：`app/services/aiops_service.py:127–130,170–175`。

Checkpoint 以 session_id 为键，结束时又读取该 session 最新状态。隔离调度复现：A 生成报告后暂停，B 在相同 session 完成，再恢复 A，A 的 complete 事件保留 run_id=A，却返回 report:B。

建议：诊断状态按 run_id 隔离，session 只用于会话归属；返回本次执行的最终状态，避免读取会话级最新状态。为并发、取消和重连加入集成测试。之后再考虑从 MemorySaver 换为持久化 checkpointer。

还需要确定性终止契约：最后一步失败且计划已空时，Replanner 若返回 continue、解析失败或预算外 replan，图可能直接 END，随后发送空报告 complete。位置：`replanner.py:160–161,208–239`、`aiops_service.py:62–70,184–190`。结束前必须得到非空报告或显式失败状态。

reason 步骤也应与工具执行分开：当前绑定全部工具，输入又缺少历史证据（`executor.py:103–128`）。建议 reason 只读取结构化证据，不绑定工具，其生成文本不得作为独立工具证据。8 步限制之外，补全流程 deadline、单工具 timeout 和模型调用预算。

## 与“端侧部署”直接相关的改进

### 8. 本地 Planner 尚不等于完整离线 Agent

`LOCAL_LLM_BASE_URL` 仅控制本地规划路径；`app/agent/aiops/replanner.py` 的决策与最终报告仍使用统一模型工厂，reason Executor 也使用该工厂，默认配置指向云端。本地模型 API 自带语法校验，并不保证语义、工具或目标正确。

建议提供两个明确模式：

- `local_only`：规划、证据校验、报告均在本地完成；禁用外部 LLM/embedding 回退；失败时返回证据不足。可以先用确定性报告模板完成最小闭环。
- `hybrid`：按失败类型和预算升级到云端，记录升级原因、调用量与成本。

这里的离线指模型推理无需外部 AI 服务；运维工具仍可按配置访问内网目标。验收应阻断外部 AI 服务访问，在本地故障测试环境中走完告警→发现→查询→证据→报告全过程。

此外，`docker-compose.yml:104–120` 未透传 `LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL / LOCAL_LLM_WITH_PARAMS`，镜像又排除 `.env`，常规 Compose 启动无法继承本地规划配置。需要补充配置并区分容器内模型服务地址与宿主地址。

### 9. 推理基准把流式 chunk 当成 token

位置：`training/serve_bench.py:27–54,79–93`。代码对每个非空 delta 加一，并以此计算 tokens、TPOT 和 tok/s；stream chunk 不保证恰好包含一个 token。隔离 fake stream 验证：一个包含多个 token 的 chunk 只计为 1，带 usage 的无 choices 事件被跳过。

因此当前吞吐与 TPOT 不宜直接作为可靠的 token 指标。TTFT 的时间边界不受这一计数问题直接影响，但单一短 prompt、预热和每档 16 请求也不能代表真实规划负载。

建议累计输出，以服务端 completion usage 或匹配的 tokenizer 计数；无输出或错误请求不能当成功样本。使用实际 system/tool catalog 的输入长度分布，区分冷启动、热启动、并发和失败率，报告 RSS/VRAM 峰值以及整次诊断耗时。标准定义可参考 [vLLM Benchmark CLI](https://docs.vllm.ai/en/stable/benchmarking/cli/)。

另外，本地 Planner 未把自己的 usage 写入 trace。现有 5 场景结果可核算为云调用统计 tokens 从 131263 到 47020，约下降 **64.2%**；不能直接写成全部计算量、总成本或延迟下降 64%。两侧现有 task_success 都为 1/5，而判分本身还需要修复。

### 10. 部署鉴权与前端渲染仍有具体缺口

- `app/core/auth.py:11` 只读取进程环境；Pydantic 读取 `.env` 不会将值导出到 `os.environ`，Settings 也没有 api_key 字段。仅把 API_KEY 写入根 `.env` 后直接启动 Web，可能仍关闭鉴权。`servers/web_server.py:24` 同时硬编码监听所有地址。应统一从 Settings 读取，补 `.env` 到 HTTP 请求的集成测试。
- `web/src/markdown.ts:8–18` 仅转义原始 HTML；源渲染函数仍生成 `javascript:` href。后续 `dangerouslySetInnerHTML` 存在点击触发脚本的风险。应净化生成 HTML 并限制 URL scheme。当前仅验证危险 href 输出，未在浏览器执行脚本。

## 最有价值的后续建设

建议按以下交付顺序推进；每一阶段都产出可以现场演示的结果。

| 顺序 | 交付 | 验收结果 |
|---|---|---|
| 1 | 修 SSH、MCP 解包、计划校验、并发状态与评测反例 | 上述已知反例全部被正确处理 |
| 2 | 冻结无泄漏新测试集与统一工具契约 | 数据版本、原始预测、判分版本和配置可追溯 |
| 3 | 完成最小 local_only 闭环与 Compose 配置 | 无外部 AI 服务也能完成本地故障诊断；失败不编造 |
| 4 | 建 fake MCP / 故障注入环境 | 超时、429、空结果、错实例、权限不足、正常对照均可稳定复现 |
| 5 | 做最小消融矩阵 | 同条件对比规则基线、原始 Qwen+prompt、加约束解码、微调、微调后量化、hybrid |
| 6 | 补一条真实告警摄入与演示 | Alertmanager webhook→去重/目标绑定→诊断→证据报告→run 回放 |

第 5 阶段必须包含规则基线：当前类别和 severity 很大程度由确定性规则生成标签，读者会合理追问为何需要模型。阈值与严重性政策可优先由规则决定，模型负责告警理解和可变的证据收集计划。用对照实验说明微调在什么输入分布下带来额外收益。

推荐最终展示的指标：全样本任务成功率、正常场景误报率、错误工具调用率、无效参数率、升级云端率、每任务云 token/成本、端到端 p50/p95、RSS/VRAM 峰值。故障、拒答与超时均保留在分母；少量重复用于展示稳定性，不把同模板数值变体当成大量独立工单。

RAG 的下一步先做固定问题→证据片段 golden set，再决定是否添加 BM25、reranker、父子分块。只因技术栈丰富而增加这些模块，无法证明收益。

量化对比需固定同一训练权重、测试集和解码策略。当前文档部分对照同时变更 v8/v9 与量化算法，不足以把改善全部归因于 AutoRound；“4-bit 无损”“CPU 只有单并发可用”等表述应限定到具体测量条件。

## 对外表述边界

两条技术主线对应不同的关注点，描述时建议分开：

| 方向 | 项目主线 | 当前最该补的证据 |
|---|---|---|
| 大模型应用 / Agent 开发 | 运维诊断闭环、工具契约、证据校验、状态隔离、回退与回放 | 故障注入与 local_only 端到端演示、成功率和失败恢复 |
| 大模型算法 / 微调与推理优化 | 小模型任务适配、数据设计、LoRA/QLoRA、量化和部署权衡 | 无泄漏盲测、完整 QLoRA 训练产物、同条件消融、准确的显存与性能基准 |

应用方向目前已经有对应架构；算法方向需要补充受控实验证据。对外陈述应写清模型承担的是规划任务、数据是合成的、优化收益对应哪个指标。只做模型接入和模板 SFT，尚不足以证明复杂 Agent 推理或微调算法创新。可以把“不同训练/量化方案在资源预算下的质量与性能权衡”作为算法侧的明确问题，不需要为了显得前沿继续堆模型或训练方法名词。

现阶段项目定位：**面向内网部署的轻量化运维诊断 Agent**。

可据代码支撑的描述：

> 基于 LangGraph 与 MCP 构建运维诊断流程，将工具规划转为结构化计划并直接执行；针对 Qwen2.5-1.5B 进行运维任务 LoRA 微调，探索 4-bit 本地规划与云端回退，并提供按 run_id 记录的诊断轨迹和评测工具。

待本报告问题修复、重新评测后，再补真实数字。当前不宜直接写“企业生产级”“完整离线”“QLoRA 已验证提升任务成功率”“量化无损”或“总成本下降 64%”。

需要回答清楚的技术问题：为何采用小模型只做规划；为何微调优于 prompt/规则；如何防止合成数据泄漏；结构合法与执行正确有何区别；如何判断工具返回可信；什么时候升级云端；单模型文件大小与整套端侧资源需求有何区别；一次失败如何定位和回放。

## 本轮验证记录

| 检查 | 结果 | 说明 |
|---|---|---|
| `.venv/Scripts/python.exe -m pytest tests -q` | 通过 | 存在 Pydantic/PyMilvus 弃用告警；通过不代表覆盖上述新反例 |
| `.venv/Scripts/python.exe scripts/validate_eval.py` | 通过 | 5 个场景、22 个注册工具；验证名称和 schema 引用一致性 |
| 前端 `npm run build` | 通过 | TypeScript 与 Vite 构建通过；使用现有依赖 |
| 评测器反例 | 已复现 | 空报告/错实例/超预算、混指标、数字子串 |
| MCP 包装结果校验 | 已复现 | block 内 unavailable 被顶层检查放过 |
| 本地计划校验 | 已复现 | `_invalid` 步骤可被整体计划校验跳过 |
| 同 session 并发 | 已复现 | run A 可能读取 run B 最终报告 |
| SSH 参数注入 | 命令构造已复现 | 使用 stub 捕获，未执行远程命令 |
| 鉴权 `.env` 行为 | 隔离复现 | 临时无敏感值配置；未暴露用户密钥 |
| Markdown 链接 | 渲染输出已复现 | 危险 scheme 未净化；未执行浏览器脚本 |
| 训练、量化、模型推理效果 | 未重跑 | 本轮不产生新的模型性能结论 |

只新增本审查文档，未修改业务代码或历史结果。

## 实验方法核查附记

覆盖 11/11 类方法问题；不适用项也明确列出。没有为这批探索性结果虚构 p 值或置信区间。

| 类型 | 本项目判断 |
|---|---|
| Simpson's paradox | 汇总分数可能掩盖类别/模板退化；应保留分层结果，未证明发生方向反转 |
| Ecological fallacy | 模板内高分不能外推到真实企业工单或完整 Agent |
| Berkson/selection bias | 少量合成模板与服务不足以代表目标工单分布 |
| Collider bias | 当前无相应因果控制分析，未见具体证据 |
| Base rate neglect | 需要更多正常对照及误报统计；当前类别比例不能代表生产告警比例 |
| Regression to the mean | 当前不是典型极端样本前后测；不据此认定问题 |
| Survivorship bias | OOM/不完整训练已记录；正式对比仍须保留所有失败请求和完整分母 |
| Look-elsewhere effect | 多版本试验属于探索；最终应在新冻结测试集确认 |
| Garden of forking paths | 看过 test 后追加原句训练；旧 test 已成为开发材料 |
| Correlation/causal attribution | 同时变训练版本与量化方法，不能单独归因于量化算法 |
| Reverse causality | 当前非此类因果设计，不适用 |

模型训练与部署基准兼具随机性和环境敏感性；本轮未做同配置重跑，历史模型效果的复现结论为 `CANNOT_VERIFY`。确定性代码反例与历史模型效果应分别使用。
