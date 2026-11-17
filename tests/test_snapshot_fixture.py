from pathlib import Path
import hashlib
import json

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.store import connect


def test_snapshot_fixture_has_two_real_distinct_source_versions(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'snapshot_mutation.json').read_text(encoding='utf-8'))
    old = (p / case['original_file']).read_bytes()
    new = (p / case['revised_file']).read_bytes()
    assert hashlib.sha256(old).hexdigest() == case['original_sha256']
    assert hashlib.sha256(new).hexdigest() == case['revised_sha256']
    assert old != new
    db = tmp_path / 'batch.db'
    freeze_config(db, p / 'config.json')
    assert import_file(db, p / case['original_file'])['imported'] == 2048
    con = connect(db)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == old
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 2048
    con.close()
