import json
from pathlib import Path

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.rules import evaluate
from receiptdesk.store import connect


BASE = Path(__file__).parent.parent / 'fixtures'


def fixture():
    return [json.loads(line) for line in (BASE / 'events.jsonl').read_text().splitlines()]


def _write_config(tmp_path, name, contract):
    cfg = json.loads((BASE / 'config.json').read_text())
    if contract:
        cfg['adjudication'] = contract
    path = tmp_path / f'{name}.json'
    path.write_text(json.dumps(cfg))
    return path


def _read_case(db):
    result = evaluate(db)
    issues = {r['code'] for r in connect(db).execute('SELECT code FROM issue')}
    details = json.loads(connect(db).execute('SELECT details FROM decision').fetchone()['details'])
    return result, issues, details


def run_case(tmp_path, events, contract=None, name='case'):
    for event in events:
        event.setdefault('channel','email-v1')
    db = tmp_path / f'{name}.db'
    freeze_config(db, _write_config(tmp_path, name, contract))
    path = tmp_path / f'{name}.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, path)
    return _read_case(db)


def run_case_files(tmp_path, files, contract=None, name='case'):
    for chunk in files:
        for event in chunk:
            event.setdefault('channel','email-v1')
    db = tmp_path / f'{name}.db'
    freeze_config(db, _write_config(tmp_path, name, contract))
    for index, chunk in enumerate(files):
        path = tmp_path / f'{name}-{index}.jsonl'
        path.write_text('\n'.join(json.dumps(e) for e in chunk) + '\n')
        import_file(db, path)
    return _read_case(db)


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


def conflict_events():
    # 同一 relay 的接收序号先 receipt 后 revoke，但 relay 设备时钟把 receipt 标到 revoke 之后
    return [
        {'kind':'queued','subject':'R1','identity':'M1:1','time':'2026-01-01T08:00:00Z','message_id':'M1','attempt':1,'device':'relay-1','seq':40,'relay':'relay-1','rseq':900},
        {'kind':'sent','subject':'R1','identity':'M1:2','time':'2026-01-01T08:05:00Z','message_id':'M1','attempt':2,'device':'relay-1','seq':41,'relay':'relay-1','rseq':901},
        {'kind':'receipt','subject':'R1','identity':'M1:3','time':'2026-01-01T09:05:00Z','message_id':'M1','attempt':3,'device':'relay-1','seq':42,'relay':'relay-1','rseq':902},
        {'kind':'revoke','subject':'R1','identity':'M1:4','time':'2026-01-01T09:00:00Z','message_id':'M1','attempt':4,'device':'admin-1','seq':7,'relay':'relay-1','rseq':903},
    ]


def test_event_time_contract_rules_revoke_before_receipt(tmp_path):
    result, issues, details = run_case(tmp_path, conflict_events(), contract='event_time')
    assert result['R1'] == 'undelivered'
    assert 'revoked_before_delivery' in issues
    assert 'causal_contradiction' in issues
    assert details['contract'] == 'event_time'
    adj = details['adjudications'][0]
    assert adj['winner'] == 'revoke' and adj['reason'] == 'event_time'
    pair = {p['kind']: p for p in adj['pair']}
    assert pair['receipt']['time'] == '2026-01-01T09:05:00Z'
    assert pair['revoke']['time'] == '2026-01-01T09:00:00Z'
    assert pair['receipt']['device'] == 'relay-1' and pair['receipt']['seq'] == 42
    assert pair['receipt']['rseq'] == 902 and pair['revoke']['rseq'] == 903
    assert pair['receipt']['attempt'] == 3 and pair['revoke']['attempt'] == 4
    assert pair['receipt']['source_id'] == 1 and pair['receipt']['line_no'] == 3


def test_relay_seq_contract_rules_receipt_before_revoke(tmp_path):
    result, issues, details = run_case(tmp_path, conflict_events(), contract='relay_seq')
    assert result['R1'] == 'effective'
    assert 'revoked_after_delivery' in issues
    assert 'causal_contradiction' in issues
    assert details['contract'] == 'relay_seq'
    assert details['messages']['M1'] == 'receipt'
    adj = details['adjudications'][0]
    assert adj['winner'] == 'receipt' and adj['reason'] == 'relay_seq'
    kept = {t['kind'] for t in details['terminals']['M1']}
    assert kept == {'receipt', 'revoke'}


def test_clock_drift_is_insufficient_under_both_contracts(tmp_path):
    # M0 与 M1 各自链内时间正常，但 relay-1 的设备时钟随序号推进发生回退
    events = conflict_events() + [
        {'kind':'queued','subject':'R1','identity':'M0:1','time':'2026-01-01T08:50:00Z','message_id':'M0','attempt':1,'device':'relay-1','seq':38,'relay':'relay-1','rseq':898},
        {'kind':'sent','subject':'R1','identity':'M0:2','time':'2026-01-01T08:55:00Z','message_id':'M0','attempt':2,'device':'relay-1','seq':39,'relay':'relay-1','rseq':899},
    ]
    result, issues, details = run_case(tmp_path, [dict(e) for e in events], contract='event_time', name='a')
    assert result['R1'] == 'insufficient'
    assert 'clock_drift' in issues
    assert details['adjudications'][0]['winner'] is None
    result, issues, _ = run_case(tmp_path, [dict(e) for e in events], contract='relay_seq', name='b')
    assert result['R1'] == 'insufficient'
    assert 'clock_drift' in issues


def test_equal_event_times_cannot_order_terminals(tmp_path):
    events = conflict_events()
    events[3]['time'] = '2026-01-01T09:05:00Z'
    result, issues, details = run_case(tmp_path, events, contract='event_time')
    assert result['R1'] == 'insufficient'
    assert 'clock_conflict' in issues
    assert details['adjudications'][0]['winner'] is None


