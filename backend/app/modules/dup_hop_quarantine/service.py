"""装载服务：读夹具 → 裁定 → 同事务落库（失败闭）。

失败闭路径（两路径择一）：发现重复立即整组进隔离，干净点直接写正式线；
任一环节异常整批回滚，正式线保持批前状态。未采用「候选区暂存 + 一次性切换」。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.db import connect
from app.modules.dup_hop_quarantine import adjudicate, schema, store


def run_batch(hops_payload: list[dict], batch_id: str | None = None, cutoff_ts: str | None = None) -> dict:
    batch_id = batch_id or f"B-{uuid.uuid4().hex[:8]}"
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        schema.ensure_tables(conn)
        now = _now_iso()
        _ensure_waybills(conn, hops_payload, now)
        plan = adjudicate.adjudicate(conn, hops_payload)
        store.apply_plan(conn, plan, batch_id, now)
        report = store.build_report(conn, plan, batch_id, cutoff_ts, len(hops_payload))
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff_ts, now, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:  # 失败闭：任何异常整批回滚，正式线保持批前状态
        conn.rollback()
        return {"ok": False, "error": str(e), "batch_id": batch_id}
    finally:
        conn.close()


def _ensure_waybills(conn, hops_payload: list[dict], now: str) -> None:
    for h in hops_payload:
        wid = h["waybill_id"]
        exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                (wid, h.get("waybill_title") or wid, "未知", now),
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
