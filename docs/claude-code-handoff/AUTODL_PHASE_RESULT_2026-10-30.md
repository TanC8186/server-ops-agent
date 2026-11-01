# AutoDL RAG 收尾阶段总报告（2026-10-30）

> 状态口径：`VERIFIED` = 真实依赖、固定输入、可重跑、证据完整；
> `ANALYZED` = 完成过一次分析，但独立重跑或真实联调不足；
> `UNVERIFIED` = 没有真实证据。

## 0. 三个问题的直接回答

**1. 真实 AutoDL SSH 目标能否被正确绑定并完成只读观测？**
→ **VERIFIED**。绑定到 `autodl-ssh`，端到端诊断 18.5 s 完成，
计划 `list_all_services / get_system_info / get_process_list / search_system_logs`
全部为 SSH 工具（零腾讯云），前两个成功返回真实数据且 `source=ssh`。

**2. AutoDL 官方文档是否已作为带来源和版本的知识进入 RAG？**
→ **VERIFIED**。9 个官方源全部抓取、规范化、入库；容器内 store 有 9 篇
AutoDL 文档、183 chunk，`target_kind=ssh` 正确落盘。
`target_kind=cloud` 过滤返回 **0 条**，`target_kind=ssh` 返回 3 条。

**3. 本地 adapter 与 cloud_multi 在同一批真实任务上的差异是什么？**
→ **UNVERIFIED（未执行）**。见 §5。

## 1. 身份

| 项 | 值 |
|---|---|
| 分支 | `g6-permissions-chat-consistency` |
| 本轮起点 HEAD | `0f0fee1` |
| 本轮最终 HEAD | `dc4c2c8` |
| 候选镜像 | `on_call_agent-app:dc4c2c8` = `sha256:be4d98ae6ced…` |
| 镜像 revision label | `dc4c2c81ee7fcc2477229b2fc19b67fa3ffe1918` = HEAD 逐字一致 |
| 镜像大小 | 10 677 200 104 B |

本轮提交（按时间顺序）：`9dd126e` → `a02dbcc` → `a145341` → `dc4c2c8`。

## 2. Docker 空间治理（VERIFIED）

| 指标 | 起点 | 现在 |
|---|---:|---:|
| C 盘可用 | **3.4 GB** | **212 GB** |
| `docker_data.vhdx` | 276 GB | 100 GB |
| Docker 镜像总量 | 261.6 GB / 66 个 | 30.37 GB / 28 个 |
| 本项目镜像 | 6 个残留 | **1 个**（在用） |
| Build cache | 8 KB | **0 B** |

清理动作全部按归属核对：删除 24 个 dangling + 163 层 + 12 个父层，以及本轮
两个旧候选镜像（`a145341`、`latest`），回收 53.75 GB。
`mact-*` / `personal_design-*` 未触碰。

**关键纠正**：我先前判断"vhdx 必须管理员权限才能压缩"是**错的**。
`wsl --manage docker-desktop --set-sparse false` + 关闭 WSL 即可，无需 diskpart。

详见 `DOCKER_SPACE_REPORT.md`。

## 3. AutoDL 官方语料入库（VERIFIED）

- **抓取 9/9**：`fetch_corpus.py` 白名单加入 `www.autodl.com` / `api.autodl.com`。
- **规范化 9/9**：修掉 manifest 硬编码旧日期；`MIN_BODY_CHARS` 400→200
  （AD-03 正文 388 字被误判）。正文从 `<article>` 提取，丢弃导航。
- **入库 9/9**：9 篇文档、183 chunk、16 031 字符。
- 产物：`knowledge/sources/autodl/{raw,normalized,notices}/manifest.json`。

详见 `AUTODL_RAG_REPORT.md`。

## 4. 本轮修复的缺陷（全部实测复现）

### 4.1 我自己引入的回归（由子 Agent 复查发现，我独立复现）

**`eval_retrieval` 只读最新 manifest** → 追加 AutoDL 语料后，语料变成只有
AD-01..09，`eval/rag/dev.jsonl` 里 11 道可答题的 `expected_source_ids`
全部不在语料里，**hit@k 恒为 0**，评测照常"跑完"。
修：按 id 合并全部 `normalized-*.json`（实测 9 → 20 条，历史基线恢复）。

### 4.2 "看起来做了、实际没生效"的三个

| 缺陷 | 实测现象 | 修法 |
|---|---|---|
| `target_kind` 加进白名单却没落盘 | snapshot 里 `target_kind` 恒为 None | 新建 + 已存在文档两处都写入 |
| 无 principal 时过滤被静默绕过 | `target_kind=cloud` 仍返回 autodl 的 ssh 文档 | 新增 `filtered_revisions()`，有 filters 无 principal 时走它 |
| `delete_document` 未清理已完成作业 | Milvus 有行、store.json 0 篇文档 | 删除时取消该文档所有 ready/failed 作业 |

### 4.3 容器观测口径（任务书 §5）

