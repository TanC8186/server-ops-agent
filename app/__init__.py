"""Server Ops Agent — 企业服务器运维 Agent

集 RAG 知识库问答和 AIOps 智能故障诊断于一体
"""

__version__ = "1.0.0"

# 必须在任何第三方库之前执行：pymilvus 在 import 时无参调用 load_dotenv()，
# 会把当前工作目录向上的 .env 写进 os.environ，破坏配置隔离。
from app.core import env_guard as _env_guard

_env_guard.install()

from app.utils import logger  # noqa: F401
