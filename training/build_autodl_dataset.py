"""Deterministic AutoDL/SSH Planner dataset builder.

The dataset deliberately uses the same target-context prompt and filtered SSH
tool catalog as production local planning. It does not call a teacher model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.aiops.local_prompt import (
    build_system_prompt,
    compact_catalog,
    target_context_for,
)
from training.schema import TriageOutput, validate_against_catalog

FULL_CATALOG = ROOT / "training" / "data" / "tool_catalog.json"
DEFAULT_OUT = ROOT / "training" / "data_autodl_ssh"

TARGET_TOOLS = (
    "list_all_services",
    "get_current_time",
    "get_current_timestamp",
    "retrieve_knowledge",
    "get_system_info",
    "get_process_list",
    "search_system_logs",
    "check_disk_usage",
    "read_log_file",
)

SERVICES = ("autodl-ssh",)
VARIANTS = (
    "AutoDL Linux 计算服务器",
    "AutoDL 上的训练容器",
    "远程 Linux 算力节点",
    "SSH 连接的 AutoDL 主机",
)

CASES = [
    (
        "cpu",
        [
            "CPU 使用率持续超过 {pct}%，训练进程响应变慢，检查 AutoDL 主机。",
            "AutoDL 主机 load average 升高，CPU {pct}%，请确认哪个进程占用最高。",
            "远程 Linux 算力节点 CPU 打满，但目前没有云监控 API，只能通过 SSH 检查。",
        ],
        "P2",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("get_process_list", {"sort_by": "cpu"}),
            ("retrieve_knowledge", {"query": "AutoDL Linux CPU 高负载进程排查"}),
        ],
    ),
    (
        "memory",
        [
            "AutoDL 主机内存使用率达到 {pct}%，系统出现 OOM 迹象。",
            "训练容器 RSS 持续增长，Linux 主机内存 {pct}%，请检查进程和系统日志。",
            "SSH 服务器疑似内存不足，不能调用云端 query_memory_metrics。",
        ],
        "P1",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("get_process_list", {"sort_by": "memory"}),
            ("search_system_logs", {"log_type": "syslog", "count": 30, "keyword": "OOM"}),
            ("retrieve_knowledge", {"query": "Linux AutoDL 内存 OOM 处置"}),
        ],
    ),
    (
        "disk",
        [
            "AutoDL 根分区使用率达到 {pct}%，训练缓存可能占满磁盘。",
            "远程 Linux 节点磁盘空间不足，检查根分区和日志目录。",
            "容器镜像和模型文件增长很快，请通过 SSH 检查磁盘。",
        ],
        "P1",
        [
            ("list_all_services", {}),
            ("check_disk_usage", {"path": "/"}),
            ("get_system_info", {}),
            ("read_log_file", {"file_path": "/var/log/syslog", "lines": 50}),
            ("retrieve_knowledge", {"query": "AutoDL Linux 磁盘满 Docker 镜像清理"}),
        ],
    ),
    (
        "service",
        [
            "AutoDL 上的 vLLM 服务无法访问，检查 Linux 进程和系统日志。",
            "训练服务启动失败，SSH 主机上的容器可能已经退出。",
            "算力节点上的模型服务报错，请不要调用腾讯云监控工具。",
        ],
        "P1",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("get_process_list", {"sort_by": "cpu"}),
            ("search_system_logs", {"log_type": "syslog", "count": 30, "keyword": "error"}),
            ("retrieve_knowledge", {"query": "AutoDL vLLM Linux 服务启动失败排查"}),
        ],
    ),
    (
        "service",
        [
            "AutoDL GPU 训练环境出现 CUDA 或 MUSA 初始化失败。",
            "算力服务器的 GPU 任务启动异常，请检查系统信息、进程和知识库手册。",
            "模型服务遇到显存不足，当前只允许 SSH 只读工具。",
        ],
        "P2",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("get_process_list", {"sort_by": "memory"}),
            ("retrieve_knowledge", {"query": "AutoDL GPU CUDA MUSA 显存不足排查"}),
        ],
    ),
    (
        "healthy",
        [
            "请巡检 AutoDL Linux 主机，确认 CPU、内存、磁盘和进程是否正常。",
            "AutoDL 算力服务器没有明确告警，只做一次只读健康检查。",
            "SSH 连接正常，请检查远程训练节点的基本资源状态。",
        ],
        "P4",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("get_process_list", {"sort_by": "memory"}),
            ("check_disk_usage", {"path": "/"}),
        ],
    ),
    (
        "network",
        [
            "AutoDL 主机网络连接不稳定，检查系统日志和当前进程。",
            "远程 SSH 主机出现连接超时，需要先获取 Linux 系统信息。",
            "训练节点无法访问模型服务，使用只读 SSH 工具确认现场。",
        ],
        "P2",
        [
            ("list_all_services", {}),
            ("get_system_info", {}),
            ("search_system_logs", {"log_type": "syslog", "count": 30, "keyword": "network"}),
            ("get_process_list", {"sort_by": "cpu"}),
        ],
    ),
]

def filtered_catalog() -> dict:
    full = json.loads(FULL_CATALOG.read_text(encoding="utf-8"))
    return {
        "tools": {
            name: full["tools"][name]
            for name in TARGET_TOOLS
            if name in full.get("tools", {})
        }
    }

def render_user(symptom: str, index: int) -> str:
    variant = VARIANTS[index % len(VARIANTS)]
    return (
        f"平台：Linux\n采集方式：SSH 只读\n目标：autodl-ssh\n"
        f"目标角色：{variant}\n告警文本：{symptom}\n请只输出 JSON。"
    )

def make_row(catalog: dict, family: str, text: str, severity: str,
             plan_specs: list[tuple[str, dict]], index: int) -> dict:
    plan = []
    for step_index, (tool, args) in enumerate(plan_specs, 1):
        plan.append({
            "id": f"s{step_index}",
            "kind": "tool",
            "tool": tool,
            "args": args,
            "description": f"使用 SSH 检查 {tool}",
            "success_criteria": "返回可核验的只读结果",
        })
    payload = {
        "severity": severity,
        "category": "healthy" if family == "healthy" else family,
        "plan": plan,
    }
    output = TriageOutput.model_validate(payload)
    errors = validate_against_catalog(output, catalog)
    if errors:
        raise ValueError(f"{family}-{index}: {errors}")
    prompt = build_system_prompt(
        compact_catalog(catalog, with_params=True),
        tool_names=None,
        target_context=target_context_for("ssh"),
    )
    user = render_user(text, index)
    return {
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "metadata": {
            "dataset": "autodl_ssh_v1",
            "family": family,
            "category": payload["category"],
            "severity": severity,
            "target_kind": "ssh",
            "platform": "linux",
            "service": "autodl-ssh",
            "template_index": index,
            "source": "deterministic_template",
        },
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--per-template", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    catalog = filtered_catalog()
    rows = []
    counter = 0
    for family, templates, severity, plan in CASES:
        for template_index, template in enumerate(templates):
            for repeat in range(args.per_template):
                values = {
                    "pct": rng.choice([82, 85, 90, 92, 95, 97, 99]),
                }
                text = template.format(**values)
                rows.append(make_row(catalog, family, text, severity, plan, counter))
                counter += 1
    rng.shuffle(rows)

    # deterministic 80/10/10 split by row identity, keeping all families in each split
    buckets = {"train": [], "val": [], "test": []}
    for index, row in enumerate(rows):
        remainder = index % 10
        bucket = "val" if remainder == 8 else "test" if remainder == 9 else "train"
        buckets[bucket].append(row)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tool_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, records in buckets.items():
        (out / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
            encoding="utf-8",
        )
    manifest = {
        "dataset": "autodl_ssh_v1",
        "seed": args.seed,
        "rows": len(rows),
        "splits": {name: len(records) for name, records in buckets.items()},
        "tool_names": list(catalog["tools"]),
        "sha256": {
            name: hashlib.sha256((out / f"{name}.jsonl").read_bytes()).hexdigest()
            for name in buckets
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

