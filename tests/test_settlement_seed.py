import json
from pathlib import Path
import pytest
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.settlement import register
from receiptdesk.store import connect


FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_settlement_fixture_freezes_real_notice_and_bank_source(tmp_path):
    db = tmp_path / 'settlement.db'
    freeze_config(db, FIX / 'settlement-config.json')
    assert import_file(db, FIX / 'settlement-events.jsonl')['imported'] == 2
    assert register(db, 'settlement-01', FIX / 'settlement-case.json',
                    FIX / 'settlement-bank.jsonl') == {'case_id': 'settlement-01',
                                                      'invoices': 2, 'bank_receipts': 2}
    con = connect(db)
    doc = json.loads(con.execute('SELECT payload FROM settlement_case').fetchone()[0])
    con.close()
    assert [x['invoice']['subject'] for x in doc['invoices']] == ['R1', 'R2']
    assert doc['bank_receipts'][0]['candidate_invoices'] == ['INV-1', 'INV-2']
    assert doc['bank_receipts'][1]['candidate_invoices'] == ['INV-1']
    assert len(doc['bank_sha256']) == 64


def test_settlement_requires_real_queued_notice(tmp_path):
    db = tmp_path / 'settlement.db'
    freeze_config(db, FIX / 'settlement-config.json')
    with pytest.raises(ValueError, match='settlement_subject_unobserved'):
        register(db, 'settlement-01', FIX / 'settlement-case.json', FIX / 'settlement-bank.jsonl')
