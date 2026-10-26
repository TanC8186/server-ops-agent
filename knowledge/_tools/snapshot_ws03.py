#!/usr/bin/env python3
"""Snapshot exactly one page for source WS-03 (family: windows).

Constraints implemented here:
  * single URL, hard-coded; refuses any other target
  * proxy http://127.0.0.1:7897, timeout 20s, redirects followed
  * normal desktop User-Agent
  * body capped at 8 MB (Content-Length pre-check + hard read cap)
  * raw bytes written verbatim to knowledge/raw/WS-03/<sha256[:12]>.<ext>
  * one JSON entry merged into knowledge/manifests/import-<utc-date>.json
  * politeness: no retry on 4xx; at most one retry after 5s on 5xx; max 2 requests
  * login wall / bare TOC / error page => status "blocked", no ready content
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = (
    "https://learn.microsoft.com/en-us/powershell/module/"
    "microsoft.powershell.diagnostics/get-counter?view=powershell-7.6"
)
ID = "WS-03"
FAMILY = "windows"
PROXY = "http://127.0.0.1:7897"
TIMEOUT = 20
MAX_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LICENSE_BASIS_URL = (
    "https://raw.githubusercontent.com/MicrosoftDocs/PowerShell-Docs/main/ThirdPartyNotices.md"
)
UPSTREAM_VERSION = "powershell-7.6"

REPO = Path(__file__).resolve().parents[2]
RAW_DIR = REPO / "knowledge" / "raw" / ID
MANIFEST_DIR = REPO / "knowledge" / "manifests"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_once() -> dict:
    """One HTTP GET. Returns a dict with body or error, never raises."""
    proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    result: dict = {"attempted_at": utc_now_iso()}
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            status = resp.status
            final_url = resp.geturl()
            content_type = resp.headers.get("Content-Type", "")
            length_header = resp.headers.get("Content-Length")
            result.update(
                status=status,
                final_url=final_url,
                content_type=content_type,
                length_header=length_header,
                error=None,
            )
            if length_header is not None:
                try:
                    declared = int(length_header)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_BYTES:
                    result["body"] = b""
                    result["error"] = f"declared Content-Length {declared} exceeds 8 MB cap"
                    result["status"] = status
                    return result
            chunks = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    result["body"] = b""
                    result["error"] = "body exceeded 8 MB cap while reading; aborted"
                    return result
                chunks.append(chunk)
            result["body"] = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(MAX_BYTES + 1) or b""
        except Exception:
            pass
        result.update(
            status=exc.code,
            final_url=exc.geturl() or URL,
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            body=body if len(body) <= MAX_BYTES else b"",
            error=f"HTTPError {exc.code}: {exc.reason}",
        )
    except Exception as exc:  # network, timeout, TLS...
        result.update(
            status=0,
            final_url=URL,
            content_type="",
            body=b"",
            error=f"{type(exc).__name__}: {exc}",
        )
    return result


def fetch_bounded() -> dict:
    """No retry on 4xx. One retry after 5s on 5xx. At most two requests."""
    first = fetch_once()
    status = first.get("status") or 0
    if 500 <= status < 600:
        time.sleep(5)
        second = fetch_once()
        second["requests_made"] = 2
        second["first_attempt_status"] = status
        return second
    first["requests_made"] = 1
    return first


def extension_for(content_type: str, final_url: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in ("text/html", "application/xhtml+xml"):
        return "html"
    if ctype in ("text/markdown", "text/x-markdown"):
        return "md"
    if ctype == "text/plain":
        return "txt"
    if ctype == "application/json":
        return "json"
    path = urllib.parse.urlparse(final_url).path.lower()
    for ext in ("html", "htm", "md", "txt", "json"):
        if path.endswith("." + ext):
            return "html" if ext == "htm" else ext
    return "html"


def classify(body: bytes, content_type: str, status: int, error: str | None) -> tuple[str, str]:
    """Return (status, reason). page quality first, then transport."""
    if status == 0:
        return "error", f"transport error: {error}"
    if 400 <= status < 500:
        return "blocked", f"HTTP {status} ({error}); no retry per 4xx policy"
    if status != 200:
        return "error", f"HTTP {status} ({error})"

    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    low = text.lower()

    if len(body) < 2000:
        return "blocked", f"body only {len(body)} bytes; not a documentation page"
    login_markers = (
        "sign in to continue",
        "you need to sign in",
        "login.live.com",
        "sign in to your account",
    )
    if any(m in low for m in login_markers):
        return "blocked", "page is a sign-in wall"

    error_markers = (
        "this page doesn't exist",
        "we're sorry, we couldn't find",
        "page not found",
        "404 - page not found",
        "the resource you are looking for",
    )
    if any(m in low for m in error_markers) and "get-counter" not in low:
        return "blocked", "page is an error page"

    if "get-counter" not in low:
        return "blocked", "expected cmdlet name Get-Counter absent; page is not the target doc"

    # Rough visible-text length to reject a bare table of contents.
    stripped = re.sub(r"<script\b.*?</script>", " ", text, flags=re.S | re.I)
    stripped = re.sub(r"<style\b.*?</style>", " ", stripped, flags=re.S | re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(stripped) < 1500:
        return "blocked", f"visible text only {len(stripped)} chars; looks like a bare TOC/index"
    return "snapshot_ok", ""


def extract_version_marker(body: bytes) -> str:
    """Upstream version as stated by the page itself; fallback to URL moniker."""
    text = body.decode("utf-8", errors="replace")

    for pat in (
        r'name="moniker"\s+content="([^"]+)"',
        r'"moniker"\s*:\s*"([^"]+)"',
        r"view=(powershell-\d+\.\d+)",
        r"PowerShell\s+(7\.\d+)",
    ):
        m = re.search(pat, text, flags=re.I)
        if m:
            val = m.group(1)
            return val if val.startswith("powershell-") else f"powershell-{val}"
    return UPSTREAM_VERSION


def extract_version_evidence(body: bytes) -> dict:
    """Non-authoritative metadata the page states about itself (for the note)."""
    text = body.decode("utf-8", errors="replace")

    out = {}
    m = re.search(r'<meta name="ms\.date" content="([^"]+)"', text, flags=re.I)
    if m:
        out["ms_date"] = m.group(1)
    m = re.search(r'original_content_git_url" content="([^"]+)"', text, flags=re.I)
    if not m:
        m = re.search(r'<meta name="original_content_git_url"\s+content="([^"]+)"', text, flags=re.I)
    if m:
        out["original_content_git_url"] = m.group(1)
    m = re.search(r'<meta name="git_commit_id"\s+content="([^"]+)"', text, flags=re.I)
    if m:
        out["git_commit_id"] = m.group(1)
    return out


def merge_manifest(entry: dict, day: str) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = MANIFEST_DIR / f"import-{day}.json"
    doc: dict
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            doc = {"entries": []}
    else:
        doc = {"entries": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        doc = {"entries": []}
    entries = [e for e in doc["entries"] if e.get("id") != entry["id"]]
    entries.append(entry)
    entries.sort(key=lambda e: (e.get("id") or ""))
    doc["entries"] = entries
    doc["updated_at_utc"] = utc_now_iso()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main() -> int:
    fetched_at = utc_now_iso()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    res = fetch_bounded()
    body: bytes = res.get("body") or b""
    status = res.get("status") or 0
    final_url = res.get("final_url") or URL
    content_type = res.get("content_type") or ""

    verdict, reason = classify(body, content_type, status, res.get("error"))
    sha = sha256_hex(body) if body else ""

    saved_path = None
    note_parts = []
    if verdict == "snapshot_ok":
        ext = extension_for(content_type, final_url)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        target = RAW_DIR / f"{sha[:12]}.{ext}"
        target.write_bytes(body)  # verbatim bytes, no reformatting
        saved_path = target
        note_parts.append(f"raw bytes stored verbatim at knowledge/raw/{ID}/{target.name}")

    upstream_version = extract_version_marker(body) if body else UPSTREAM_VERSION
    ev = extract_version_evidence(body) if body else {}
    if ev.get("ms_date"):
        note_parts.append(f"page ms.date={ev['ms_date']}")
    if ev.get("git_commit_id"):
        note_parts.append(f"page git_commit_id={ev['git_commit_id']}")
    if ev.get("original_content_git_url"):
        note_parts.append(f"page original_content_git_url={ev['original_content_git_url']}")

    note_parts.append(
        "license family windows: docs CC-BY-4.0 per PowerShell-Docs LICENSE.md (legal code) "
        "+ ThirdPartyNotices.md (operative grant); repo code MIT per LICENSE-CODE.md"
    )
    note_parts.append(
        "rendered learn.microsoft.com HTML carries NO page-level CC notice (footer only "
        "Privacy/Terms/Trademarks/(c) Microsoft); per the family license report this cached HTML "
        "is evidence/needs_review until a page-level license statement exists, and the preferred "
        "ingest source is repo markdown PowerShell-Docs/reference/7.6/... at pinned commit "
        "live=88927f1ae80cac82b1183c7048ee161b50377b29"
    )
    note_parts.append(
        "attribution to ship if any text is distributed: (c) Microsoft Corporation. This "
        "documentation is licensed under CC BY 4.0 "
        "(https://creativecommons.org/licenses/by/4.0/). Source: " + URL
    )
    note_parts.append(f"requests_made={res.get('requests_made', 1)}")
    if res.get("first_attempt_status"):
        note_parts.append(f"first_attempt_status={res['first_attempt_status']} (retried once after 5s)")
    if res.get("error"):
        note_parts.append(f"error={res['error']}")
    if verdict == "blocked":
        note_parts.append(f"blocked_reason={reason}")

    entry = {
        "id": ID,
        "source_url": URL,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "bytes": len(body),
        "sha256": sha,
        "fetched_at_utc": fetched_at,
        "upstream_version": upstream_version,
        "license_basis_url": LICENSE_BASIS_URL,
        "status": verdict,
        "note": "; ".join(note_parts),
    }
    path = merge_manifest(entry, day)

    print(json.dumps({"entry": entry, "manifest": str(path), "saved": str(saved_path)}, indent=2))
    return 0 if verdict == "snapshot_ok" else 1


if __name__ == "__main__":
    sys.exit(main())
