"""重复组证明（只读）：运单、重复键、成员列表、处置结论。

证明与隔离列表、正式时间线、装载报告同源——都查 dup_groups / dup_group_members /
hops 三表，禁止另算一套导致刷新后对不上。
"""
from __future__ import annotations

import json


def list_groups(conn, waybill_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, waybill_id, event_ts, location_code, event_code, member_count, "
        "reason, disposition, batch_id, created_at FROM dup_groups"
    )
    args: tuple = ()
    if waybill_id:
        sql += " WHERE waybill_id=?"
        args = (waybill_id,)
    sql += " ORDER BY created_at DESC, id"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_proof(conn, group_id: str) -> dict | None:
    g = conn.execute(
        "SELECT id, waybill_id, event_ts, location_code, event_code, member_count, "
        "reason, disposition, batch_id, created_at FROM dup_groups WHERE id=?",
        (group_id,),
    ).fetchone()
    if not g:
        return None
    members = conn.execute(
        "SELECT hop_id, waybill_id, event_ts, location_code, event_code, seq, origin, "
        "batch_id, payload_json, created_at FROM dup_group_members "
        "WHERE group_id=? ORDER BY hop_id",
        (group_id,),
    ).fetchall()
    member_list = []
    for m in members:
        d = dict(m)
        d["payload"] = json.loads(d.pop("payload_json"))
        member_list.append(d)
    # 同源校验：组成员任一不得出现在正式时间线
    leaked = conn.execute(
        """
        SELECT m.hop_id FROM dup_group_members m
        JOIN hops h ON h.id = m.hop_id AND h.channel='primary' AND h.retracted=0
        WHERE m.group_id=?
        """,
        (group_id,),
    ).fetchall()
    return {
        "group": dict(g),
        "dup_key": {
            "waybill_id": g["waybill_id"],
            "event_ts": g["event_ts"],
            "location_code": g["location_code"],
            "event_code": g["event_code"],
        },
        "members": member_list,
        "disposition": g["disposition"],
        "official_leak": [r["hop_id"] for r in leaked],  # 恒应为空；非空即证明失败
    }


def quarantine_rows(conn) -> list[dict]:
    """隔离列表（与证明同源）：组 + 成员键集合，供 /api/quarantine 与隔离页使用。"""
    groups = list_groups(conn)
    out = []
    for g in groups:
        members = conn.execute(
            "SELECT hop_id, origin FROM dup_group_members WHERE group_id=? ORDER BY hop_id",
            (g["id"],),
        ).fetchall()
        out.append(
            {
                **g,
                "member_json": json.dumps(
                    [{"hop_id": m["hop_id"], "origin": m["origin"]} for m in members],
                    ensure_ascii=False,
                ),
            }
        )
    return out
