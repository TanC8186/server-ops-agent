"""FastAPI Web 服务启动器

启动主 FastAPI 应用，提供 Web 界面和 API 服务。
运行方式: python servers/web_server.py

监听地址与端口统一来自 ``Settings``（默认 ``127.0.0.1:9900``），
不再硬编码 ``0.0.0.0``：容器/部署必须显式设置 ``HOST=0.0.0.0`` 才对外暴露。
"""

import os
import sys
from pathlib import Path
from typing import Any

# ── 必须在所有 HF/huggingface 相关模块导入前设置 ──
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_ENDPOINT"] = "https://hf-mirror.com"

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def uvicorn_options() -> dict[str, Any]:
    """返回 uvicorn.run 的监听参数（配置在调用时读取，便于测试替换）。

    这是容器 entrypoint 实际 exec 的进程，必须与 ``app.main`` 使用同一份
    配置，否则 HOST/PORT 只在文档里生效。
    """
    from app.config import config

    return {
        "host": config.host,
        "port": config.port,
        "log_level": "info",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", **uvicorn_options())
