import hashlib
import itertools
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import receiptdesk.report as report
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.report import (disclosure_ledger, export,
                                forced_individual_channels, publish_disclosure,
                                recover_disclosures)
from receiptdesk.rules import evaluate
from receiptdesk.store import connect

BASE = Path(__file__).parent.parent / 'fixtures'
METRIC = 'email-v2_recipient_count'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def recipients(channels):
    return [{'id': subject, 'channel': channel,
             'aliases': [f'{subject.lower()}@example.invalid']}
            for subject, channel in channels.items()]


def make_db(tmp_path, channels, with_events=False, name='ledger.db'):
    config = tmp_path / f'{name}.config.json'
    config.write_text(json.dumps({'deadline': '2026-01-02T00:00:00Z',
                                  'recipients': recipients(channels)}), encoding='utf-8')
    db = tmp_path / name
    freeze_config(db, config)
    if with_events:
        path = tmp_path / f'{name}.events.jsonl'
        path.write_text('\n'.join(json.dumps({
            'kind': 'queued', 'subject': subject, 'identity': f'{subject}:1',
            'time': '2026-01-01T08:00:00Z', 'message_id': subject,
            'attempt': 1, 'address': f'{subject.lower()}@example.invalid',
            'channel': channel,
        }) for subject, channel in channels.items()) + '\n', encoding='utf-8')
        import_file(db, path)
    return db


def fixture_db(tmp_path):
    db = tmp_path / 'ledger.db'
    freeze_config(db, BASE / 'channel_disclosure_config.json')
    import_file(db, BASE / 'channel_disclosure_events.jsonl')
    return db


def fixture_request():
    return json.loads((BASE / 'channel_disclosure_requests.json').read_text(encoding='utf-8'))


def request_doc(audience, universe, request_id, cohort, previous=None,
                expansion=(), policies=('deny_unsafe',)):
    doc = {
        'audience': audience,
        'metric': METRIC,
        'universe': list(universe),
        'new_request': {'id': request_id, 'recipients': list(cohort)},
        'allowed_expansion': list(expansion),
        'policies': list(policies),
    }
    if previous is not None:
        doc['previous_release'] = previous
    return doc


def out_dir(db):
    return Path(db).resolve().parent / 'disclosures'


def external_files(db, audience):
    folder = out_dir(db) / audience
    if not folder.exists():
        return []
    return sorted(path for path in folder.iterdir() if path.is_file())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_no_personal_fields(path):
    record = json.loads(Path(path).read_text(encoding='utf-8'))
    # The external contract: actual cohort, v2 count, batch/config version and ID.
    assert set(record) == {'release_id', 'cohort', 'count', 'batch_id', 'config_hash'}
    assert not any(str(value).lower().endswith('@example.invalid')
                   for value in record['cohort'])
    raw = Path(path).read_text(encoding='utf-8')
    for forbidden in ('address', 'alias', 'payload', 'source', 'receipt', 'revoke',
                      'attempt', 'email-v1', '@example.invalid', 'subject', 'identity'):
        assert forbidden not in raw
    assert '"channel"' not in raw
    return record


def release_rows(db, audience):
    return disclosure_ledger(db, audience)['releases']


def attempt_rows(db, audience):
    return disclosure_ledger(db, audience)['attempts']


# ---------------------------------------------------------------------------
# Fixture scenario: deny vs minimal coarsening
# ---------------------------------------------------------------------------

def test_fixture_deny_unsafe_blocks_req02_and_keeps_prior_release(tmp_path):
    db = fixture_db(tmp_path)
    result = publish_disclosure(db, fixture_request(), 'deny_unsafe',
                                now='2026-01-03T00:00:00Z')
    assert result['status'] == 'denied'
    assert result['reason'] == 'individual_channel_inferable'
    # The refusal response itself is not a disclosure channel either.
    assert 'identifiable' not in result and 'count' not in result and 'cohort' not in result
    assert result['file'] is None
    attempts = attempt_rows(db, 'external-partner')
    assert attempts[0]['status'] == 'denied'
    assert attempts[0]['identifiable'] == {'R3': 1}

    rows = release_rows(db, 'external-partner')
    assert [row['release_id'] for row in rows] == ['REL-01']
    assert rows[0]['cohort'] == ['R1', 'R2'] and rows[0]['recipient_count'] == 1
    # No file for the refused three-person cohort, but the prior release is real.
    files = external_files(db, 'external-partner')
    assert len(files) == 1
    assert_no_personal_fields(files[0])
    assert json.loads(files[0].read_text())['release_id'] == 'REL-01'
    assert all('R3' not in json.loads(path.read_text())['cohort'] for path in files)


