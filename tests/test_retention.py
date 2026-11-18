"""Retention / erasure contract tests.

Covers the genuinely different active-database bytes produced by the two
contracts, R1 source provenance through the rebase, fail-closed handling of
unknown/bad/cross-subject lines, disappearance of R2 addresses from the
active library and reports, idempotent reruns, hold release, serialized
concurrency and transaction-interruption recovery.
"""
import json
import threading
from pathlib import Path

import pytest

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.report import export
from receiptdesk.store import connect, hash_bytes, masked_subjects, visible_events
from receiptdesk import retention

BASE = Path(__file__).parent.parent / 'fixtures'
CASE = json.loads((BASE / 'retention_case.json').read_text(encoding='utf-8'))
SOURCE_BYTES = (BASE / CASE['source_file']).read_bytes()
HOLD_PATH = BASE / CASE['hold_file']
HOLD_SHA = CASE['hold_sha256']
SOURCE_SHA = CASE['source_sha256']
R1_LINE = SOURCE_BYTES.splitlines(keepends=True)[0]
R2_ADDR = 'r2@example.invalid'
R1_ADDR = 'r1@example.invalid'
CRED_VERSION = 'credential-v1'


@pytest.fixture()
def db(tmp_path):
    d = tmp_path / 'case.db'
    freeze_config(d, BASE / 'retention_config.json')
    res = import_file(d, BASE / CASE['source_file'])
    assert res['imported'] == 2
    return d


def _hold(db, request_id='HOLD-R1-001'):
    return retention.submit_shared_hold(
        db, request_id=request_id, source_sha256=SOURCE_SHA,
        hold_subject=CASE['hold_recipient'], credential_path=HOLD_PATH,
        credential_sha256=HOLD_SHA, reason='R1 litigation hold')


def _erase(db, request_id='ERASE-R2-001', contract='subject_erasure_with_rebase'):
    return retention.request_erasure(
        db, request_id=request_id, source_sha256=SOURCE_SHA,
        subject=CASE['erase_recipient'], contract=contract,
        credential_version=CRED_VERSION, reason='R2 retention period expired')


def _active_blob(con):
    rows = con.execute('SELECT raw,sha256 FROM source ORDER BY id').fetchall()
    assert len(rows) == 1
    return rows[0]['raw'], rows[0]['sha256']


# ---------------------------------------------------------------- shared hold

def test_shared_hold_keeps_whole_blob_and_original_hash(db):
    out = _hold(db)
    assert out['status'] == 'active'
    con = connect(db)
    raw, sha = _active_blob(con)
    assert raw == SOURCE_BYTES
    assert sha == SOURCE_SHA
    req = con.execute("SELECT * FROM retention_request WHERE request_type='hold'").fetchone()
    assert req['hold_until'] == '2027-01-01'
    assert req['credential_sha256'] == HOLD_SHA
    con.close()


def test_erasure_under_active_hold_is_pending_not_pretended_done(db):
    _hold(db)
    out = _erase(db)
    assert out['status'] == 'pending'
    assert out['blocked_by_hold'] == 'HOLD-R1-001'
    con = connect(db)
    # The whole mixed blob, including R2 bytes, is still physically present.
    raw, sha = _active_blob(con)
    assert raw == SOURCE_BYTES and sha == SOURCE_SHA
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] == 2
    req = con.execute("SELECT * FROM retention_request WHERE id='ERASE-R2-001'").fetchone()
    assert req['status'] == 'pending'
    assert json.loads(req['details'])['blocked_by_hold'] == 'HOLD-R1-001'
    con.close()


def test_pending_erasure_masks_r2_on_normal_read_paths(db):
    _hold(db)
    _erase(db)
    con = connect(db)
    assert masked_subjects(con) == {'R2'}
    visible = visible_events(con)
    assert {r['subject'] for r in visible} == {'R1'}
    con.close()
    report = json.loads(export(db, db.parent / 'report.json').read_text(encoding='utf-8'))
    assert report['masked_subjects'] == ['R2']
    assert {e['subject'] for e in report['event']} == {'R1'}
    blob = json.dumps(report, ensure_ascii=False)
    assert R2_ADDR not in blob
    assert R1_ADDR in blob
    # The report's source section never exports raw blob bytes.
    assert all('raw' not in s for s in report['source'])


