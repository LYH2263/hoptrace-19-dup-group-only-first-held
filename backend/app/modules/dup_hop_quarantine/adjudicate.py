"""重复裁定：按 (waybill_id, event_ts, location_code, event_code) 重复键找组。

裁定口径：
- 同一 hop id 再次出现（无论落在正式线还是已在隔离）= 幂等重放，跳过，不算重复；
- 同一运单下、同一重复键被 ≥2 个不同 hop id 占用 = 重复组，默认整组隔离——
  组内任一点都不得进正式时间线，即使单点字段看起来合法；
- 组成员可能来自本批，也可能是历史批次已写入正式线的同键点（后写撞先写）——
  一经发现，已在正式线的成员一并拉出，整组进隔离；
- 本批新点撞上「历史已隔离组」的同键，直接并入该组，不另立新组。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

REASON = "同运单重复键 (event_ts, location_code, event_code) 整组隔离"
DISPOSITION = "整组隔离：组内任一点不得进正式时间线"


def group_id_for(waybill_id: str, event_ts: str, location_code: str, event_code: str) -> str:
    """组 id 由重复键确定性派生：同键同组，重放/跨批次幂等。"""
    raw = f"{waybill_id}|{event_ts}|{location_code}|{event_code}"
    return "DG-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class HopRef:
    hop_id: str
    waybill_id: str
    event_ts: str
    location_code: str
    event_code: str
    seq: int | None
    origin: str  # 'batch' | 'official_pulled' | None（待定）
    payload: dict


@dataclass
class GroupPlan:
    id: str
    waybill_id: str
    event_ts: str
    location_code: str
    event_code: str
    members: list = field(default_factory=list)  # list[HopRef]，含本批新点与被拉出的正式点


@dataclass
class Plan:
    clean: list = field(default_factory=list)      # 可安全写入正式线的点（裁定结论，非随机）
    replayed: list = field(default_factory=list)   # 幂等重放跳过的 hop id
    groups: list = field(default_factory=list)     # list[GroupPlan]，本波次发现的重复组
    affected_waybills: set = field(default_factory=set)


def adjudicate(conn, hops: list[dict]) -> Plan:
    """对本批入点做重复裁定，返回处置计划（不落库）。

    失败闭路径择一：发现重复立即整组进隔离（含从正式线拉出），干净点直接写正式线；
    未采用「候选区 + 确认无重复后一次性切换」。
    """
    plan = Plan()
    if not hops:
        return plan

    # 同一 hop id 在本批内重复出现：重放语义，后者视为同点重投，不另算成员
    batch_ids = set()
    candidates = []
    for h in hops:
        hid = h["id"]
        if hid in batch_ids:
            plan.replayed.append(hid)
            continue
        batch_ids.add(hid)
        candidates.append(h)

    waybill_ids = sorted({h["waybill_id"] for h in candidates} or {""})
    marks = ",".join("?" for _ in waybill_ids)

    official_rows = conn.execute(
        f"SELECT id, waybill_id, event_ts, location_code, event_code, seq FROM hops "
        f"WHERE channel='primary' AND retracted=0 AND waybill_id IN ({marks})",
        waybill_ids,
    ).fetchall()
    official_by_id = {r["id"]: r for r in official_rows}

    quarantined_ids = {
        r[0]
        for r in conn.execute(
            f"SELECT hop_id FROM dup_group_members WHERE waybill_id IN ({marks})",
            waybill_ids,
        ).fetchall()
    }

    existing_groups = conn.execute(
        f"SELECT id, waybill_id, event_ts, location_code, event_code FROM dup_groups "
        f"WHERE waybill_id IN ({marks})",
        waybill_ids,
    ).fetchall()
    group_by_key = {
        (r["waybill_id"], r["event_ts"], r["location_code"], r["event_code"]): r["id"]
        for r in existing_groups
    }

    # 幂等重放：同 id 已在正式线或已在隔离成员中 → 跳过
    fresh = []
    for h in candidates:
        hid = h["id"]
        if hid in official_by_id or hid in quarantined_ids:
            plan.replayed.append(hid)
        else:
            fresh.append(h)

    # 键 → 成员点集合。正式线上的点参与找组：后写撞先写时整组（含先写）一起隔离
    key_members = {}
    for r in official_rows:
        key = (r["waybill_id"], r["event_ts"], r["location_code"], r["event_code"])
        key_members.setdefault(key, {})[r["id"]] = HopRef(
            hop_id=r["id"],
            waybill_id=r["waybill_id"],
            event_ts=r["event_ts"],
            location_code=r["location_code"],
            event_code=r["event_code"],
            seq=r["seq"],
            origin="official_pulled",
            payload=dict(r),
        )
    for h in fresh:
        key = (h["waybill_id"], h["event_ts"], h["location_code"], h["event_code"])
        key_members.setdefault(key, {})[h["id"]] = HopRef(
            hop_id=h["id"],
            waybill_id=h["waybill_id"],
            event_ts=h["event_ts"],
            location_code=h["location_code"],
            event_code=h["event_code"],
            seq=h.get("seq"),
            origin="batch",
            payload=dict(h),
        )

    grouped_keys = set()
    for key, members in key_members.items():
        gid = group_by_key.get(key) or group_id_for(*key)
        is_dup = len(members) >= 2
        is_tainted = key in group_by_key  # 历史已隔离组：新点直接并入
        if not (is_dup or is_tainted):
            continue
        if is_tainted and not is_dup:
            # 撞历史隔离组：仅本批新点成为新成员（旧成员已在库）
            members = {k: v for k, v in members.items() if k not in quarantined_ids}
        if not members:
            continue
        grouped_keys.add(key)
        # 整组隔离：全员进组，一个不留——组内任一点都不得进正式时间线；
        # 已在正式线的成员（official_pulled）由落库层逐一拉出后并入隔离。
        plan.groups.append(
            GroupPlan(
                id=gid,
                waybill_id=key[0],
                event_ts=key[1],
                location_code=key[2],
                event_code=key[3],
                members=list(members.values()),
            )
        )

    # 干净点：不在任何重复组里的本批新点（组内任一点都不得进正式线）
    for h in fresh:
        key = (h["waybill_id"], h["event_ts"], h["location_code"], h["event_code"])
        if key not in grouped_keys and len(key_members.get(key, {})) <= 1:
            plan.clean.append(
                HopRef(
                    hop_id=h["id"],
                    waybill_id=h["waybill_id"],
                    event_ts=h["event_ts"],
                    location_code=h["location_code"],
                    event_code=h["event_code"],
                    seq=h.get("seq"),
                    origin="batch",
                    payload=h,
                )
            )

    # 受影响运单：本波有正式写入、或有成员从正式线拉出、或有新隔离组的运单
    for g in plan.groups:
        plan.affected_waybills.add(g.waybill_id)
    for h in candidates:
        plan.affected_waybills.add(h["waybill_id"])

    return plan
