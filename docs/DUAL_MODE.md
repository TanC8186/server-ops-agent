# 本地小模型与 API 多 Agent 双模式

本功能将一次诊断的执行方式作为请求参数：`local` 使用一个本地 Qwen 服务规划，`cloud_multi` 使用 API 模型完成主管、指标专科、日志专科和复核角色。模式随运行记录传递，不通过修改全局模型配置切换。这里描述实现边界和验收方法；不代表已经完成真实服务、真实故障或端侧资源基准验证。

## 两条执行链路

| 项目 | `local` | `cloud_multi` |
| --- | --- | --- |
| 模型 | 一个 OpenAI 兼容的本地 Qwen2.5-1.5B 4-bit 服务 | `LLM_PROVIDER` 配置的 API 模型；各角色可以共用同一模型 |
| 调度 | 小模型生成结构化计划，程序执行允许的工具 | Supervisor 制定调查方向，指标与日志专科并行调查，Reviewer 汇总复核 |
| 上下文 | 本次告警、服务与工具目录 | 各专科只接收自己的任务与允许工具；Reviewer 接收调查结果 |
| 执行 | 按工具契约校验参数、执行、检查结果 | 各专科在各自工具集合中取证，再交给复核角色 |
| 报告 | 确定性模板输出工具证据、失败和结论边界 | API 模型依据专科证据形成报告，保留失败与证据来源 |
| 失败策略 | 报错或报告证据不足；不自动调用云 LLM 或远程 embedding | 记录专科/API/工具失败；部分失败不等于完整调查成功 |

`local` 的承诺是本次诊断不依赖云端模型推理和远程 embedding。它不是整个部署的离线保证：MCP 可能查询远程监控、日志或 SSH 服务，告警取数也可能使用云厂商 API。完全离线演示还需要本地工具服务、本地数据和预先下载的模型；应分别记录模型调用和工具网络访问。

云端多 Agent 的区别来自任务、上下文与工具权限隔离，而不是要求加载四份模型或配置四家厂商。把相同输入发给多个相同提示词不能算本实现的专科协作。专科之间不共享可变执行状态，汇总阶段才合并证据。

本地确定性验证只能检查已编码的条件，例如结构、工具参数和取数结果。成功调用工具不代表已证明根因，模板报告也不应将缺乏证据的推测写成确定结论。两个模式均以调查为目标，不应把未授权的变更命令作为诊断步骤。

## API 契约

`GET /api/execution-modes` 返回以下结构，供界面展示模式和配置状态：

```json
{
  "modes": [
    {
      "id": "local",
      "label": "本地小模型",
      "description": "本地规划、工具取证与证据报告",
      "configured": true,
      "model": "qwen-ops"
    },
    {
      "id": "cloud_multi",
      "label": "API 多 Agent",
      "description": "主管、指标专科、日志专科与复核",
      "configured": false,
      "model": "your-api-model"
    }
  ]
}
```

上例中的标签和描述是示意文案。`configured` 表示所需设置是否存在，不是连通性、模型加载、鉴权或工具健康检查。接口不得返回模型服务 API Key。它与其他 `/api` 接口沿用项目的 `X-API-Key` 访问控制。

`POST /api/aiops` 接收 JSON，返回 SSE：

```json
{
  "session_id": "demo-local-001",
  "execution_mode": "local",
  "symptom": "api-gateway-service CPU 使用率 97%，请求延迟升高。",
  "service_name": "api-gateway-service"
}
```

- `execution_mode` 只接受 `local` 或 `cloud_multi`，省略时使用 `local`；未知值或显式 `null` 返回 422。
- `symptom` 为可选告警文本，最多 4000 个字符；省略时使用通用服务巡检任务，不会自动读取外部告警源。
- `service_name` 为可选调查目标，最多 120 个字符。服务名必须经工具发现和校验，不能将用户输入直接当成任意主机命令。未填写时优先使用症状中唯一匹配的已发现服务，否则选服务列表第一项；实际目标会通过状态事件和报告明确展示。涉及多个服务应显式选择目标。
- `session_id` 沿用原会话标识。一次执行另有独立 `run_id`，运行元数据保留 `execution_mode`，便于同会话比较两种模式。

API 总会传入显式模式。Python 兼容入口 `AIOpsService.diagnose(..., execution_mode=None)` 和 `execute(..., execution_mode=None)` 继续走历史流程，以免改变现有脚本；历史流程的本地优先与云端回退行为不能用于证明严格 `local` 模式无云调用。

调用示例（PowerShell 的 `curl` 别名行为不同，使用 `curl.exe`）：

```powershell
$requestBody = '{"session_id":"local-001","execution_mode":"local","symptom":"CPU 97%，请求延迟升高","service_name":"api-gateway-service"}'
curl.exe -N http://127.0.0.1:9900/api/aiops -H "Content-Type: application/json" --data-raw $requestBody
```

如配置了应用访问密钥，再加 `-H "X-API-Key: <应用访问密钥>"`。这是应用的 `API_KEY`，不同于上游模型的 `LLM_API_KEY`。

## 运行设置与 8GB 显存边界

本地模式需要先启动一个与领域权重、聊天模板匹配的 Qwen 推理服务，并让模型 ID 与以下设置一致：

```dotenv
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL=qwen-ops
LOCAL_LLM_WITH_PARAMS=False
```