def test_release_hold_reprocesses_pending_erasure_for_real(db):
    _hold(db)
    _erase(db)
    out = retention.release_hold(db, request_id='HOLD-R1-001', reason='hold ended')
    assert len(out['processed']) == 1
    assert out['processed'][0]['status'] == 'completed'
    con = connect(db)
    raw, sha = _active_blob(con)
    assert raw == R1_LINE
    assert sha == hash_bytes(R1_LINE)
    assert R2_ADDR.encode() not in raw
    assert {r['subject'] for r in con.execute('SELECT subject FROM event')} == {'R1'}
    assert masked_subjects(con) == set()
    req = con.execute("SELECT * FROM retention_request WHERE id='ERASE-R2-001'").fetchone()
    assert req['status'] == 'completed'
    hold = con.execute("SELECT * FROM retention_request WHERE id='HOLD-R1-001'").fetchone()
    assert hold['status'] == 'released'
    con.close()


def test_release_hold_is_idempotent(db):
    _hold(db)
    _erase(db)
    first = retention.release_hold(db, request_id='HOLD-R1-001', reason='end')
    second = retention.release_hold(db, request_id='HOLD-R1-001', reason='end again')
    assert second['idempotent_rerun'] is True
    assert second['processed'] == []
    assert len(first['processed']) == 1
    con = connect(db)
    assert con.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    con.close()


def test_hold_credential_hash_must_match(db):
    with pytest.raises(retention.RetentionError, match='hold_credential_hash_mismatch'):
        retention.submit_shared_hold(
            db, request_id='H-BAD', source_sha256=SOURCE_SHA,
            hold_subject='R1', credential_path=HOLD_PATH,
            credential_sha256='0' * 64, reason='bad credential')


def test_hold_requires_attested_subject_event(db):
    with pytest.raises(retention.RetentionError, match='hold_subject_has_no_event'):
        retention.submit_shared_hold(
            db, request_id='H-RX', source_sha256=SOURCE_SHA,
            hold_subject='R-X', credential_path=HOLD_PATH,
            credential_sha256=HOLD_SHA, reason='unknown subject')


# ------------------------------------------------------------ rebase contract

def test_rebase_erasure_produces_real_r1_only_bytes(db):
    out = _erase(db)
    assert out['status'] == 'completed'
    assert out['old_sha256'] == SOURCE_SHA
    assert out['new_sha256'] == hash_bytes(R1_LINE)
    assert out['erased_line_nos'] == [2] and out['erased_event_ids']
    con = connect(db)
    raw, sha = _active_blob(con)
    assert raw == R1_LINE and sha == hash_bytes(R1_LINE)
    # Active library no longer contains any R2 trace.
    assert R2_ADDR.encode() not in raw
    assert not con.execute("SELECT 1 FROM event WHERE subject='R2'").fetchone()
    assert not con.execute(
        "SELECT 1 FROM event WHERE payload LIKE ?", ('%' + R2_ADDR + '%',)).fetchone()
    con.close()


def test_two_contracts_leave_different_active_blobs(tmp_path):
    # Contract A: shared hold retains the whole mixed blob.
    db_hold = tmp_path / 'hold.db'
    freeze_config(db_hold, BASE / 'retention_config.json')
    import_file(db_hold, BASE / CASE['source_file'])
    _hold(db_hold, 'H1')
    _erase(db_hold, 'E1', contract='shared_source_hold')
    con = connect(db_hold)
    hold_blob, hold_sha = _active_blob(con)
    con.close()
    assert hold_blob == SOURCE_BYTES and hold_sha == SOURCE_SHA

    # Contract B: immediate rebase keeps only the R1 original line.
    db_rebase = tmp_path / 'rebase.db'
    freeze_config(db_rebase, BASE / 'retention_config.json')
    import_file(db_rebase, BASE / CASE['source_file'])
    _erase(db_rebase, 'E2', contract='subject_erasure_with_rebase')
    con = connect(db_rebase)
    rebase_blob, rebase_sha = _active_blob(con)
    con.close()
    assert rebase_blob == R1_LINE and rebase_sha == hash_bytes(R1_LINE)
    assert hold_blob != rebase_blob and hold_sha != rebase_sha


