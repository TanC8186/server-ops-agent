"""测试公共夹具。

测试会调用真实的 graph 节点，这些节点会写 trace。这里把 trace 目录指到临时目录，
避免污染 `logs/traces/`（否则前端会把测试用的假 run 当成真实诊断展示）。

同理，principal 归属侧表（``data/principal_acl.json``）也指向临时文件：真实
API 测试会上传文档/建会话并写入归属，跑到仓库目录里会污染下一次真实启动。
"""

import pytest

from app import observability
from app.core import principal as principal_acl


@pytest.fixture(autouse=True)
def isolated_trace_dir(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)
    return trace_dir


@pytest.fixture(autouse=True)
def isolated_acl_file(tmp_path, monkeypatch):
    """归属侧表隔离：每个测试拿到一份空的 ACL 文件。"""
    acl_file = tmp_path / "acl" / "principal_acl.json"
    monkeypatch.setenv(principal_acl.ACL_FILE_ENV, str(acl_file))
    return acl_file