| 缺陷 | 实测现象 | 修法 |
|---|---|---|
| `read_log_file` 伪造观测 | 不存在的文件返回 `ok=true content="(empty)"` | 先 `test -f` 探测，不存在返回 unavailable |
| `get_system_info` 用宿主内存 | `free` 报 1 TiB，cgroup 实为 2 GiB（**高估 512 倍**） | 新增 `cgroup` 块（memory.max/cpu.max/throttled/nproc） |
| 数据盘不可见 | `df -h` 顶层看不到 `/root/autodl-tmp` | 新增 `data_disk` 字段 |
| `check_disk_usage` 默认 `C:/` | Linux 上无参调用必失败 | 按平台取默认（win=C:/、linux=/） |

### 4.4 目标配置（任务书 §4）

- `Target` 新增 `product` / `ssh_port`；`services.json` 记录
  `product=autodl`、`ssh_port=12792`。
- `LOCAL_LLM_MODEL_SSH` 未配置时写 `local_model_ssh_unconfigured` trace，
  不再静默退回通用模型。

## 5. 未完成

### 5.1 双模式同题对比（UNVERIFIED）

**未执行**。原因：本轮把主要精力放在修 §3/§4/§5 的阻塞上，且每次都发现
更深一层的缺陷（语料未入库 → 元数据未落盘 → 过滤被绕过）。在链路刚打通
的状态下跑对比，得到的数字依然不可信——这正是上一轮 `F1=0.766 < 常数 0.800`
教训的复现。

现有 `eval/mode_benchmark/tasks.jsonl` 的 18 条任务**面向腾讯云服务**，
14/18 无法在 AutoDL 目标上绑定，不能直接复用。

### 5.2 其他

1. **journal 日志读取未实现**：这台机器只有 `/var/log/journal`，
   `search_system_logs` 只支持 syslog/auth/kern，当前明确返回 unavailable。
2. **无 GPU 工具**：`get_system_info` 不检测 GPU，任务书"GPU 不可用明确返回"
   没有对应工具。
3. **`ssh_port` 未用于实际连接**：目录里的端口目前只是元数据，
   `_get_ssh_client()` 仍读全局 `SSH_PORT`，也未做一致性校验。
4. **两套 store 并存**：本机 `data/knowledge/store.json` 与容器命名卷
   `oncall-data` 是两份独立数据，无自动同步机制。
5. **Milvus 有 992 行孤儿向量**（17 个不在 store 里的历史 id），
   expr 预过滤使其不进入结果，但 §3.4 "零残留"未完全达成。
6. **语料目录是手工复制**：`knowledge/sources/autodl/` 的 28 个文件没有
   生成脚本产出，重跑文档命令不会重建它。

## 6. 测试与验收

| 项 | 结果 |
|---|---|
| 后端测试 | **937 passed, 1 skipped** |
| 偶发失败（已记录） | `test_server_env_isolation::test_full_suite_ordering_regression_passes_in_one_process` 在一次全量运行中失败 1 次；单独跑通过、随后两次全量均通过。原因：该测试会嵌套起一个 pytest 子进程跑 `test_p0_upload_index.py`（连真实 Milvus），全量并发时连接竞争导致子进程非零退出。**是既有测试的资源竞争，与本轮改动无关，但如实记录。** |
| ruff (F821/F811/F841) | All checks passed |
| 新增测试 | `test_autodl_corpus_metadata.py`(6) / `test_corpus_pipeline.py`(7) |
| 空测试检查 | 已验证：移除 `target_kind` 落盘 → 失败；改回 `paths[-1]` → 失败 |

端到端验收（容器 + 候选镜像 + AutoDL 目标）：

```
计划: list_all_services / get_system_info / get_process_list / search_system_logs
  get_system_info      ok=True  src=ssh
  get_process_list     ok=True  src=ssh
  search_system_logs   ok=False src=ssh（该机无 syslog，如实报错）
完成: 18456 ms lifecycle=completed
```

RAG 验收（容器内真实 Milvus）：

```
target_kind=ssh   -> 3 hits（AutoDL 文档）
target_kind=cloud -> 0 hits
```

## 7. 不得作为结论的主张

- ✗ "已完成本地 adapter 与 cloud_multi 的 AutoDL 同题对比" —— 未执行。
- ✗ "AutoDL adapter 提升准确率" —— 旧的 16 条评测 **16/16 输出泄漏**，
  且 Tool F1 0.766 **低于常数基线 0.800**。
- ✗ "支持 journal 日志查询" —— 未实现。
- ✗ "GPU 不可用可被明确识别" —— 无对应工具。
- ✗ "`ssh_port` 已贯通到连接" —— 仅是目标元数据。
- ✗ "多副本 / 多租户" —— 单 API Key、单进程存储。

## 8. 复现命令

```powershell
# 语料
python scripts/fetch_corpus.py                 # 抓取（白名单已含 autodl）
python scripts/normalize_corpus.py             # 规范化（自动选最新 import-*）
python scripts/ingest_autodl_corpus.py --dry-run
python scripts/ingest_autodl_corpus.py         # 入库

# 测试
.venv/Scripts/python.exe -m pytest tests -q -o addopts= -p no:cacheprovider

# 镜像
docker build --no-cache --build-arg GIT_REVISION=$(git rev-parse HEAD) `
  --build-arg HTTP_PROXY=http://host.docker.internal:7897 `
  --build-arg HTTPS_PROXY=http://host.docker.internal:7897 `
  -t on_call_agent-app:$(git rev-parse --short HEAD) .
$env:APP_IMAGE="on_call_agent-app:dc4c2c8"; docker compose up -d --force-recreate app
```
