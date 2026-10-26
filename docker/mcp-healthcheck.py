#!/usr/bin/env python3
"""MCP 端口就绪探针（容器启动用，只依赖标准库）。

用途：``docker-entrypoint.sh`` 在启动主应用前，逐个轮询 MCP 服务端口
（8003/8004/8005），确认“端口已经能接受连接/响应 HTTP”后再继续。这替代了
历史上固定的 ``sleep 3``：固定等待既可能在慢机器上等不够，也会在快机器上
白白拖慢启动。

判定方式：

- ``tcp``：建立 TCP 连接成功即视为就绪（服务正在监听）。
- ``http``：先建立 TCP 连接，再对 ``--path`` 发一次 GET；**收到任何 HTTP 状态
  码**都算就绪（FastMCP 的 ``/mcp`` 对 GET 可能返回 405/406，这同样证明进程
  已在正常接收请求）。连接被拒绝、超时、DNS 失败都算未就绪。

退出码：全部就绪返回 0；超时返回 1；参数/用法错误返回 2。输出只包含端口与
状态，不读取、不打印任何环境变量或凭证。
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PORTS = (8003, 8004, 8005)
DEFAULT_PATH = "/mcp"


def _tcp_ready(host: str, port: int, timeout: float) -> bool:
    """TCP 端口能否建立连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ready(host: str, port: int, path: str, timeout: float) -> bool:
    """HTTP 端点是否已响应（任何状态码都算“进程在听”）。"""
    if not _tcp_ready(host, port, timeout):
        return False
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # 4xx/5xx 也是服务端真实响应，说明 HTTP 服务已经起来了。
        return True
    except (urllib.error.URLError, OSError):
        return False


def wait_for_port(
    *,
    host: str,
    port: int,
    mode: str = "tcp",
    path: str = DEFAULT_PATH,
    timeout: float = 60.0,
    interval: float = 1.0,
    request_timeout: float = 2.0,
    log=print,
) -> bool:
    """轮询单个端口直到就绪或超时，返回是否就绪。"""
    deadline = time.monotonic() + max(timeout, 0.0)
    attempt = 0
    while True:
        attempt += 1
        if mode == "http":
            ready = _http_ready(host, port, path, request_timeout)
        else:
            ready = _tcp_ready(host, port, request_timeout)
        if ready:
            log(f"  [ready] {host}:{port} 在第 {attempt} 次探测后就绪（{mode}）")
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log(f"  [timeout] {host}:{port} 在 {timeout:g}s 内未就绪（{mode}）")
            return False
        time.sleep(min(interval, remaining))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="轮询 MCP 端口直到就绪；全部就绪返回 0，超时返回 1。"
    )
    parser.add_argument(
        "--port",
        dest="ports",
        action="append",
        type=int,
        help="要探测的端口，可重复；默认 8003/8004/8005",
    )
    parser.add_argument("--host", default="127.0.0.1", help="目标主机，默认 127.0.0.1")
    parser.add_argument(
        "--mode", choices=("tcp", "http"), default="tcp", help="探测方式，默认 tcp"
    )
    parser.add_argument("--path", default=DEFAULT_PATH, help="http 模式的请求路径")
    parser.add_argument("--timeout", type=float, default=60.0, help="单端口总超时秒数")
    parser.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数")
    parser.add_argument(
        "--request-timeout", type=float, default=2.0, help="单次探测超时秒数"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ports = tuple(args.ports) if args.ports else DEFAULT_PORTS
    if not ports:
        print("没有要探测的端口", file=sys.stderr)
        return 2

    print(
        f"轮询 MCP 端口: {', '.join(str(port) for port in ports)} "
        f"(host={args.host}, mode={args.mode}, timeout={args.timeout:g}s)"
    )
    # 逐个探测：每个端口独立超时，日志里能直接看出是哪一个没起来。
    ok = True
    for port in ports:
        if not wait_for_port(
            host=args.host,
            port=port,
            mode=args.mode,
            path=args.path,
            timeout=args.timeout,
            interval=args.interval,
            request_timeout=args.request_timeout,
        ):
            ok = False
    if ok:
        print("所有 MCP 端口已就绪")
        return 0
    print("有 MCP 端口未在超时内就绪", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
