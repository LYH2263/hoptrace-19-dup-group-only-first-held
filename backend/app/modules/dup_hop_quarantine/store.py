"""隔离落库与正式线写入：裁定计划的持久化，全部在同一事务内完成（失败闭）。

- 重复组：upsert 组行 + 成员行（成员含原文快照）；已在正式线的成员先拉出再入隔离；
- 干净点：直接 INSERT 正式 hops，任何冲突都抛错回滚整批，不做静默忽略；
- 受影响运单重投影状态并写状态证明，与正式线同源；
- 整组撕裂核证：隔离物化后逐组核证（全员在册、正式轴零成员、投影前后），
  核证不过即抛错整批回滚——宁可整批失败，不留半组撕裂。
"""
from __future__ import annotations

import hashlib
import json

from app.modules.dup_hop_quarantine import adjudicate


def apply_plan(conn, plan: adjudicate.Plan, batch_id: str, now: str) -> None:
    # 投影前态：批前快照，供整组撕裂核证记录「投影前后」（组前态）
    group_waybills = sorted({g.waybill_id for g in plan.groups})
    proj_before = {}
    for wid in group_waybills:
        row = conn.execute("SELECT status_proj FROM waybills WHERE id=?", (wid,)).fetchone()
        proj_before[wid] = row["status_proj"] if row else None

    for g in plan.groups:
        conn.execute(
            """
            INSERT OR IGNORE INTO dup_groups
              (id, waybill_id, event_ts, location_code, event_code,
               member_count, reason, disposition, batch_id, created_at)
            VALUES (?,?,?,?,?,0,?,?,?,?)
            """,
            (
                g.id,
                g.waybill_id,
                g.event_ts,
                g.location_code,
                g.event_code,
                adjudicate.REASON,
                adjudicate.DISPOSITION,
                batch_id,
                now,
            ),
        )
        for m in g.members:
            if m.origin == "official_pulled":
                # 已在正式线的成员先拉出（DELETE）再入隔离：全员逐一执行，一个不留
                conn.execute("DELETE FROM hops WHERE id=?", (m.hop_id,))
            conn.execute(
                """
                INSERT OR IGNORE INTO dup_group_members
                  (group_id, hop_id, waybill_id, event_ts, location_code, event_code,
                   seq, origin, batch_id, payload_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    g.id,
                    m.hop_id,
                    m.waybill_id,
                    m.event_ts,
                    m.location_code,
                    m.event_code,
                    m.seq,
                    m.origin,
                    batch_id,
                    json.dumps(m.payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        # member_count 与成员表同源刷新，禁止组行与成员对不上
        conn.execute(
            """
            UPDATE dup_groups
            SET member_count = (SELECT COUNT(*) FROM dup_group_members WHERE group_id=?)
            WHERE id=?
            """,
            (g.id, g.id),
        )

    for h in plan.clean:
        conn.execute(
            """
            INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES (?,?,?,?,?,?,'primary')
            """,
            (h.hop_id, h.waybill_id, h.event_ts, h.location_code, h.event_code, h.seq),
        )

    for wid in sorted(plan.affected_waybills):
        refresh_projection(conn, wid, now)

    # 整组撕裂核证物化：全员名册 + 正式轴排除证明 + 投影前后，同事务落库；
    # 核证不过（缺员 / 泄漏）即抛错，整批回滚，正式线保持批前状态
    for g in plan.groups:
        materialize_tear_check(conn, g, batch_id, now, proj_before.get(g.waybill_id))


def materialize_tear_check(conn, group: adjudicate.GroupPlan, batch_id: str, now: str, proj_before) -> None:
    """整组撕裂核证：从库内同源派生全员名册与正式轴排除证明，落核证行。

    失败闭硬约束：
    - 隔离成员数不得少于命中数——本波计划成员必须全部在册，缺一抛错；
    - 组内任一点不得仍在正式轴——逐成员核证，泄漏即抛错。
    """
    rows = conn.execute(
        "SELECT hop_id FROM dup_group_members WHERE group_id=? ORDER BY hop_id",
        (group.id,),
    ).fetchall()
    roster = [r["hop_id"] for r in rows]
    roster_set = set(roster)

    missing = [m.hop_id for m in group.members if m.hop_id not in roster_set]
    if missing:
        raise RuntimeError(f"隔离物化缺成员 {missing}（组 {group.id}），整批回滚")

    exclusion = []
    leaks = []
    for hid in roster:
        on_axis = (
            conn.execute(
                "SELECT 1 FROM hops WHERE id=? AND channel='primary' AND retracted=0",
                (hid,),
            ).fetchone()
            is not None
        )
        exclusion.append({"hop_id": hid, "on_official_axis": on_axis})
        if on_axis:
            leaks.append(hid)
    if leaks:
        raise RuntimeError(f"组成员仍挂正式轴 {leaks}（组 {group.id}），整批回滚")

    row = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (group.waybill_id,)
    ).fetchone()
    proj_after = row["status_proj"] if row else None

    tc_id = "TC-" + hashlib.sha1(f"{group.id}|{batch_id}".encode("utf-8")).hexdigest()[:12]
    conn.execute(
        """
        INSERT OR REPLACE INTO dup_group_tear_checks
          (id, group_id, waybill_id, event_ts, location_code, event_code, batch_id,
           member_ids_json, member_count, official_exclusion_json, leak_count,
           proj_before, proj_after, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)
        """,
        (
            tc_id,
            group.id,
            group.waybill_id,
            group.event_ts,
            group.location_code,
            group.event_code,
            batch_id,
            json.dumps(roster, ensure_ascii=False),
            len(roster),
            json.dumps(exclusion, ensure_ascii=False),
            proj_before,
            proj_after,
            now,
        ),
    )


def refresh_projection(conn, waybill_id: str, now: str) -> None:
    """按正式线末点重投影运单状态，并留状态证明（与正式时间线同源）。"""
    from app.seed import map_status  # 延迟导入，避免与 seed 循环依赖

    row = conn.execute(
        """
        SELECT id, event_code FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts DESC, id DESC LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    proj = map_status(row["event_code"]) if row else "未知"
    conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, waybill_id))
    conn.execute(
        """
        INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
        VALUES (?,?,?,?,?)
        """,
        (waybill_id, row["id"] if row else None, row["event_code"] if row else None, proj, now),
    )


