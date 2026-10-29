"""回归测试：模型可控参数不得逃逸到 shell 或读取白名单外文件。"""

import pytest

from servers.ssh_server import _safe_count, _safe_log_path, _safe_token


@pytest.mark.parametrize(
    "payload",
    [
        "; rm -rf /",
        "$(whoami)",
        "`id`",
        "foo' | shutdown -h now",
        "foo\nrm -rf /",
        "foo && cat /etc/shadow",
    ],
)
def test_safe_token_rejects_shell_metacharacters(payload):
    with pytest.raises(ValueError):
        _safe_token(payload, "keyword")


def test_safe_token_accepts_normal_keywords():
    assert _safe_token("ERROR timeout", "keyword") == "ERROR timeout"
    assert _safe_token("磁盘告警", "keyword") == "磁盘告警"


@pytest.mark.parametrize(
    "path",
    [
        "C:/logs/../../Windows/win.ini",
        "/var/log/../../etc/passwd",
        "C:/Users/secret.txt",
        "/etc/shadow",
        "C:/logs/a'; whoami; '",
    ],
)
def test_safe_log_path_rejects_traversal_and_injection(path):
    with pytest.raises(ValueError):
        _safe_log_path(path)


def test_safe_log_path_accepts_whitelisted_logs():
    assert _safe_log_path("/var/log/syslog") == "/var/log/syslog"
    assert _safe_log_path("C:/logs/app.log") == "C:/logs/app.log"


@pytest.mark.parametrize("value", [0, -1, 10000, "abc"])
def test_safe_count_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        _safe_count(value, "count")


def test_missing_log_file_is_unavailable_not_a_fake_entry(monkeypatch):
    """日志文件不存在时必须报 unavailable，不能伪造成"查到一条日志"。

    实测（G8，AutoDL 容器）：`/var/log/syslog` 不存在。旧实现是
    `tail ... 2>/dev/null || echo 'Log file not found'`，那句提示被当成
    正常日志条目返回，status=ok、count=1、entries=[{"message":"(empty)"}]。
    于是"这台机器没有 syslog"被展示成"查到了日志"。
    """
    from servers import ssh_server

    monkeypatch.setattr(ssh_server, "SERVER_TYPE", "linux")
    sentinel = "__ONCALL_NOLOGFILE__"
    monkeypatch.setattr(
        ssh_server, "_exec_readonly",
        lambda commands: {commands[0]: sentinel},
    )

    result = ssh_server.search_system_logs(log_type="syslog")

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["count"] == 0
    assert result["entries"] == []
    assert "不存在" in result["error"]
    # 关键：不能再出现那条伪造的 "(empty)" 记录。
    assert result["entries"] != [{"message": "(empty)"}]


def test_existing_log_file_still_returns_entries(monkeypatch):
    """文件存在时行为不变：正常返回行。"""
    from servers import ssh_server

    monkeypatch.setattr(ssh_server, "SERVER_TYPE", "linux")
    monkeypatch.setattr(
        ssh_server, "_exec_readonly",
        lambda commands: {commands[0]: "line one\nline two\n"},
    )

    result = ssh_server.search_system_logs(log_type="syslog")
    assert result["count"] == 2
    assert result["entries"][0]["message"] == "line one"


def test_connect_disables_key_fallback(monkeypatch):
    """连接时必须关掉 look_for_keys / allow_agent。

    实测（G8）：paramiko 默认会在配置的 key 被拒时**静默换用 ~/.ssh 里的其它
    key** 连上——把 key_filename 指向一个未授权的 key，连接依然成功。于是
    "本次用的是哪把钥匙"无法证明，SSH 验收证据不可信。
    """
    from servers import ssh_server

    captured = {}

    class _Client:
        def set_missing_host_key_policy(self, *_a):
            pass

        def connect(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ssh_server, "SSH_HOST", "example.invalid")
    monkeypatch.setattr(ssh_server.paramiko, "SSHClient", _Client)

    ssh_server._get_ssh_client()

    assert captured["allow_agent"] is False
    assert captured["look_for_keys"] is False
    assert captured["key_filename"] == ssh_server.SSH_KEY_FILE


def test_banner_failure_is_attributed_to_throttling(monkeypatch):
    """拿不到 banner 时不能报成"主机不可达"，要说清是限流/过载。

    实测：AutoDL 网关在并发握手时返回明文 Exceeded MaxStartups，paramiko 包成
    Error reading SSH protocol banner。TCP 层 10/10 可连，所以这不是连不上。
    """
    from servers import ssh_server

    class _Client:
        def __init__(self):
            self.calls = 0

        def set_missing_host_key_policy(self, *_a):
            pass

        def connect(self, **_kwargs):
            self.calls += 1
            raise ssh_server.paramiko.SSHException("Error reading SSH protocol banner")

    client = _Client()
    monkeypatch.setattr(ssh_server, "SSH_HOST", "example.invalid")
    monkeypatch.setattr(ssh_server.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(ssh_server.time, "sleep", lambda _s: None)

    import pytest as _pytest
    with _pytest.raises(RuntimeError) as excinfo:
        ssh_server._get_ssh_client()

    message = str(excinfo.value)
    assert "限流" in message or "banner" in message
    assert "非主机不可达" in message
    # 恰好重连一次，不是无限重试
    assert client.calls == 2
