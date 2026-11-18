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
CREATE TABLE IF NOT EXISTS paper_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_pack(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES paper_case(id),ink_grade TEXT NOT NULL,mass_kg REAL NOT NULL,fiber_kg REAL NOT NULL,residue_kg REAL NOT NULL,container TEXT,location TEXT NOT NULL,disposition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_container(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES paper_case(id),capacity_kg REAL NOT NULL,location TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_operation(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES paper_case(id),kind TEXT NOT NULL,result TEXT NOT NULL,intent TEXT);
CREATE TABLE IF NOT EXISTS paper_authorization(case_id TEXT NOT NULL,route TEXT NOT NULL,enabled INTEGER NOT NULL,PRIMARY KEY(case_id,route));
CREATE TABLE IF NOT EXISTS paper_plan(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES paper_case(id),contract TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_plan_step(plan_id TEXT NOT NULL REFERENCES paper_plan(id),seq INTEGER NOT NULL,operation_id TEXT NOT NULL,kind TEXT NOT NULL,intent TEXT NOT NULL,status TEXT NOT NULL,result TEXT,PRIMARY KEY(plan_id,seq));
CREATE TABLE IF NOT EXISTS paper_quarantine(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,scope TEXT NOT NULL,ref_id TEXT NOT NULL,stage TEXT NOT NULL,reason TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
"""


def _migrate(db):
    """Idempotent migrations for workspaces created before the campaign tables."""
    cols = {r['name'] for r in db.execute('PRAGMA table_info(paper_operation)')}
    if 'intent' not in cols:
        db.execute('ALTER TABLE paper_operation ADD COLUMN intent TEXT')


def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db
