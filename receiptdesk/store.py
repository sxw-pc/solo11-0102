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
CREATE TABLE IF NOT EXISTS label_stock(serial TEXT PRIMARY KEY,status TEXT NOT NULL,job TEXT,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS printer_operation(id TEXT PRIMARY KEY,serial TEXT NOT NULL,job TEXT NOT NULL,action TEXT NOT NULL,result TEXT NOT NULL,revision_before INTEGER NOT NULL,revision_after INTEGER NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
