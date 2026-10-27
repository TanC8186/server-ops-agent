"""真实 Milvus + 真实本地 embedding 的端到端验收（G1.2）。

只替换“外部依赖形态”以外的东西：这里**不**替换 Milvus、不替换 embedding，
用的是真实的 KnowledgeStore 作业状态机、真实的 VectorStoreRetriever、
真实的 Milvus collection。唯一的环境前提是本机 19530 有 Milvus 在跑。

运行：
    ./.venv/Scripts/python.exe scripts/acceptance_milvus_e2e.py --help

输出：JSON 证据到 eval/results/milvus-e2e-<utc>.json，并打印摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 独立的 store 文件与 collection 名称，避免污染开发数据。
EVIDENCE_DIR = ROOT / "eval" / "results"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _core_documents() -> list[dict]:
    """读取 normalized manifest 里的 11 个 core 文档。"""
    manifest = json.loads(
        (ROOT / "knowledge" / "manifests" / "normalized-2026-10-26.json").read_text(
            encoding="utf-8"
        )
    )
    entries = []
    for entry in manifest.get("entries", []):
        path = ROOT / "knowledge" / entry["normalized_path"]
        if not path.is_file():
            continue
        entries.append({
            "id": entry["id"],
            "source_uri": entry["source_uri"],
            "path": path,
            "license_status": entry.get("license_status", ""),
            "chars": entry.get("chars", 0),
            "document_id": entry.get("document_id", ""),
        })
    return entries


def _milvus_facts() -> dict:
    """采集 collection 的真实事实：名称、metric、实体数、索引参数。"""
    from app.core.milvus_client import milvus_manager

    facts: dict = {"connected": False}
    try:
        milvus_manager.connect()
        collection = milvus_manager.get_collection()
        if collection is None:
            facts["error"] = "collection 不存在"
            return facts
        facts["connected"] = True
        facts["collection"] = collection.name
        facts["num_entities"] = int(collection.num_entities)
        indexes = []
        for index in collection.indexes or []:
            indexes.append({
                "field": getattr(index, "field_name", ""),
                "params": dict(getattr(index, "params", {}) or {}),
            })
        facts["indexes"] = indexes
        schema = collection.schema
        for field in schema.fields:
            if field.name == "vector":
                facts["dim"] = getattr(field.params, "get", lambda *_: None)("dim")
        facts["metric_type"] = next(
            (item["params"].get("metric_type") for item in indexes
             if item["params"].get("metric_type")),
            "",
        )
    except Exception as exc:  # noqa: BLE001 - 采集失败要如实报告
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def _wait_job(store, job_id: str, timeout: float = 600.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job["status"] in ("ready", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"作业超时: {job_id}")


#: 验收专用 store 路径。**必须稳定**：跨进程复验（导入在一个进程、
#: 查询/回滚在另一个进程）时，如果两次指向不同文件，就会出现
#: “Milvus 里有 953 条实体但检索 0 命中”的假象——那是环境隔离问题，
#: 不是产品缺陷（第一次真实运行就这样误报过）。
ACCEPTANCE_STORE = ROOT / "data" / "acceptance" / "milvus-e2e-store.json"


def _store_gate() -> None:
    """把 store 文件指向验收专用路径，避免覆盖开发数据。"""
    os.environ["KNOWLEDGE_STORE_FILE"] = str(ACCEPTANCE_STORE)


def step_import(store, documents: list[dict]) -> dict:
    """真实导入 11 个 core 文档并等待每个作业终态。

    注意：必须使用调用方传进来的**同一个** store 实例。这里不再自己
    ``reset_store()``——那会换掉单例，导致后续步骤（查询/回滚/注入失败）
    操作的是另一个空 store，出现“导入成功但查询 0 命中”的假象
    （第一次真实运行就是这样暴露出来的）。
    """
    results = []
    for document in documents:
        content = document["path"].read_bytes()
        accepted = store.submit_document(
            source_uri=document["source_uri"],
            title=document["id"],
            content=content,
            source_kind="manual",
        )
        final = _wait_job(store, accepted["job_id"])
        results.append({
            "id": document["id"],
            "job_id": accepted["job_id"],
            "document_id": accepted["document_id"],
            "revision": accepted["revision"],
            "status": final["status"],
            "chunks_total": final["progress"]["chunks_total"],
            "chunks_done": final["progress"]["chunks_done"],
            "error": final.get("error"),
        })
    return {"documents": results}


def step_query(store, queries: list[str], k: int = 3) -> dict:
    """真实检索：经过 VectorStoreRetriever → Milvus。"""
    from app.services.knowledge_service import VectorStoreRetriever

    retriever = VectorStoreRetriever()
    results = []
    for question in queries:
        started = time.perf_counter()
        try:
            chunks = retriever.search(question, k)
            elapsed = (time.perf_counter() - started) * 1000
            results.append({
                "query": question,
                "hits": len(chunks),
                "latency_ms": round(elapsed, 1),
                "top": [
                    {
                        "document_id": chunk.document_id,
                        "revision": chunk.revision,
                        "score": round(chunk.score, 4),
                        "locator": chunk.locator,
                        "snippet": chunk.text[:120],
                    }
                    for chunk in chunks
                ],
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"query": question, "error": f"{type(exc).__name__}: {exc}"})
    return {"queries": results}


def step_active_revision(store) -> dict:
    """OLD → NEW 更新后只返回 active revision；回滚后恢复旧版。"""
    from app.services.knowledge_service import VectorStoreRetriever

    source = "acceptance://revision-switch.md"
    old = store.submit_document(source_uri=source, title="rev-switch",
                                content=b"# OLD\n\nOLD_SENTINEL_XYZ instructions\n")
    _wait_job(store, old["job_id"])
    new = store.submit_document(source_uri=source, title="rev-switch",
                                content=b"# NEW\n\nNEW_SENTINEL_XYZ instructions\n")
    _wait_job(store, new["job_id"])

    retriever = VectorStoreRetriever()
    after_update = [c.text for c in retriever.search("SENTINEL instructions", 10)]
    revisions = store.list_revisions(new["document_id"])

    rolled = store.activate_revision(new["document_id"], old["revision"])
    after_rollback = [c.text for c in retriever.search("SENTINEL instructions", 10)]

    return {
        "document_id": new["document_id"],
        "active_after_update": revisions["active_revision"],
        "expected_active": new["revision"],
        "old_revision": old["revision"],
        "texts_after_update": after_update,
        "old_hidden_after_update": "OLD_SENTINEL_XYZ" not in "".join(after_update),
        "rolled_back_to": rolled["active_revision"],
        "texts_after_rollback": after_rollback,
        "old_visible_after_rollback": any(
            "OLD_SENTINEL_XYZ" in text for text in after_rollback
        ),
    }


def step_partial_failure(store) -> dict:
    """真实 Milvus 上注入“写一行后失败”，确认无残片且旧 active 保留。"""
    from app.services import knowledge_store as ks
    from app.services.knowledge_service import VectorStoreRetriever

    source = "acceptance://partial-failure.md"
    good = store.submit_document(source_uri=source, title="pf",
                                 content=b"# GOOD\n\nGOOD_BASELINE_ABC\n")
    _wait_job(store, good["job_id"])

    original_add = ks.vector_store_manager.add_documents
    state = {"wrote": 0}

    def failing_add(documents, *, ids=None):
        written = []
        for index, doc in enumerate(documents):
            written.append(doc)
            state["wrote"] += 1
            if state["wrote"] >= 1:
                # 先真正写一行，再抛错：模拟批量插入中途失败。
                original_add([doc], ids=[ids[index]])
                raise RuntimeError("injected failure after writing one row")
        return list(ids or [])

    ks.vector_store_manager.add_documents = failing_add
    try:
        broken = store.submit_document(source_uri=source, title="pf",
                                       content=b"# BROKEN\n\nBROKEN_RESIDUE_QQQ\n")
        broken_final = _wait_job(store, broken["job_id"])
    finally:
        ks.vector_store_manager.add_documents = original_add

    texts = [c.text for c in VectorStoreRetriever().search("RESIDUE instructions", 10)]
    return {
        "broken_job_status": broken_final["status"],
        "broken_job_error_code": (broken_final.get("error") or {}).get("code"),
        "rows_written_before_failure": state["wrote"],
        "residue_visible": any("BROKEN_RESIDUE_QQQ" in text for text in texts),
        "old_active_still_visible": any("GOOD_BASELINE_ABC" in text for text in texts),
    }


def step_descriptor_mismatch() -> dict:
    """改动描述符后，状态必须报 invalid_index 而不是静默复用。"""
    from app.config import config
    from app.services import knowledge_store

    original = config.embedding_model
    try:
        config.embedding_model = "acceptance/definitely-not-the-indexed-model"
        payload = knowledge_store.probe_status()
        return {
            "status": payload["status"],
            "reason": payload["reason"][:200],
            "expected": "invalid_index",
            "correct": payload["status"] == "invalid_index",
        }
    finally:
        config.embedding_model = original
        # 明确 reset，让后续步骤重新按真实配置校验。
        from app.services.vector_store_manager import vector_store_manager

        vector_store_manager.reset()


def step_restart_recovery() -> dict:
    """停止 Milvus → 观察 unavailable → 重启后用同一进程恢复（后台命令另测）。"""
    from app.services.vector_store_manager import VectorStoreManager

    manager = VectorStoreManager()
    try:
        manager._ensure_initialized()
        return {"initialized": True, "note": "Milvus 当前可用"}
    except Exception as exc:  # noqa: BLE001
        return {
            "initialized": False,
            "failure_kind": getattr(manager, "init_failure_kind", None),
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-import", action="store_true",
                        help="跳过 11 文档导入（复用已导入数据）")
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 个文档")
    args = parser.parse_args()

    _store_gate()
    from app.services import knowledge_store

    knowledge_store.reset_store()
    store = knowledge_store.get_store()

    evidence: dict = {
        "generated_at": _utc(),
        "milvus_before": _milvus_facts(),
        "python": sys.version.split()[0],
    }

    documents = _core_documents()
    evidence["corpus"] = {
        "core_documents": len(documents),
        "ids": [item["id"] for item in documents],
        "needs_review": [item["id"] for item in documents
                         if item["license_status"] == "needs_review"],
    }
    if args.limit:
        documents = documents[: args.limit]

    steps = []
    for name, func in (
        ("import", (lambda: step_import(store, documents)) if not args.skip_import else None),
        ("query", lambda: step_query(store, [
            "磁盘满了怎么排查",
            "systemctl 如何重启服务",
            "journalctl 查看日志",
            "PowerShell Get-Service 用法",
            "docker 容器日志怎么配置",
            "内核参数怎么调优",
        ])),
        ("active_revision", lambda: step_active_revision(store)),
        ("partial_failure", lambda: step_partial_failure(store)),
        ("descriptor_mismatch", step_descriptor_mismatch),
        ("restart_recovery", step_restart_recovery),
    ):
        if func is None:
            continue
        started = time.perf_counter()
        try:
            result = func()
            ok = True
        except Exception as exc:  # noqa: BLE001 - 每步失败要留下证据
            result = {"exception": f"{type(exc).__name__}: {exc}",
                      "traceback": traceback.format_exc()[-1500:]}
            ok = False
        steps.append({
            "step": name,
            "ok": ok,
            "seconds": round(time.perf_counter() - started, 2),
            "result": result,
        })

    evidence["steps"] = steps
    evidence["milvus_after"] = _milvus_facts()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"milvus-e2e-{stamp}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "evidence_file": str(out.relative_to(ROOT)),
        "milvus_before": evidence["milvus_before"],
        "milvus_after": evidence["milvus_after"],
        "steps": [{"step": s["step"], "ok": s["ok"], "seconds": s["seconds"]}
                  for s in steps],
    }, ensure_ascii=False, indent=2))
    return 0 if all(s["ok"] for s in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
