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
CREATE TABLE IF NOT EXISTS print_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS label_stock(serial TEXT PRIMARY KEY,status TEXT NOT NULL,job TEXT,revision INTEGER NOT NULL,content_sha256 TEXT);
CREATE TABLE IF NOT EXISTS printer_operation(id TEXT PRIMARY KEY,serial TEXT NOT NULL,job TEXT NOT NULL,action TEXT NOT NULL,result TEXT NOT NULL,revision_before INTEGER NOT NULL,revision_after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS label_case(case_id TEXT PRIMARY KEY,print_case_id TEXT NOT NULL,contract TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS label_job(case_id TEXT NOT NULL,job_id TEXT NOT NULL,ordinal INTEGER NOT NULL,subject TEXT NOT NULL,message_id TEXT NOT NULL,queued_event_id INTEGER NOT NULL,queued_identity TEXT NOT NULL,queued_event_sha256 TEXT NOT NULL,source_id INTEGER NOT NULL,source_sha256 TEXT NOT NULL,layout_version INTEGER NOT NULL,layout_hash TEXT NOT NULL,postage_cents INTEGER NOT NULL,state TEXT NOT NULL,current_serial TEXT,voided_serial TEXT,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(case_id,job_id));
CREATE TABLE IF NOT EXISTS label_assignment(serial TEXT PRIMARY KEY,case_id TEXT NOT NULL,job_id TEXT NOT NULL,sequence INTEGER NOT NULL,state TEXT NOT NULL,layout_hash TEXT NOT NULL,device_revision INTEGER,print_operation_id TEXT,void_operation_id TEXT,paper_consumed INTEGER NOT NULL DEFAULT 0,postage_cents INTEGER NOT NULL DEFAULT 0,ack_lost INTEGER NOT NULL DEFAULT 0,retained INTEGER NOT NULL DEFAULT 0,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(case_id,job_id,sequence));
CREATE UNIQUE INDEX IF NOT EXISTS label_one_current ON label_assignment(case_id,job_id) WHERE state IN ('allocated','intent_print','printed','void_intent','void_ack_lost','manual_review');
CREATE TABLE IF NOT EXISTS label_operation(operation_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,job_id TEXT NOT NULL,serial TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL,expected_revision INTEGER NOT NULL,result TEXT,revision_after INTEGER,detail TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS label_postage(serial TEXT PRIMARY KEY,case_id TEXT NOT NULL,job_id TEXT NOT NULL,cents INTEGER NOT NULL,reason TEXT NOT NULL,operation_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS label_audit(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL,job_id TEXT,serial TEXT,event TEXT NOT NULL,detail TEXT,created_at TEXT NOT NULL,UNIQUE(case_id,job_id,serial,event));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    columns={row[1] for row in db.execute('PRAGMA table_info(label_stock)')}
    if 'content_sha256' not in columns:
        db.execute('ALTER TABLE label_stock ADD COLUMN content_sha256 TEXT')
    return db
