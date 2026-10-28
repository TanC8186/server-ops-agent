# On-Call Agent

一个**可评测**的 AIOps 诊断 Agent：LangGraph 编排 + MCP 工具平面 + 本地小模型快路径。
重点不是"接了多少框架"，而是每个结论都能被 `make eval` 复算、每次运行都能按 `run_id` 回放。

```
web (React + Vite)  ──SSE──▶  FastAPI (:9900)
                                  │
                     ┌────────────┼─────────────┐
                     ▼            ▼             ▼
              LangGraph 图    Milvus RAG    traces/*.jsonl
       planner→executor→verifier→replanner
                     │
        ┌────────────┼──────────────┐
        ▼            ▼              ▼
   Monitor MCP   CLS MCP        SSH MCP        (FastMCP, 20 tools)
   腾讯云监控     腾讯云日志      远程只读巡检
```

## 为什么这个项目值得看

| 能力 | 实现 | 怎么验证 |
|---|---|---|
| **结构化计划** | Planner 输出 typed `PlanStep {kind, tool, args}`，对上 MCP 注册表校验 | `tests/test_typed_plan.py` |
| **真 replan** | Verifier 做确定性校验（空结果 / `ok=false` / `unavailable`），失败才触发重写剩余计划，预算 2 次 | `tests/test_typed_plan.py::test_replanner_*` |
| **工具快路径** | tool 步骤直接执行，不再让 LLM 二次翻译成工具调用 | 单次诊断 LLM 调用 ~12 → ≤5 |
| **可回放** | 每 run 一份 `logs/traces/<run_id>.jsonl`：节点、工具、决策、token、报告 | 前端"最近运行"点开即回放 |
| **评测** | 5 个场景 + 自动判分（tool P/R/F1、facts、groundedness、tokens） | `python scripts/run_eval.py --live` |
| **溯源** | 所有 MCP 结果带 `source: tencent_cloud / demo / unavailable`，没有数据就报 `unavailable` | `tests/test_knowledge_tool.py` |
| **安全** | SSH 参数白名单 + 路径规范化 + 10 条注入 payload 测试；可选 `X-API-Key` | `tests/test_ssh_safety.py` |

## 快速开始

### 双模式诊断入口

首页可选择 **本地端侧**（`local`）或 **在线多智能体**（`cloud_multi`），并输入症状与目标服务。

- 本地端侧：一个 Qwen 本地服务生成计划，程序执行只读工具并输出证据报告；本地模型失败时停止，不会调用在线 LLM 或远程 embedding。
- 在线多智能体：API 模型分别承担 Supervisor、指标专科、日志专科、Reviewer；专科拥有独立上下文和工具权限，汇合证据后生成报告，无需在本机加载多个模型。
- 两种模式共享服务发现、参数验证、工具预算和证据记录；模式随请求传入，不能通过切换界面改变另一运行的配置。
- “本地”限定模型推理路径；MCP 仍可连接远程监控、日志与 SSH。界面的“已配置”不等于模型连通性验证通过。

配置 `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_MODEL` 后可使用本地模式；在线模式使用 `LLM_*`。
模式、运行方法、8GB 显存边界与后续实验见 [双模式说明](docs/DUAL_MODE.md)。

以下历史模型指标来自旧评测；新增双模式没有据此宣称诊断准确率或吞吐提升。

```bash
# 1. 依赖
uv venv && uv pip install -e ".[dev]"

# 2. 配置
cp .env.example .env    # 填 LLM_API_KEY；没有云凭证时设 DEMO_MODE=1

# 3. 起 MCP 工具（三个终端）
.venv/Scripts/python.exe servers/monitor_server.py   # :8004
.venv/Scripts/python.exe servers/cls_server.py       # :8003
.venv/Scripts/python.exe servers/ssh_server.py       # :8005

# 4. 起后端
.venv/Scripts/python.exe -m uvicorn app.main:app --port 9900

# 5. 构建前端（产物由后端直接托管）
cd web && npm install && npm run build && cd ..
```

开发前端时用 `cd web && npm run dev`（:5173，已配置 `/api` 代理到 :9900）。

## 验证

后端（Python 3.11–3.13，Windows 用 `.venv`）：

```bash
.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider -q -o addopts=""   # 回归测试
.venv/Scripts/python.exe -m ruff check servers/ app/ scripts/ --select F821,F811,F841
.venv/Scripts/python.exe scripts/validate_eval.py     # 场景 ↔ 工具注册表 ↔ 文档
```

前端（**Node v22.17.0**；`.ts` 测试文件需要 `--experimental-strip-types`，
已写进 `web/package.json` 的 `test` 脚本，不要直接 `node --test src/*.ts`）：

