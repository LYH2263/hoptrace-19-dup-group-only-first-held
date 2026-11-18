"""重复组隔离的落库形态（两形态择一：独立隔离表）。

需求允许「独立隔离表按组挂成员点键与原因」或「本批不可变快照列」二选一。
本模块采用独立隔离表：
  - dup_groups        按 (waybill_id, event_ts, location_code, event_code) 重复键成组，
                      记录组级原因与处置结论；
  - dup_group_members 按组挂成员点（成员键 + 原文快照），与正式 hops 表物理分离，
                      隔离集合可独立查询，刷新后仍对得上。
未采用「成员键集合写入本批不可变快照列」形态。
"""

DDL = """
CREATE TABLE IF NOT EXISTS dup_groups (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  member_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,
  disposition TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_group_members (
  group_id TEXT NOT NULL REFERENCES dup_groups(id),
  hop_id TEXT NOT NULL,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  origin TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (group_id, hop_id)
);
"""


def ensure_tables(conn) -> None:
    conn.executescript(DDL)
