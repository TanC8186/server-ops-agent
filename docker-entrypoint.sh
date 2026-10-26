#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# On-Call Agent 容器入口
#
# 职责：
#   1. 轮询 Milvus 健康端点（软依赖：失败只让 RAG 降级，不阻止启动）；
#   2. 拉起 3 个 MCP 子进程（8003/8004/8005）；
#   3. 用 TCP/HTTP 轮询端口直到真正就绪（替代历史固定 sleep 3），
#      每个端口独立超时，日志明确写出“哪个端口、第几次探测、是否超时”；
#   4. 启动 Web 服务，并由监督循环盯着全部子进程：任一进程退出就记录其
#      名字与退出码，清理其余进程，再以非零码退出（不静默留半死容器）。
#
# 可调环境变量（只影响启动节奏，不涉及任何凭证）：
#   MILVUS_READY_TIMEOUT   等待 Milvus 的总秒数，默认 60（软依赖，超时不退出）
#   MCP_READY_TIMEOUT      单个 MCP 端口等待秒数，默认 60（硬依赖，超时退出）
#   MCP_READY_MODE         tcp | http，默认 tcp
#   SUPERVISE_INTERVAL     监督轮询间隔秒数，默认 1
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

MILVUS_READY_TIMEOUT="${MILVUS_READY_TIMEOUT:-60}"
MCP_READY_TIMEOUT="${MCP_READY_TIMEOUT:-60}"
MCP_READY_MODE="${MCP_READY_MODE:-tcp}"
SUPERVISE_INTERVAL="${SUPERVISE_INTERVAL:-1}"

MCP_NAMES=("CLS" "Monitor" "SSH")
MCP_PORTS=(8003 8004 8005)
MCP_SCRIPTS=("servers/cls_server.py" "servers/monitor_server.py" "servers/ssh_server.py")
MCP_PIDS=()
WEB_PID=""

log() {
    echo "[entrypoint] $*"
}

log_error() {
    echo "[entrypoint][ERROR] $*" >&2
}

# ── 清理：容器收到 SIGTERM/SIGINT 或异常退出时，确保不留下孤儿子进程 ──
cleanup() {
    local code=$?
    trap - EXIT INT TERM
    if [[ -n "${WEB_PID}" ]] && kill -0 "${WEB_PID}" 2>/dev/null; then
        log "停止 Web 服务 (PID ${WEB_PID})"
        kill "${WEB_PID}" 2>/dev/null || true
    fi
    for pid in "${MCP_PIDS[@]:-}"; do
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    # 给子进程一点时间退出，仍未退出的强制结束。
    local waited=0
    while (( waited < 5 )); do
        local alive=0
        for pid in "${MCP_PIDS[@]:-}" ${WEB_PID:-}; do
            [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && alive=1
        done
        (( alive == 0 )) && break
        sleep 0.2
        waited=$((waited + 1))
    done
    for pid in "${MCP_PIDS[@]:-}" ${WEB_PID:-}; do
        [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && kill -9 "${pid}" 2>/dev/null || true
    done
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "============================================"
echo "  On-Call Agent — Docker 容器启动"
echo "============================================"

# ── 1. 等待 Milvus（软依赖） ──────────────────────────────────────────
log "[1/4] 等待 Milvus 就绪（最多 ${MILVUS_READY_TIMEOUT}s，失败仅降级 RAG）..."
if python - "${MILVUS_READY_TIMEOUT}" <<'PY'
import os
import sys
import time
import urllib.request

timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
host = os.environ.get("MILVUS_HOST", "milvus-standalone")
url = f"http://{host}:9091/healthz"
deadline = time.monotonic() + max(timeout, 0.0)
attempt = 0
while True:
    attempt += 1
    try:
        urllib.request.urlopen(url, timeout=3)
    except Exception:
        if time.monotonic() >= deadline:
            print(f"  [timeout] Milvus 在 {timeout:g}s 内不可达，RAG 能力将降级")
            sys.exit(1)
        time.sleep(2)
    else:
        print(f"  [ready] Milvus 在第 {attempt} 次探测后就绪")
        sys.exit(0)
PY
then
    log "Milvus 已就绪"
else
    log_error "Milvus 未就绪：继续启动，/health 会把 rag 标记为 unavailable"
fi

# ── 2. 拉起 MCP 子进程 ────────────────────────────────────────────────
log "[2/4] 启动 MCP 服务..."
for i in "${!MCP_NAMES[@]}"; do
    name="${MCP_NAMES[$i]}"
    script="${MCP_SCRIPTS[$i]}"
    port="${MCP_PORTS[$i]}"
    python "${script}" &
    pid=$!
    MCP_PIDS+=("${pid}")
    log "  ${name} MCP Server → :${port} (PID ${pid})"
done

# ── 3. 轮询端口就绪（硬依赖：超时则整体失败） ─────────────────────────
log "[3/4] 轮询 MCP 端口（mode=${MCP_READY_MODE}, 单端口超时 ${MCP_READY_TIMEOUT}s）..."
port_args=()
for port in "${MCP_PORTS[@]}"; do
    port_args+=(--port "${port}")
done
if ! python docker/mcp-healthcheck.py \
        --host 127.0.0.1 \
        --mode "${MCP_READY_MODE}" \
        --timeout "${MCP_READY_TIMEOUT}" \
        "${port_args[@]}"; then
    log_error "MCP 端口未全部就绪，终止启动"
    exit 1
fi
log "MCP 服务已全部就绪"

# 启动阶段再看一眼：就绪之前可能已经有进程崩溃（端口探测会先超时，这里补充
# 明确日志，避免只看到“端口超时”而不知道是哪一个进程先退出的）。
for i in "${!MCP_PIDS[@]}"; do
    pid="${MCP_PIDS[$i]}"
    if ! kill -0 "${pid}" 2>/dev/null; then
        wait "${pid}" 2>/dev/null
        log_error "MCP ${MCP_NAMES[$i]} (PID ${pid}) 在启动期退出，退出码 $?"
        exit 1
    fi
done

# ── 4. 启动 Web 服务并监督所有子进程 ──────────────────────────────────
log "[4/4] 启动 Web 服务 → :9900"
echo "============================================"
echo "  🌐 访问: http://localhost:9900"
echo "  📚 API:  http://localhost:9900/docs"
echo "============================================"

python servers/web_server.py &
WEB_PID=$!
log "Web 服务已启动 (PID ${WEB_PID})"

# 监督循环：每秒检查一次全部子进程。任一退出即记录并整体退出；
# 不用 `wait -n` 是为了兼容旧 bash，并能在日志里精确指出是谁先退出。
while true; do
    all_pids=("${MCP_PIDS[@]}" "${WEB_PID}")
    all_names=("${MCP_NAMES[@]}" "Web")
    for i in "${!all_pids[@]}"; do
        pid="${all_pids[$i]}"
        if ! kill -0 "${pid}" 2>/dev/null; then
            wait "${pid}" 2>/dev/null
            code=$?
            log_error "${all_names[$i]} 进程退出 (PID ${pid}, exit=${code})，容器将退出"
            exit $(( code == 0 ? 1 : code ))
        fi
    done
    sleep "${SUPERVISE_INTERVAL}"
done
