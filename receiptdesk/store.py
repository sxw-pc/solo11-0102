import sqlite3
from pathlib import Path

SCHEMA = """
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

-- Import staging.  Rows here are disposable until one final transaction copies
-- them into source/event/issue, so a crash can never expose a partial source.
-- A staged snapshot is addressed by its own identity (stage_snapshot.id); that
-- same identity is recorded on the committed source, so staging, commit and a
-- retry after interruption all agree on which snapshot was imported.
CREATE TABLE IF NOT EXISTS stage_snapshot(
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    file_key TEXT NOT NULL,
    file_sig TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS stage_snapshot_file_key ON stage_snapshot(file_key,state);
CREATE TABLE IF NOT EXISTS stage_chunk(
    snapshot_id TEXT NOT NULL REFERENCES stage_snapshot(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY(snapshot_id,seq)
);
CREATE TABLE IF NOT EXISTS stage_event(
    snapshot_id TEXT NOT NULL REFERENCES stage_snapshot(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    identity TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    time TEXT NOT NULL,
    payload TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,line_no)
);
CREATE TABLE IF NOT EXISTS stage_issue(
    snapshot_id TEXT NOT NULL REFERENCES stage_snapshot(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    error TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,line_no)
);

-- One row per committed source snapshot: which snapshot identity was chosen,
-- under which contract, and how many attempts it took to settle on stable
-- bytes.  Together with source.sha256 and the event rows this makes the choice
-- auditable after the fact.
CREATE TABLE IF NOT EXISTS import_audit(
    source_id INTEGER PRIMARY KEY REFERENCES source(id),
    snapshot_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    resumed INTEGER NOT NULL DEFAULT 0,
    path_changed INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    imported INTEGER NOT NULL,
    parse_errors INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db