def test_r1_lineage_preserves_original_line_number_and_hash_tombstone(db):
    r1_before = connect(db).execute("SELECT * FROM event WHERE subject='R1'").fetchone()
    out = _erase(db)
    con = connect(db)
    lin = con.execute('SELECT * FROM source_lineage').fetchone()
    assert lin['old_sha256'] == SOURCE_SHA
    assert lin['new_sha256'] == out['new_sha256']
    line_map = json.loads(lin['line_map'])
    assert line_map == [{'old_line': 1, 'new_line': 1,
                         'event_id': r1_before['id'], 'identity': r1_before['identity']}]
    tomb = con.execute('SELECT * FROM source_hash_tombstone').fetchone()
    assert tomb['sha256'] == SOURCE_SHA
    assert tomb['replaced_by_sha256'] == out['new_sha256']
    assert tomb['request_id'] == 'ERASE-R2-001'
    r1 = con.execute('SELECT * FROM event WHERE subject=?', ('R1',)).fetchone()
    assert r1['source_id'] == out['new_source_id']
    assert r1['identity'] == r1_before['identity']
    assert r1['sha256'] == r1_before['sha256']
    assert r1['line_no'] == 1
    # R1 is never an orphan: its event points at a complete, hash-consistent source.
    src = con.execute('SELECT * FROM source WHERE id=?', (r1['source_id'],)).fetchone()
    assert hash_bytes(src['raw']) == src['sha256'] == out['new_sha256']
    con.close()


def test_rebased_r1_blob_is_complete_parseable_source(db):
    _erase(db)
    con = connect(db)
    src = con.execute('SELECT raw FROM source').fetchone()
    lines = [json.loads(l) for l in src['raw'].decode('utf-8').splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0]['subject'] == 'R1'
    con.close()


def test_r2_disappears_from_report_after_rebase(db):
    _erase(db)
    report = json.loads(export(db, db.parent / 'r.json').read_text(encoding='utf-8'))
    blob = json.dumps(report, ensure_ascii=False)
    assert R2_ADDR not in blob and 'R2' not in [d['subject'] for d in report['decision']]
    assert {e['subject'] for e in report['event']} == {'R1'}
    assert report['source_hash_tombstone'][0]['sha256'] == SOURCE_SHA
    assert report['source_lineage'][0]['new_sha256'] == hash_bytes(R1_LINE)


def test_erasure_request_is_idempotent(db):
    first = _erase(db)
    second = _erase(db)
    assert second['idempotent_rerun'] is True
    assert second['status'] == 'completed'
    con = connect(db)
    assert con.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM retention_request WHERE id='ERASE-R2-001'").fetchone()[0] == 1
    con.close()
    assert first['new_sha256'] == hash_bytes(R1_LINE)


def test_repeated_request_after_erase_records_duplicate(db):
    _erase(db)
    out = _erase(db, request_id='ERASE-R2-002')
    assert out['idempotent_rerun'] is True
    assert out['superseded_by'] == 'ERASE-R2-001'
    con = connect(db)
    assert con.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    con.close()


def test_tombstoned_source_cannot_be_reimported(db):
    _erase(db)
    with pytest.raises(ValueError, match='erased_source_tombstone'):
        import_file(db, BASE / CASE['source_file'])


def test_audit_ledger_records_request_ids_hashes_and_reason(db):
    _erase(db)
    audits = retention.audits(db)
    actions = {a['action']: json.loads(a['details']) for a in audits}
    assert 'erasure_completed' in actions
    detail = actions['erasure_completed']
    assert detail['old_sha256'] == SOURCE_SHA
    assert detail['new_sha256'] == hash_bytes(R1_LINE)
    assert detail['credential_version'] == CRED_VERSION
    assert detail['erased_event_ids'] and detail['erased_line_nos'] == [2]
    req = retention.request_status(db, 'ERASE-R2-001')
    assert req['reason'] == 'R2 retention period expired'
    assert json.loads(req['details'])['line_map'][0]['old_line'] == 1


# --------------------------------------------------- attestation / bad lines

def _reimport_with(tmp_path, content):
    """Build a fresh DB whose mixed source carries the supplied raw content."""
    path = tmp_path / 'tampered.jsonl'
    path.write_bytes(content)
    d = tmp_path / 'tampered.db'
    freeze_config(d, BASE / 'retention_config.json')
    import_file(d, path)
    return d, hash_bytes(content)


