"""dup_hop_quarantine — 同运单重复键 (event_ts, location_code, event_code) 整组隔离。

- 裁定：adjudicate.adjudicate（重复键找组，默认整组隔离）
- 落库：store.apply_plan（独立隔离表形态；失败闭，同事务）
- 装载：service.run_batch（读夹具 → 裁定 → 落库 → 同源报告）
- 证明：proofs（只读，运单 / 重复键 / 成员列表 / 处置结论）
"""
from app.modules.dup_hop_quarantine import adjudicate, proofs, schema, service, store

__all__ = ["adjudicate", "proofs", "schema", "service", "store"]
