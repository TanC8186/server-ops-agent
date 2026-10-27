# Docker / Compose 真实依赖验收（2026-10-27）

对象：当前工作区 commit `266a54c`（构建前）+ 见下方镜像 digest。
环境：Windows 11, Docker 29.8.2 (desktop-linux), Milvus v2.5.10, Python 3.11-slim 基础镜像。

## 1. 构建

**第一次失败（Docker Hub 认证）**

```
docker build --progress=plain -t oncall-agent-acceptance .
# ERROR: failed to fetch oauth token: Post "https://auth.docker.io/token":
#   dial tcp 173.244.217.42:443: connectex: ...
server: A connection attempt failed because the connected party did not
        properly respond after a period of time
Dockerfile:1  >>> # syntax=docker/dockerfile:1.7
```

根因是 `# syntax=docker/dockerfile:1.7` 需要从 `docker.io` 拉前端镜像，而直连不可用
（`curl https://auth.docker.io/token` → `000`；本机代理 127.0.0.1:7897 可通 → `200`）。

**第二次失败（`npm ci` 锁文件损坏）**

改用 legacy builder（`DOCKER_BUILDKIT=0`）绕开前端镜像后，卡在：

```
Step 4/28 : RUN npm ci --no-audit --no-fund
npm error Invalid Version:
```

定位到 `web/package-lock.json` 里有一条缺少 `version` 的条目
（`node_modules/rolldown/node_modules/@rolldown/binding-freebsd-x64` 只有
`{"dev":true,"optional":true}`）。这是**真实缺陷**：本地 `npm install` 容忍它，
`npm ci` 不容忍，所以镜像必然构建失败，而本地开发看不出来。

修复方式：删除并从官方 registry 重新生成 `package-lock.json`（`lockfileVersion: 3`，
0 条缺 `version` 的条目），随后 `npm ci` 与 `npm test`（53 passed）、`npm run build`
本地全部通过。

**第三次失败（Debian 镜像站 502）**

```
Err:62 http://deb.debian.org/debian trixie/main amd64 libdpkg-perl all 1.22.22
  502  Bad Gateway [IP: 199.232.162.132 80]
```

**第四次：成功**

```
docker build -t oncall-agent-acceptance .
# Successfully built 7788efe30239
# Successfully tagged oncall-agent-acceptance:latest
```

| 项 | 值 |
|---|---|
| 镜像 ID | `sha256:7788efe302394b1225c96aee1bf804da8cde7399e8a781d6d2f13dde090a4eb9` |
| 创建时间 | `2026-10-27T12:50:49Z` |
| 大小 | 18.9 GB（未压缩 `18868738945` 字节） |
| 阶段 | 28 步，含 `web`(node:22-alpine) / `python-base` / `python-builder` / `runtime` |

**没有修改 Dockerfile 来绕过任何失败**。`DOCKER_BUILDKIT=0` 只是选择 legacy builder
（不需要 `docker/dockerfile` 前端镜像），交叉验证时可换回 BuildKit + 代理。

## 2. Compose 启动

一个**真实发现**：`docker compose up -d app` 复用了陈旧镜像 `on_call_agent-app:latest`
（`e333242f9477`，2026-06 构建），容器内 `/app/app/api/health.py` 只有 1894 字节、
没有 `capabilities`，`app/core/principal.py` 不存在。也就是说 **compose 的 `build:`
不会在已有同名镜像时自动重建**，很容易把旧代码当成“刚验收过”。

本次做法：`docker tag oncall-agent-acceptance:latest on_call_agent-app:latest` 后
`--force-recreate`，并用镜像内文件尺寸/内容确认真的换成了新代码。

启动顺序（日志原文节选）：

```
[ready] 127.0.0.1:8003 在第 3 次探测后就绪（tcp）
[ready] 127.0.0.1:8004 在第 1 次探测后就绪（tcp）
[ready] 127.0.0.1:8005 在第 1 次探测后就绪（tcp）
所有 MCP 端口已就绪
[entrypoint] MCP 服务已全部就绪
[entrypoint] [4/4] 启动 Web 服务 → :9900
[entrypoint] Web 服务已启动 (PID 15)
```

