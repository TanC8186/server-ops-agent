"""Read-only software audit probes; temporary metadata and fake vector/provider I/O.

Run from any directory with the project's Python environment. This does not
connect to Milvus, invoke an embedding API, call an LLM, download a model, or
change application code/data. The JSON output records whether each audited
failure is reproduced; it is not a production integration test.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.documents import Document

from app.config import config
from app.services import knowledge_store as ks
from app.services import vector_embedding_service as emb
from app.services import vector_store_manager as vsm
from app.services.knowledge_service import VectorStoreRetriever


class FakeVectors:
    score_kind = "similarity"

    def __init__(self):
        self.rows = {}
        self.fail_after_first_row = False

    def add_documents(self, documents, *, ids):
        for document, row_id in zip(documents, ids):
            self.rows[row_id] = document
            if self.fail_after_first_row:
                raise RuntimeError("audit: injected failure after writing a row")
        return list(ids)

    def delete_by_ids(self, ids):
        removed = 0
        for row_id in ids:
            if row_id in self.rows:
                self.rows.pop(row_id)
                removed += 1
        return removed

    def similarity_search_with_score(self, query, k=3, **kwargs):
        return [(document, 0.9) for document in self.rows.values()][:k]


class FakeSplitter:
    def split_document(self, content, source, *, document_id, revision):
        return [Document(page_content=content, metadata={
            "_source": source,
            "_document_id": document_id,
            "_revision": revision,
            "_chunk_id": "assigned-by-real-job",
            "_line_start": 1,
            "_line_end": 1,
        })]


def version_and_partial_insert_probes():
    vectors = FakeVectors()
    with (
        tempfile.TemporaryDirectory(prefix="kb-audit-") as temporary,
        patch.object(ks, "vector_store_manager", vectors),
        patch.object(ks, "document_splitter_service", FakeSplitter()),
    ):
        store = ks.KnowledgeStore(Path(temporary) / "store.json")
        # Run the real worker synchronously, with no background or external I/O.
        store._submit = lambda _job_id: None

        def import_text(text):
            accepted = store.submit_document(
                source_uri="upload://audit-runbook.md",
                title="audit only",
                content=text.encode("utf-8"),
            )
            store._run_job(accepted["job_id"])
            return accepted

        old = import_text("OLD instructions")
        new = import_text("NEW instructions")
        texts = [chunk.text for chunk in VectorStoreRetriever(vectors).search("instructions", 10)]
        version = {
            "old_job": store.get_job(old["job_id"])["status"],
            "new_job": store.get_job(new["job_id"])["status"],
            "active_revision": store.list_revisions(new["document_id"])["active_revision"],
            "retrieved_texts": texts,
            "bug_reproduced": "OLD instructions" in texts and "NEW instructions" in texts,
        }

        vectors.fail_after_first_row = True
        failed = import_text("FAILED PARTIAL instructions")
        failed_texts = [chunk.text for chunk in VectorStoreRetriever(vectors).search("instructions", 10)]
        partial = {
            "job_status": store.get_job(failed["job_id"])["status"],
            "retrieved_texts": failed_texts,
            "bug_reproduced": "FAILED PARTIAL instructions" in failed_texts,
        }
    return {"superseded_revision_visible": version, "failed_batch_visible": partial}


def strict_local_provider_probe():
    calls = []
    sentinel = object()

    def fake_remote_factory(**kwargs):
        # Do not construct an SDK client or print credentials.
        calls.append({"factory": "remote", "model": kwargs.get("model")})
        return sentinel

    with (
        patch.object(config, "embedding_provider", "openai"),
        patch.object(config, "embedding_model", "audit-fake-model"),
        patch.object(config, "embedding_api_key", "audit-fake-key"),
        patch.object(config, "embedding_base_url", "https://audit.invalid/v1"),
        patch.object(emb, "_create_openai_embeddings", fake_remote_factory),
    ):
        result = emb.create_embedding_service(strict_local=True)
    return {
        "strict_local": True,
        "configured_provider": "openai",
        "selected_factories": calls,
        "network_requests": 0,
        "bug_reproduced": result is sentinel and bool(calls),
    }


def initialization_recovery_probe():
    manager = vsm.VectorStoreManager()
    errors = []
    # First connection fails; the dependency would be available on the next try.
    with patch.object(vsm.milvus_manager, "connect", side_effect=[RuntimeError("audit down"), object()]) as connect:
        for _attempt in range(2):
            try:
                manager._ensure_initialized()
            except Exception as exc:
                errors.append(type(exc).__name__)
        attempts = connect.call_count
    return {
        "requests": 2,
        "actual_connect_attempts": attempts,
        "errors": errors,
        "bug_reproduced": attempts == 1 and len(errors) == 2,
    }


def main():
    results = version_and_partial_insert_probes()
    results["strict_local_selects_remote_factory"] = strict_local_provider_probe()
    results["first_initialization_failure_is_sticky"] = initialization_recovery_probe()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
