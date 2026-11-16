import json
from pathlib import Path
from receiptdesk.__main__ import demo
from receiptdesk.ingest import freeze_config,import_file
from receiptdesk.rules import evaluate
from receiptdesk.store import connect

def test_demo(tmp_path):
    path=demo(tmp_path);data=json.loads(path.read_text(encoding='utf-8'))
    assert data['decision'] and data['source'] and data['event']
    assert data['decision'][0]['status']=='effective'
    assert path.with_suffix('.md').exists()

def test_idempotency_and_conflict(tmp_path):
    base=Path(__file__).parent.parent/'fixtures';db=tmp_path/'x.db'
    freeze_config(db,base/'config.json')
    assert import_file(db,base/'events.jsonl')['imported']>0
    assert import_file(db,base/'events.jsonl')['duplicate_file']
    first=json.loads((base/'events.jsonl').read_text(encoding='utf-8').splitlines()[0]);first['kind']='changed'
    p=tmp_path/'conflict.jsonl';p.write_text(json.dumps(first),encoding='utf-8')
    import_file(db,p)
    con=connect(db)
    assert con.execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0]==1

def test_missing_evidence(tmp_path):
    base=Path(__file__).parent.parent/'fixtures';db=tmp_path/'x.db'
    freeze_config(db,base/'config.json')
    result=evaluate(db)
    assert 'effective' not in result.values()

def test_configuration_is_frozen(tmp_path):
    base=Path(__file__).parent.parent/'fixtures';db=tmp_path/'x.db'
    freeze_config(db,base/'config.json')
    bad=tmp_path/'other.json';bad.write_text('{}')
    import pytest
    with pytest.raises(ValueError,match='frozen_config_conflict'):freeze_config(db,bad)

def test_original_bytes_and_bad_line_are_preserved(tmp_path):
    base=Path(__file__).parent.parent/'fixtures';db=tmp_path/'x.db'
    freeze_config(db,base/'config.json')
    p=tmp_path/'broken.jsonl'
    raw=b'{broken-json\n'
    p.write_bytes(raw)
    import_file(db,p)
    con=connect(db)
    assert con.execute('SELECT raw FROM source').fetchone()[0]==raw
    detail=json.loads(con.execute("SELECT detail FROM issue WHERE code='parse_error'").fetchone()[0])
    assert detail['raw_line']=='{broken-json'