def test_broken_json_line_blocks_erasure_instead_of_being_guessed_r2(tmp_path):
    r1 = SOURCE_BYTES.splitlines(keepends=True)[0]
    content = r1 + b'{broken-json\n'
    d, sha = _reimport_with(tmp_path, content)
    with pytest.raises(retention.RetentionError, match='unattested_line_blocked') as exc:
        retention.request_erasure(
            d, request_id='E-BAD', source_sha256=sha, subject='R2',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='expired')
    assert 'parse' in str(exc.value)
    con = connect(d)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == content
    assert con.execute('SELECT count(*) FROM event').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM retention_request').fetchone()[0] == 0
    con.close()


def test_unknown_identity_line_is_not_attributed_to_r2(tmp_path):
    # A line whose derived identity has no owning event row must fail closed
    # rather than be guessed as the R2 erasure row. Simulate the missing
    # attribution by dropping the R2 event while its original line stays put.
    d = tmp_path / 'case.db'
    freeze_config(d, BASE / 'retention_config.json')
    import_file(d, BASE / CASE['source_file'])
    con = connect(d)
    con.execute("DELETE FROM event WHERE identity='RET:2'")
    con.commit()
    con.close()
    with pytest.raises(retention.RetentionError, match='identity_not_in_event_table'):
        retention.request_erasure(
            d, request_id='E-STR', source_sha256=SOURCE_SHA, subject='R2',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='expired')
    con = connect(d)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == SOURCE_BYTES
    assert con.execute('SELECT count(*) FROM retention_request').fetchone()[0] == 0
    con.close()


def test_cross_subject_identity_mismatch_blocks_deletion(tmp_path):
    # The raw line claims R1 while reusing identity RET:2, whose stored event
    # still belongs to R2. This cross-subject misuse must never be guessed as
    # an R2 row and deleted; the request fails closed with all bytes intact.
    r1_line = SOURCE_BYTES.splitlines(keepends=True)[0].decode()
    tampered_r2 = json.loads(SOURCE_BYTES.splitlines(keepends=True)[1])
    tampered_r2['subject'] = 'R1'
    content = (r1_line + json.dumps(tampered_r2) + '\n').encode()
    d, sha = _reimport_with(tmp_path, content)
    con = connect(d)
    con.execute("UPDATE event SET subject='R2' WHERE identity='RET:2'")
    con.commit()
    con.close()
    with pytest.raises(retention.RetentionError, match='cross_subject_identity_mismatch'):
        retention.request_erasure(
            d, request_id='E-CROSS', source_sha256=sha, subject='R2',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='expired')
    con = connect(d)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == content
    assert con.execute('SELECT count(*) FROM retention_request').fetchone()[0] == 0
    con.close()


def test_event_attribution_mismatch_blocks_deletion(tmp_path):
    # R2's event exists but is attributed to a different source/line than the
    # raw line under review: an erasure request must not trust the identity alone.
    d = tmp_path / 'case.db'
    freeze_config(d, BASE / 'retention_config.json')
    import_file(d, BASE / CASE['source_file'])
    con = connect(d)
    other = con.execute(
        "INSERT INTO source(path,sha256,raw) VALUES (?,?,?)",
        ('other.jsonl', 'b' * 64, b'{}\n')).lastrowid
    con.execute("UPDATE event SET source_id=?,line_no=9 WHERE identity='RET:2'", (other,))
    con.commit()
    con.close()
    with pytest.raises(retention.RetentionError, match='event_attribution_mismatch'):
        retention.request_erasure(
            d, request_id='E-ATTR', source_sha256=SOURCE_SHA, subject='R2',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='expired')
    con = connect(d)
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == SOURCE_BYTES
    assert con.execute('SELECT count(*) FROM retention_request').fetchone()[0] == 0
    con.close()


def test_unknown_source_hash_is_rejected(db):
    with pytest.raises(retention.RetentionError, match='unknown_source_hash'):
        retention.request_erasure(
            db, request_id='E-NOPE', source_sha256='f' * 64, subject='R2',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='expired')


def test_request_id_reuse_with_different_terms_is_rejected(db):
    _erase(db)
    with pytest.raises(retention.RetentionError, match='request_id_reused'):
        retention.request_erasure(
            db, request_id='ERASE-R2-001', source_sha256=SOURCE_SHA, subject='R1',
            contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
            reason='different subject')