def build_report(conn, plan: adjudicate.Plan, batch_id: str, cutoff_ts, input_count: int) -> dict:
    """装载报告：重复组数 / 隔离点数 / 正式写入数全部从库内同源派生，
    与隔离列表、正式时间线、证明接口查的是同一批行。"""
    group_ids = [g.id for g in plan.groups]
    member_keys = [(g.id, m.hop_id) for g in plan.groups for m in g.members]
    clean_ids = [h.hop_id for h in plan.clean]

    quarantined_points = 0
    for gid, hid in member_keys:
        quarantined_points += conn.execute(
            "SELECT COUNT(*) AS c FROM dup_group_members WHERE group_id=? AND hop_id=?",
            (gid, hid),
        ).fetchone()["c"]

    written_official = 0
    for hid in clean_ids:
        written_official += conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE id=? AND channel='primary' AND retracted=0",
            (hid,),
        ).fetchone()["c"]

    groups = []
    for gid in group_ids:
        row = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, member_count, "
            "reason, disposition FROM dup_groups WHERE id=?",
            (gid,),
        ).fetchone()
        members = conn.execute(
            "SELECT hop_id, origin FROM dup_group_members WHERE group_id=? ORDER BY hop_id",
            (gid,),
        ).fetchall()
        groups.append(
            {
                "group_id": row["id"],
                "waybill_id": row["waybill_id"],
                "dup_key": {
                    "event_ts": row["event_ts"],
                    "location_code": row["location_code"],
                    "event_code": row["event_code"],
                },
                "member_hop_ids": [m["hop_id"] for m in members],
                "member_count": row["member_count"],
                "reason": row["reason"],
                "disposition": row["disposition"],
            }
        )

    return {
        "batch_id": batch_id,
        "cutoff_ts": cutoff_ts,
        "input": input_count,
        "replayed": len(plan.replayed),
        "written_official": written_official,
        "dup_group_count": len(groups),
        "quarantined_points": quarantined_points,
        "groups": groups,
    }
