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
CREATE TABLE IF NOT EXISTS packet_run(id INTEGER PRIMARY KEY,input_hash TEXT NOT NULL UNIQUE,contract TEXT NOT NULL,config_hash TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packet(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES packet_run(id),recipient TEXT NOT NULL,requested_at TEXT,status TEXT NOT NULL,reason TEXT);
CREATE TABLE IF NOT EXISTS packet_item(packet_id INTEGER NOT NULL REFERENCES packet(id),form_id TEXT NOT NULL,version TEXT NOT NULL,sha256 TEXT NOT NULL,signer_id TEXT NOT NULL,PRIMARY KEY(packet_id,form_id));
CREATE TABLE IF NOT EXISTS seal_ledger(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES packet_run(id),packet_id INTEGER NOT NULL REFERENCES packet(id),signer_id TEXT NOT NULL,form_id TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
