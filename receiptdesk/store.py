import sqlite3
from pathlib import Path

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL,event_id INTEGER REFERENCES event(id));
CREATE TABLE IF NOT EXISTS decision(subject TEXT PRIMARY KEY,status TEXT NOT NULL,details TEXT NOT NULL,rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id));
CREATE TABLE IF NOT EXISTS evidence(subject TEXT NOT NULL REFERENCES subject(id),event_id INTEGER NOT NULL REFERENCES event(id),rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id),PRIMARY KEY(subject,event_id,rule_version));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS field_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS field_case_state(
    case_id TEXT PRIMARY KEY REFERENCES field_case(id),
    status TEXT NOT NULL DEFAULT 'registered',
    next_plan_version INTEGER NOT NULL DEFAULT 1,
    recheck_required INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS field_recipient(
    case_id TEXT NOT NULL REFERENCES field_case(id),
    recipient TEXT NOT NULL,
    node TEXT NOT NULL,
    requested_minute INTEGER NOT NULL,
    observation TEXT NOT NULL,
    original_status TEXT NOT NULL,
    original_details TEXT NOT NULL,
    original_rule_version TEXT,
    original_run_id INTEGER,
    original_event_ids TEXT NOT NULL,
    original_event_shas TEXT NOT NULL DEFAULT '[]',
    original_source_shas TEXT NOT NULL,
    snapshot_status TEXT NOT NULL,
    snapshot_details TEXT NOT NULL,
    snapshot_rule_version TEXT,
    snapshot_run_id INTEGER,
    snapshot_event_ids TEXT NOT NULL,
    snapshot_event_shas TEXT NOT NULL DEFAULT '[]',
    snapshot_source_shas TEXT NOT NULL,
    snapshot_signature TEXT,
    eligibility TEXT NOT NULL DEFAULT 'active',
    invalidation_reason TEXT,
    snapshot_at TEXT,
    rechecked_at TEXT,
    PRIMARY KEY(case_id,recipient)
);
CREATE TABLE IF NOT EXISTS field_courier(
    case_id TEXT NOT NULL REFERENCES field_case(id),
    courier_id TEXT NOT NULL,
    node TEXT NOT NULL,
    minute INTEGER NOT NULL,
    PRIMARY KEY(case_id,courier_id)
);
CREATE TABLE IF NOT EXISTS field_edge(
    case_id TEXT NOT NULL REFERENCES field_case(id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    PRIMARY KEY(case_id,origin,destination)
);
CREATE TABLE IF NOT EXISTS field_closure(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES field_case(id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    UNIQUE(case_id,origin,destination,start_minute,end_minute)
);
CREATE TABLE IF NOT EXISTS field_move(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES field_case(id),
    courier_id TEXT NOT NULL DEFAULT 'c1',
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    arrival INTEGER NOT NULL,
    plan_id INTEGER,
    plan_version INTEGER
);
CREATE TABLE IF NOT EXISTS field_inspection(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES field_case(id),
    courier_id TEXT NOT NULL DEFAULT 'c1',
    recipient TEXT NOT NULL,
    minute INTEGER NOT NULL,
    observation TEXT NOT NULL,
    late_minutes INTEGER NOT NULL DEFAULT 0,
    plan_id INTEGER,
    plan_version INTEGER,
    UNIQUE(case_id,recipient)
);
CREATE TABLE IF NOT EXISTS field_plan(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES field_case(id),
    version INTEGER NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    metrics TEXT NOT NULL,
    optimality TEXT NOT NULL,
    digest TEXT,
    base_state TEXT,
    request_id TEXT,
    UNIQUE(case_id,version)
);
CREATE TABLE IF NOT EXISTS field_plan_stop(
    plan_id INTEGER NOT NULL REFERENCES field_plan(id),
    courier_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    node TEXT NOT NULL,
    requested_minute INTEGER NOT NULL,
    planned_arrival INTEGER NOT NULL,
    late_minutes INTEGER NOT NULL,
    PRIMARY KEY(plan_id,courier_id,sequence)
);
CREATE TABLE IF NOT EXISTS field_plan_leg(
    plan_id INTEGER NOT NULL REFERENCES field_plan(id),
    courier_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    depart_minute INTEGER NOT NULL,
    arrive_minute INTEGER NOT NULL,
    path TEXT NOT NULL,
    recipient TEXT,
    PRIMARY KEY(plan_id,courier_id,sequence)
);
CREATE TABLE IF NOT EXISTS field_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    details TEXT NOT NULL
);
"""

_COURIER_TABLE="""CREATE TABLE IF NOT EXISTS field_courier(
    case_id TEXT NOT NULL REFERENCES field_case(id),
    courier_id TEXT NOT NULL,
    node TEXT NOT NULL,
    minute INTEGER NOT NULL,
    PRIMARY KEY(case_id,courier_id))"""


def _columns(db, table):
    return {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}


def _add_column(db, table, column, declaration):
    if column not in _columns(db, table):
        db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + declaration)


def _migrate(db):
    """Bring databases created by the original single-courier seed forward.

    The original schema had one courier per case and no place to distinguish a
    planned route from the moves that were actually executed.  Existing rows
    are preserved and attributed to the default courier.
    """
    # Older single-courier table: rebuild with a composite key.
    cols = _columns(db, 'field_courier')
    if cols and 'courier_id' not in cols:
        db.execute('ALTER TABLE field_courier RENAME TO field_courier_legacy')
        db.execute(_COURIER_TABLE)
        db.execute("INSERT OR IGNORE INTO field_courier(case_id,courier_id,node,minute) "
                   "SELECT case_id,'c1',node,minute FROM field_courier_legacy")
        db.execute('DROP TABLE field_courier_legacy')
    # Executed action ledger additions.
    _add_column(db, 'field_move', 'courier_id', "TEXT NOT NULL DEFAULT 'c1'")
    _add_column(db, 'field_move', 'plan_id', 'INTEGER')
    _add_column(db, 'field_move', 'plan_version', 'INTEGER')
    _add_column(db, 'field_inspection', 'courier_id', "TEXT NOT NULL DEFAULT 'c1'")
    _add_column(db, 'field_inspection', 'late_minutes', 'INTEGER NOT NULL DEFAULT 0')
    _add_column(db, 'field_inspection', 'plan_id', 'INTEGER')
    _add_column(db, 'field_inspection', 'plan_version', 'INTEGER')
    _add_column(db, 'field_recipient', 'original_event_shas', "TEXT NOT NULL DEFAULT '[]'")
    _add_column(db, 'field_recipient', 'snapshot_event_shas', "TEXT NOT NULL DEFAULT '[]'")
    _add_column(db, 'field_recipient', 'snapshot_signature', 'TEXT')
    _add_column(db, 'field_plan', 'request_id', 'TEXT')
    db.commit()


def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db