```bash
cd web
npm run build      # tsc -b && vite build
npm run lint       # oxlint
npm test           # node --experimental-strip-types --test src/*.test.ts
```

知识库语料（M3）与离线检索基线（不依赖 Milvus）：

```bash
.venv/Scripts/python.exe scripts/fetch_corpus.py --stage core        # 只抓目录里的明确页面
.venv/Scripts/python.exe scripts/normalize_corpus.py                # 快照 -> Markdown + notices
.venv/Scripts/python.exe scripts/eval_retrieval.py --k 3            # 切分+embedding+cosine 基线
```

RAG 三层评测（检索 / 门控 / 生成，都需要真实 Milvus；生成层还需要真实 LLM）：

```bash
# 1) 在独立的 calibration 集上拟合分域阈值（不在 blind 上拟合，脚本会硬性拒绝）
.venv/Scripts/python.exe scripts/eval_calibration.py --store data/acceptance/milvus-e2e-store.json
# 2) 用某组阈值跑任一题集（--set 改题集；blind 的 sha256 由测试锁定，不会被改）
.venv/Scripts/python.exe scripts/eval_blind.py \
    --set eval/rag/calibrate-dev2.jsonl --threshold 0.5135 \
    --store data/acceptance/milvus-e2e-store.json
# 3) 生成层：真实模型 + 真实 Milvus，引用支持率 / 事实正确率 / 无答案误答率分别报告
.venv/Scripts/python.exe scripts/eval_generation.py --limit 12 \
    --store data/acceptance/milvus-e2e-store.json
```

本轮（G5）的真实数字、分母与**不能据此下的结论**见
[G5_RESULTS_2026-10-28.md](docs/claude-code-handoff/G5_RESULTS_2026-10-28.md)。
工程维护（Pydantic/Ruff/Docker/CI/MCP 同名实例）的实测命令见
[G5_INFRA_NOTES.md](docs/claude-code-handoff/G5_INFRA_NOTES.md)。
G6 的权限与聊天流一致性收尾（作业 ACL、诊断 run 与聊天流 run 拆分、评测快照一致性）
见 [G6_RESULTS_2026-10-28.md](docs/claude-code-handoff/G6_RESULTS_2026-10-28.md)。
G7 的上传归属一致性、评测快照边界与镜像 provenance（含标准 Dockerfile 构建的
commit/digest）见 [G7_RESULTS_2026-10-29.md](docs/claude-code-handoff/G7_RESULTS_2026-10-29.md)。

生成层产物里的 `kind` 是 `generation_eval_llm_sampled`；读取 `gate_profile` 前会核对
题集 sha256、语料 manifest、embedding descriptor 与当前 store，对不上直接停止。

真实运行过的结果与未验证项见 [VALIDATION_RESULTS.md](docs/claude-code-handoff/VALIDATION_RESULTS.md)，
逐项实施状态见 [IMPLEMENTATION_STATUS.md](docs/claude-code-handoff/IMPLEMENTATION_STATUS.md)，
浏览器检查见 [BROWSER_CHECK.md](docs/BROWSER_CHECK.md)，部署见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。

`scripts/run_eval.py --live` 需要真实 LLM 凭证，本仓库没有附带任何实测数字。

## 模型后端

所有 LLM 调用都走 `app/core/llm_factory.py` 一个入口，provider 由 `.env` 决定：

| provider | 适用 | 关键配置 |
|---|---|---|
| `anthropic`（默认） | Messages API，含第三方 Anthropic 兼容网关 | `LLM_BASE_URL=https://www.autodl.art/api/v1/anthropic` |
| `openai` | DeepSeek / DashScope / 本地 vLLM / Ollama | `LLM_PROVIDER=openai` + `LLM_BASE_URL` |

```bash
LLM_PROVIDER=anthropic
LLM_BASE_URL=https://www.autodl.art/api/v1/anthropic
LLM_API_KEY=...
LLM_MODEL=DeepSeek-V4.1-Flash-1017
```

> 网关兼容性：第三方 Anthropic 网关会在 `message_delta` 里返回 dict 形式的
> `context_management`，而 `langchain-anthropic` 1.7.6 假设它是 pydantic 对象，
> 直接 `.model_dump()` 会抛 `AttributeError`。`llm_factory` 里的
> `_CompatChatAnthropic` 在解析前摘掉该字段，流式与 tool calling 都恢复正常。

## 本地小模型（可选）

`training/` 是一个独立的子项目：Qwen2.5-1.5B-Instruct 做 LoRA SFT，输出
`{severity, category, plan[]}`，通过 vLLM 提供 OpenAI 兼容接口，作为 Planner 的本地快路径；
工具/参数校验失败时自动回退云端。

