from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules.dup_hop_quarantine import proofs as dup_proofs
from app.modules.dup_hop_quarantine import schema as dup_schema
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        return {
            "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM dup_groups").fetchone()["c"],
            "quarantine_points": conn.execute(
                "SELECT COUNT(*) AS c FROM dup_group_members"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, status_proj, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        hops = timeline_svc.list_primary(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        return {
            "waybill": dict(wb),
            "hops": hops,
            "proof": dict(proof) if proof else None,
            "dup_groups": dup_proofs.list_groups(conn, waybill_id),
        }
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        return dup_proofs.quarantine_rows(conn)
    finally:
        conn.close()


@router.get("/dup-groups")
def list_dup_groups(waybill_id: str | None = None):
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        return dup_proofs.list_groups(conn, waybill_id)
    finally:
        conn.close()


@router.get("/dup-groups/{group_id}/proof")
def dup_group_proof(group_id: str):
    """重复组证明（只读）：运单、重复键、成员列表、处置结论。"""
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        proof = dup_proofs.get_proof(conn, group_id)
        if not proof:
            raise HTTPException(404, "重复组不存在")
        return proof
    finally:
        conn.close()


@router.get("/dup-groups/{group_id}/tear-check")
def dup_group_tear_check(group_id: str):
    """整组撕裂核证（只读）：组键、全员、正式轴排除证明、投影前后。"""
    conn = connect()
    try:
        dup_schema.ensure_tables(conn)
        tear = dup_proofs.get_tear_check(conn, group_id)
        if not tear:
            raise HTTPException(404, "重复组撕裂核证不存在")
        return tear
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _report_view_compat(payload: dict) -> dict:
    """兼容旧报告字段别名，读路径可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "ordered_hop_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("ordered_hop_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "proj_value" in out and "status" not in out:
        out["status"] = out.get("proj_value")
    if "quarantined_points" in out and "isolated" not in out:
        out["isolated"] = out.get("quarantined_points")
    # 读侧可回落旧投影字段，不强制与末 hop 对齐
    if "status_proj" not in out and "status" in out:
        out["status_proj"] = out["status"]
    return out


def _axis_read_fallback(rows: list) -> list:
    """读轴时保留全通道点，由页面自行过滤。"""
    return list(rows or [])


def _seal_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("verified", True)
    data.setdefault("writable", True)
    return data

