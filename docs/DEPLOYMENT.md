# 部署与运维说明（M5 / OPS-01 / OPS-02）

本文只写**当前代码真实具备**的能力与边界。没有在本机实际执行过的验收项会明确
标注“未验证”，不要把文档当成构建/部署证据。

## 1. 镜像结构

`Dockerfile` 分四阶段：

| 阶段 | 作用 | 关键约束 |
|---|---|---|
| `web` | `npm ci` + `vite build` | 锁文件与 `package.json` 不一致时**构建失败**，不回退 `npm install` |
| `python-base` | 系统包、固定版本 uv、非敏感环境变量 | 不含业务代码，便于复用到构建/运行两阶段 |
| `python-builder` | 装编译工具链，`uv sync --frozen --no-dev` | 只依赖 `uv.lock`；生产镜像不装 pytest/ruff |
| `runtime` | 只拷贝已装好的 `.venv` 与业务代码 | 不含编译器；`web/dist` 从前一阶段复制 |

可复现性要点：

- Python 依赖来自 `uv.lock`（`--frozen` 严格模式）；升级依赖必须重新生成锁文件。
- `web/package-lock.json` 必须在仓库中，`npm ci` 才能成功。
- uv 版本固定为镜像标签（`ghcr.io/astral-sh/uv:0.12.7`），与生成 `uv.lock` 的版本
  成对升级。

## 2. 启动流程（`docker-entrypoint.sh`）

1. **Milvus 就绪探测（软依赖）**：轮询 `MILVUS_HOST:9091/healthz`，超时只记录
   警告并继续启动；`/health` 会把 `capabilities.rag` 标成 `unavailable`。
2. **拉起三个 MCP 子进程**：CLS(8003)、Monitor(8004)、SSH(8005)。
3. **端口就绪探测（硬依赖）**：`docker/mcp-healthcheck.py` 依次探测每个端口
   （默认 `tcp`，可选 `http`），单个端口超时即终止启动并给出端口号与尝试次数。
   **不再使用固定 `sleep` 当就绪判据。**
4. **监督循环**：Web 服务在前台启动后，每秒检查全部子进程；任一退出即记录
   名字、PID 与退出码，清理其余进程并以非零码退出，避免“半死容器”。

可调环境变量（只影响启动节奏，不含任何凭证）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `MILVUS_READY_TIMEOUT` | 60 | 等待 Milvus 的总秒数（软依赖） |
| `MCP_READY_TIMEOUT` | 60 | 单个 MCP 端口等待秒数（硬依赖） |
| `MCP_READY_MODE` | `tcp` | `tcp` / `http` |
| `SUPERVISE_INTERVAL` | 1 | 监督轮询间隔秒数 |

## 3. 数据持久化

`docker-compose.yml` 为应用容器挂了四个命名卷。**它们都是必需的**：

| 卷 | 容器路径 | 内容 | 丢失后果 |
|---|---|---|---|
| `oncall-uploads` | `/app/uploads` | 上传的原始文档 | 上传文件消失，需重新上传 |
| `oncall-logs` | `/app/logs` | 应用日志、`logs/traces/*.jsonl` | 运行历史/回放全部丢失 |
| `oncall-eval-results` | `/app/eval/results` | 评测产物 | 评测结果页清空 |
| `oncall-data` | `/app/data` | embedding 描述符、知识库作业/版本元数据、会话历史 | 已有 Milvus collection 会被判为 `invalid_index`（缺描述符），必须重建索引 |

Milvus 自身的数据在 `./volumes/milvus`，由 compose 的 Milvus 服务维护；
**应用容器重建不会保留知识原文与运行记录**，必须依赖上面的命名卷。

备份：

```bash
docker run --rm -v oncall-agent-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/oncall-data-$(date +%F).tar.gz -C /data .
docker run --rm -v oncall-agent-logs:/data -v "$PWD":/backup alpine \
  tar czf /backup/oncall-logs-$(date +%F).tar.gz -C /data .
```

恢复时把归档解回同名卷即可；恢复后首次检索会重新校验描述符。

## 4. 健康与能力

`GET /health` 保持 `{code, message, data}` 外壳，`data.capabilities` 包含：

- `diagnosis_local`：只看 `LOCAL_LLM_BASE_URL` 是否合法 http(s) 且模型名非空；
- `diagnosis_cloud`：`LLM_API_KEY` 非空且不是 `your-` 占位符、`LLM_BASE_URL` 合法；
- `rag`：Milvus 连接状态 + 向量库初始化结果，额外允许 `invalid_index`；
- `tracing`：`{status, writes, failures}`，来自进程内计数。

状态取值固定为 `unconfigured | ready | degraded | unavailable`（`rag` 另有
`invalid_index`）。**Milvus 或模型不可用不会让 `/health` 返回非 200**：容器编
排不该因为知识库暂不可用反复重启进程。任何字段都不包含密钥、掩码或长度。

## 5. 干净部署与依赖掉线验证

```bash
# 1) 准备配置（不要提交 .env）
cp .env.example .env        # 填入真实密钥；AUTH_MODE 建议 deployment

# 2) 构建并启动
CONTAINER_AUTH_MODE=deployment docker compose up -d --build

# 3) 能力检查
curl -s http://127.0.0.1:9900/health | python -m json.tool

# 4) 依赖掉线验证：停掉 Milvus，应用应保持 200 且 rag=unavailable
docker compose stop milvus
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9900/health   # 期望 200
curl -s http://127.0.0.1:9900/health | python -m json.tool | grep -A3 '"rag"'

# 5) 依赖恢复
docker compose start milvus
sleep 10
curl -s http://127.0.0.1:9900/health | python -m json.tool | grep -A3 '"rag"'
```

## 6. 已知限制（务必按此理解）

- **镜像构建未在本轮执行**：本机只有 Docker CLI，Docker Desktop 守护进程未运行
  （`docker build` 报 `dockerDesktopLinuxEngine` 管道不存在），因此没有跑过完整
  build；“镜像可构建”只经过静态检查，不是实测。`docker-entrypoint.sh` 过了
  `bash -n` 语法检查，但未在容器内真实启动过。
- **Milvus 依赖未在真实集群验证**：本轮所有 RAG 测试都用假 store / 临时目录，
  真实 Milvus 的索引创建、metric 校验与 `num_entities` 统计没有端到端验证。
- **MCP 子进程仍在同一容器内**：由 entrypoint 监督（退出即整体退出），没有拆成
  独立 service + 健康检查。拆分的理由与代价：拆分后 `/health` 的 MCP 依赖需要
  额外的跨服务探测，且 start-windows.bat 的本地开发路径需要另一套编排；当前
  选择“同容器监督”，代价是任一 MCP 崩溃会让整个容器重启。
- **会话历史与知识库元数据是文件存储（JSON + 原子替换）**：适合单实例；多实例
  部署前必须换成共享存储，否则会话/作业状态不共享。
- **没有引入反向代理/HTTPS**：`HOST` 默认 `127.0.0.1`；对外暴露必须显式设置
  `HOST=0.0.0.0` 并在前面加 TLS 终止，否则 API Key 会明文传输。
