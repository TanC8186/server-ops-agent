#!/usr/bin/env python3
"""DK-01 snapshot: https://docs.docker.com/engine/logging/ (license-gated).

One GET through http://127.0.0.1:7897, 20s timeout, redirects followed, normal
User-Agent, 8 MiB body ceiling. Records final URL, status, content-type, byte
length, sha256 and UTC timestamp, then merges one entry into
knowledge/manifests/import-<utc-date>.json.

Docker-family license gate (from the run's authoritative license report):
the docker/docs repository is Apache-2.0, but the rendered docs.docker.com
pages end with "Legal Copyright (c) 2013-2026 Docker Inc. All rights reserved."
plus "Automated traffic detection", which is NOT the Apache-2.0 grant. The
report's instruction is therefore: do not treat scraped docs.docker.com HTML as
Apache-2.0 and do not store it; ingest docker/docs markdown at a pinned commit
and keep the canonical page URL only as the human-facing citation.

So this script always records metadata but does NOT write the body under
knowledge/raw unless --allow-store is passed. For DK-01 that flag must not be
used: the entry is written with status "pending_license" and the body is
discarded. --allow-store exists only so the recording path is testable and so a
future, clearly licensed URL can reuse the script.

Politeness: no retry after 4xx; one retry after 5s on 5xx; never more than two
GETs of the page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_snapshot import (  # noqa: E402  (same-directory helper)
    MAX_BYTES,
    PROXY,
    fetch_once,
    header_get,
    pick_extension,
    utc_iso,
    utc_now,
)

SOURCE_ID = "DK-01"
SOURCE_URL = "https://docs.docker.com/engine/logging/"
SOURCE_FAMILY = "docker"
LICENSE_BASIS_URL = (
    "https://raw.githubusercontent.com/docker/docs/"
    "1d503f8b7c2b2008da6cbc08bb5be163666c84bf/LICENSE"
)
MAX_ATTEMPTS = 2


def classify(body: bytes, content_type: str, final_url: str) -> tuple[str, list[str]]:
    """Return (status, problem_notes) for a 200 response."""
    lowered = body.decode("utf-8", "replace").lower()
    problems: list[str] = []

    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.S)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()

    if title and any(word in title for word in ("sign in", "log in", "login", "unauthorized")):
        problems.append(f"login wall (title={title!r})")

    if len(body) < 2048 and any(word in title for word in ("error", "not found", "404", "403")):
        problems.append(f"error page (title={title!r})")

    # Bare table of contents: very little prose relative to link markup.
    anchors = len(re.findall(r"<a\b", lowered))
    text_len = len(re.sub(r"<[^>]+>", " ", lowered))
    if body and text_len < 4000 and anchors > 40:
        problems.append(f"looks like a bare table of contents ({anchors} links, {text_len} chars of text)")

    # The docker docs footer that blocks Apache-2.0 treatment.
    if "all rights reserved" in lowered and "docker" in lowered:
        problems.append('page footer states "All rights reserved" (not the Apache-2.0 grant)')

    if b"automated traffic detection" in lowered:
        problems.append("page carries automated-traffic-detection notice")

    return ("pending_license" if problems else "snapshot_ok"), problems


def page_version(text: str) -> str | None:
    """Only accept an explicit Engine version statement, if the page has one."""
    match = re.search(
        r"(?:docker\s+)?engine(?:\s+version)?\s*:?\s*v?(\d{2}\.\d{2}(?:\.\d+)?)",
        text,
        re.I,
    )
    return match.group(1) if match else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-root", default="knowledge", help="knowledge/ directory")
    parser.add_argument(
        "--allow-store",
        action="store_true",
        help="write the body under raw/ (must NOT be used for docs.docker.com HTML)",
    )
    args = parser.parse_args(argv)

    started = utc_now()
    result = fetch_once(SOURCE_URL)
    attempts = 1
    if 500 <= result["status"] < 600 and attempts < MAX_ATTEMPTS:
        print(f"[polite] HTTP {result['status']}; sleeping 5s before the single retry", file=sys.stderr)
        time.sleep(5)
        result = fetch_once(SOURCE_URL)
        attempts = 2

    body = result["body"]
    status = result["status"]
    final_url = result["final_url"]
    content_type = header_get(result["headers"], "Content-Type")
    sha256 = hashlib.sha256(body).hexdigest()
    byte_len = len(body)
    fetched_at = utc_iso(utc_now())

    entry_status = "error"
    notes: list[str] = []
    if result["error"] and status == 0:
        notes.append(result["error"])
    elif 400 <= status < 500:
        entry_status = "blocked"
        notes.append(f"HTTP {status}; 4xx, no retry attempted")
    elif status >= 500:
        entry_status = "error"
        notes.append(f"HTTP {status} after {attempts} attempt(s)")
    elif status != 200:
        entry_status = "blocked"
        notes.append(f"unexpected HTTP {status}")
    else:
        if byte_len > MAX_BYTES:
            notes.append("body over 8 MiB; rejected")
            entry_status = "error"
        else:
            entry_status, problems = classify(body, content_type, final_url)
            notes.extend(problems)

    raw_rel = ""
    stored = False
    if entry_status == "snapshot_ok" and args.allow_store:
        ext = pick_extension(content_type, final_url, body)
        raw_dir = os.path.join(args.out_root, "raw", SOURCE_ID)
        os.makedirs(raw_dir, exist_ok=True)
        raw_name = f"{sha256[:12]}.{ext}"
        with open(os.path.join(raw_dir, raw_name), "wb") as handle:
            handle.write(body)
        raw_rel = f"raw/{SOURCE_ID}/{raw_name}"
        stored = True
        notes.insert(0, f"body saved as {raw_name}")
    elif entry_status == "snapshot_ok":
        # License gate fired: the page is only metadata-worthy.
        entry_status = "pending_license"

    if entry_status == "pending_license":
        notes.insert(
            0,
            "body withheld and not written to raw/: docker-family license report says rendered "
            "docs.docker.com HTML is explicitly NOT Apache-2.0 and must not be stored; ingest from "
            "docker/docs markdown at pinned commit 1d503f8b7c2b2008da6cbc08bb5be163666c84bf "
            "(content/manuals/engine/logging/_index.md) and keep this URL as citation only",
        )
        if not stored:
            notes.append("metadata recorded, body discarded after hashing")
    if result["error"]:
        notes.append(f"requests_made={attempts}")
    else:
        notes.append(f"HTTP {status} via proxy {PROXY} (requests_made={attempts})")

    text = body[:400000].decode("utf-8", "replace")
    version = page_version(text)
    upstream_version = (
        f"{version}" if version else f"unversioned rolling docs (body sha256 {sha256[:12]})"
    )

    entry = {
        "id": SOURCE_ID,
        "url": SOURCE_URL,
        "source_url": SOURCE_URL,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "bytes": byte_len,
        "sha256": sha256,
        "fetched_at_utc": fetched_at,
        "upstream_version": upstream_version,
        "license_basis_url": LICENSE_BASIS_URL,
        "family": SOURCE_FAMILY,
        "status": entry_status,
        "note": "; ".join(notes),
        "raw_path": raw_rel,
        "stored": stored,
        "requests_made": attempts,
    }

    manifests_dir = os.path.join(args.out_root, "manifests")
    os.makedirs(manifests_dir, exist_ok=True)
    manifest_path = os.path.join(manifests_dir, f"import-{started.strftime('%Y-%m-%d')}.json")
    manifest = {"import_date_utc": started.strftime("%Y-%m-%d"), "entries": []}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    entries = [item for item in manifest.get("entries", []) if item.get("id") != SOURCE_ID]
    entries.append(entry)
    manifest["entries"] = entries
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, manifest_path)

    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print(f"MANIFEST={manifest_path}", file=sys.stderr)
    return 0 if entry_status in ("snapshot_ok", "pending_license") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
