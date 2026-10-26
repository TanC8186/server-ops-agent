#!/usr/bin/env python
"""Snapshot one allowlisted documentation page with provenance metadata.

Target: LK-01 (linux-kernel family) -- https://docs.kernel.org/admin-guide/mm/concepts.html

Behaviour (see task constraints):
  * single GET through http://127.0.0.1:7897, timeout 20 s, redirects followed
  * normal browser User-Agent, Accept-Encoding: identity (raw bytes untouched)
  * body refused if larger than 8 MiB
  * raw body written byte-for-byte to knowledge/raw/<ID>/<sha256[:12]>.<ext>
  * provenance appended/merged into knowledge/manifests/import-<utc-date>.json
  * bounded politeness: no retry after 4xx, at most one retry (after 5 s) on 5xx,
    never more than two GETs of the page

Stdlib only. No credentials are read or printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROXY = "http://127.0.0.1:7897"
TIMEOUT_S = 20
MAX_BYTES = 8 * 1024 * 1024
RETRY_DELAY_S = 5
MAX_ATTEMPTS = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "OnCallAgent-KnowledgeSnapshot/1.0"
)

KNOWLEDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DIR = os.path.join(KNOWLEDGE_DIR, "manifests")
RAW_ROOT = os.path.join(KNOWLEDGE_DIR, "raw")

# --- LK-01 configuration -----------------------------------------------------
DOC_ID = "LK-01"
SOURCE_URL = "https://docs.kernel.org/admin-guide/mm/concepts.html"
FAMILY = "linux-kernel"
LICENSE_BASIS_URL = "https://raw.githubusercontent.com/torvalds/linux/master/COPYING"
LICENSE_ID = "GPL-2.0 (kernel COPYING; documentation inherits GPL-2.0)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch(url: str) -> tuple[int | None, bytes, dict[str, str], str, str | None]:
    """GET url once. Returns (status, body, headers, final_url, error)."""
    handlers = [
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
        urllib.request.HTTPRedirectHandler(),
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
        method="GET",
    )
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            status = resp.getcode()
            final_url = resp.geturl()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return status, b"", headers, final_url, "body exceeds 8 MiB limit; refused"
            return status, body, headers, final_url, None
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        try:
            body = exc.read(MAX_BYTES + 1)
        except Exception:
            body = b""
        return exc.code, body, headers, getattr(exc, "url", url), None
    except urllib.error.URLError as exc:
        return None, b"", {}, url, f"URLError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - record and stop
        return None, b"", {}, url, f"{type(exc).__name__}: {exc}"


def extension_for(content_type: str, final_url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return ".html"
    if ct in ("text/markdown", "text/x-markdown"):
        return ".md"
    if ct == "text/plain":
        return ".txt"
    path = final_url.split("?", 1)[0].lower()
    for ext in (".html", ".htm", ".md", ".txt"):
        if path.endswith(ext):
            return ".html" if ext == ".htm" else ext
    return ".bin"


def visible_text_len(html: str) -> int:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return len(re.sub(r"\s+", " ", text).strip())


def detect_version(html: str) -> str | None:
    """Return a kernel version explicitly stated by the page, if any."""
    patterns = [
        r'(?i)<meta[^>]+name="version"[^>]+content="([^"]+)"',
        r"(?i)\b(?:linux\s+)?kernel\s+v?(\d+\.\d+(?:\.\d+)?)\b",
        r"(?i)\bkernel\s+version[:\s]+v?(\d+\.\d+(?:\.\d+)?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def classify(status: int | None, html: str, headers: dict[str, str]) -> tuple[str, str]:
    """Return (status_word, reason)."""
    if status is None:
        return "error", "transport failure"
    if status == 200:
        pass
    elif status in (401, 403, 407):
        return "blocked", f"HTTP {status}: access denied / login wall"
    else:
        return "blocked" if status != 200 else "snapshot_ok", f"HTTP {status}"
    ctype = (headers.get("content-type") or "").lower()
    if "html" not in ctype:
        return "error", f"unexpected content-type {ctype!r}"
    text_len = visible_text_len(html)
    if text_len < 300:
        return "blocked", f"page has only {text_len} visible characters (bare/empty page)"
    title_m = re.search(r"(?is)<title>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    if re.search(r"(?i)\b(404|not found|access denied|forbidden|sign in|log in)\b", title):
        return "blocked", f"error/login page title: {title!r}"
    return "snapshot_ok", f"content page ({text_len} visible chars)"


def merge_manifest(entry: dict) -> str:
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    date_tag = entry["fetched_at_utc"][:10]
    path = os.path.join(MANIFEST_DIR, f"import-{date_tag}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            data = {"schema": "knowledge-import/1", "utc_date": date_tag, "entries": data}
    else:
        data = {"schema": "knowledge-import/1", "utc_date": date_tag, "entries": []}
    entries = data.setdefault("entries", [])
    for i, old in enumerate(entries):
        if old.get("id") == entry["id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    return path


def main() -> int:
    fetched_at = utc_now()
    fetched_iso = fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    status, body, headers, final_url, error = fetch(SOURCE_URL)
    attempts = 1

    # Bounded politeness: retry at most once, only on 5xx.
    if status is not None and 500 <= status <= 599 and attempts < MAX_ATTEMPTS:
        time.sleep(RETRY_DELAY_S)
        status, body, headers, final_url, error = fetch(SOURCE_URL)
        attempts += 1

    result: dict = {
        "id": DOC_ID,
        "source_url": SOURCE_URL,
        "final_url": final_url,
        "http_status": status if status is not None else 0,
        "content_type": headers.get("content-type", ""),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest() if body else "",
        "fetched_at_utc": fetched_iso,
        "attempts": attempts,
        "raw_path": None,
        "note": "",
    }

    if error:
        result.update(status="error", note=f"fetch failed: {error}; attempts={attempts}")
        path = merge_manifest(result)
        print(json.dumps({"result": result, "manifest": path}, ensure_ascii=True))
        return 1

    ctype = headers.get("content-type", "")
    html = body.decode("utf-8", errors="replace")
    verdict, reason = classify(status, html, headers)

    if verdict != "snapshot_ok":
        result.update(
            status="blocked",
            note=f"{reason}; HTTP {status}; body NOT saved as ready content; attempts={attempts}",
        )
        path = merge_manifest(result)
        print(json.dumps({"result": result, "manifest": path}, ensure_ascii=True))
        return 0

    ext = extension_for(ctype, final_url)
    sha = result["sha256"]
    out_dir = os.path.join(RAW_ROOT, DOC_ID)
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, f"{sha[:12]}{ext}")
    with open(raw_path, "wb") as fh:
        fh.write(body)

    # verify byte-for-byte
    with open(raw_path, "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() != sha:
            result.update(status="error", note="post-write sha256 mismatch")
            path = merge_manifest(result)
            print(json.dumps({"result": result, "manifest": path}, ensure_ascii=True))
            return 1

    stated_version = detect_version(html)
    # docs.kernel.org serves the rolling "latest" tree; the page itself carries no
    # kernel version marker, so the content hash is the only pin we can state.
    upstream_version = stated_version if stated_version else "unversioned rolling docs"

    result.update(
        raw_path=os.path.relpath(raw_path, os.path.dirname(KNOWLEDGE_DIR)).replace("\\", "/"),
        upstream_version=upstream_version,
        license_basis_url=LICENSE_BASIS_URL,
        license_id=LICENSE_ID,
        status="snapshot_ok",
        note=(
            f"{reason}; redirected={final_url != SOURCE_URL}; "
            f"page-stated version={'none' if not stated_version else stated_version}; "
            f"docs.kernel.org serves the rolling 'latest' tree, so the sha256 of the raw body "
            f"is the pin; GPL-2.0 (kernel COPYING) permits storage and redistribution"
        ),
    )
    path = merge_manifest(result)
    print(json.dumps({"result": result, "manifest": path}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
