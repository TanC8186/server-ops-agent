# syntax=docker/dockerfile:1.7
# ═══════════════════════════════════════════════════════════════════════
# On-Call Agent 生产镜像（多阶段）
#
# 阶段划分：
#   1. web            —— Node 构建前端静态资源（npm ci 失败即失败，不回退）
#   2. python-base    —— 运行时基础层：系统包、uv、非敏感环境变量
#   3. python-builder —— 构建期依赖（编译器/头文件）＋从 uv.lock 冻结安装"仅运行期"依赖
#   4. runtime        —— 最终镜像：只拷贝已建好的 venv 与业务代码，不含构建工具
#
# 版本策略：
#   - Python 依赖一律 `uv sync --frozen`（严格以 uv.lock 为准），**不装 dev extra**；
#     锁文件与 pyproject 不一致时构建直接失败，而不是悄悄解析新版本。
#   - uv 版本固定，避免不同时间构建出不同依赖图；升级 uv 需同时重新生成 uv.lock。
# ═══════════════════════════════════════════════════════════════════════

# ---------- 1. 前端构建 ----------
FROM node:22-alpine AS web

WORKDIR /web

# 先只复制清单文件以利用缓存；npm ci 必须有 package-lock.json。
# 注意：绝不写 `|| npm install` 回退——锁文件与清单不一致时必须让构建失败，
# 否则镜像里的依赖是构建时现解析的，不可复现。
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
RUN npm run build

# ---------- 2. Python 运行时基础层 ----------
FROM python:3.11-slim AS python-base

LABEL org.opencontainers.image.title="On-Call Agent"
LABEL org.opencontainers.image.description="可评测的 AIOps 诊断 Agent"

# uv 版本与生成 uv.lock 的版本保持一致；升级请成对修改。
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/root/.cache/huggingface \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---------- 3. 依赖构建层（含构建期依赖） ----------
FROM python-base AS python-builder

# 构建期依赖只存在于本阶段：部分 wheel 缺失时需要编译 C 扩展；
# 最终镜像从 python-base 重新开始，不会带上编译器。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        python3-dev \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 只复制依赖清单：业务代码变化不会让依赖层缓存失效。
COPY pyproject.toml uv.lock ./
# --frozen：严格使用锁文件；--no-dev：生产镜像不含 pytest/ruff 等开发依赖；
# --no-install-project：本层只装第三方依赖，项目代码在最终阶段才出现。
RUN uv sync --frozen --no-dev --no-install-project

# ---------- 4. 最终运行镜像 ----------
FROM python-base AS runtime

# 运行时需要 libgomp（torch/sentence-transformers 的 OpenMP 运行时）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 已冻结安装好的虚拟环境；最终镜像不含 uv 的编译工具链。
COPY --from=python-builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV=/app/.venv

# 业务代码与前端产物
COPY . .
COPY --from=web /web/dist ./web/dist

# 可写目录：与 docker-compose.yml 的命名卷一一对应。
# 目录必须存在于镜像中，命名卷首次创建时才会继承属主与权限。
RUN mkdir -p uploads logs/traces eval/results data

EXPOSE 9900 8003 8004 8005

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# 健康检查只打本机 /health；返回 200 即通过（Milvus 故障仍返回 200）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9900/health', timeout=3)"]

ENTRYPOINT ["/docker-entrypoint.sh"]
