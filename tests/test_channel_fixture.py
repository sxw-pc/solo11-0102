from pathlib import Path
import json

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.report import export


def test_channel_fixture_has_real_external_leak(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    request = json.loads((p / 'channel_disclosure_requests.json').read_text(encoding='utf-8'))
    db = tmp_path / 'batch.db'
    freeze_config(db, p / 'channel_disclosure_config.json')
    assert import_file(db, p / 'channel_disclosure_events.jsonl')['imported'] == 4
    raw = export(db, tmp_path / 'report.json').read_text(encoding='utf-8')
    assert 'r3@example.invalid' in raw and 'email-v2' in raw
    assert request['previous_release']['count'] == 1
    assert request['new_request']['recipients'] == ['R1', 'R2', 'R3']
