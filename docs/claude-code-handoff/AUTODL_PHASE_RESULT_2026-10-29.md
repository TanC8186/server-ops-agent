# AutoDL RAG E2E 阶段结果（2026-10-29）

> 状态口径：`VERIFIED` = 真实依赖、固定输入、可重跑、证据完整；
> `ANALYZED` = 完成过一次分析/实验，但独立重跑或真实联调不足；
> `UNVERIFIED` = 没有真实证据，不能写成已完成。

## 0. 先回答任务书 §9 的三个问题

**1. 真实 AutoDL SSH 目标能否被正确绑定并完成只读观测？**
→ **VERIFIED（绑定 + 只读观测）/ UNVERIFIED（生产链路完整走通）**。
SSH 只读观测真实跑通：key 认证 1.74 s 连上，`uname`/`nproc`/`df`/`free`/`ps`/
`/proc/loadavg`/cgroup 限制全部返回真实值，单命令 0.2–0.3 s。
目标绑定到 `autodl-ssh` 正常。**但**发现并修复了三个会让"观测"失真的代码缺陷
（见 §2），修复前那些观测会被系统判成"非实时证据"。

**2. AutoDL 官方文档是否已作为带来源和版本的知识进入 RAG？**
→ **UNVERIFIED（未入库）**。MCP 的抓取白名单、manifest 硬编码、metadata 契约
三处阻塞，`knowledge/sources/` 至今不存在。**已验证官方文档本身可抓**（见 §3.1），
但"已进入 RAG"不成立。

**3. 本地 AutoDL adapter 与 cloud_multi 在同一批真实任务上的差异是什么？**
→ **UNVERIFIED**。本轮没有跑同题对比。**但**推翻了任务书 §1 引用的旧数字
（见 §1），并修掉了让那些数字失效的 train/serve skew（见 §2.4）。

## 1. 【重要】任务书 §1 的成果表不能作为证据使用

任务书引用了"新 AutoDL adapter 优于原 v9"的离线表。本轮用项目**自己的 scorer**
独立复算，两条结论必须推翻：

### 1.1 Tool F1 0.766 没有超过平凡基线（P0，已复现）

用 `training/scoring.py` 的同一套判分，**冻结常数预测**在同样 16 条测试上：

| 预测方式 | Tool F1 |
|---|---:|
| 永远输出训练集最常见工具集（`get_process_list, get_system_info, list_all_services, retrieve_knowledge`，42/136 行） | **0.800** |
| 新 AutoDL adapter（任务书引用值） | 0.766 |

常数预测（0.800）**高于** adapter（0.766）。也就是说这个 F1 数字没有证明
模型学到了任何东西。

（对抗验证还指出：任务书里"常数也能赢 required_tool_recall 0.850 /
essential 0.750 / critical 0.771 / arg_exact 0.906"这句是**错的**，
三个"幸存指标"里有两个常数其实不占优。已按修正后表述记录。）

### 1.2 16 条"held-out"事实上不是 held-out（P0，已复现）

| 检查 | 结果 |
|---|---|
| 按 `(family, template_index)` 分组 | train 136 组 / test 16 组 / 重叠 0 组 |
| 但按 **assistant 输出文本**比对 | **test 16/16 行与训练集逐字相同** |
| 按 user 输入文本比对 | test 8/16 行与训练集逐字相同 |

`template_index` 唯一只代表"每个模板一条"，**不同模板索引复用了同一段输出**
（21 个模板 × 8 次重复 = 168 行，模板数远少于行数）。所以"零模板重叠"这个
表面结论是假的，实际是 100% 输出泄漏。

→ **这 16 条的 F1 / recall / severity 全部不能当泛化证据。**

## 2. 已修复的真实缺陷（全部有实测复现，均已提交）

### 2.1 真实 SSH 观测被判成非实时证据（P0，已修）

- **现象**：AutoDL 目标下 SSH 工具真的连上主机并执行成功，但报告整体降级为
  "证据不足"，证据表 `live=false`。
