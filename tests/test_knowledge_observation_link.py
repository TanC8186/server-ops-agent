"""知识片段与观测的显式关联（G2.3 第 2 条 / BE-06）。

以前“知识依据”只有一个笼统的免责声明：读者无法判断某段手册到底在支撑哪条
观测，也无法判断它其实什么都没支撑。这里锁定三件事：

1. 客观线索重合（共享指标族 / 指标名命令词元）时，知识行写
   ``关联观测: E1``，观测行写 ``知识依据: K1``；
2. 毫无线索重合时，知识行必须写
   ``仅为背景依据（未关联到本轮观测）``，**绝不**编造关联；
3. 小节顺序与 ``（手册，不代表当前主机状态）`` 免责声明保持不变。
"""

from __future__ import annotations

import pytest

from app.agent.aiops.quality import (
    MANUAL_DISCLAIMER,
    REPORT_SECTIONS,
    UNLINKED_KNOWLEDGE_NOTE,
    assess,
    link_knowledge,
    render_report,
)

CPU_EVIDENCE = {
    "id": "E1", "tool": "query_cpu_metrics", "ok": True, "status": "ok",
    "live": True, "demo": False, "source": "tencent_cloud", "target": "svc-a",
    "error": "", "data": {"metric": "cpu", "value": 96},
}

DISK_EVIDENCE = {
    "id": "E2", "tool": "query_disk_metrics", "ok": True, "status": "ok",
    "live": True, "demo": False, "source": "tencent_cloud", "target": "svc-a",
    "error": "", "data": {"metric": "disk", "value": 99},
}


def _knowledge(chunk_id: str, *, title: str, text: str) -> dict:
    return {
        "id": chunk_id, "tool": "retrieve_knowledge", "ok": True, "status": "ok",
        "kind": "knowledge", "live": False, "demo": False, "source": "manual",
        "target": "", "error": "",
        "data": {"title": title, "text": text},
    }


def _review(*observations: dict) -> dict:
    return {"observations": [dict(item) for item in observations],
            "hypotheses": [], "recommendations": [], "evidence_gaps": []}


def _sections(report: str) -> dict[str, str]:
    parts = report.split("\n## ")
    result = {}
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        result[title.strip()] = body
    return result


def _assessment(evidence, review):
    return assess(evidence, [], review, target_bound=True)


# ── 1. 命中：共享指标族 ─────────────────────────────────────


def test_shared_metric_family_links_both_sides():
    evidence = [
        CPU_EVIDENCE,
        _knowledge("K1", title="CPU 高负载排查手册",
                   text="CPU 使用率持续偏高时，先看 load average 与 run queue。"),
        _knowledge("K2", title="Redis 超时排查手册",
                   text="连接池耗尽时先检查 maxclients。"),
    ]
    result = _assessment(
        evidence,
        _review({"claim": "CPU 使用率 96%", "status": "supported",
                "evidence_ids": ["E1"]}),
    )
    report = render_report(result)
    sections = _sections(report)

    assert result["knowledge_links"]["K1"] == ["E1"]
    assert result["knowledge_links"]["K2"] == []
    # 知识行：关联到被证实的观测 id（报告里可见的那个 id）。
    assert "关联观测: E1" in sections["知识依据"]
    # 观测行：给出对应的知识编号。
    assert "知识依据: K1" in sections["当前观测"]
    # K2 与 CPU 无关，必须保持“仅为背景依据”。
    assert UNLINKED_KNOWLEDGE_NOTE in sections["知识依据"]
    # 手册免责声明对有链接的知识片段同样成立。
    assert MANUAL_DISCLAIMER in sections["知识依据"]


def test_multiple_observations_link_to_one_chunk():
    evidence = [
        CPU_EVIDENCE,
        DISK_EVIDENCE,
        _knowledge("K1", title="主机容量与负载手册",
                   text="同时检查 CPU 使用率与磁盘使用率。"),
    ]
    result = _assessment(
        evidence,
        _review(
            {"claim": "CPU 使用率 96%", "status": "supported", "evidence_ids": ["E1"]},
            {"claim": "磁盘使用率 99%", "status": "supported", "evidence_ids": ["E2"]},
        ),
    )
    report = render_report(result)
    sections = _sections(report)

    assert result["knowledge_links"]["K1"] == ["E1", "E2"]
    assert "关联观测: E1, E2" in sections["知识依据"]
    assert "知识依据: K1" in sections["当前观测"]