| 指标（v9，held-out 178 条） | fp16 (HF) | Q4_K_M (AutoRound→GGUF, CPU) | AWQ/SignRound (vLLM) | 说明 |
|---|---:|---:|---:|---|
| Schema 合法 | **1.000** | **1.000** | 0.989 | |
| **关键工具召回** | **1.000** | **1.000** | 0.989 | 训练集里 ≥90% 出现的工具 |
| **Plan OK** | **1.000** | **1.000** | 0.989 | 关键工具全覆盖 + 多余 ≤2 |
| Arg exact | 0.843 | **0.858** | 0.778 | |
| **Severity** | **0.989** | 0.787 | **0.989** | Q4 在 OOM 边界（92% vs 95%）上掉 |
| Category | 1.000 | 1.000 | 0.989 | |

三个后端是同一份 v9 权重、同一个 test split、同一套判分。

**severity 这一行要打折看。** test 只有 6 种句式、每种 30 条。把题干换一种说法
（`training/paraphrase_rules.py`，规则改写、可复现），severity 从 0.989 掉到 0.865；
schema / 关键工具 / category 完全不变。受控探针（只把分句顺序换一下）显示：
数字在前的语序准确率 **0.966**，线索在前的语序 **0.534** —— 模型学的是句式，
不是"OOM 且 ≥95%"这条逻辑。原因是 train 里 90 条同类样本**全是**一种语序。

所以对外只讲两件事：plan 类指标（稳，改写前后都是 1.000），和"4-bit 无损"
（三个后端互相印证）。severity 的绝对值不要单独引用。
**4-bit 不再有系统性损失**：AWQ/SignRound 在共同 149 条上 schema / 关键工具 / plan_ok /
severity 全是 **1.000**，只比 fp16 低 arg exact。v8 时代"4-bit 掉 13 点 severity"的结论
**是训练数据没覆盖到边界，不是比特数**——v9 补了否定句与 OOM 边界样本后差距消失。

CPU 端要用 AutoRound 产的 GGUF（`training/quantize_gguf.py`），不要用 `llama-quantize`：
同样是 Q4_K_M，前者 schema 1.000，后者 0.664。

推理性能（同一台机、同一轮测量，`max_tokens=128`、每档 16 请求）：

| 推理（RTX 4060 8GB / CPU） | vLLM fp16 | vLLM AWQ W4A16 | llama.cpp Q4 (CPU) |
|---|---:|---:|---:|
| 模型大小 | 2.9 GB | **1.15 GB** | **941 MB** |
| TTFT p50 @1 | 71.1 ms | 77.3 ms | 34.7 ms |
| TTFT p50 @8 | 73.6 ms | **69.7 ms** | 2384 ms |
| TPOT p50 @8 | 16.7 ms | **15.1 ms** | 38.9 ms |
| 吞吐 @8 | 464 tok/s | **514 tok/s** | 102 tok/s |

（性能表是 v8 权重测的；v9 的模型结构与量化配置相同，未重测。）

### 接入效果（5 个场景，本地 = Q4_K_M GGUF on CPU）

| | 云端 Planner | 本地 Planner |
|---|---:|---:|
| 本地命中 | — | 5/5（无回退） |
| 平均工具 F1 | 0.39 | **0.71** |
| 总 tokens | 131,263 | **47,020（−64%）** |
| 平均步骤 | 8.0 | 2.6 |

失败场景两边一样多，原因是评测集的 gold 用了 `search_log`，而 690 条训练数据里
没有这个工具的样本（模型学的是 `search_service_logs` / `search_system_logs`）。
详情见 `training/RESULTS.md`。

接入方式：

```bash
# GPU：vLLM 加载 AWQ 产物
wsl ~/vllm-venv/bin/vllm serve training/out/qwen15-v9-signround \
    --served-model-name qwen-ops-v9-awq --quantization awq \
    --max-model-len 2048 --enforce-eager --port 8000

# CPU：llama.cpp 加载 AutoRound 产的 GGUF
llama-server.exe -m training/out/qwen15-v9-ops-Q4_K_M-autoround.gguf -t 6 -c 4096 --alias qwen-ops-v9

# env 必须加在跑评测的那个进程上
LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1 LOCAL_LLM_MODEL=qwen-ops-v9-awq \
  .venv/Scripts/python.exe -m uvicorn app.main:app --port 9900
```

## 目录

