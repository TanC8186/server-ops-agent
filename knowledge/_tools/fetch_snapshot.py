#!/usr/bin/env python3
"""Bounded single-page snapshot fetcher for the knowledge import.

Usage:
    python fetch_snapshot.py --id SD-01 --url <url> --out-root <knowledge dir>

Behaviour (per task spec):
  * GET through http://127.0.0.1:7897, 20s timeout, follow redirects, normal UA
  * refuse a body larger than 8 MiB
  * records final URL, status, content-type, byte length, sha256, UTC timestamp
  * writes the body byte-for-byte to <root>/raw/<ID>/<sha256[:12]>.<ext>
  * merges one JSON entry into <root>/manifests/import-<utc-date>.json
  * no retry after 4xx; at most one retry after 5s on 5xx; never more than 2 GETs
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
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


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def utc_iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_once(url: str) -> dict:
    """One GET. Returns dict with status, final_url, headers, body bytes."""
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
        chunks = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                resp.close()
                raise RuntimeError(f"body exceeds 8 MiB limit (> {MAX_BYTES} bytes)")
            chunks.append(chunk)
        resp.close()
        body = b"".join(chunks)
        return {
            "status": status,
            "final_url": final_url,
            "headers": headers,
            "body": body,
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        # HTTPError is also a response object; read a small amount for the error page
        status = exc.code
        final_url = exc.geturl() if hasattr(exc, "geturl") else url
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
    except Exception as exc:  # timeouts, DNS, proxy refusal
        return {
            "status": 0,
            "final_url": url,
            "headers": {},
            "body": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def pick_extension(content_type: str, final_url: str, body: bytes) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return "html"
    if ct in ("text/markdown", "text/x-markdown"):
        return "md"
    if ct.startswith("text/"):
        return "txt"
    path = urllib.parse.urlparse(final_url).path.lower()
    for ext in (".html", ".htm", ".md", ".txt"):
        if path.endswith(ext):
            return "html" if ext in (".html", ".htm") else ext[1:]
    head = body[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "html"
    return "txt"


def header_get(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def find_upstream_version(text: str, fallback: str) -> str:
    """Look for an explicit version statement in the page text."""
    import re

    patterns = [
        r"systemd\s+(\d{2,4})",  # systemd 262
        r"System\s+and\s+Service\s+Manager[^<]{0,40}?(?:version\s+)?(\d{3})",
        r"powershell-(\d+(?:\.\d+)?)",
        r"[Vv]ersion\s+(\d+\.\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return fallback


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="source id, e.g. SD-01")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-root", required=True, help="knowledge/ directory")
    parser.add_argument("--license-basis-url", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument(
        "--fallback-version",
        default="unversioned rolling docs",
        help="value used when the page states no version",
    )
    args = parser.parse_args(argv)

    started = utc_now()
    result = fetch_once(args.url)
    requests_made = 1

    status = result["status"]
    if status >= 500 and status < 600:
        print(f"[polite] {status} on attempt 1; sleeping 5s before single retry", file=sys.stderr)
        time.sleep(5)
        result = fetch_once(args.url)
        requests_made = 2

    body = result["body"]
    headers = result["headers"]
    status = result["status"]
    final_url = result["final_url"]
    content_type = header_get(headers, "Content-Type")
    content_encoding = header_get(headers, "Content-Encoding").lower()
    note_parts: list[str] = []
    if content_encoding == "gzip" and body:
        body = gzip.decompress(body)
        note_parts.append("server sent gzip despite Accept-Encoding: identity; body decompressed before hashing")

    sha256 = hashlib.sha256(body).hexdigest() if body else hashlib.sha256(b"").hexdigest()
    byte_len = len(body)
    fetched_at = utc_iso(utc_now())

    # --- classify status ---
    entry_status = "snapshot_ok"
    note = ""
    if result["error"] and status == 0:
        entry_status = "error"
        note = result["error"]
    elif 400 <= status < 500:
        entry_status = "blocked"
        note = f"HTTP {status}; 4xx, no retry attempted"
    elif status >= 500:
        entry_status = "error"
        note = f"HTTP {status} after {requests_made} attempt(s)"
    elif status != 200:
        entry_status = "blocked"
        note = f"unexpected HTTP {status}"
    else:
        sample = body[:4096].decode("utf-8", "replace").lower()
        title = ""
        if "<title" in sample:
            import re

            match = re.search(r"<title[^>]*>(.*?)</title>", sample, re.S)
            if match:
                title = re.sub(r"\s+", " ", match.group(1)).strip()
        lowered_title = title.lower()
        if "sign in" in lowered_title or "log in" in lowered_title or "login" in lowered_title:
            entry_status = "blocked"
            note = f"login wall detected (title: {title!r}); body not saved"
        elif byte_len < 2048 and ("error" in lowered_title or "not found" in lowered_title):
            entry_status = "blocked"
            note = f"error page (title: {title!r}); body not saved"

    text_sample = body[:400000].decode("utf-8", "replace")
    upstream_version = find_upstream_version(text_sample, args.fallback_version)

    raw_path_rel = ""
    raw_path_abs = ""
    if entry_status == "snapshot_ok":
        ext = pick_extension(content_type, final_url, body)
        raw_dir = os.path.join(args.out_root, "raw", args.id)
        os.makedirs(raw_dir, exist_ok=True)
        raw_name = f"{sha256[:12]}.{ext}"
        raw_path_abs = os.path.join(raw_dir, raw_name)
        with open(raw_path_abs, "wb") as handle:
            handle.write(body)
        raw_path_rel = f"raw/{args.id}/{raw_name}"
        note_parts.insert(0, f"saved {raw_name}")
    else:
        note_parts.insert(0, "body not saved")

    if result["error"] and note:
        note_parts.append(note)
    else:
        note_parts.append(f"HTTP {status} via proxy {PROXY}")

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
        "note": "; ".join(note_parts),
        "raw_path": raw_path_rel,
        "requests_made": requests_made,
    }

    manifests_dir = os.path.join(args.out_root, "manifests")
    os.makedirs(manifests_dir, exist_ok=True)
    manifest_name = f"import-{started.strftime('%Y-%m-%d')}.json"
    manifest_path = os.path.join(manifests_dir, manifest_name)
    manifest = {"import_date_utc": started.strftime("%Y-%m-%d"), "entries": []}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            try:
                manifest = json.load(handle)
            except json.JSONDecodeError:
                backup = manifest_path + ".corrupt"
                os.replace(manifest_path, backup)
                manifest = {"import_date_utc": started.strftime("%Y-%m-%d"), "entries": []}
    entries = manifest.setdefault("entries", [])
    entries = [item for item in entries if item.get("id") != args.id]
    entries.append(entry)
    manifest["entries"] = entries
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, manifest_path)

    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print(f"MANIFEST={manifest_path}", file=sys.stderr)
    if raw_path_abs:
        print(f"RAW={raw_path_abs}", file=sys.stderr)
    return 0 if entry_status == "snapshot_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
