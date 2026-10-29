# AutoDL SSH 端侧 Planner 重训结果

状态：`ANALYZED`。本轮训练、离线评估和合并模型加载已经完成；真实 AutoDL SSH 联调、正式 RAG 入库和多次独立重跑仍未完成，因此本文件不把结果写成生产验证或泛化准确率。

## 产物

- 数据集：`training/data_autodl_ssh/`
- 数据集 manifest：168 条，`train=136 / val=16 / test=16`，seed `42`
- adapter：`training/out/qwen15-autodl-ssh-v1/`
- 合并模型：`training/out/qwen15-autodl-ssh-v1/merged/`
- 合并模型已用 `AutoModelForCausalLM.from_pretrained` 在 CPU 上加载成功，参数量 `1,543,714,304`
- 官方文档来源清单：`docs/claude-code-handoff/AUTODL_OFFICIAL_SOURCES.md`

数据集文件 hash：

| 文件 | SHA-256 |
|---|---|
| train.jsonl | `c48e101527c76f81fa4a202dc4a3441d6a7da83eb18b976ffd2e4d4970747f8d` |
| val.jsonl | `26e1317299af0c70fac484a53156fbac1677eadb48a1d25a6cd54ab8b5e9c2f9` |
| test.jsonl | `5f4c0f2d4852ea0a255945caa18fb01574e939e9e365c66e7bed94475c35085b` |

训练数据只使用 AutoDL SSH 目标可用的工具目录：`get_system_info`、`get_process_list`、`check_disk_usage`、`search_system_logs`、`read_log_file`，以及发现、时间和知识检索工具；没有把云监控 API 工具放回 AutoDL 目标。

## 可复现命令

生成数据集：

```powershell
./.venv-train/Scripts/python.exe training/build_autodl_dataset.py --output training/data_autodl_ssh --per-template 8 --seed 42
```

基线评估：

```powershell
./.venv-train/Scripts/python.exe training/eval_lora.py --model E:/models/Qwen/Qwen2.5-1.5B-Instruct --adapter training/out/qwen15-lora-v9 --data-dir training/data_autodl_ssh --split test --out training/out/eval_autodl_v9.json --max-new-tokens 512
```

训练：

```powershell
./.venv-train/Scripts/python.exe training/train_lora.py --model E:/models/Qwen/Qwen2.5-1.5B-Instruct --output training/out/qwen15-autodl-ssh-v1 --data-dir training/data_autodl_ssh --epochs 2 --max-length 1536 --batch-size 1 --grad-accum 8 --lora-r 8 --lora-alpha 16 --attn-only --fp16 --merge
```

AutoDL 专项评估：

```powershell
./.venv-train/Scripts/python.exe training/eval_lora.py --model E:/models/Qwen/Qwen2.5-1.5B-Instruct --adapter training/out/qwen15-autodl-ssh-v1 --data-dir training/data_autodl_ssh --split test --out training/out/eval_autodl_v1.json --max-new-tokens 512
```

旧数据回归抽样：

```powershell
./.venv-train/Scripts/python.exe training/eval_lora.py --model E:/models/Qwen/Qwen2.5-1.5B-Instruct --adapter training/out/qwen15-autodl-ssh-v1 --data-dir training/data --split test --limit 30 --out training/out/eval_autodl_v1_regression30.json --max-new-tokens 512
```

## 结果

| 指标 | 旧 `qwen15-lora-v9`，AutoDL 集 | 新 `qwen15-autodl-ssh-v1`，AutoDL 集 |
|---|---:|---:|
| n | 16 | 16 |
| JSON 解析率 | 0.812 | 1.000 |
| schema 有效率 | 0.750 | 0.938 |
| tool F1 | 0.410 | 0.766 |
| required tool recall | 0.372 | 0.691 |
| essential tool recall | 0.281 | 0.729 |
| critical tool recall | 0.260 | 0.709 |
| plan OK rate | 0.000 | 0.250 |
| 参数 exact | 0.557 | 0.771 |
| severity accuracy | 0.062 | 0.625 |
| category accuracy | 0.688 | 1.000 |

新 adapter 在旧的通用/Tencent 分布上进行 30 条回归抽样时，schema 有效率为 `0`、tool F1 为 `0.167`、severity accuracy 为 `0.033`。这说明它是 AutoDL SSH 专用 adapter，不能直接替换原来的通用 adapter；生产配置应按目标类型选择 adapter 或模型服务。

训练过程最终验证损失为 `0.1179746761918068`，全程 34 个 optimizer steps。这个数值只用于记录本次训练过程，不能当作线上质量指标。

## 工程核对

- 后端针对目标绑定、SSH 安全和 JSON 提取的回归：`69 passed, 1 warning`。
- ruff 的 `F821/F811/F841` 检查通过；`E501` 仍报告现有长行，未把无关格式清理混入本轮功能改动。
- 初始训练命令生成的 `merged` 目录曾只有配置文件。之后使用 CPU 合并路径重新生成 2 个 safetensors 分片，并用 Transformers 实际加载验证；因此后续启动端侧模型应使用当前 `merged` 目录或 adapter 目录，不使用早先的残缺目录状态。

## AutoDL 官方知识库边界

当前已经建立官方来源清单，但还没有把网页正文抓取、规范化并写入 Milvus。清单包括 AutoDL 环境、快速开始、镜像、实例数据、GPU、SCP、SSH 问答、文件系统和 Pro API 文档。

入库时要保留 raw HTML、规范化 Markdown、source URL、抓取时间、HTTP 元数据、原文 hash、revision 和 license 状态，并使用 `product=autodl`、`os_family=linux`、`target_kind=ssh` 等 metadata。官方手册只能支撑平台操作和处置知识，不能代替当前实例的 CPU、内存、磁盘、GPU 或日志观测；这些仍必须来自 SSH 只读工具。

## 下一步

1. 在不记录密钥内容的前提下补齐 AutoDL 实例的 SSH 端口、用户和 key 路径配置，完成一次授权的只读真实联调。
2. 抓取并审核 `AUTODL_OFFICIAL_SOURCES.md` 中的官方页面，写入独立的 AutoDL 语料 manifest 和 Milvus revision。
3. 用至少 30 条固定问题评估 AutoDL 文档检索，单独报告有答案、无答案、引用支持和版本过滤结果。
4. 用当前 `merged` 模型启动端侧 Planner，和云端模式跑同一批真实 SSH 只读任务；报告计划质量、工具成功率、延迟和失败原因。
5. 将 AutoDL adapter 作为按目标类型选择的模型版本，保留原通用 adapter，避免目标域训练造成回归。