# ── 2. 未命中：不编造关联 ───────────────────────────────────


def test_unrelated_knowledge_is_marked_as_background_only():
    evidence = [
        CPU_EVIDENCE,
        _knowledge("K1", title="Redis 超时排查手册",
                   text="连接池耗尽时先检查 maxclients 与 slowlog。"),
    ]
    result = _assessment(
        evidence,
        _review({"claim": "CPU 使用率 96%", "status": "supported",
                "evidence_ids": ["E1"]}),
    )
    report = render_report(result)
    sections = _sections(report)

    assert result["knowledge_links"]["K1"] == []
    assert UNLINKED_KNOWLEDGE_NOTE in sections["知识依据"]
    assert "关联观测" not in sections["知识依据"]
    assert "知识依据: K" not in sections["当前观测"]


def test_no_observations_means_no_fabricated_links():
    """只有知识、没有观测时，任何“关联”都是编造的。"""
    evidence = [
        _knowledge("K1", title="CPU 高负载排查手册", text="cpu usage high"),
    ]
    result = _assessment(evidence, _review())

    assert result["observations"] == []
    assert result["knowledge_links"]["K1"] == []
    assert UNLINKED_KNOWLEDGE_NOTE in render_report(result)


def test_hypotheses_are_not_linked_to_knowledge():
    """未被证实的声明只是假设，手册不能“支撑”它。"""
    evidence = [
        CPU_EVIDENCE,
        _knowledge("K1", title="CPU 高负载排查手册", text="cpu usage high"),
    ]
    # 数值与证据不符 → 降级为假设，因而不参与关联。
    result = _assessment(
        evidence,
        _review({"claim": "CPU 使用率 87%", "status": "supported",
                "evidence_ids": ["E1"]}),
    )

    assert result["observations"] == []
    assert result["knowledge_links"]["K1"] == []
    report = render_report(result)
    assert UNLINKED_KNOWLEDGE_NOTE in _sections(report)["知识依据"]


def test_generic_words_alone_do_not_create_links():
    """只是共用通用英文词（value/status/error）不算相关。"""
    chunk = _knowledge("K1", title="Change record",
                       text="value status error note here")
    links = link_knowledge(
        [{"id": "O1", "claim": "CPU 使用率 96%", "status": "supported",
          "evidence_ids": ["E1"], "rendered": "E1 cpu=96%"}],
        [chunk],
        {"E1": CPU_EVIDENCE},
    )
    assert links["K1"] == []


# ── 3. 结构不回归 ───────────────────────────────────────────


def test_section_order_and_disclaimer_preserved():
    evidence = [
        CPU_EVIDENCE,
        _knowledge("K1", title="CPU 高负载排查手册", text="cpu usage high"),
    ]
    report = render_report(_assessment(
        evidence,
        _review({"claim": "CPU 使用率 96%", "status": "supported",
                "evidence_ids": ["E1"]}),
    ))

    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert headings == [f"## {section}" for section in REPORT_SECTIONS]
    knowledge_body = _sections(report)["知识依据"]
    assert MANUAL_DISCLAIMER in knowledge_body
    knowledge_line = next(
        line for line in knowledge_body.splitlines() if "CPU 高负载排查手册" in line
    )
    # 行结构保持：短横线开头、手册正文、免责声明、关联说明、来源。
    assert knowledge_line.startswith("- ")
    assert knowledge_line.index(MANUAL_DISCLAIMER) < knowledge_line.index("关联观测: E1")
    assert knowledge_line.rstrip().endswith("）")


@pytest.mark.parametrize("linked", [True, False])
def test_knowledge_never_leaks_into_other_sections(linked):
    evidence = [
        CPU_EVIDENCE,
        _knowledge("K1", title="Redis 或 CPU 手册",
                   text="cpu usage high" if linked else "maxclients"),
    ]
    report = render_report(_assessment(
        evidence,
        _review({"claim": "CPU 使用率 96%", "status": "supported",
                "evidence_ids": ["E1"]}),
    ))
    sections = _sections(report)

    assert "CPU 或 Redis 手册" not in sections["当前观测"]
    assert "K1" not in sections["原始证据索引"]
    assert "手册" in sections["知识依据"]