- **原因**：三重叠加
  1. SSH 工具返回纯文本/dict，`payload_sources()` 取不到来源 → `source=unspecified`；
  2. `unspecified` 在 `NON_LIVE_SOURCES` 里 → 直接判非实时；
  3. `evidence_is_live()` 末尾还有 `return evidence.get("target") != "configured_ssh_endpoint"`
     —— 凡是 SSH 证据一律否决。
- **验证**：`evidence_is_live({'ok':True,'source':'ssh','target':'configured_ssh_endpoint'})`
  旧实现返回 `False`，而腾讯云证据返回 `True`。方向完全相反。
- **修法**：SSH 工具成功时标 `source=ssh`（`tool_contract` 文档早就把 ssh 列为
  合法来源，只是一直没人写入）；`evidence_is_live` 认可 ssh 来源。
- **实测**：ssh 证据 → `True`；未标来源的 ssh → `False`（保持原判）；云证据 → `True`。

### 2.2 日志文件不存在被伪造成"查到一条日志"（P1，已修）

- **现象**：`search_system_logs` 返回 `ok=true, count=1, entries=[{"message":"(empty)"}]`。
- **原因**：`tail ... 2>/dev/null || echo 'Log file not found'`，那句提示被当成
  正常日志条目。
- **环境事实**：AutoDL 容器**没有** `/var/log/syslog`、`/var/log/auth.log`
  （`test -f` 均为 no），有 `/var/log/journal` 目录。这是环境事实，不是 bug。
- **修法**：用哨兵前缀把探测与读取合并成一条命令，文件不存在时明确
  `status=unavailable` + 原因。
- **实测**：`syslog` / `auth` 均返回 `ok=False status=unavailable "日志文件不存在"`。

### 2.3 工具目录不按目标收窄（P0，已修，上一轮提交 25231af）

- `services.json` 的 `type: ssh` 之前**没有任何代码消费**，`Target` 没有该字段。
- 修：`Target.kind` + `RunTools.tools_for_target()`，ssh 目标只给
  `SSH_TOOLS ∪ TARGET_AGNOSTIC_TOOLS`；cloud 目标保持全量。

### 2.4 【本轮最关键】AutoDL adapter 的 train/serve prompt skew（P0，已修）

之前所有"AutoDL 模型效果一般"的实测都不可信——**根本没在训练同款 prompt 上测**。

| 维度 | 训练数据 | 运行时（修复前） |
|---|---|---|
| 工具目录 | `compact_catalog(..., with_params=True)`，带 `check_disk_usage(path?: string)` 签名 | `LOCAL_LLM_WITH_PARAMS=False`，**无签名** |
| user turn | 六行：平台／采集方式／目标／目标角色／告警文本／请只输出 JSON | 三行：`服务: x` |

修法：`_with_params_for(model)` 与 `_user_turn(model, service, symptom, target_kind)`
按模型名分派，AutoDL adapter 用训练同款，通用模型保持历史格式。

**修复后效果**（训练同款 prompt，AutoDL adapter 起在 `:8002`）：

| 症状 | severity/category | 计划工具 |
|---|---|---|
| CPU 负载高 | P1/cpu | `get_system_info, search_system_logs` |
| 内存 95% OOM | P1/memory | `get_system_info, search_system_logs, get_process_list` |
| 磁盘快满 | P1/disk | `check_disk_usage, get_system_info, search_system_logs` |
| 查错误日志 | P3/healthy | `get_system_info, search_system_logs` |

**零腾讯云工具**。磁盘场景此前会无限重复直到截断（1536 token 都不够），
修复后稳定输出。

### 2.5 SSH 连接不可靠 + 会静默换 key（P1，已修）

两个实测确认的问题：

**（a）服务端限流被报成"主机不可达"**
AutoDL 网关在并发握手时返回明文 ``Exceeded MaxStartups``，paramiko 包成笼统的
``Error reading SSH protocol banner``。实测：TCP 层 10/10 可连，但 10 次握手里
2–4 次收到 MaxStartups。生产函数 ``_get_ssh_client()`` 连续 10 次只有 5 次成功。

