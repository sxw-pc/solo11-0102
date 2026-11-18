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
-- The disclosure ledger is deliberately separate from the evidence tables.
-- Only rows that were actually released are visible facts for later safety
-- proofs; denied attempts live in disclosure_attempt and never enter those
-- proofs (and never enter another audience's proofs at all).
CREATE TABLE IF NOT EXISTS disclosure_release(
    audience TEXT NOT NULL,
    release_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_cohort TEXT NOT NULL,
    cohort TEXT NOT NULL,
    recipient_count INTEGER NOT NULL,
    metric TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    file_path TEXT,
    file_sha256 TEXT,
    origin TEXT NOT NULL DEFAULT 'published',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(audience,release_id),
    UNIQUE(audience,request_id)
);
CREATE TABLE IF NOT EXISTS disclosure_attempt(
    id INTEGER PRIMARY KEY,
    audience TEXT NOT NULL,
    request_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_cohort TEXT NOT NULL,
    cohort TEXT,
    recipient_count INTEGER,
    release_id TEXT,
    reason TEXT,
    identifiable TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(audience,request_id,policy)
);
CREATE INDEX IF NOT EXISTS disclosure_release_audience_status
    ON disclosure_release(audience,status);
CREATE INDEX IF NOT EXISTS disclosure_attempt_request
    ON disclosure_attempt(audience,request_id);
-- One external summary per (audience, request, policy).  Two policies that both
-- approve the same request ID would publish two different cohorts, so this is
-- rejected in the database as well as in code.
CREATE UNIQUE INDEX IF NOT EXISTS disclosure_attempt_published
    ON disclosure_attempt(audience,request_id) WHERE status='published';
"""
def connect(path,timeout=30.0,autocommit=False):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=timeout)
    if autocommit: db.isolation_level=None
    db.row_factory=sqlite3.Row
    db.execute(f'PRAGMA busy_timeout={int(timeout*1000)}')
    db.executescript(SCHEMA)
    return db

def connect_immediate(path,timeout=30.0):
    """Connection whose transactions are explicit and can take a write lock.

    Publishing a disclosure needs the audience's whole history to be read under
    the same write lock as the new release row.  Otherwise two requests that are
    individually safe could both be approved before either is recorded, and the
    pair would leak an individual's channel.
    """
    return connect(path,timeout=timeout,autocommit=True)
