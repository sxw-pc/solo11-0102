"""Migration test: a database from the original seed keeps working."""
import hashlib
import json
import sqlite3
from pathlib import Path

from receiptdesk import field_visit
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.rules import evaluate
from receiptdesk.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'

LEGACY_SCHEMA = """
DROP TABLE IF EXISTS field_plan_leg;
DROP TABLE IF EXISTS field_plan_stop;
DROP TABLE IF EXISTS field_plan;
DROP TABLE IF EXISTS field_audit;
DROP TABLE IF EXISTS field_closure;
DROP TABLE IF EXISTS field_edge;
DROP TABLE IF EXISTS field_recipient;
DROP TABLE IF EXISTS field_case_state;
DROP TABLE IF EXISTS field_inspection;
DROP TABLE IF EXISTS field_move;
DROP TABLE IF EXISTS field_courier;
DROP TABLE IF EXISTS field_case;
CREATE TABLE field_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE field_courier(case_id TEXT PRIMARY KEY REFERENCES field_case(id),node TEXT NOT NULL,minute INTEGER NOT NULL);
CREATE TABLE field_move(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES field_case(id),origin TEXT NOT NULL,destination TEXT NOT NULL,minutes INTEGER NOT NULL,arrival INTEGER NOT NULL);
CREATE TABLE field_inspection(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES field_case(id),recipient TEXT NOT NULL,minute INTEGER NOT NULL,observation TEXT NOT NULL,UNIQUE(case_id,recipient));
"""


def test_legacy_single_courier_database_migrates(tmp_path):
    db = tmp_path / 'legacy.db'
    freeze_config(db, FIX / 'field_config.json')
    import_file(db, FIX / 'field_events.jsonl')
    evaluate(db)
    raw = (FIX / 'field_case.json').read_bytes()
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA)
    con.execute('INSERT INTO field_case VALUES (?,?,?)',
                ('route-1', json.dumps(json.loads(raw)), hashlib.sha256(raw).hexdigest()))
    con.execute("INSERT INTO field_courier VALUES ('route-1','D',0)")
    con.commit()
    con.close()

    # The next connection migrates in place and the old ledger survives.
    live = field_visit.state(db, 'route-1')
    assert live['couriers'] == [{'courier_id': 'c1', 'node': 'D', 'minute': 0}]
    assert live['moves'] == []
    migrated = connect(db)
    assert {r['name'] for r in migrated.execute('PRAGMA table_info(field_courier)')} == {
        'case_id', 'courier_id', 'node', 'minute'}
    assert 'plan_id' in {r['name'] for r in migrated.execute('PRAGMA table_info(field_move)')}
    migrated.close()

    # Re-registering the same digest fills the new per-recipient/edge tables
    # without conflicting with the legacy case row.
    assert field_visit.register(db, 'route-1', FIX / 'field_case.json')['recipients'] == 2
    planned = field_visit.plan(db, 'route-1', 'min_total_travel')
    assert planned['metrics']['total_travel_minutes'] == 16
    field_visit.move(db, 'route-1', 'A', 'm1')
    field_visit.inspect(db, 'route-1', 'R1', 'v1')
    assert [i['recipient'] for i in field_visit.state(db, 'route-1')['inspections']] == ['R1']


def test_legacy_courier_rows_are_not_duplicated(tmp_path):
    db = tmp_path / 'legacy2.db'
    freeze_config(db, FIX / 'field_config.json')
    import_file(db, FIX / 'field_events.jsonl')
    evaluate(db)
    con = sqlite3.connect(db)
    con.executescript(LEGACY_SCHEMA)
    con.execute('INSERT INTO field_case VALUES (?,?,?)',
                ('route-1', (FIX / 'field_case.json').read_text(), 'legacy'))
    con.execute("INSERT INTO field_courier VALUES ('route-1','D',0)")
    con.execute("INSERT INTO field_move VALUES ('old-move','route-1','D','A',1,1)")
    con.commit()
    con.close()
    recovered = field_visit.recover(db, 'route-1')
    assert recovered['couriers'][0]['node'] == 'A'
    assert recovered['couriers'][0]['minute'] == 1
    assert len(recovered['couriers']) == 1