修：把这种情况翻译成可诊断的错误（"服务端限流或过载，非主机不可达"），并做
**一次**有界重连（间隔 0.5s）。实测成功率 2/6 → **8/10**。
不做无限重试：那会掩盖真实故障，也会加重网关限流。

**（b）paramiko 默认会静默换用 ~/.ssh 里的其它 key**
``client.connect()`` 没关 ``look_for_keys`` / ``allow_agent``。实测把
``key_filename`` 指向一个未授权的 key，连接**依然成功**（用了 ``~/.ssh`` 里的另一把）。
于是"本次用的是哪把钥匙"无法证明，SSH 验收证据不可信。

修：显式 ``allow_agent=False, look_for_keys=False``，只认配置的那一把。

### 2.6 测试竞态与会话残留（已修 / 已清理）

- `test_acl_seal_failure_keeps_uploader_visibility` 上传后没等后台 job 完成就断言
  可见性，高负载下偶发失败。补 `wait_for_job`（同文件第 146 行早已这么等）。
  连续两次全量运行 920 passed。
- `SSH_KEY_FILE` 一个变量承担"宿主挂载源"和"容器内路径"两种语义，导致容器内
  ENOENT。已分离（上一轮提交）。
- 子 Agent 在只读任务中误写了 `knowledge/manifests/import-2026-10-29.json`，
  已从提交剔除并删除。

## 3. RAG：能抓但没入库

### 3.1 官方文档可抓（VERIFIED）

实测 9 个官方源中的 4 个：HTTP 200，48–55 KB HTML，**服务端渲染**。
但直接去标签得到的大半是**侧边栏导航**；正文在 `<article>` 里。
`docs/env` 的 `<article>` 含真正正文（系统盘/数据盘、`/root/autodl-tmp`、
容器内不支持 Docker 等）。

→ **抓取成功 ≠ 语料可用**，必须检查"可见字符是正文还是导航"。

### 3.2 三处阻塞（P0，均未修）

| 阻塞 | 证据 | 状态 |
|---|---|---|
| 抓取白名单不含 autodl.com | `scripts/fetch_corpus.py:49-58` 八个域名无 autodl；`:110-111` 抛 `域名不在白名单` | 未修 |
| manifest 名硬编码 | `normalize_corpus.py:243` 写死 `import-2026-10-26.json`，而 fetch 写 `import-<UTC日期>.json` | 未修 |
| metadata 契约不含 `target_kind` | `knowledge_store.py:152-157` `METADATA_TEXT_FIELDS` 只有 os_family/product/product_version/license_id | 未修 |

- `knowledge/sources/` **不存在**；`knowledge/normalized/` 里 grep `autodl` → **0 命中**。
- `RetrievalFilters` 只有 os_family/product/product_version/at_date，**无 target_kind**。
- 新增的 `target_context_for()` 等代码不受影响，但任务书 §3.4 要求的
  `target_kind=ssh` 过滤**今天无法实现**。

## 4. 双模式对比：未执行

- 上一轮的 `eval/mode_benchmark/*.json` 是在**腾讯云目录**下测的，不能当 AutoDL 结果。
- 本轮未跑同题对比。原因：RAG 未入库、prompt skew 刚刚才修好，
  在未修复的链路上跑出来的数字依然是错的（这正是 §1 教训的复现）。
- **下一步**：等 §3.2 三处阻塞解开、adapter 路由接好后，用固定题集重跑。

## 5. 已实测的 AutoDL 主机事实（供后续验收参照）

