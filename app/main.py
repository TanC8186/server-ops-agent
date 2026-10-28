"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api import aiops, chat, file, health, knowledge, runs
from app.config import config
from app.core.auth import require_api_key, startup_check
from app.core.milvus_client import milvus_manager
from app.services import chat_events


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    startup_check()
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus（如果 Docker 没启动则跳过，基本对话功能仍可用）
    logger.info("🔌 正在连接 Milvus...")
    try:
        milvus_manager.connect()
        logger.info("✅ Milvus 连接成功")
    except Exception as e:
        logger.warning(f"⚠️  Milvus 连接失败: {e}")
        logger.warning("⚠️  RAG 知识库和文档上传功能不可用，但 AI 对话功能正常")
    
    logger.info("=" * 60)
    
    # trace retention 后台清理：显式启动、可观测（/health -> trace_retention）。
    # 必须在 try 里启动：清理只是运维动作，任何异常都不能让进程起不来。
    try:
        runs.start_trace_purge_scheduler()
    except Exception as e:  # noqa: BLE001 - 启动路径不能因清理失败而中断
        logger.warning(f"⚠️  trace retention 后台任务启动失败: {type(e).__name__}")

    # 聊天事件只在一次断流续读窗口内有意义：启动时清一次过期文件即可，
    # 不为它再起一个后台线程。清理失败只告警，不能让进程起不来。
    try:
        removed = chat_events.purge_expired()
        if removed:
            logger.info(f"启动清理过期聊天事件: {removed} 个文件")
    except Exception as e:  # noqa: BLE001 - 清理是运维动作，不是启动前提
        logger.warning(f"⚠️  过期聊天事件清理失败: {type(e).__name__}")

    yield

    # 关闭时执行
    try:
        await runs.stop_trace_purge_scheduler()
    except Exception as e:  # noqa: BLE001 - 退出路径同样不能阻塞
        logger.warning(f"⚠️  trace retention 后台任务停止异常: {type(e).__name__}")

    logger.info("🔌 正在关闭 Milvus 连接...")
    try:
        milvus_manager.close()
    except Exception:
        pass
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="企业级智能 On-Call 运维助手 — AIOps 自动故障诊断与 RAG 知识库问答",
    lifespan=lifespan
)

# 配置 CORS（来源统一来自 Settings，只写在 .env 里也能生效）
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(
    chat.router, prefix="/api", tags=["对话"], dependencies=[Depends(require_api_key)]
)
app.include_router(
    file.router, prefix="/api", tags=["文件管理"], dependencies=[Depends(require_api_key)]
)
app.include_router(
    aiops.router, prefix="/api", tags=["AIOps智能运维"], dependencies=[Depends(require_api_key)]
)
app.include_router(
    runs.router, prefix="/api", tags=["运行追踪"], dependencies=[Depends(require_api_key)]
)
app.include_router(
    knowledge.router, prefix="/api", tags=["知识库"], dependencies=[Depends(require_api_key)]
)

from pathlib import Path

# 项目根目录（main.py 在 app/ 下，根目录在上层）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 前端构建产物：web/dist（cd web && npm run build）
_web_dist = _PROJECT_ROOT / "web" / "dist"
if (_web_dist / "assets").exists():
    app.mount(
        "/assets", StaticFiles(directory=str(_web_dist / "assets")), name="assets"
    )


@app.get("/")
async def root():
    """返回前端首页（Vite 构建产物或旧版静态页）。"""
    index_path = _web_dist / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        log_level="info"
    )
