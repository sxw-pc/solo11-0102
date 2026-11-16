import json
from pathlib import Path

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.rules import evaluate
from receiptdesk.store import connect


BASE = Path(__file__).parent.parent / 'fixtures'


def fixture():
    return [json.loads(line) for line in (BASE / 'events.jsonl').read_text().splitlines()]


def run_case(tmp_path, events):
    for event in events:
        event.setdefault('channel','email-v1')
    db = tmp_path / 'case.db'
    freeze_config(db, BASE / 'config.json')
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, path)
    result = evaluate(db)
    issues = {r['code'] for r in connect(db).execute('SELECT code FROM issue')}
    details = json.loads(connect(db).execute('SELECT details FROM decision').fetchone()['details'])
    return result, issues, details


def test_alias_is_scoped_to_frozen_roster(tmp_path):
    events = fixture()
    for e in events:
        e['subject'] = 'r1-alias@example.invalid'
    result, issues, _ = run_case(tmp_path, events)
    assert result['R1'] == 'effective'
    assert 'unknown_recipient' not in issues


def test_late_receipt_is_undelivered(tmp_path):
    events = fixture()
    events[-1]['time'] = '2026-01-03T00:00:00Z'
    result, issues, _ = run_case(tmp_path, events)
    assert result['R1'] == 'undelivered'
    assert 'late_delivery' in issues


def test_illegal_transition_cannot_establish_delivery(tmp_path):
    events = fixture()
    events[1]['kind'] = 'receipt'
    events.pop()
    result, issues, _ = run_case(tmp_path, events)
    assert result['R1'] == 'insufficient'
    assert 'illegal_transition' in issues


def test_bounce_history_survives_resend(tmp_path):
    events = fixture()[:2]
    events.append({'kind': 'bounced', 'subject': 'R1', 'identity': 'M1:3',
                   'time': '2026-01-01T08:15:00Z', 'message_id': 'M1'})
    events.extend([
        {'kind': 'queued', 'subject': 'R1', 'identity': 'M2:1', 'time': '2026-01-01T08:20:00Z', 'message_id': 'M2', 'resends': 'M1'},
        {'kind': 'sent', 'subject': 'R1', 'identity': 'M2:2', 'time': '2026-01-01T08:25:00Z', 'message_id': 'M2'},
        {'kind': 'receipt', 'subject': 'R1', 'identity': 'M2:3', 'time': '2026-01-01T08:30:00Z', 'message_id': 'M2'},
    ])
    result, issues, details = run_case(tmp_path, events)
    assert result['R1'] == 'effective'
    assert 'hard_bounce_history' in issues
    assert details['messages']['M1'] == 'bounced'


def test_unknown_resend_origin_is_flagged(tmp_path):
    events = fixture()
    events[0]['resends'] = 'unknown'
    _, issues, _ = run_case(tmp_path, events)
    assert 'unknown_original' in issues


def test_channel_version_mismatch_is_not_effective(tmp_path):
    events = fixture()
    events[-1]['channel'] = 'email-v2'
    result, issues, _ = run_case(tmp_path, events)
    assert result['R1'] == 'insufficient'
    assert 'channel_mismatch' in issues


def test_missing_queue_does_not_prove_delivery(tmp_path):
    events=fixture()[1:]
    result,issues,_=run_case(tmp_path,events)
    assert result['R1']=='insufficient'
    assert 'missing_queue' in issues


def test_revoke_before_receipt_is_insufficient(tmp_path):
    events=fixture()[:2]
    events.append({'kind':'revoke','subject':'R1','identity':'M1:3',
        'time':'2026-01-01T08:15:00Z','message_id':'M1'})
    result,_,_=run_case(tmp_path,events)
    assert result['R1']=='insufficient'


def test_unknown_recipient_is_isolated(tmp_path):
    events=fixture()
    events.append({'kind':'queued','subject':'R-X','identity':'M2:1',
        'time':'2026-01-01T08:20:00Z','message_id':'M2'})
    result,issues,_=run_case(tmp_path,events)
    assert result['R1']=='effective'
    assert 'unknown_recipient' in issues


def test_ambiguous_alias_contract_is_rejected(tmp_path):
    import pytest
    cfg=json.loads((BASE/'config.json').read_text())
    cfg['recipients'].append({'id':'R2','channel':'email-v1','aliases':['r1-alias@example.invalid']})
    p=tmp_path/'bad.json';p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='ambiguous_alias'):
        freeze_config(tmp_path/'bad.db',p)


def test_deadline_exactly_is_timely(tmp_path):
    events=fixture();events[-1]['time']='2026-01-02T00:00:00Z'
    result,issues,_=run_case(tmp_path,events)
    assert result['R1']=='effective'
    assert 'late_delivery' not in issues


def test_report_contains_frozen_roster_and_evidence(tmp_path):
    from receiptdesk.report import export
    run_case(tmp_path,fixture())
    data=json.loads(export(tmp_path/'case.db',tmp_path/'report.json').read_text())
    assert data['batch'] and data['subject'] and data['source'] and data['event']
    assert data['decision'] and data['evidence'] and data['run']
    assert len(data['evidence'])==len(fixture())
