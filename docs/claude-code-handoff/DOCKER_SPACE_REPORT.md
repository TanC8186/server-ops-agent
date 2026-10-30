# Docker 空间治理报告（2026-10-30）

## 1. 起因：上一轮把 C 盘占满

上一轮为了验证 AutoDL 修复，我用 `--no-cache` 反复全量重建镜像 **6 次**。每次产出
10.7 GB 最终镜像 + 20.7 GB `COPY . .` 中间层，且中间从不清理。

后果：`docker_data.vhdx`（在 C 盘）从 62 GB 涨到 **276 GB**，C 盘可用从 109 GB
掉到 **3.4 GB（100% 满）**，Docker daemon 直接起不来。

**根因三条**（都是我的判断失误，不是环境问题）：

1. 把"镜像 label == HEAD"当成每次提交后都要重新满足的门槛，导致反复重建；
2. 中间不清理 dangling 层，只在最后一次性清；
3. **动手前没确认 Docker 数据盘在 C 盘**——这直接违反"只在 E 盘操作"的既有约束。

## 2. 清理前后对比

| 指标 | 清理前 | 清理后 |
|---|---:|---:|
| C 盘可用 | **3.4 GB** | **230 GB** |
| `docker_data.vhdx` 物理大小 | **276 GB** | **87 GB** |
| Docker 镜像总量 | 261.6 GB / 66 个 | 30.37 GB / 28 个 |
| 本项目镜像 | 6 个残留 | 1 个（在用） |
| dangling 镜像 | 24 个 | 0 |
| 容器占用 | 13.29 GB | 978 KB |
| Build cache | 8 KB | **0 B** |

## 3. 具体动作（按归属核对后执行）

**删除（全部确认属于本项目）：**

- 24 个 dangling 镜像 + 163 层，以及第二轮 12 个 830 MB 父层
  —— ID 逐个核对：`4791e2f`、`2ef4e9a7`、`6a7ff2b7`、`3256c23a`、`475c4ca9`
  都是本轮历次构建产物，label 为 `org.opencontainers.image.title:On-Call Agent`
- 构建失败残留容器 `gifted_kirch`（镜像是 `6ec329db`，已删）
- 项目临时副本 `%TEMP%/tmp.1GlxepKMOT`（18 GB，含 training 模型拷贝）
- Docker 诊断文件、`swap.vhdx`

**保留（不得删除）：**

- `oncall-agent:g8-final` = `on_call_agent-app:latest`（同一 ID `b4623c391ab3`，
  被运行中的 `oncall-agent` 容器使用）
- 4 个项目数据卷：`oncall-agent-data` / `-eval-results` / `-logs` / `-uploads`
- **其他项目全部资源原地未动**：`mact-*`、`personal_design-*` 的镜像与卷

未执行 `docker system prune -a` / `docker image prune -a` / `docker volume prune`
——按任务书 §2.4 禁止项。

## 4. 关键发现：vhdx 不需要管理员权限也能回收

我一开始判断"必须 diskpart + 管理员"，**这是错的**。实际可行的是：

```powershell
# 1) 把 WSL 发行版的 vhdx 转为动态扩展
wsl --manage docker-desktop --set-sparse false

# 2) 关闭 Docker 与 WSL，系统自动把虚占区块还给 Windows
Stop-Process -Name 'Docker Desktop','com.docker.backend' -Force
wsl --shutdown
```

关掉之后 C 盘从 37 GB 跳到 **226 GB**，vhdx 从 276 GB 降到 87 GB。
全程无需管理员、无需 diskpart。

## 5. 本轮起的构建纪律（写进流程）

按任务书 §2.2：

1. **代码、配置、测试、文档全部改完**才构建一次候选镜像；
2. 修改期间不为了验证小改动反复 `docker build`；
3. Python/前端/配置检查优先用本机环境；
4. 候选镜像用可追溯 tag：`on_call_agent-app:<git-short-sha>`；
5. 验收结束**立即删除**旧的、不再使用的本项目镜像与临时容器；
6. 每次构建后 `docker image prune -f` 清中间层。

本轮实际执行：从 `1436b5f` 到 `a02dbcc` 累积了 3 个提交，只在代码全部稳定后
构建了 **1 次**候选镜像。

## 6. 仍未处理

- Docker 数据盘仍在 C 盘。彻底根治需要迁移到 E 盘（`vhd` 迁移 + Docker Desktop
  重装或 `--data-root` 重配），操作较重，本轮未做。
- 若再次出现 `--no-cache` 全量重建，同样会增长 vhdx；上面第 5 节的纪律是约束。