# ------------------------------------------------------------- transactions

def test_injected_fault_rolls_back_to_complete_old_state(db):
    retention.set_fault('after_new_source_insert')
    try:
        with pytest.raises(RuntimeError, match='injected_fault'):
            _erase(db)
    finally:
        retention.clear_faults()
    con = connect(db)
    # Complete old state: single mixed blob with original hash, both events live.
    raw, sha = _active_blob(con)
    assert raw == SOURCE_BYTES and sha == SOURCE_SHA
    assert con.execute('SELECT count(*) FROM event').fetchone()[0] == 2
    assert con.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM retention_request').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM source_hash_tombstone').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM source_lineage').fetchone()[0] == 0
    con.close()
    # After the failed attempt, a clean rerun succeeds.
    out = _erase(db)
    assert out['status'] == 'completed'


def test_retry_after_fault_does_not_duplicate_lineage(db):
    retention.set_fault('after_new_source_insert')
    with pytest.raises(RuntimeError):
        _erase(db)
    retention.clear_faults()
    _erase(db)
    con = connect(db)
    assert con.execute('SELECT count(*) FROM source_lineage').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM source_hash_tombstone').fetchone()[0] == 1
    con.close()


def test_hold_release_and_erasure_are_serialized(db):
    _hold(db)
    _erase(db, request_id='E-PENDING')
    barrier = threading.Barrier(2)
    results, errors = [], []

    def release():
        barrier.wait()
        try:
            results.append(('release', retention.release_hold(
                db, request_id='HOLD-R1-001', reason='ended')))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def direct_erase():
        barrier.wait()
        try:
            results.append(('erase', retention.request_erasure(
                db, request_id='E-DIRECT', source_sha256=SOURCE_SHA, subject='R2',
                contract='subject_erasure_with_rebase',
                credential_version=CRED_VERSION, reason='concurrent')))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=release)
    t2 = threading.Thread(target=direct_erase)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    con = connect(db)
    # Exactly one source survives and it is the R1-only rebuild; R2 is gone.
    raw, sha = _active_blob(con)
    assert raw == R1_LINE and sha == hash_bytes(R1_LINE)
    assert not con.execute("SELECT 1 FROM event WHERE subject='R2'").fetchone()
    statuses = {r['id']: r['status'] for r in con.execute(
        'SELECT id,status FROM retention_request')}
    assert statuses['E-PENDING'] == 'completed'
    # The direct request either lost the race against release (resolves against
    # the tombstone) or won the serial lock and completed itself; no half-state.
    assert statuses['E-DIRECT'] == 'completed'
    assert con.execute('SELECT count(*) FROM source_hash_tombstone').fetchone()[0] == 1
    con.close()


def test_concurrent_holds_serialize_without_corruption(db):
    out = []
    errors = []
    barrier = threading.Barrier(2)

    def hold(rid):
        barrier.wait()
        try:
            out.append(retention.submit_shared_hold(
                db, request_id=rid, source_sha256=SOURCE_SHA, hold_subject='R1',
                credential_path=HOLD_PATH, credential_sha256=HOLD_SHA, reason='h'))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=hold, args=('HA',))
    t2 = threading.Thread(target=hold, args=('HB',))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors and len(out) == 2
    con = connect(db)
    assert con.execute(
        "SELECT count(*) FROM retention_request WHERE request_type='hold' AND status='active'"
    ).fetchone()[0] == 2
    raw, sha = _active_blob(con)
    assert raw == SOURCE_BYTES and sha == SOURCE_SHA
    con.close()


def test_pending_erasure_rerun_stays_pending_and_keeps_blob(db):
    _hold(db)
    first = _erase(db)
    second = _erase(db)
    assert first['status'] == second['status'] == 'pending'
    assert second['idempotent_rerun'] is True
    con = connect(db)
    assert con.execute(
        "SELECT count(*) FROM retention_request WHERE id='ERASE-R2-001'").fetchone()[0] == 1
    raw, sha = _active_blob(con)
    assert raw == SOURCE_BYTES and sha == SOURCE_SHA
    con.close()


def test_hold_rerun_is_idempotent(db):
    first = _hold(db)
    second = _hold(db)
    assert first['status'] == 'active' and second['idempotent_rerun'] is True
    con = connect(db)
    assert con.execute(
        "SELECT count(*) FROM retention_request WHERE id='HOLD-R1-001'").fetchone()[0] == 1
    con.close()


