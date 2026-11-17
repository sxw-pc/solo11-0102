from pathlib import Path
import pytest
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.paper_waste import register, state, load, ship, process


FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def prepared(tmp_path, name):
    db = tmp_path / name
    freeze_config(db, FIX / 'paper-config.json')
    assert import_file(db, FIX / 'paper-events.jsonl')['imported'] == 2
    register(db, 'paper-01', FIX / 'paper-case.json')
    return db


def test_separate_recovery_physically_yields_fiber(tmp_path):
    db = prepared(tmp_path, 'recovery.db')
    load(db, 'paper-01', 'P1', 'C5', 'l1')
    with pytest.raises(ValueError, match='container_capacity_exceeded'):
        load(db, 'paper-01', 'P2', 'C5', 'heavy')
    load(db, 'paper-01', 'P2', 'C9', 'l2')
    assert ship(db, 'paper-01', 'C5', 's1')['mass_kg'] == 4
    assert ship(db, 'paper-01', 'C9', 's2')['mass_kg'] == 5
    a = process(db, 'paper-01', 'C5', 'recover', 'p1')
    b = process(db, 'paper-01', 'C9', 'recover', 'p2')
    assert a['recovered_kg'] + b['recovered_kg'] == 7
    assert a['secured_kg'] + b['secured_kg'] == 2
    assert {p['disposition'] for p in state(db, 'paper-01')['packs']} == {'recover'}


def test_consolidated_secure_thermal_has_no_recovered_fiber(tmp_path):
    db = prepared(tmp_path, 'thermal.db')
    load(db, 'paper-01', 'P1', 'C9', 'l1')
    load(db, 'paper-01', 'P2', 'C9', 'l2')
    assert ship(db, 'paper-01', 'C9', 's1')['mass_kg'] == 9
    with pytest.raises(ValueError, match='mixed_ink_grade_cannot_recover'):
        process(db, 'paper-01', 'C9', 'recover', 'p1')
    result = process(db, 'paper-01', 'C9', 'thermal', 'p2')
    assert result['recovered_kg'] == 0 and result['secured_kg'] == 9
    assert process(db, 'paper-01', 'C9', 'thermal', 'p2')['status'] == 'replayed'


def test_requires_original_queued_source(tmp_path):
    db = tmp_path / 'missing.db'
    freeze_config(db, FIX / 'paper-config.json')
    with pytest.raises(ValueError, match='paper_notice_source_missing'):
        register(db, 'paper-01', FIX / 'paper-case.json')