def test_fixture_minimum_coarsening_publishes_actual_four_person_set(tmp_path):
    db = fixture_db(tmp_path)
    result = publish_disclosure(db, fixture_request(), 'minimum_safe_coarsening',
                                now='2026-01-03T00:00:00Z')
    assert result['status'] == 'published'
    assert result['requested_cohort'] == ['R1', 'R2', 'R3']
    assert result['cohort'] == ['R1', 'R2', 'R3', 'R4']
    assert result['count'] == 2

    record = assert_no_personal_fields(result['file'])
    assert record['count'] == 2 and record['cohort'] == ['R1', 'R2', 'R3', 'R4']
    assert record['batch_id'] == 'batch-1'
    assert record['release_id'] == 'REL-REQ-02'

    rows = {row['release_id']: row for row in release_rows(db, 'external-partner')}
    assert rows['REL-REQ-02']['cohort'] == ['R1', 'R2', 'R3', 'R4']
    assert rows['REL-REQ-02']['requested_cohort'] == ['R1', 'R2', 'R3']
    assert rows['REL-REQ-02']['file_sha256'] == digest(result['file'])

    # The external view only knows one of R3/R4 uses v2; no individual is forced.
    facts = [(row['cohort'], row['recipient_count'])
             for row in release_rows(db, 'external-partner')]
    assert forced_individual_channels(facts) == {}


def test_minimum_coarsening_refuses_when_expansion_is_insufficient(tmp_path):
    db = fixture_db(tmp_path)
    doc = fixture_request()
    doc['allowed_expansion'] = []
    result = publish_disclosure(db, doc, 'minimum_safe_coarsening')
    assert result['status'] == 'denied'
    assert result['reason'] == 'no_safe_coarsening'
    assert 'cohort' not in result and 'count' not in result
    assert 'REL-REQ-02' not in [row['release_id'] for row in release_rows(db, 'external-partner')]
    # The prior release is still known, but no unsafe new file exists.
    assert [row['release_id'] for row in release_rows(db, 'external-partner')] == ['REL-01']
    assert len(external_files(db, 'external-partner')) == 1


def test_minimum_coarsening_can_rescue_an_all_zero_group(tmp_path):
    db = make_db(tmp_path, {'A': 'email-v1', 'B': 'email-v1', 'C': 'email-v2'})
    doc = request_doc('partner', ['A', 'B', 'C'], 'REQ-Z', ['A', 'B'],
                      expansion=['C'], policies=('deny_unsafe', 'minimum_safe_coarsening'))
    denied = publish_disclosure(db, doc, 'deny_unsafe')
    assert denied['status'] == 'denied'
    assert attempt_rows(db, 'partner')[0]['identifiable'] == {'A': 0, 'B': 0}

    result = publish_disclosure(db, doc, 'minimum_safe_coarsening')
    assert result['status'] == 'published'
    assert result['cohort'] == ['A', 'B', 'C'] and result['count'] == 1
    assert_no_personal_fields(result['file'])


# ---------------------------------------------------------------------------
# Inference across three or more overlapping groups, extreme counts
# ---------------------------------------------------------------------------

