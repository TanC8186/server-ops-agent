"""按白名单目录抓取核心语料原始快照（M3）。

用途：把 ``docs/claude-code-handoff/knowledge_sources.json`` 里
``ingestion_stage=core`` 且尚未取证的来源，逐页抓取一次并落盘为
``knowledge/raw/<source_id>/<sha256[:12]><ext>``，同时更新
``knowledge/manifests/import-<date>.json``。

边界（与 RAG_CORPUS_PLAN.md 第 4 节一致）：

- 只抓目录里的明确 URL，不跟随链接、不递归、不读用户输入；
- 每个 URL 最多请求 ``MAX_REQUESTS`` 次，单页大小上限 ``MAX_BYTES``；
- 抓取结果按字节保存，sha256 与字节数写入 manifest；
- 许可未知（``license.identifier`` 为空）的来源仍然保存原文，但 manifest 里
  标记 ``license_status=needs_review``，不进入 ready。

用法（仅在本机、显式执行）::

    ./.venv/Scripts/python.exe scripts/fetch_corpus.py --stage core [--proxy URL]

**不要**在测试或 CI 里跑它：它会访问外网。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "docs" / "claude-code-handoff" / "knowledge_sources.json"
RAW_DIR = ROOT / "knowledge" / "raw"
MANIFEST_DIR = ROOT / "knowledge" / "manifests"

#: 单页最大字节数（8 MiB）。手册页远小于此值。
MAX_BYTES = 8 * 1024 * 1024
#: 每个 URL 的最大请求次数（首请求 + 一次重试）。
MAX_REQUESTS = 2
#: 请求超时（秒）。
TIMEOUT = 30
#: 只允许这些域名（白名单目录里的来源域）。
ALLOWED_HOSTS = frozenset({
    "docs.redhat.com",
    "www.freedesktop.org",
    "docs.kernel.org",
    "docs.docker.com",
    "kubernetes.io",
    "prometheus.io",
    "learn.microsoft.com",
    "www.postgresql.org",
    # AutoDL 官方文档（G8 追加）。注意 api.autodl.com 是**文档站**的另一个主机名，
    # 不是可调用 API 端点——抓取器只读 GET 页面，不会发任何 API 请求。
    "www.autodl.com",
    "api.autodl.com",
})
#: 拒绝入库的内容类型（登录墙/错误页）。
REJECT_CONTENT = ("text/plain", "application/json", "text/xml")


def _slug(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url)[-40:].strip("_") or "page"


def _ext_for(content_type: str) -> str:
    if "html" in content_type:
        return ".html"
    if "markdown" in content_type or "text/plain" in content_type:
        return ".md"
    return ".bin"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_sources() -> dict:
    return json.loads(SOURCES.read_text(encoding="utf-8"))


def manifest_path() -> Path:
    return MANIFEST_DIR / f"import-{datetime.now(timezone.utc):%Y-%m-%d}.json"


def load_manifest() -> dict:
    path = manifest_path()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"import_date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entries": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = manifest_path()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    temp.replace(path)


def fetch(url: str, *, proxy: str | None = None) -> tuple[bytes, str, str, int]:
    """抓取一个 URL；返回 (bytes, final_url, content_type, http_status)。

    只允许白名单主机；重定向后的主机也要重新校验，避免被跳转到内网。
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"域名不在白名单: {host}")

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "OnCallAgent-corpus-fetch/1.0 (manual run)")]

    last_error: Exception | None = None
    for _attempt in range(MAX_REQUESTS):
        try:
            with opener.open(url, timeout=TIMEOUT) as response:
                final_url = response.geturl()
                final_host = urllib.parse.urlsplit(final_url).hostname or ""
                if final_host not in ALLOWED_HOSTS:
                    raise ValueError(f"重定向到白名单外主机: {final_host}")
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError(f"响应超过 {MAX_BYTES} 字节上限")
                return body, final_url, content_type, int(response.status)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1.0)
    raise RuntimeError(f"抓取失败: {last_error}")


def upsert(manifest: dict, entry: dict) -> None:
    entries = manifest.setdefault("entries", [])
    for index, existing in enumerate(entries):
        if existing.get("id") == entry.get("id"):
            entries[index] = {**existing, **entry}
            return
    entries.append(entry)


def fetch_source(source: dict, manifest: dict, *, proxy: str | None,
                 force: bool = False) -> dict:
    source_id = source["id"]
    url = source["source_url"]
    manifest_ids = {entry.get("id") for entry in manifest.get("entries", [])}
    if source_id in manifest_ids and not force:
        print(f"[skip] {source_id} 已有快照")
        return {"id": source_id, "status": "already_present"}

    print(f"[fetch] {source_id} {url}")
    try:
        body, final_url, content_type, status = fetch(url, proxy=proxy)
    except Exception as exc:  # noqa: BLE001 - 单个来源失败不影响其它来源
        print(f"[fail ] {source_id}: {exc}")
        return {"id": source_id, "status": "fetch_failed", "error": str(exc)[:300]}

    digest = hashlib.sha256(body).hexdigest()
    directory = RAW_DIR / source_id
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{digest[:12]}{_ext_for(content_type)}"
    path = directory / filename
    path.write_bytes(body)

    license_info = source.get("license") or {}
    entry = {
        "id": source_id,
        "url": url,
        "source_url": url,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "bytes": len(body),
        "sha256": digest,
        "fetched_at_utc": _now(),
        "upstream_version": source.get("version_scope") or "",
        "license_basis_url": license_info.get("evidence_url") or "",
        "license_id": license_info.get("identifier"),
        "license_status": license_info.get("status") or "needs_review",
        "family": source.get("source_family") or "",
        "os_family": source.get("os_family") or "",
        "title": source.get("title") or "",
        "raw_path": path.relative_to(ROOT / "knowledge").as_posix(),
        "status": "snapshot_ok",
        "note": ("saved byte-for-byte; content type "
                 f"{content_type or 'unknown'}; fetched by scripts/fetch_corpus.py"),
    }
    upsert(manifest, entry)
    save_manifest(manifest)
    print(f"[ ok  ] {source_id} {len(body)} bytes {digest[:12]} -> {entry['raw_path']}")
    return {"id": source_id, "status": "snapshot_ok", "bytes": len(body)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取白名单核心语料原始快照")
    parser.add_argument("--stage", default="core",
                        choices=["core", "extension", "optional", "all"])
    parser.add_argument("--proxy", default=None,
                        help="可选 HTTP 代理（例如 http://127.0.0.1:7897）")
    parser.add_argument("--force", action="store_true", help="已有快照也重新抓取")
    parser.add_argument("--only", action="append", default=None,
                        help="只抓指定 source id（可重复）")
    args = parser.parse_args(argv)

    data = load_sources()
    sources = [
        source for source in data.get("sources", [])
        if args.stage == "all" or source.get("ingestion_stage") == args.stage
    ]
    if args.only:
        wanted = set(args.only)
        sources = [source for source in sources if source.get("id") in wanted]
    if not sources:
        print("没有匹配的来源")
        return 1

    manifest = load_manifest()
    results = [fetch_source(source, manifest, proxy=args.proxy, force=args.force)
               for source in sources]
    ok = sum(1 for item in results if item.get("status") in {"snapshot_ok", "already_present"})
    print(f"\n完成: {ok}/{len(results)}；manifest: {manifest_path()}")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
