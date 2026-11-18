import hashlib
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
CREATE TABLE IF NOT EXISTS retention_request(
  id TEXT PRIMARY KEY,
  contract TEXT NOT NULL,
  request_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  source_sha256 TEXT NOT NULL,
  subject TEXT NOT NULL,
  credential_version TEXT NOT NULL,
  credential_sha256 TEXT,
  hold_until TEXT,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_audit(
  id INTEGER PRIMARY KEY,
  request_id TEXT NOT NULL,
  action TEXT NOT NULL,
  at TEXT NOT NULL,
  details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_lineage(
  new_source_id INTEGER NOT NULL REFERENCES source(id),
  old_source_id INTEGER NOT NULL,
  old_sha256 TEXT NOT NULL,
  new_sha256 TEXT NOT NULL,
  request_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  line_map TEXT NOT NULL,
  PRIMARY KEY(new_source_id,old_source_id,request_id)
);
CREATE TABLE IF NOT EXISTS source_hash_tombstone(
  sha256 TEXT PRIMARY KEY,
  source_id INTEGER,
  request_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  replaced_by_sha256 TEXT,
  at TEXT NOT NULL
);
"""
def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    db.executescript(SCHEMA)
    return db

def begin_immediate(db):
    """Serialize all retention writers so hold release and erasure cannot interleave."""
    db.execute('BEGIN IMMEDIATE')

def masked_subjects(db):
    """Subjects whose erasure is requested but not physically completed.

    A pending erasure (blocked by an authorized shared-source hold) still leaves
    the bytes in the active blob, so every normal read/report path must filter
    the subject out rather than expose its content.
    """
    rows=db.execute(
        "SELECT DISTINCT subject FROM retention_request "
        "WHERE request_type='erasure' AND status='pending'").fetchall()
    return {r['subject'] for r in rows}

def visible_events(db):
    """Events allowed on normal read paths: never rows of a masked subject."""
    hidden=masked_subjects(db)
    rows=db.execute('SELECT * FROM event ORDER BY time,id').fetchall()
    return [r for r in rows if r['subject'] not in hidden]

def active_source_blobs(db):
    return [dict(r) for r in db.execute('SELECT id,path,sha256,raw FROM source ORDER BY id')]