def test_three_overlapping_groups_only_force_in_combination(tmp_path):
    channels = {'R1': 'email-v2', 'R2': 'email-v1', 'R3': 'email-v1',
                'R4': 'email-v1', 'R5': 'email-v2'}
    db = make_db(tmp_path, channels)
    universe = list(channels)
    history_requests = [
        request_doc('aud', universe, 'REQ-A', ['R1', 'R2']),
        request_doc('aud', universe, 'REQ-B', ['R1', 'R3']),
        request_doc('aud', universe, 'REQ-C', ['R1', 'R4']),
    ]
    for doc in history_requests:
        assert publish_disclosure(db, doc, 'deny_unsafe')['status'] == 'published'

    facts = [(row['cohort'], row['recipient_count'])
             for row in release_rows(db, 'aud')]
    assert len(facts) == 3
    # Every single fact and every pair of facts is individually safe...
    for size in (1, 2):
        for subset in itertools.combinations(facts, size):
            assert forced_individual_channels(list(subset)) == {}
    # ...but all three plus the new total pin every channel down.
    forced = forced_individual_channels(facts + [(['R1', 'R2', 'R3', 'R4', 'R5'], 2)])
    assert forced == {'R1': 1, 'R2': 0, 'R3': 0, 'R4': 0, 'R5': 1}

    final = request_doc('aud', universe, 'REQ-D', ['R1', 'R2', 'R3', 'R4', 'R5'])
    result = publish_disclosure(db, final, 'deny_unsafe')
    assert result['status'] == 'denied'
    assert attempt_rows(db, 'aud')[-1]['identifiable'] == forced
    assert len(release_rows(db, 'aud')) == 3
    assert len(external_files(db, 'aud')) == 3


def test_all_zero_and_all_one_groups_are_refused(tmp_path):
    channels = {'R1': 'email-v1', 'R2': 'email-v1',
                'R3': 'email-v2', 'R4': 'email-v2'}
    db = make_db(tmp_path, channels)
    universe = list(channels)

    zero = request_doc('zero', universe, 'REQ-ZERO', ['R1', 'R2'])
    result = publish_disclosure(db, zero, 'deny_unsafe')
    assert result['status'] == 'denied'
    assert attempt_rows(db, 'zero')[0]['identifiable'] == {'R1': 0, 'R2': 0}

    one = request_doc('one', universe, 'REQ-ONE', ['R3', 'R4'])
    result = publish_disclosure(db, one, 'deny_unsafe')
    assert result['status'] == 'denied'
    assert attempt_rows(db, 'one')[0]['identifiable'] == {'R3': 1, 'R4': 1}

    # Denied attempts are not visible facts, so a genuinely mixed group is fine.
    mixed = request_doc('zero', universe, 'REQ-MIXED', ['R1', 'R2', 'R3'])
    result = publish_disclosure(db, mixed, 'deny_unsafe')
    assert result['status'] == 'published'
    assert result['count'] == 1
    assert result['cohort'] == ['R1', 'R2', 'R3']


def test_singleton_cohort_cannot_disclose_a_personal_channel(tmp_path):
    db = make_db(tmp_path, {'R1': 'email-v2', 'R2': 'email-v1'})
    doc = request_doc('aud', ['R1', 'R2'], 'REQ-SINGLE', ['R1'])
    result = publish_disclosure(db, doc, 'deny_unsafe')
    assert result['status'] == 'denied'
    assert attempt_rows(db, 'aud')[0]['identifiable'] == {'R1': 1}
    assert external_files(db, 'aud') == []


def test_safety_check_handles_inconsistent_and_extreme_history():
    assert forced_individual_channels([]) == {}
    assert forced_individual_channels([(['A', 'B'], 0)]) == {'A': 0, 'B': 0}
    assert forced_individual_channels([(['A', 'B'], 2)]) == {'A': 1, 'B': 1}
    assert forced_individual_channels([(['A', 'B'], 1)]) == {}
    # Contradictory visible facts can never certify safety.
    inconsistent = forced_individual_channels([(['A', 'B'], 1), (['A', 'B'], 2)])
    assert inconsistent and all(value is None for value in inconsistent.values())


# ---------------------------------------------------------------------------
# Audience isolation and denial visibility
# ---------------------------------------------------------------------------