def test_fixture_contract_names_are_supported():
    assert set(CASE['contracts']) == set(retention.CONTRACTS)


def test_expired_hold_credential_is_rejected(tmp_path):
    expired = tmp_path / 'HOLD-OLD.txt'
    expired.write_text('Synthetic hold for R1 evidence through 2020-01-01\n')
    d = tmp_path / 'case.db'
    freeze_config(d, BASE / 'retention_config.json')
    import_file(d, BASE / CASE['source_file'])
    with pytest.raises(retention.RetentionError, match='hold_credential_expired'):
        retention.submit_shared_hold(
            d, request_id='H-OLD', source_sha256=SOURCE_SHA, hold_subject='R1',
            credential_path=expired, credential_sha256=hash_bytes(expired.read_bytes()),
            reason='stale', now='2026-11-18T00:00:00+00:00')


def test_release_of_unknown_hold_is_rejected(db):
    with pytest.raises(retention.RetentionError, match='unknown_hold_request'):
        retention.release_hold(db, request_id='GHOST', reason='n/a')


def test_evaluate_and_report_under_hold_do_not_leak_r2(db):
    from receiptdesk.rules import evaluate
    _hold(db)
    _erase(db)
    evaluate(db)
    report = json.loads(export(db, db.parent / 'r.json').read_text(encoding='utf-8'))
    assert 'r2@example.invalid' not in json.dumps(report, ensure_ascii=False)
    assert {d['subject'] for d in report['decision']} <= {'R1'}
    assert {e['subject'] for e in report['evidence']} <= {'R1'}


def test_rebase_with_three_lines_keeps_all_retained_line_numbers(tmp_path):
    r1a = json.loads(SOURCE_BYTES.splitlines(keepends=True)[0])
    r2 = json.loads(SOURCE_BYTES.splitlines(keepends=True)[1])
    r1b = dict(r1a); r1b['identity'] = 'RET:3'; r1b['time'] = '2026-01-01T08:03:00Z'
    content = ('\n'.join(json.dumps(e) for e in (r1a, r2, r1b)) + '\n').encode()
    path = tmp_path / 'three.jsonl'
    path.write_bytes(content)
    d = tmp_path / 'three.db'
    freeze_config(d, BASE / 'retention_config.json')
    import_file(d, path)
    out = retention.request_erasure(
        d, request_id='E3', source_sha256=hash_bytes(content), subject='R2',
        contract='subject_erasure_with_rebase', credential_version=CRED_VERSION,
        reason='expired')
    con = connect(d)
    expected = (json.dumps(r1a) + '\n' + json.dumps(r1b) + '\n').encode()
    raw = con.execute('SELECT raw FROM source').fetchone()['raw']
    assert raw == expected
    assert out['new_sha256'] == hash_bytes(expected)
    line_map = json.loads(con.execute('SELECT line_map FROM source_lineage').fetchone()['line_map'])
    assert [(m['old_line'], m['new_line']) for m in line_map] == [(1, 1), (3, 2)]
    nos = [r['line_no'] for r in con.execute('SELECT line_no FROM event ORDER BY line_no')]
    assert nos == [1, 2]
    assert {r['subject'] for r in con.execute('SELECT subject FROM event')} == {'R1'}
    con.close()


def test_immediate_erasure_via_hold_contract_without_active_hold_still_rebases(db):
    # Without an active hold, neither contract keeps R2: the shared-hold
    # contract name simply runs the same physical rebase immediately.
    out = _erase(db, contract='shared_source_hold')
    assert out['status'] == 'completed'
    con = connect(db)
    raw, sha = _active_blob(con)
    assert raw == R1_LINE and sha == hash_bytes(R1_LINE)
    con.close()


def test_r2_address_absent_from_all_active_blob_and_payload_columns(db):
    _erase(db)
    con = connect(db)
    for table, column in (('source', 'raw'), ('event', 'payload')):
        rows = con.execute(f'SELECT {column} AS v FROM {table}').fetchall()
        joined = b''.join(r['v'] if isinstance(r['v'], bytes)
                          else r['v'].encode('utf-8') for r in rows)
        assert R2_ADDR.encode() not in joined
    con.close()
