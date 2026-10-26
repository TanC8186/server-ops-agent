#!/usr/bin/env python3
"""Single-page snapshot fetcher for WS-04 (Get-Service, PowerShell 7.6 docs).

Bounded politeness, byte-exact raw storage, one merged manifest entry.

  * GET via proxy http://127.0.0.1:7897, timeout 20s, redirects followed, normal UA
  * refuses a body larger than 8 MiB (8 * 1024 * 1024 bytes)
  * records final_url, http_status, content-type, bytes, sha256, fetched_at_utc
  * writes the raw body byte-for-byte to <root>/raw/<ID>/<sha256[:12]>.<ext>
  * merges one entry into <root>/manifests/import-<utc-date>.json
  * 4xx: never retried. 5xx: at most one retry after 5s. Never more than 2 GETs.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROXY = "http://127.0.0.1:7897"
TIMEOUT = 20.0
MAX_BYTES = 8 * 1024 * 1024
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def utc_iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_once(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    try:
        resp = opener.open(req, timeout=TIMEOUT)
        status = resp.getcode()
        final_url = resp.geturl()
        headers = dict(resp.headers.items())
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                resp.close()
                raise RuntimeError("body exceeds 8 MiB limit; aborted")
            chunks.append(chunk)
        resp.close()
        return {
            "status": status,
            "final_url": final_url,
            "headers": headers,
            "body": b"".join(chunks),
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            final_url = exc.geturl()
        except Exception:
            final_url = url
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            body = exc.read(MAX_BYTES + 1)
        except Exception:
            body = b""
        exc.close()
        return {
            "status": status,
            "final_url": final_url,
            "headers": headers,
            "body": body,
            "error": f"HTTPError {status}",
        }
    except Exception as exc:
        return {
            "status": 0,
            "final_url": url,
            "headers": {},
            "body": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def header_get(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value or ""
    return ""


def pick_extension(content_type: str, final_url: str, body: bytes) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return "html"
    if ct in ("text/markdown", "text/x-markdown"):
        return "md"
    if ct.startswith("text/"):
        return "txt"
    path = urllib.parse.urlparse(final_url).path.lower()
    if path.endswith((".html", ".htm")):
        return "html"
    if path.endswith(".md"):
        return "md"
    if path.endswith(".txt"):
        return "txt"
    head = body[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "html"
    return "txt"


def classify(text: str) -> tuple[str, str]:
    """Return (status, reason) using only cheap signals on the fetched page."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    low_title = title.lower()
    lowered = text.lower()
    if any(token in low_title for token in ("sign in", "log in", "login", "sign-in")):
        return "blocked", f"login wall (title: {title!r})"
    if any(token in lowered for token in ("404 - page not found", "page not found", "error 404")):
        return "blocked", f"error page (title: {title!r})"
    # A usable cmdlet page states the cmdlet name and has a Syntax section.
    if "get-service" not in lowered:
        return "blocked", f"page does not appear to document Get-Service (title: {title!r})"
    if "syntax" not in lowered:
        return "blocked", "bare table of contents / no Syntax section"
    return "snapshot_ok", ""


def find_version(text: str) -> str:
    """The page is version-scoped by its view= query and in-page moniker links."""
    match = re.search(r"powershell-(\d+\.\d+)", text, re.I)
    if match:
        return f"powershell-{match.group(1)}"
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--license-basis-url", required=True)
    parser.add_argument("--family", default="windows")
    args = parser.parse_args(argv)

    started = _dt.datetime.now(_dt.timezone.utc)
    result = fetch_once(args.url)
    requests_made = 1
    status = result["status"]
    if 500 <= status < 600:
        print(f"[polite] HTTP {status} on attempt 1; one retry after 5s", file=sys.stderr)
        time.sleep(5)
        result = fetch_once(args.url)
        requests_made = 2

    body = result["body"]
    headers = result["headers"]
    status = result["status"]
    final_url = result["final_url"]
    content_type = header_get(headers, "Content-Type")
    notes: list[str] = []

    if header_get(headers, "Content-Encoding").lower() == "gzip" and body:
        body = gzip.decompress(body)
        notes.append("server sent gzip despite identity; decompressed before hashing")

    sha256 = hashlib.sha256(body).hexdigest()
    byte_len = len(body)
    fetched_at = utc_iso(_dt.datetime.now(_dt.timezone.utc))
    text = body.decode("utf-8", "replace")

    if result["error"] and status == 0:
        entry_status, reason = "error", result["error"]
    elif 400 <= status < 500:
        entry_status, reason = "blocked", f"HTTP {status}; 4xx, not retried"
    elif status >= 500:
        entry_status, reason = "error", f"HTTP {status} after {requests_made} attempt(s)"
    elif status != 200:
        entry_status, reason = "blocked", f"unexpected HTTP {status}"
    else:
        entry_status, reason = classify(text)
    if reason:
        notes.append(reason)

    upstream_version = find_version(text) or "powershell-7.6 (from URL view= moniker)"
    if not upstream_version.startswith("powershell-"):
        upstream_version = "powershell-7.6"

    raw_rel = ""
    if entry_status == "snapshot_ok":
        ext = pick_extension(content_type, final_url, body)
        raw_dir = os.path.join(args.out_root, "raw", args.id)
        os.makedirs(raw_dir, exist_ok=True)
        raw_name = f"{sha256[:12]}.{ext}"
        raw_abs = os.path.join(raw_dir, raw_name)
        with open(raw_abs, "wb") as handle:
            handle.write(body)
        raw_rel = f"raw/{args.id}/{raw_name}"
        notes.append(f"saved {raw_name} byte-for-byte")
    else:
        notes.append("body not saved")

    entry = {
        "id": args.id,
        "url": args.url,
        "source_url": args.url,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "bytes": byte_len,
        "sha256": sha256,
        "fetched_at_utc": fetched_at,
        "upstream_version": upstream_version,
        "license_basis_url": args.license_basis_url,
        "family": args.family,
        "status": entry_status,
        "note": "; ".join(notes),
        "raw_path": raw_rel,
        "requests_made": requests_made,
    }

    manifest_dir = os.path.join(args.out_root, "manifests")
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_path = os.path.join(manifest_dir, f"import-{started.strftime('%Y-%m-%d')}.json")
    manifest = {"import_date_utc": started.strftime("%Y-%m-%d"), "entries": []}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            try:
                manifest = json.load(handle)
            except json.JSONDecodeError:
                os.replace(manifest_path, manifest_path + ".corrupt")
                manifest = {"import_date_utc": started.strftime("%Y-%m-%d"), "entries": []}
    entries = [item for item in manifest.get("entries", []) if item.get("id") != args.id]
    entries.append(entry)
    manifest["entries"] = entries
    tmp_path = manifest_path + f".{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, manifest_path)

    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print(f"MANIFEST={manifest_path}", file=sys.stderr)
    return 0 if entry_status == "snapshot_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