def test_audiences_and_denied_requests_are_isolated(tmp_path):
    db = fixture_db(tmp_path)
    denied = publish_disclosure(db, fixture_request(), 'deny_unsafe')
    assert denied['status'] == 'denied'

    # Another audience has none of external-partner's facts, so the same
    # three-person cohort is safe there.
    other = request_doc('other-partner', ['R1', 'R2', 'R3', 'R4'], 'REQ-OTHER',
                        ['R1', 'R2', 'R3'])
    published = publish_disclosure(db, other, 'deny_unsafe')
    assert published['status'] == 'published'
    assert published['count'] == 2
    assert_no_personal_fields(published['file'])

    assert [row['release_id'] for row in release_rows(db, 'other-partner')] == ['REL-REQ-OTHER']
    assert 'REL-REQ-OTHER' not in [row['release_id'] for row in release_rows(db, 'external-partner')]
    assert 'REL-01' not in [row['release_id'] for row in release_rows(db, 'other-partner')]
    assert all('REL-REQ-OTHER' not in path.read_text()
               for path in external_files(db, 'external-partner'))

    # The refused REQ-02 must not become a visible fact for external-partner.
    # If it did, R3 would be pinned and the following group would be refused.
    follow = request_doc('external-partner', ['R1', 'R2', 'R3', 'R4'],
                         'REQ-FOLLOW', ['R3', 'R4'])
    follow_result = publish_disclosure(db, follow, 'deny_unsafe')
    assert follow_result['status'] == 'published'
    assert follow_result['count'] == 1

    partner_attempts = attempt_rows(db, 'external-partner')
    assert any(row['request_id'] == 'REQ-02' and row['status'] == 'denied'
               for row in partner_attempts)
    assert all(row['release_id'] != 'REQ-02' for row in release_rows(db, 'external-partner'))


# ---------------------------------------------------------------------------
# Replay and concurrency
# ---------------------------------------------------------------------------

def test_same_request_id_replay_does_not_disclose_twice(tmp_path):
    db = fixture_db(tmp_path)
    first = publish_disclosure(db, fixture_request(), 'minimum_safe_coarsening',
                               now='2026-01-03T00:00:00Z')
    original = Path(first['file'])
    original_bytes = original.read_bytes()
    original_mtime = original.stat().st_mtime_ns

    second = publish_disclosure(db, fixture_request(), 'minimum_safe_coarsening',
                                now='2026-01-04T00:00:00Z')
    assert second['status'] == 'replayed'
    assert second['release_id'] == first['release_id']
    assert second['file'] == first['file']
    assert second['file_sha256'] == first['file_sha256']
    assert original.read_bytes() == original_bytes
    assert original.stat().st_mtime_ns == original_mtime

    req_rows = [row for row in release_rows(db, 'external-partner')
                if row['request_id'] == 'REQ-02']
    assert len(req_rows) == 1
    assert len([row for row in attempt_rows(db, 'external-partner')
                if row['request_id'] == 'REQ-02']) == 1
    assert len(external_files(db, 'external-partner')) == 2


def test_denied_request_replay_is_not_recorded_twice(tmp_path):
    db = fixture_db(tmp_path)
    first = publish_disclosure(db, fixture_request(), 'deny_unsafe')
    second = publish_disclosure(db, fixture_request(), 'deny_unsafe')
    assert first['status'] == second['status'] == 'denied'
    assert len([row for row in attempt_rows(db, 'external-partner')
                if row['request_id'] == 'REQ-02']) == 1

    # Reusing a request ID for a different cohort is a conflict, not a replay.
    changed = fixture_request()
    changed['new_request']['recipients'] = ['R1', 'R2']
    with pytest.raises(ValueError, match='request_id_conflict'):
        publish_disclosure(db, changed, 'deny_unsafe')


