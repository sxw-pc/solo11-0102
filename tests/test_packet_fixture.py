from pathlib import Path
import hashlib
import json

from receiptdesk.ingest import freeze_config
from receiptdesk.store import connect


def test_packet_fixture_has_frozen_recipients_and_real_files(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'packet_signing.json').read_text(encoding='utf-8'))
    db = tmp_path / 'batch.db'
    freeze_config(db, p / 'packet_config.json')
    con = connect(db)
    assert {r['id'] for r in con.execute('SELECT id FROM subject')} == {x['recipient'] for x in case['requests']}
    con.close()
    for form in case['forms']:
        data = (p / 'packet_files' / form['version']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == form['sha256']
    assert sum(s['remaining_seals'] for s in case['signers']) == 2
