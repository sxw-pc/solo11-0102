from pathlib import Path
import pytest
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.print_lab import register, invoke, state

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_printer_fixture_has_real_jobs_and_physical_side_effects(tmp_path):
    db = tmp_path / 'print.db'
    freeze_config(db, FIX / 'print-config.json')
    assert import_file(db, FIX / 'print-events.jsonl')['imported'] == 2
    assert register(db, 'print-01', FIX / 'print-case.json') == {
        'case_id': 'print-01', 'jobs': 2, 'stock': 4}
    assert invoke(db, 'L100', 'J1', 'op-1', 0)['status'] == 'timeout'
    assert state(db, 'L100')['status'] == 'printed'
    assert invoke(db, 'L100', 'J1', 'op-1', 0)['revision'] == 1
    assert invoke(db, 'L101', 'J2', 'op-2', 0)['status'] == 'jammed'
    assert state(db, 'L101')['status'] == 'jammed'
    assert invoke(db, 'L100', 'J1', 'op-3', 1, 'void')['status'] == 'committed'
    assert state(db, 'L100')['status'] == 'void'


def test_printer_requires_queued_evidence(tmp_path):
    db = tmp_path / 'print.db'
    freeze_config(db, FIX / 'print-config.json')
    with pytest.raises(ValueError, match='print_subject_unobserved'):
        register(db, 'print-01', FIX / 'print-case.json')