| 项 | 实测值 |
|---|---|
| 主机名 | `autodl-container-73d6479980-a8bf3ae3` |
| 内核 | Linux 5.15.0-105-generic |
| CPU 配额（cgroup） | `50000 100000` = **0.5 core** |
| 内存配额（cgroup） | `2147483648` = **2 GiB** |
| `nproc` | 1（与 cgroup 一致） |
| 系统盘 | overlay 30 GB，已用 2.7 GB（9%） |
| 数据盘 | `/dev/md0` 150 GB，已用 18 GB（12%），挂在 `/root/autodl-tmp` |
| GPU | **无**（`nvidia-smi` 不存在，是 CPU 容器） |
| 传统 syslog | **不存在**（`/var/log/syslog`、`auth.log` 均无） |
| journal | `/var/log/journal` 存在 |
| load average | 21.87 / 23.99 / 24.35（宿主机视角，含他人任务；已运行 553 天） |
| SSH 连接 | key 认证 1.74 s；单命令 0.2–0.3 s |

**注意**：`free -m` 显示 1 TB 是**宿主机**内存，容器真实上限是 cgroup 的 2 GiB。
判断容器配额永远以 cgroup 为准。

## 6. 测试与构建

| 项 | 结果 |
|---|---|
| 后端 | **920 passed, 1 skipped**（连续两次全量运行） |
| ruff (F821/F811/F841) | All checks passed |
| 新增测试 | ssh live 判定 1 条；日志缺失 2 条；prompt skew 1 条；SSH 工具范围 13 条（上轮） |

## 7. 未完成 / 不能写进简历的结论

**未完成（UNVERIFIED）：**

1. AutoDL 官方文档**未入库**（三处阻塞未解）。
2. RAG 固定题集（30 条）**未建立、未评测**。
3. 本地 adapter 与 cloud_multi **未做同题对比**。
4. `target.kind=ssh + product=autodl` 的**产品级路由未实现**：`Target` 无 `product`
   字段，`services.json` 也没有，只有 `kind=ssh` 的模型名路由。
5. `check_disk_usage` 空参在 Linux 上默认 `C:/`（Windows 默认值），未修。
6. ~~SSH 连接约 50% 概率报 banner~~ → **已修**（见 §2.5a：归因 + 一次有界重连，8/10）。
7. ~~paramiko 未关 look_for_keys/allow_agent~~ → **已修**（见 §2.5b）。
8. `get_system_info` 用 `free`/`df`，在容器里读到宿主机数字（2.5 说的 cgroup 问题）。
9. SSH 日志工具只支持 syslog/auth/kern，不支持 journalctl——而这台机器只有 journal。

**不能写进简历：**

- 不能说"AutoDL adapter Tool F1 0.766 提升了工具选择准确率"——常数基线 0.800 就赢它。
- 不能说"在 16 条 held-out 上验证"——16/16 输出与训练集逐字相同。
- 不能说"AutoDL 官方文档已进入 RAG"——`knowledge/sources/` 不存在。
- 不能说"本地模型学会 AutoDL"——它只学会了在收窄后的 SSH 工具集里选，
  且只在 prompt 对齐之后才做到。
- 不能说"双模式在 AutoDL 目标上完成对比"——没跑。

## 8. 复现命令

```powershell
# 数据集指纹
python -c "import hashlib;print(hashlib.sha256(open('training/data_autodl_ssh/test.jsonl','rb').read()).hexdigest())"

# 常数基线（证明 F1 0.766 不超平凡基线）
python - <<'PY'
import json, collections
load=lambda p:[json.loads(l) for l in open(p,encoding='utf-8') if l.strip()]
tr,te=load('training/data_autodl_ssh/train.jsonl'),load('training/data_autodl_ssh/test.jsonl')
tools=lambda r:frozenset(s['tool'] for s in json.loads(r['messages'][-1]['content'])['plan'] if s.get('tool'))
f1=lambda p,g:(lambda tp:0.0 if not tp else 2*(tp/len(p))*(tp/len(g))/((tp/len(p))+(tp/len(g))))(len(p&g))
best=collections.Counter(tools(r) for r in tr).most_common(1)[0][0]
print('常数预测 F1:', round(sum(f1(best,tools(r)) for r in te)/len(te),3))
PY

# 训练同款 prompt 下的 adapter 行为
# （需先把 merged 模型起在 :8002，见 docs/claude-code-handoff/AUTODL_ISSUES_NOTES.md §C3）

# 全量测试
.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider
```