`LOCAL_LLM_WITH_PARAMS` 应与训练时工具目录格式一致。可使用兼容的 llama.cpp、vLLM 或 Ollama 服务；具体量化格式必须被该后端支持，不能将 GGUF 文件直接视为所有后端都能加载的权重。模型服务器在应用之外启动，应用不负责下载或训练模型。

API 多 Agent 使用现有模型工厂配置：

```dotenv
LLM_PROVIDER=openai
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-api-model
LLM_API_KEY=replace-with-your-provider-key
```

也可使用项目已支持的 Anthropic 兼容 provider 和对应地址。不要把示例域名当作真实服务。两组设置可同时存在，选择模式不会覆盖另一组设置；纯本地诊断不应因已配置云 Key 而自动升级。

RTX 4060 Laptop 8GB 的起始配置建议是**只运行一个 1.5B 4-bit 模型服务，模型并发设为 1**，并从与训练一致的有限上下文开始。云端专科使用 API，不在本机另加载多个大模型。MCP、后端和浏览器也会占用系统资源。

量化文件大小不等于 GPU 峰值显存；KV cache、上下文、batch、CUDA 工作区以及显存共享都会影响 OOM。8GB 是否满足这套实际配置，需要启动后测量，不能由权重文件大小推断吞吐或延迟。CPU GGUF 可作为另一种部署路径，也必须单独测量。

资源验收至少保存 GPU/CPU 型号、驱动、推理后端及版本、模型与量化文件 hash、上下文和 batch 设置、预热方法、峰值 allocated/reserved 显存、进程 RAM、冷启动与暖启动延迟、TTFT、实际 completion token 数、吞吐和超时/OOM。stream chunk 数不能替代 token 数。并发 1 的结果不能外推到多用户服务。

## 验收范围

2026-10-26 本轮实现验证：全套 `pytest tests -q -o addopts=''` 为 **105 passed**；新运行器与 tests 的 Ruff 检查、前端 `npm run lint` / `npm run build`、`git diff --check`、`uv lock --check --offline` 及场景注册校验均通过。已用浏览器检查初始双模式卡片、在线问答入口随模式变化和历史回放。界面截图见 [dual-mode.png](screenshots/dual-mode.png)。

本轮没有运行真实 Qwen 推理、在线模型调用、SSH 或 GPU 性能测试，以上结果不代表真实模型质量与端侧资源验收。全套测试仍有既有 Pydantic/PyMilvus 弃用告警。

`tests/test_execution_modes_api.py` 使用假诊断服务验证 HTTP 默认值、模式透传、非法模式和长度限制，以及配置接口不回显模型密钥；不访问网络。`test_local_runtime.py` 与 `test_cloud_multi.py` 另覆盖本地无云调用、各专科工具隔离、并行汇合、证据失败、超时和取消。

单元测试通过只说明这些代码契约满足测试，不代表模型质量、监控数据真实性、完整网络隔离或线上可靠性。完整集成验收应在显式声明的 demo 数据或测试环境执行，检查实际 SSE、trace、工具结果、报告中的引用和故障处理，并清楚标记数据来源。

## 后续算法实验与结论边界

当前主训练入口仍是普通 LoRA SFT，再进行部署量化。`probe_qlora.py` 的 NF4/双重量化单样本反向探针不等于已经完成 QLoRA 训练；将 QLoRA 列为成果前，需要完整训练入口、配置、adapter、训练日志和独立评测记录。

建议分开回答模型和协作架构两个问题，避免一次对比同时改变所有因素：

1. **模型适配消融**：固定单 Agent 运行器、工具集和提示词，对比 base + 提示词/约束、LoRA、真正 QLoRA。固定数据与训练预算；保存至少多个训练 seed 的结果和失败记录。把确定性 severity 规则作为基线，以判断小模型究竟学到了什么。
2. **量化消融**：固定同一 merged checkpoint 和校准集，对比 FP16 与不同 4-bit 算法。分别报告原始输出、纠错后结构合法性、参数与目标服务正确率、完整执行成功率及资源使用。
3. **协作架构消融**：同一个 API 模型比较单 Agent、两专科并行、加入 Reviewer；使用相同工具、信息范围和明确的总 token/调用预算。不能仅凭“本地小模型 vs API 多 Agent”的差值推断多 Agent 带来提升。
4. **严格数据分割**：现有 test 已用于反复调试和补训练模板，应当作为回归集。另建按来源、规范化模板和原始事件分组的盲测集，分割后再扩写；六类故障均需覆盖，保留组合故障、无故障、否定句、干扰线索、阈值边界与未知服务。
5. **可靠结果**：冻结代码、数据和权重 hash；保存每条任务的原始预测、实际调用、修复与回退、错误、token 来源和时延。报告分母、每类/模板指标及不确定性，不把少数模板的变体当成大量独立事件。云 API 的非确定性应通过重复配对任务记录，不承诺逐字复现。

在完成这些实验前，可准确表述为：“实现本地小模型与 API 多 Agent 两种诊断模式，通过结构化工具执行、上下文隔离和运行追踪支持对比。”暂不声称“零云依赖企业全离线”“QLoRA 已提高任务成功率”“多 Agent 已提升准确率”或“8GB 上已验证生产吞吐”。