```
app/
  agent/aiops/       planner · executor · verifier · replanner · plan_schema
  agent/             MCP 客户端 + 进程级工具缓存
  api/               chat · aiops · file · health · runs
  services/          AIOps 编排 · RAG · 向量索引
  observability.py   per-run JSONL trace
servers/             3 个 FastMCP server（20 个工具）
aiops-docs/          运维知识库（6 篇）
eval/scenarios/      YAML 故障场景 + 阴性对照
scripts/             validate_eval.py · run_eval.py
training/            LoRA 数据管线 · 训练 · 评测 · 服务基准
tests/               回归测试
web/                 React + Vite 前端
```

## 当前限制

- **知识问答会话历史落盘到 JSON**（`data/chat/`，有 TTL 与条数上限），但仍是单进程文件锁；
  多实例部署前必须换成共享存储。
- **聊天流只支持一个活动标签页**：同一浏览器多标签页共享 `oncall.chat.pending`，
  后发的流会覆盖续读位置；不做跨标签页同步。
- **鉴权可选**：`AUTH_MODE=deployment` 时未配置密钥会拒绝启动；`dev` 模式不做鉴权。
  有并发准入闸门（`max_concurrent_diagnostics`），没有按 IP 的限流。
- **`/health` 的 `status=healthy` 只表示进程活着**：Milvus/RAG/模型可用性在
  `data.capabilities` 里单独报告（`rag` 可能是 `unavailable`/`invalid_index`）。
  容器 `healthy` 与“知识库可用”是两件事，验收报告必须分开写。
- **评测集是合成的**：5 条诊断场景 + 240 条训练数据均为模板生成并校验，不是真实工单。
  `eval/rag/dev.jsonl` 的 14 条检索问题同样是维护者编写，且阈值在该集上拟合，
  **不是盲测结论**。
- **RAG 未做混合检索**：只有 dense top-k + 适用性门控，没有 rerank。门控阈值需要在
  **独立的 calibration 集**上拟合（`scripts/eval_calibration.py`），不能拿 blind 集调参；
  当前两组工作点见 [G5_RESULTS](docs/claude-code-handoff/G5_RESULTS_2026-10-28.md) §1.3。
- **真实 Milvus 已验证检索/写入/回滚/失败清理**（v2.5.10，见
  [MILVUS_ACCEPTANCE_2026-10-27.md](docs/claude-code-handoff/MILVUS_ACCEPTANCE_2026-10-27.md)），
  但仍是**单机、11 篇文档、单一 embedding 模型**，没有容量/并发/多副本数据。
- **读权限是 `owner OR ACL`，写权限只认 owner**：上传时 owner 与文档级 ACL 一起写；
  ACL 补写失败也不会让上传者看不到自己的文档（早期"ACL 非空即忽略 owner"的写法
  会让人 202 成功却什么都做不了，已修）。
- **多租户做到“文档级 ACL + 检索前过滤”，问答与诊断共用同一可见性规则**：
  `KNOWLEDGE_TENANT_MODE=multi` 下无 owner 的文档对非 dev principal fail closed；
  `/api/chat` 与诊断路径的 `retrieve_knowledge` 都经 `VectorStoreRetriever(principal_id=…)`，
  没有第二套过滤器。**读写分开**：读按 owner/ACL 判定（不可见统一 404），
  activate/delete/retry/覆盖上传只认 owner（无 owner 时才认 ACL 成员），
  所以"共享阅读"不会顺带拿到"回滚/删除"。**部署形态仍要求单 API Key**
  （一个进程只有一个生效密钥），因此“A 看不到 B 的文档”在测试里用不同 principal_id 验证，
  不是多用户账号体系。
- **SSH 只读联调未执行**：没有获授权的 staging 主机，`servers/ssh_server.py` 的安全边界只有单测。
- **镜像 10.7 GB**：仍包含 CUDA 版 torch（应用只用 CPU）；替换为 CPU wheel 的配方已实测可行，
  但会改变全平台依赖解析，尚未落地。镜像带 `org.opencontainers.image.revision` 标签，
  可用 `docker image inspect` 反查它是哪次 commit 构建的。
  但会改变全平台依赖解析，尚未落地。
- **11 页核心语料仍未完成许可人工核对**：其中 4 份（SD-02、LK-01、RH-01 与 RH-02 待抓）
  的许可状态是 `needs_review`，在此之前只能作为内部验证数据。
- **0.5B 尚未跑通**：对学习率敏感，两次训练分别发散/停滞；当前用 1.5B + 4-bit 作为端侧方案。
- **本地 Planner 的评测口径与训练集不一致**：5 个场景里 4 个的 gold 用了 `search_log`，
  而训练数据（690 条）里它出现 0 次；这会让 `task_success` 在本地/云端都偏低。

完整路线图见 [BACKLOG.md](BACKLOG.md)，环境与算力限制见 [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)。

## License

MIT © TanC