def test_sequence_gap_is_pending_review(tmp_path):
    events = conflict_events()
    events[3]['rseq'] = 904
    result, issues, details = run_case(tmp_path, [dict(e) for e in events], contract='relay_seq', name='gap')
    assert result['R1'] == 'insufficient'
    assert 'sequence_gap' in issues
    assert details['adjudications'][0]['winner'] is None
    result, issues, _ = run_case(tmp_path, [dict(e) for e in events], contract='event_time', name='tim')
    assert result['R1'] == 'undelivered'
    assert 'sequence_gap' not in issues


def test_receipt_referencing_wrong_attempt_is_insufficient(tmp_path):
    events = fixture()
    events[2]['attempt'] = 5
    result, issues, _ = run_case(tmp_path, events, name='gap')
    assert result['R1'] == 'insufficient'
    assert 'attempt_gap' in issues
    events = fixture()
    events[2]['attempt'] = 2
    result, issues, _ = run_case(tmp_path, events, name='dup')
    assert result['R1'] == 'insufficient'
    assert 'attempt_conflict' in issues


def test_duplicate_terminal_is_insufficient(tmp_path):
    events = fixture()
    events.append({'kind':'bounced','subject':'R1','identity':'M1:4',
        'time':'2026-01-01T08:20:00Z','message_id':'M1','attempt':4})
    result, issues, details = run_case(tmp_path, events)
    assert result['R1'] == 'insufficient'
    assert 'duplicate_terminal' in issues
    assert details['messages']['M1'] == 'contested'


def test_revoke_crossing_resend_boundary_is_insufficient(tmp_path):
    events = [
        {'kind':'queued','subject':'R1','identity':'M1:1','time':'2026-01-01T08:00:00Z','message_id':'M1','attempt':1},
        {'kind':'sent','subject':'R1','identity':'M1:2','time':'2026-01-01T08:05:00Z','message_id':'M1','attempt':2},
        {'kind':'queued','subject':'R1','identity':'M2:1','time':'2026-01-01T08:10:00Z','message_id':'M2','attempt':1,'resends':'M1'},
        {'kind':'sent','subject':'R1','identity':'M2:2','time':'2026-01-01T08:15:00Z','message_id':'M2','attempt':2},
        {'kind':'receipt','subject':'R1','identity':'M2:3','time':'2026-01-01T08:20:00Z','message_id':'M2','attempt':3},
        {'kind':'revoke','subject':'R1','identity':'M1:3','time':'2026-01-01T08:25:00Z','message_id':'M1','attempt':3},
    ]
    result, issues, _ = run_case(tmp_path, events)
    assert result['R1'] == 'insufficient'
    assert 'revoke_crosses_resend' in issues


def test_late_arriving_receipt_belongs_to_original_attempt(tmp_path):
    first = [
        {'kind':'queued','subject':'R1','identity':'M1:1','time':'2026-01-01T08:00:00Z','message_id':'M1','attempt':1},
        {'kind':'sent','subject':'R1','identity':'M1:2','time':'2026-01-01T08:05:00Z','message_id':'M1','attempt':2},
        {'kind':'bounced','subject':'R1','identity':'M1:3','time':'2026-01-01T08:15:00Z','message_id':'M1','attempt':3},
        {'kind':'queued','subject':'R1','identity':'M2:1','time':'2026-01-01T08:20:00Z','message_id':'M2','attempt':1,'resends':'M1'},
        {'kind':'sent','subject':'R1','identity':'M2:2','time':'2026-01-01T08:25:00Z','message_id':'M2','attempt':2},
        {'kind':'receipt','subject':'R1','identity':'M2:3','time':'2026-01-01T08:30:00Z','message_id':'M2','attempt':3},
    ]
    late = [{'kind':'receipt','subject':'R1','identity':'M1:4','time':'2026-01-01T08:18:00Z','message_id':'M1','attempt':4}]
    result, issues, details = run_case_files(tmp_path, [first, late], name='split')
    assert result['R1'] == 'effective'
    assert 'hard_bounce_history' in issues
    assert 'duplicate_terminal' in issues
    assert details['messages']['M1'] == 'contested'
    assert details['messages']['M2'] == 'receipt'
    assert {t['kind'] for t in details['terminals']['M1']} == {'bounced', 'receipt'}
    assert [t['kind'] for t in details['terminals']['M2']] == ['receipt']
    same, _, details2 = run_case(tmp_path, first + late, name='joined')
    assert same['R1'] == 'effective'
    assert details2['messages'] == details['messages']


def test_unrelated_recipient_isolated_from_adjudication(tmp_path):
    events = conflict_events() + [
        {'kind':'queued','subject':'R-X','identity':'MX:1','time':'2026-01-01T08:00:00Z','message_id':'MX','attempt':1},
        {'kind':'receipt','subject':'R-X','identity':'MX:2','time':'2026-01-01T09:05:00Z','message_id':'MX','attempt':2},
        {'kind':'revoke','subject':'R-X','identity':'MX:3','time':'2026-01-01T09:00:00Z','message_id':'MX','attempt':3},
    ]
    result, issues, details = run_case(tmp_path, events, contract='event_time')
    assert result['R1'] == 'undelivered'
    assert 'R-X' not in result
    assert 'unknown_recipient' in issues
    assert details['adjudications'][0]['winner'] == 'revoke'


def test_unknown_adjudication_contract_is_rejected(tmp_path):
    import pytest
    cfg = json.loads((BASE / 'config.json').read_text())
    cfg['adjudication'] = 'guess'
    p = tmp_path / 'bad-contract.json'
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='unknown_adjudication_contract'):
        freeze_config(tmp_path / 'bad.db', p)
