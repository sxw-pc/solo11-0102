from pathlib import Path
import hashlib
import json

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.store import connect


def test_retention_fixture_has_a_mixed_source_blob(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'retention_case.json').read_text(encoding='utf-8'))
    source = (p / case['source_file']).read_bytes()
    assert hashlib.sha256(source).hexdigest() == case['source_sha256']
    assert hashlib.sha256((p / case['hold_file']).read_bytes()).hexdigest() == case['hold_sha256']
    db = tmp_path / 'batch.db'
    freeze_config(db, p / 'retention_config.json')
    assert import_file(db, p / case['source_file'])['imported'] == 2
    con = connect(db)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == source
    assert {r['subject'] for r in con.execute('SELECT subject FROM event')} == {'R1', 'R2'}
    con.close()
