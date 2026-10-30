# AutoDL 容器观测口径报告（2026-10-30）

## 1. 真实主机事实（只读实测）

| 项 | 实测值 | 来源 |
|---|---|---|
| 主机名 | `autodl-container-73d6479980-a8bf3ae3` | `hostname` |
| 内核 | Linux 5.15.0-105-generic | `uname -a` |
| **CPU 配额** | `50000 100000` = **0.5 核** | `/sys/fs/cgroup/cpu.max` |
| CPU 被限流次数 | `nr_throttled 4533` | `/sys/fs/cgroup/cpu.stat` |
| **内存上限** | `2147483648` = **2 GiB** | `/sys/fs/cgroup/memory.max` |
| 内存当前用量 | `473878528` ≈ 452 MiB | `/sys/fs/cgroup/memory.current` |
| `nproc` | 1 | `nproc` |
| `free -h` | **1.0 TiB total** | 宿主机视角，**不是**容器可用量 |
| `/proc/cpuinfo` | 128 个核 | 宿主机视角 |
| 系统盘 | overlay 30 GB，已用 2.7 GB（9%） | `df -h /` |
| **数据盘** | `/dev/md0` 150 GB，已用 18 GB（12%） | `df -h /root/autodl-tmp` |
| GPU | **无**（`nvidia-smi` 不存在） | `test -x /usr/bin/nvidia-smi` |
| 传统 syslog | **不存在**（`/var/log/{syslog,auth.log,kern.log}` 均无） | `test -f` |
| journal 目录 | 存在（`/var/log/journal`） | `test -d` |
| load average | 21.87 / 23.99 / 24.35 | `/proc/loadavg`（宿主机，含他人任务） |

**最关键的一条**：`free` 报 1 TiB，容器真实上限 2 GiB——**高估 512 倍**。
任何把 `free` 当作"容器可用内存"的输出都是错的。

## 2. 修复的四个观测缺陷

### 2.1 `read_log_file` 伪造观测（P0）

- **现象**：`read_log_file("/var/log/syslog")` 返回 `ok=true`、`content="(empty)"`。
- **原因**：函数直接 `tail`，`_exec_readonly` 把空 stdout 替换成字符串
  `"(empty)"`，`classify_payload` 把它判成 ok。
- **修**：先探测文件存在性（`test -f ... || echo <哨兵>`），不存在返回
  `ok=false status=unavailable "文件不存在: <path>"`。
- **实测**：不存在 → `unavailable`；`/var/log/dpkg.log` → `ok=true`，209 字符。

### 2.2 `get_system_info` 用宿主内存冒充容器内存（P1）

- **现象**：`memory` 字段是 `free -h` 的输出（1.0 TiB）。
- **修**：新增 `cgroup` 块，采集 `memory.max` / `memory.current` / `cpu.max` /
  `cpu.stat` 的 throttled / `nproc`，并注明"free 反映宿主机，容器真实上限以
  cgroup 为准"。
- **实测**：`memory_max_bytes=2147483648`、`cpu_max="50000 100000"`、`nproc=1`。

### 2.3 数据盘不可见（P1）

- **现象**：`df -h` 顶层看不到 `/root/autodl-tmp`——它是独立挂载的
  `/dev/md0`，而 AutoDL 的数据盘口径恰恰是它。
- **修**：`get_system_info` 新增 `data_disk` 字段单独采集。
- **实测**：返回 `/dev/md0 150G 18G 133G 12% /root/autodl-tmp`。

### 2.4 `check_disk_usage` 默认 `C:/` 在 Linux 必失败（P1）

- **现象**：无参调用返回 `磁盘路径必须是无 shell 元字符的绝对路径`。
  规划模型按 schema 默认值调用时拿到的就是这条错误——磁盘维度等于不可用。
- **修**：默认值改为空串，按平台选择（Windows `C:/`、Linux `/`）。
- **实测**：无参调用 → `path="/"`。

## 3. 前置修复（上一轮已提交，此处记录）

| 缺陷 | 修法 |
|---|---|
| 真实 SSH 观测被判 `live=false` | SSH 工具成功时标 `source=ssh`；`evidence_is_live` 认可 ssh 来源，但失败调用不标 live |
| `search_system_logs` 把"文件不存在"当成功日志 | 哨兵探测，不存在返回 `unavailable` |
| SSH 连接约 50% 失败（网关 MaxStartups 限流） | 归因为"服务端限流非主机不可达" + 一次有界重连（实测 2/6 → 8/10） |
| paramiko 静默换用 `~/.ssh` 其它 key | 显式 `allow_agent=False, look_for_keys=False` |

## 4. 任务书 §5 八条要求的落实情况

| # | 要求 | 状态 |
|---|---|---|
| 1 | CPU/内存/进程限制优先读 cgroup | **已实现**（cgroup 块） |
| 2 | `free` 不作容器可用内存 | **已实现**（cgroup 优先 + 显式 note） |
| 3 | `check_disk_usage` 默认路径适配 Linux | **已实现** |
| 4 | `/root/autodl-tmp` 作为可选路径 | **已实现**（data_disk 字段） |
| 5 | `search_system_logs` 支持 journal 或明确不支持 | **部分**：目前明确返回 unavailable，**未实现 journal 读取** |
| 6 | syslog/auth 不存在时返回 unavailable 不造假 | **已实现** |
| 7 | GPU 不存在时明确返回 unavailable | **未实现**：没有 GPU 工具，无法"明确返回 unavailable" |
| 8 | 证据标 `source=ssh`、`live=true`、host/port 摘要、duration | **部分**：`source`/`live`/`duration` 已有；host/port 摘要尚未写入证据 |

## 5. 未完成

1. **journal 读取未实现**：这台机器只有 `/var/log/journal`，`search_system_logs`
   只支持 syslog/auth/kern，所以日志维度实际不可用。工具现在会明确报
   unavailable（不是造假），但"能读日志"这件事在本机做不到。
2. **无 GPU 工具**：`get_system_info` 不检测 GPU，任务书要求"GPU 不存在时明确
   返回 unavailable"没有对应工具可改。
3. **证据缺 host/port 摘要**：`duration_ms` 已有，host/port 尚未进 evidence。
4. **`free` 字段仍在**：为了向后兼容保留了原始 `free -h` 输出，只在 `cgroup`
   块里给出真实配额。下游如果只读 `memory` 字段仍会看到宿主数字。