def test_concurrent_requests_are_verified_serially(tmp_path):
    # Two requests are safe on their own; together they pin C3 to email-v1.
    db = make_db(tmp_path, {'C1': 'email-v2', 'C2': 'email-v1', 'C3': 'email-v1'})
    universe = ['C1', 'C2', 'C3']
    docs = [
        request_doc('partner-x', universe, 'REQ-CONCURRENT-A', ['C1', 'C2']),
        request_doc('partner-x', universe, 'REQ-CONCURRENT-B', ['C1', 'C2', 'C3']),
    ]
    # Both are individually safe before either is published.
    for doc in docs:
        facts = [(doc['new_request']['recipients'],
                  sum(1 for member in doc['new_request']['recipients']
                      if member in ('C1',)))]
        assert forced_individual_channels(facts) == {}

    barrier = threading.Barrier(len(docs))

    def run(doc):
        barrier.wait(timeout=10)
        return publish_disclosure(db, doc, 'deny_unsafe')

    with ThreadPoolExecutor(max_workers=len(docs)) as pool:
        results = [future.result(timeout=60) for future in
                   [pool.submit(run, doc) for doc in docs]]

    statuses = sorted(result['status'] for result in results)
    assert statuses == ['denied', 'published']
    denied = next(result for result in results if result['status'] == 'denied')
    assert denied['reason'] == 'individual_channel_inferable'

    rows = release_rows(db, 'partner-x')
    assert len(rows) == 1 and rows[0]['status'] == 'published'
    assert len(external_files(db, 'partner-x')) == 1
    assert_no_personal_fields(external_files(db, 'partner-x')[0])
    # The single delivered summary is exposed in the ledger exactly once.
    published = next(result for result in results if result['status'] == 'published')
    assert digest(published['file']) == rows[0]['file_sha256']


# ---------------------------------------------------------------------------
# Interrupted file / ledger publication and recovery
# ---------------------------------------------------------------------------

def test_crash_after_file_write_is_cleaned_up_by_recovery(tmp_path, monkeypatch):
    db = make_db(tmp_path, {'R1': 'email-v2', 'R2': 'email-v1', 'R3': 'email-v1'})
    doc = request_doc('aud', ['R1', 'R2', 'R3'], 'REQ-CRASH', ['R1', 'R2'])
    real_write = report._atomic_write

    def write_then_fail(path, data):
        real_write(path, data)
        raise OSError('simulated power loss')

    with monkeypatch.context() as patch:
        patch.setattr(report, '_atomic_write', write_then_fail)
        with pytest.raises(OSError):
            publish_disclosure(db, doc, 'deny_unsafe')

    # A file was left behind, but no ledger row was committed.
    assert external_files(db, 'aud')
    assert release_rows(db, 'aud') == []
    assert attempt_rows(db, 'aud') == []

    summary = recover_disclosures(db)
    assert summary['removed_orphans'] >= 1
    assert external_files(db, 'aud') == []
    assert release_rows(db, 'aud') == []

    # The same request can be published cleanly afterwards.
    result = publish_disclosure(db, doc, 'deny_unsafe')
    assert result['status'] == 'published'
    assert len(release_rows(db, 'aud')) == 1
    assert digest(result['file']) == result['file_sha256']


def test_recovery_keeps_published_row_when_file_lost(tmp_path):
    db = make_db(tmp_path, {'R1': 'email-v2', 'R2': 'email-v1'})
    doc = request_doc('aud', ['R1', 'R2'], 'REQ-LOST', ['R1', 'R2'])
    published = publish_disclosure(db, doc, 'deny_unsafe')
    Path(published['file']).unlink()

    summary = recover_disclosures(db)
    assert summary['repaired'] == 1
    row = release_rows(db, 'aud')[0]
    assert Path(row['file_path']).exists()
    assert digest(row['file_path']) == row['file_sha256']
    assert assert_no_personal_fields(row['file_path'])['cohort'] == ['R1', 'R2']


def test_unverified_previous_release_cannot_poison_the_safety_proof(tmp_path):
    db = fixture_db(tmp_path)
    doc = fixture_request()
    # A crafted "history" that would make the new group look safe/count wrong.
    doc['previous_release']['count'] = 0
    with pytest.raises(ValueError, match='previous_release_count_mismatch'):
        publish_disclosure(db, doc, 'deny_unsafe')
    assert release_rows(db, 'external-partner') == []
    assert external_files(db, 'external-partner') == []

    doc = fixture_request()
    doc['previous_release'] = {'id': 'REL-X', 'recipients': ['R3'], 'count': 1}
    with pytest.raises(ValueError, match='previous_release_unsafe'):
        publish_disclosure(db, doc, 'deny_unsafe')
    assert release_rows(db, 'external-partner') == []


