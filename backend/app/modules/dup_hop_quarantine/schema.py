"""重复组隔离的落库形态（两形态择一：独立隔离表）。

需求允许「独立隔离表按组挂成员点键与原因」或「本批不可变快照列」二选一。
本模块采用独立隔离表：
  - dup_groups        按 (waybill_id, event_ts, location_code, event_code) 重复键成组，
                      记录组级原因与处置结论；
  - dup_group_members 按组挂成员点（成员键 + 原文快照），与正式 hops 表物理分离，
                      隔离集合可独立查询，刷新后仍对得上。
未采用「成员键集合写入本批不可变快照列」形态。

整组撕裂核证实体（与隔离物化同事务写入，只读接口对外）：
  - dup_group_tear_checks 按 (组, 批次) 记录：组键、全员名册、正式轴排除证明、
    投影前后值。读路径在此快照之上再做实时同源复核，禁止只回放旧快照。
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

CREATE TABLE IF NOT EXISTS dup_group_tear_checks (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL REFERENCES dup_groups(id),
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  member_ids_json TEXT NOT NULL,
  member_count INTEGER NOT NULL,
  official_exclusion_json TEXT NOT NULL,
  leak_count INTEGER NOT NULL DEFAULT 0,
  proj_before TEXT,
  proj_after TEXT,
  created_at TEXT NOT NULL
);
"""


def ensure_tables(conn) -> None:
    conn.executescript(DDL)