`docker compose ps`（全部 healthy）：

```
milvus-etcd        Up (healthy)
milvus-minio       Up (healthy)
milvus-standalone  Up (healthy)
oncall-agent       Up (healthy)
```

**容器内 AUTH_MODE 的行为（确认非缺陷）**：不带 `API_KEY` 时容器按设计拒绝启动：

```
RuntimeError: AUTH_MODE=deployment 需要设置 API_KEY；拒绝在没有访问控制的部署模式启动
```

用一次性随机 key（写入 `/tmp`，不落盘到仓库、不打印）启动后：

| 请求 | 结果 |
|---|---|
| `GET /api/runs` 无 key | **401** |
| `GET /api/runs` 带 key | **200** |
| `GET /health` 带 key | 200，`status=healthy`，5+1 个能力项 |

能力项实测（容器内、真实 Milvus）：

```
diagnosis_local = unconfigured | 未配置 LOCAL_LLM_BASE_URL
diagnosis_cloud = ready        | (配置齐全)
mcp             = degraded     | 已配置 3 个 MCP 服务，但本进程尚未成功加载工具
rag             = ready        | 
tracing         = ready        |
trace_retention = ready        |
```

## 3. 容器内真实知识库链路（HTTP → service → retriever → Milvus）

在**容器里**通过 HTTP 完成一次真实上传与索引：

| 步骤 | 观测 |
|---|---|
| `GET /api/knowledge/status` | `ready`，`chunks=954`，`cosine` / 384 维 |
| `POST /api/knowledge/documents`（multipart） | **202**，`job-7a556378cca4`，`doc-f6f6d9e55cac`，`status=queued` |
| 轮询 `GET /api/knowledge/jobs/{id}` | `ready`，chunks `1/1`，无 error |
| `GET /api/knowledge/documents` | 该文档 `active`，`chunks=1` |

即“202 已接收”没有被当成“索引成功”：必须轮询到 `ready` 才算完成。

## 4. SIGTERM 与子进程监督

**SIGTERM**：`docker stop --timeout 25 oncall-agent` → 退出码 **0**，日志：

```
INFO: Shutting down
INFO: Finished server process [8] [9] [10] [15]
```

无残留进程（容器停止后 `docker exec` 不再可达，且 compose 判定 exited(0)）。

**MCP 子进程异常退出**：`python -c "os.kill(8, SIGKILL)"` 杀掉 `servers/cls_server.py`：

```
[entrypoint][ERROR] CLS 进程退出 (PID 8, exit=137)，容器将退出
```

随后 `RestartCount=1`、`StartedAt` 前移、`Status=running`、`Health=healthy` ——
`restart: unless-stopped` 把容器拉起来了，entrypoint 的监督循环**按设计**判定
“子进程死了 → 整体退出（非 0）”，由编排层重启，而不是留着一个半残进程继续服务。

## 5. 命名卷重建

四个命名卷（`oncall-agent-uploads` / `-logs` / `-data` / `-eval-results`）各写一个
marker，然后 `docker compose up -d --force-recreate app`：

```
/app/uploads/marker.txt: uploads-marker
/app/logs/traces/seed.jsonl: {"event":"seed"}
/app/data/marker.txt: data-marker
/app/eval/results/marker.json: {}
```

四个卷全部保留。Milvus 的 bind mount（`./volumes/milvus`）同样验证：`docker stop` +
`docker start milvus-standalone` 后 `collection_count()` 仍是 **955**。

## 6. 结论边界（不能夸大的部分）

- 镜像**能构建、能启动、健康检查通过**：这是本轮新增的真实证据。
- 但**没有**做：多副本并发（per-path trace 锁只在进程内）、跨主机部署、
  镜像体积优化（18.9 GB 因为把完整 venv + 模型依赖打进去了，属于已知取舍）、
  干净机器网络下的首次拉取（本轮依赖本机代理与已有基础镜像缓存）。
- `docker compose build` 的“陈旧镜像复用”行为已记录；生产流水线应显式
  `--pull --no-cache` 或固定 digest，否则会拿旧代码验收。