def test_recovery_completes_pending_row_and_repairs_file(tmp_path):
    db = make_db(tmp_path, {'R1': 'email-v2', 'R2': 'email-v1'})
    target = out_dir(db) / 'aud' / ('REL-PENDING-' + 'a' * 64 + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    # A stale/foreign file at the recorded path must be replaced.
    target.write_text('{"cohort":["R1"],"count":1}\n', encoding='utf-8')

    con = connect(db)
    con.execute(
        'INSERT INTO disclosure_release(audience,release_id,request_id,policy,status,'
        'requested_cohort,cohort,recipient_count,metric,batch_id,config_hash,file_path,'
        'file_sha256,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('aud', 'REL-PENDING', 'REQ-PENDING', 'minimum_safe_coarsening', 'pending',
         '["R1","R2"]', '["R1","R2"]', 1, METRIC, 'batch-1', 'cfg',
         str(target.resolve()), 'b' * 64, 'published',
         '2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00'))
    con.commit()
    con.close()

    summary = recover_disclosures(db)
    assert summary['repaired'] == 1
    row = release_rows(db, 'aud')[0]
    assert row['status'] == 'published'
    assert row['file_path'] == str(target.resolve())
    assert row['file_sha256'] == digest(target)
    record = assert_no_personal_fields(target)
    assert record['cohort'] == ['R1', 'R2'] and record['count'] == 1


# ---------------------------------------------------------------------------
# Internal report remains complete; disclosure does not depend on decisions
# ---------------------------------------------------------------------------

def test_internal_audit_report_stays_complete_and_disclosure_is_independent(tmp_path):
    db = fixture_db(tmp_path)
    evaluate(db)
    before = json.loads(Path(export(db, tmp_path / 'before.json')).read_text(encoding='utf-8'))
    assert before['event'] and before['decision'] and before['evidence']
    assert 'r3@example.invalid' in (tmp_path / 'before.json').read_text(encoding='utf-8')

    result = publish_disclosure(db, fixture_request(), 'minimum_safe_coarsening')
    assert result['status'] == 'published'

    after = json.loads(Path(export(db, tmp_path / 'after.json')).read_text(encoding='utf-8'))
    for table in ('batch', 'subject', 'event', 'issue', 'decision', 'evidence', 'run',
                  'source', 'config_hash'):
        assert after[table] == before[table]
    assert 'r1@example.invalid' in (tmp_path / 'after.json').read_text(encoding='utf-8')

    # The external file never carries the internal evidence.
    raw = Path(result['file']).read_text(encoding='utf-8')
    for leaked in ('r1@example.invalid', 'r2@example.invalid', 'r3@example.invalid',
                   'CM1', 'CM2', 'queued', 'email-v1'):
        assert leaked not in raw


def test_disclosure_counts_frozen_roster_not_delivery_events(tmp_path):
    # No events and therefore no delivery decisions exist at all.
    db = make_db(tmp_path, {'R1': 'email-v2', 'R2': 'email-v1', 'R3': 'email-v2'})
    con = connect(db)
    assert con.execute('SELECT count(*) FROM event').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM decision').fetchone()[0] == 0
    con.close()

    doc = request_doc('aud', ['R1', 'R2', 'R3'], 'REQ-NO-EVENTS', ['R1', 'R2'])
    result = publish_disclosure(db, doc, 'deny_unsafe')
    assert result['status'] == 'published'
    assert result['count'] == 1
    assert_no_personal_fields(result['file'])


def test_request_validation_rejects_out_of_contract_inputs(tmp_path):
    db = fixture_db(tmp_path)
    doc = fixture_request()
    doc['new_request']['recipients'] = ['R1', 'R9']
    with pytest.raises(ValueError, match='unknown_recipient'):
        publish_disclosure(db, doc, 'deny_unsafe')

    doc = fixture_request()
    doc['policies'] = ['deny_unsafe']
    with pytest.raises(ValueError, match='policy_not_permitted'):
        publish_disclosure(db, doc, 'minimum_safe_coarsening')
    with pytest.raises(ValueError, match='unknown_policy'):
        publish_disclosure(db, fixture_request(), 'not-a-policy')


def test_disclosure_demo_cli_helper(tmp_path):
    from receiptdesk.__main__ import disclosure_demo
    result, ledger = disclosure_demo(tmp_path)
    assert result['status'] == 'published'
    assert result['cohort'] == ['R1', 'R2', 'R3', 'R4']
    assert ledger['releases'] and ledger['attempts']
