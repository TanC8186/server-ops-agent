"""可信目标目录与绑定规则。

一个诊断目标由服务/实例/地域/日志主题/SSH 主机共同描述。绑定之后，工具参数
不能覆盖这些字段——否则模型或用户输入可以把查询指向另一台机器，却把证据标成
当前目标（BE-03 / BE-04）。

设计约束：

- 服务发现失败、候选为空 → ``not_found``。
- 唯一候选（显式指定或被症状唯一提及）→ ``bound``。
- 多个候选 → ``target_required``，由 UI 选择；绝不默认取列表第一项。
- 服务名只做**完整词**匹配，避免 ``svc`` 命中 ``svc-a``。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 绑定后可被工具使用、但必须与目标一致的地域参数名。
REGION_KEYS = ("region_code", "region")

#: 日志主题定位参数：必须在目标目录声明的 topics 内，否则拒绝。
TOPIC_KEYS = ("topic_id", "topic_name", "topic")

#: 主题参数的宽松匹配：允许 topic_id 出现在 topic_name 里（反之亦然），
#: 但必须命中同一个已声明主题，不做前缀模糊匹配。

#: 服务名允许的字符集合（与云厂商命名一致，避免正则注入）。
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


@dataclass(frozen=True)
class Target:
    """一次诊断绑定的可信目标。"""

    service: str
    instance_id: str = ""
    region: str = ""
    topics: tuple[str, ...] = ()
    ssh_host: str = ""

    @property
    def ssh_in_scope(self) -> bool:
        """SSH 主机是否由目标目录显式声明。

        未声明时 SSH 证据只代表“独立配置的主机”，不能支撑目标服务的结论。
        """
        return bool(self.ssh_host)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "instance_id": self.instance_id,
            "region": self.region,
            "topics": list(self.topics),
            "ssh_host": self.ssh_host,
            "ssh_in_scope": self.ssh_in_scope,
        }


def candidate_id(target: Target) -> str:
    """为“完整可信目标”签发稳定候选 ID（A04）。

    同一服务名下的多个实例/地域是不同目标；只用服务名做 ``target_choice``
    无法区分它们（点 ``svc/i-a`` 与 ``svc/i-b`` 会发出同一个字符串）。这里用
    完整目标键（service+instance+region+topics+ssh_host）派生摘要，服务端在
    重新校验资源目录后据此绑定到唯一实例。

    ID 只由服务端目录内容决定，不含时间或随机量：同一候选在多次请求里保持不变。
    """
    payload = "\x1f".join((
        target.service,
        target.instance_id,
        target.region,
        "|".join(target.topics),
        target.ssh_host,
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"tgt-{digest}"


def target_candidate(target: Target) -> dict[str, Any]:
    """候选卡片载荷：完整目标 + 稳定 ID（前端按 ID 选择，不再按服务名去重）。"""
    payload = target.to_dict()
    payload["target_id"] = candidate_id(target)
    return payload


@dataclass(frozen=True)
class TargetResolution:
    """绑定结果。``candidates`` 仅在 target_required 时有意义。"""

    status: str  # bound | target_required | not_found
    target: Target | None = None
    candidates: tuple[Target, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": target_candidate(self.target) if self.target else None,
            "candidates": [target_candidate(candidate) for candidate in self.candidates],
            "reason": self.reason,
        }


def _coerce_entry(row: Any) -> Target | None:
    if not isinstance(row, Mapping):
        return None
    service = str(row.get("service_name") or "").strip()
    if not service or not _SERVICE_NAME_RE.fullmatch(service):
        return None
    topics: Iterable[Any] = row.get("topics") or ()
    if isinstance(topics, str):
        topics = (topics,)
    return Target(
        service=service,
        instance_id=str(row.get("instance_id") or "").strip(),
        region=str(row.get("region") or row.get("region_code") or "").strip(),
        topics=tuple(str(topic) for topic in topics if str(topic).strip()),
        ssh_host=str(row.get("ssh_host") or "").strip(),
    )


def _target_key(target: Target) -> tuple[str, str, str, tuple[str, ...], str]:
    return (target.service, target.instance_id, target.region, target.topics,
            target.ssh_host)


def catalog_targets(catalog: Iterable[Any]) -> list[Target]:
    """把服务发现结果规范为去重、稳定的目标列表。

    去重键是**完整目标**（服务+实例+地域+主题+SSH），不是服务名：同一服务的多
    个实例/地域是不同目标，按服务名去重会静默丢掉后出现的实例，再让用户“选择”
    到一个已经不存在的目标。
    """
    seen: dict[tuple, Target] = {}
    for row in catalog:
        target = _coerce_entry(row)
        if target is not None:
            seen.setdefault(_target_key(target), target)
    return list(seen.values())


def mentioned_services(targets: list[Target], text: str) -> list[Target]:
    """按完整词匹配文本中提到的服务名。"""
    haystack = text or ""
    return [
        target
        for target in targets
        if re.search(rf"(?<![\w.-]){re.escape(target.service)}(?![\w.-])", haystack)
    ]


def resolve_target(catalog: Iterable[Any], requested: str | None,
                   symptom: str, *, candidate: str | None = None) -> TargetResolution:
    """决定本次诊断的目标，或要求用户选择。

    ``candidate`` 是 UI 候选卡片上的稳定候选 ID（见 :func:`candidate_id`）。
    它优先于服务名：服务名无法区分同名多实例，候选 ID 可以。服务端仍然在
    **当前**资源目录里重新校验——候选 ID 只用于定位，不构成信任来源。
    """
    targets = catalog_targets(catalog)
    if not targets:
        return TargetResolution(status="not_found", reason="没有可用的服务映射，请先配置服务列表")

    candidate_key = (candidate or "").strip()
    if candidate_key:
        chosen = [target for target in targets if candidate_id(target) == candidate_key]
        if len(chosen) == 1:
            return TargetResolution(status="bound", target=chosen[0])
        return TargetResolution(
            status="not_found",
            reason=(
                "所选候选已不在当前资源目录中（可能实例已下线或服务映射已变更），"
                "已停止诊断；请重新描述症状以获取最新候选"
            ),
        )

    requested_name = (requested or "").strip()
    if requested_name:
        matches = [target for target in targets if target.service == requested_name]
        if len(matches) == 1:
            return TargetResolution(status="bound", target=matches[0])
        if len(matches) > 1:
            # 同名多实例/多地域：只给服务名不足以确定目标，必须让用户选实例。
            return TargetResolution(
                status="target_required",
                candidates=tuple(matches),
                reason=(
                    f"服务 {requested_name} 有 {len(matches)} 个候选实例/地域，"
                    "请选择本次诊断的目标"
                ),
            )
        return TargetResolution(
            status="not_found",
            reason="指定服务不在发现列表中，已停止诊断",
        )

    mentioned = mentioned_services(targets, symptom)
    if len(mentioned) == 1:
        return TargetResolution(status="bound", target=mentioned[0])
    if len(mentioned) > 1:
        return TargetResolution(
            status="target_required",
            candidates=tuple(mentioned),
            reason="症状涉及多个服务，请选择本次诊断的目标",
        )

    if len(targets) == 1:
        return TargetResolution(status="bound", target=targets[0])

    return TargetResolution(
        status="target_required",
        candidates=tuple(targets),
        reason="未指定目标服务，且有多个候选，请选择本次诊断的目标",
    )


def bind_tool_args(args: Mapping[str, Any], target: Target | None, *,
                   accepts: Iterable[str] | None = None) -> dict[str, Any]:
    """校验工具参数没有越过已绑定的目标，并补齐目标字段。

    Args:
        args: 模型给出的工具参数
        target: 已绑定的目标
        accepts: 该工具 schema 接受的参数名。给出时会把目标拥有的字段补进参数
            （只补 schema 真正接受的键，避免注入不存在的参数）。

    Raises:
        ValueError: 参数试图访问目标之外的服务/实例/地域/日志主题
    """
    if target is None:
        raise ValueError("尚未绑定诊断目标，不能执行目标数据工具")
    bound = dict(args or {})
    allowed = set(accepts) if accepts is not None else set(bound)

    if "service_name" in bound and bound["service_name"] != target.service:
        raise ValueError("工具目标与本次选择的服务不一致")
    for key in REGION_KEYS:
        if key in bound and target.region and bound[key] != target.region:
            raise ValueError("工具目标与本次选择的地域不一致")
    if "instance_id" in bound:
        if not target.instance_id or bound["instance_id"] != target.instance_id:
            raise ValueError("不允许覆盖已绑定的实例")

    # 日志主题：目标目录没有声明任何 topic 时，任何显式 topic 参数都无法核验，
    # 必须拒绝。旧实现在 ``target.topics`` 为空时直接放行，于是模型可以拿一个
    # 任意 topic_id 读到别的服务/地域的日志，再把证据标成本次目标（BE-04）。
    for key in TOPIC_KEYS:
        if key not in bound:
            continue
        requested = str(bound[key] or "").strip()
        if not requested:
            continue
        if not target.topics:
            raise ValueError(
                f"目标 {target.service} 未声明日志主题，拒绝按 {key}={requested} 读取日志"
            )
        if requested not in target.topics:
            raise ValueError(
                f"日志主题 {requested} 不属于本次绑定的服务（已声明: "
                + ", ".join(target.topics) + "）"
            )

    # 只补目标确实拥有、且 schema 允许的字段。
    if "service_name" in allowed:
        bound["service_name"] = target.service
    if target.instance_id and "instance_id" in allowed:
        bound["instance_id"] = target.instance_id
    if target.region:
        for key in REGION_KEYS:
            if key in allowed:
                bound[key] = target.region
    # 只声明了一个主题时补上，避免模型猜 topic_id；多个主题时留给模型显式选择。
    if len(target.topics) == 1:
        for key in TOPIC_KEYS:
            if key in allowed and not bound.get(key):
                bound[key] = target.topics[0]
    return bound
