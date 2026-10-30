"""把 AutoDL 官方语料导入知识库（Milvus + JSON store）。

只用生产接口（``knowledge_store.submit_document``），不直接写向量库，
这样 revision / ACL / 作业状态都由同一套规则产出。

用法::

    python scripts/ingest_autodl_corpus.py --dry-run     # 只打印将导入什么
    python scripts/ingest_autodl_corpus.py               # 真正入库并等待作业

每个文档都带任务书 §3.3 要求的 metadata：

    product=autodl, os_family=linux, target_kind=ssh, source_kind=manual

``product`` 决定它和腾讯云/其它语料分属不同过滤域；``target_kind=ssh`` 让
"检索 SSH 目标时只看 SSH 相关文档"成为可核验的过滤条件，而不是靠调用方
自己拼条件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "knowledge" / "sources" / "autodl"
MANIFEST = CORPUS / "manifest.json"
KNOWLEDGE = ROOT / "knowledge"


def load_entries() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return list(data.get("entries") or [])


def body_path(entry: dict) -> Path:
    """规范化 Markdown 的绝对路径（manifest 里存的是相对 knowledge/ 的路径）。"""
    return KNOWLEDGE / str(entry.get("normalized_path") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印，不入库")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待单个作业的秒数")
    args = parser.parse_args()

    from app.services import knowledge_store as store_api

    entries = load_entries()
    if not entries:
        print("manifest 里没有条目，先跑 fetch_corpus.py 与 normalize_corpus.py")
        return 2

    print(f"语料条目: {len(entries)}")
    planned = []
    for entry in entries:
        path = body_path(entry)
        if not path.is_file():
            print(f"[skip] {entry['id']}: 找不到规范正文 {path}")
            continue
        content = path.read_bytes()
        metadata = {
            "product": "autodl",
            "os_family": "linux",
            "target_kind": "ssh",
            "source_kind": "manual",
            "product_version": entry.get("product_version") or "",
            "license_id": entry.get("license_id") or "",
        }
        planned.append({
            "id": entry["id"],
            "source_uri": entry.get("source_uri") or "",
            "title": entry.get("title") or entry["id"],
            "content": content,
            "metadata": metadata,
        })
        print(f"  {entry['id']:6s} {len(content):6d} B  target_kind=ssh product=autodl "
              f"{entry.get('source_uri','')}")

    if args.dry_run:
        print(f"\n--dry-run：将导入 {len(planned)} 篇，未写入任何数据")
        return 0

    store = store_api.get_store()
    results = []
    for item in planned:
        accepted = store.submit_document(
            source_uri=item["source_uri"],
            title=item["title"],
            content=item["content"],
            source_kind="manual",
            metadata=item["metadata"],
        )
        job_id = accepted.get("job_id") or ""
        print(f"[submit] {item['id']} document_id={accepted.get('document_id')} job={job_id}")
        state = store_api.wait_for_job(job_id, timeout=args.timeout) if job_id else {}
        status = (state or {}).get("status", "unknown")
        print(f"[job]    {item['id']} -> {status}")
        results.append({"id": item["id"], "document_id": accepted.get("document_id"),
                        "job_id": job_id, "status": status})

    ok = sum(1 for r in results if r["status"] in {"ready", "succeeded", "completed"})
    print(f"\n完成: {ok}/{len(results)} 个作业成功")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
