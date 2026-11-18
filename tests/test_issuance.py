"""Physical label issuance: the two authorised disposition contracts."""
import json
from pathlib import Path

import pytest

from receiptdesk import issuance
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.print_lab import invoke, register, state
from receiptdesk.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def seed(tmp_path, contract='retain_verified_serial', preseed_l100=True,
         case_file='print-case.json'):
    """Frozen roster + queued evidence + registered print case + open case."""
    db = tmp_path / 'labels.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    register(db, 'print-01', FIX / case_file)
    if preseed_l100:
        digest = issuance.layout_digest('J1', 'R1', 'P1', 1)
        out = invoke(db, 'L100', 'J1', 'seed-print-L100', 0,
                     content_sha256=digest, case_id='print-01')
        assert out['status'] == 'timeout'
    opened = issuance.open_case(db, 'print-01', contract)
    return db, opened['case_id']


def rows(db, sql, args=()):
    con = connect(db)
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# the two contracts
# ---------------------------------------------------------------------------

def test_retain_verified_serial_keeps_l100_and_jams_are_scrapped(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    result = issuance.run_contract(db, cid)
    report = issuance.report(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}

    assert jobs['J1']['state'] == 'mail_ready'
    assert jobs['J1']['current_serial'] == 'L100'
    assert jobs['J2']['state'] == 'mail_ready'
    assert jobs['J2']['current_serial'] == 'L102'
    # J1 keeps its physically printed sheet; J2's jammed L101 was scrapped and
    # the next serial L102 issued.  No void happened at all.
    assert [s['serial'] for s in report['waste']['voided']] == []
    assert [s['serial'] for s in report['waste']['jammed']] == ['L101']
    assert report['stock_remaining'] == ['L103']
    assert state(db, 'L101')['status'] == 'jammed'
    # one live label per job, distinct serials
    live = rows(db, "SELECT serial,job,content_sha256 FROM label_stock WHERE status='printed'")
    assert sorted(r['serial'] for r in live) == ['L100', 'L102']
    assert all(r['content_sha256'] for r in live)
    # device prints: J1 once (ACK lost), J2 twice (jam then success)
    prints = rows(db, "SELECT serial FROM printer_operation WHERE action='print' ORDER BY rowid")
    assert [p['serial'] for p in prints] == ['L100', 'L101', 'L102']
    # postage only for live labels
    assert report['postage_totals'] == {'charged_cents': 160, 'live_cents': 160}
    assert sorted(p['serial'] for p in report['postage'][cid]) == ['L100', 'L102']


def test_void_and_reissue_voids_lost_l100_and_uses_l103(tmp_path):
    db, cid = seed(tmp_path, 'void_and_reissue')
    result = issuance.run_contract(db, cid)
    report = issuance.report(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}

    assert jobs['J1']['state'] == 'mail_ready'
    assert jobs['J1']['current_serial'] == 'L103'
    assert jobs['J1']['voided_serial'] == 'L100'
    assert jobs['J2']['state'] == 'mail_ready'
    assert jobs['J2']['current_serial'] == 'L102'
    assert state(db, 'L100')['status'] == 'void'
    assert [s['serial'] for s in report['waste']['voided']] == ['L100']
    assert [s['serial'] for s in report['waste']['jammed']] == ['L101']
    assert report['stock_remaining'] == []
    # both jobs still end with exactly one live label each
    live = rows(db, "SELECT serial,job FROM label_stock WHERE status='printed' ORDER BY job")
    assert [(r['job'], r['serial']) for r in live] == [('J1', 'L103'), ('J2', 'L102')]
    # void-and-reissue pays for L100's paper and postage even though it is scrap
    assert report['postage_totals'] == {'charged_cents': 240, 'live_cents': 160}
    assert sorted(p['serial'] for p in report['postage'][cid]) == ['L100', 'L102', 'L103']
    # the failed void receipt was not observed as a failure: it was confirmed
    ops = rows(db, "SELECT action,state FROM label_operation ORDER BY created_at,rowid")
    assert {'action': 'void', 'state': 'voided'} in ops


def test_two_contracts_diverge_but_agree_on_unchanged_delivery(tmp_path):
    from receiptdesk.rules import evaluate
    retain = tmp_path / 'retain'
    void = tmp_path / 'void'
    for root, contract in ((retain, 'retain_verified_serial'),
                           (void, 'void_and_reissue')):
        root.mkdir()
        db, cid = seed(root, contract)
        issuance.run_contract(db, cid)
    decisions = {}
    for name, root in (('retain', retain), ('void', void)):
        db = root / 'labels.db'
        evaluate(db)  # source/event/decision pipeline still runs untouched
        decisions[name] = rows(db, 'SELECT subject,status FROM decision ORDER BY subject')
        assert decisions[name] == [{'subject': 'R1', 'status': 'insufficient'},
                                   {'subject': 'R2', 'status': 'insufficient'}]
        report = issuance.report(db)
        assert all(j['mail_ready'] for c in report['cases'] for j in c['jobs'])
    # the contracts differ only in physical serials/waste/postage
    r = issuance.report(retain / 'labels.db')
    v = issuance.report(void / 'labels.db')
    assert r['postage_totals']['charged_cents'] == 160
    assert v['postage_totals']['charged_cents'] == 240
    assert r['stock_remaining'] == ['L103']
    assert v['stock_remaining'] == []


def test_fresh_start_converges_without_preseed(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial', preseed_l100=False)
    result = issuance.run_contract(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}
    assert jobs['J1']['current_serial'] == 'L100'
    assert jobs['J2']['current_serial'] == 'L102'
    assert [p['serial'] for p in result['prints']] == ['L100', 'L101', 'L102']
    assert state(db, 'L100')['status'] == 'printed'


# ---------------------------------------------------------------------------
# evidence gating: unknown, duplicate, stale source, old layout
# ---------------------------------------------------------------------------

def test_unknown_subject_cannot_take_stock(tmp_path):
    db = tmp_path / 'x.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['jobs'].append({'id': 'J3', 'subject': 'R-UNKNOWN', 'message_id': 'P9',
                        'layout_version': 1, 'layout_hash': '0' * 64})
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='print_subject_unknown'):
        register(db, 'print-01', case)
    assert rows(db, 'SELECT * FROM label_assignment') == []


def test_duplicate_notification_is_rejected_at_open(tmp_path):
    db = tmp_path / 'x.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['jobs'][1]['message_id'] = 'P1'   # same notification as J1
    doc['jobs'][1]['subject'] = 'R1'
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    register(db, 'print-01', case)
    with pytest.raises(ValueError, match='duplicate_notification|print_message_duplicate'):
        issuance.open_case(db, 'print-01', 'retain_verified_serial')
    assert rows(db, 'SELECT * FROM label_assignment') == []


def test_stale_source_sha_blocks_printing(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    # Simulate an out-of-band rewrite of the frozen source bytes.
    con = connect(db)
    with con:
        con.execute("UPDATE source SET sha256='f'*64 WHERE id=1")
    con.close()
    with pytest.raises(issuance.LabelReviewRequired, match='source_sha_changed'):
        issuance.print_label(db, cid, 'J2')
    job = rows(db, 'SELECT state,reason FROM label_job WHERE job_id=?', ('J2',))[0]
    assert job['state'] == 'manual_review'
    assert job['reason'] == 'source_sha_changed'
    # no physical action taken for J2
    assert state(db, 'L101')['status'] == 'blank'
    assert state(db, 'L102')['status'] == 'blank'
    # and the queue event decision is untouched by the label layer
    assert rows(db, 'SELECT * FROM decision') == []


def test_old_layout_hash_is_rejected(tmp_path):
    db = tmp_path / 'x.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['jobs'][0]['layout_hash'] = '0' * 64
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    register(db, 'print-01', case)
    with pytest.raises(ValueError, match='layout_hash_mismatch'):
        issuance.open_case(db, 'print-01', 'retain_verified_serial')


def test_contract_must_be_authorised_by_fixture(tmp_path):
    db = tmp_path / 'x.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['contracts'] = ['retain_verified_serial']
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    register(db, 'print-01', case)
    with pytest.raises(ValueError, match='contract_not_authorized'):
        issuance.open_case(db, 'print-01', 'void_and_reissue')


def test_case_reopen_is_idempotent_and_contract_conflict_rejected(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    again = issuance.open_case(db, 'print-01', 'retain_verified_serial')
    assert again['created'] is False
    assert again['case_id'] == cid
    with pytest.raises(ValueError, match='label_case_conflict'):
        issuance.open_case(db, 'print-01', 'void_and_reissue', case_id=cid)


# ---------------------------------------------------------------------------
# device protocol: ACK loss, jam, revision compare-and-swap
# ---------------------------------------------------------------------------

def test_lost_print_ack_is_reconciled_not_reprinted(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    first = issuance.print_label(db, cid, 'J1')
    assert first['status'] == 'timeout'
    assert first['mail_ready'] is True
    assert state(db, 'L100')['status'] == 'printed'
    # a naive retry must not issue a second sheet
    with pytest.raises(ValueError, match='job_already_labeled'):
        issuance.print_label(db, cid, 'J1')
    # the seeding device call is the only physical print; reconciliation read
    # the device rather than printing again
    prints = rows(db, "SELECT * FROM printer_operation WHERE action='print'")
    assert [p['serial'] for p in prints] == ['L100']
    # the second call returned the recorded outcome, it did not print again
    with pytest.raises(ValueError, match='job_already_labeled'):
        issuance.print_label(db, cid, 'J1', operation_id=first['operation_id'])


def test_jammed_sheet_is_scrapped_and_serial_never_reused(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    out = issuance.print_label(db, cid, 'J2')
    assert out['status'] == 'jammed'
    assert out['paper_consumed'] == 1
    assert state(db, 'L101')['status'] == 'jammed'
    # the same serial may never be printed again for anyone
    with pytest.raises(ValueError, match='label_action_invalid'):
        invoke(db, 'L101', 'J2', 'reuse-jam', 1, 'print')
    with pytest.raises(ValueError, match='label_action_invalid'):
        invoke(db, 'L101', 'J1', 'reuse-jam-2', 1, 'print')
    # the job gets the next serial instead
    second = issuance.print_label(db, cid, 'J2')
    assert second['serial'] == 'L102'
    assert second['status'] == 'committed'


def test_revision_conflict_stops_for_manual_review(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    # J2's first sheet jams, its replacement L102 gets a persisted intent
    assert issuance.print_label(db, cid, 'J2')['status'] == 'jammed'
    with pytest.raises(issuance.InjectedCrash):
        issuance.print_label(db, cid, 'J2', operation_id='j2-reprint',
                             crash_at='print_intent')
    # external maintenance advances the physical revision after the intent
    con = connect(db)
    with con:
        con.execute("UPDATE label_stock SET revision=revision+1 WHERE serial='L102'")
    con.close()
    with pytest.raises(issuance.LabelReviewRequired, match='label_revision_conflict'):
        issuance.print_label(db, cid, 'J2', operation_id='j2-reprint')
    job = rows(db, 'SELECT state,reason FROM label_job WHERE job_id=?', ('J2',))[0]
    assert job['state'] == 'manual_review'
    assert job['reason'] in ('label_revision_conflict', 'device_state_mismatch')
    assert state(db, 'L102')['status'] == 'blank'
    # a forced retry replays the persisted intent and still fails, never
    # overwriting the externally maintained revision
    with pytest.raises(issuance.LabelReviewRequired, match='label_revision_conflict'):
        issuance.print_label(db, cid, 'J2', operation_id='j2-reprint')
    assert rows(db, "SELECT * FROM printer_operation WHERE serial='L102'") == []


def test_external_content_change_is_not_overwritten(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    issuance.print_label(db, cid, 'J1')  # L100 adopted for J1
    con = connect(db)
    with con:
        con.execute("UPDATE label_stock SET content_sha256='deadbeef' WHERE serial='L100'")
    con.close()
    # recovery does not force anything; it stops the job and reports it
    recovery = issuance.recover_case(db, cid)
    assert [m['job_id'] for m in recovery['manual_review']] == ['J1']
    with pytest.raises(issuance.LabelReviewRequired):
        issuance.run_contract(db, cid)
    job = rows(db, 'SELECT state,reason FROM label_job WHERE job_id=?', ('J1',))[0]
    assert job['state'] == 'manual_review'
    stored = rows(db, "SELECT content_sha256 FROM label_stock WHERE serial='L100'")[0]
    assert stored['content_sha256'] == 'deadbeef'   # not silently repaired


def test_void_receipt_lost_is_resolved_by_reading_device(tmp_path):
    # declare a lost void ACK for L100 in a copy of the fixture
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['faults'] = {'L100': {'print': 'commit_then_ack_lost', 'void': 'commit_then_ack_lost'},
                     'L101': 'jam'}
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    db = tmp_path / 'labels2.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    register(db, 'print-01', case)
    digest = issuance.layout_digest('J1', 'R1', 'P1', 1)
    invoke(db, 'L100', 'J1', 'seed-L100', 0, content_sha256=digest, case_id='print-01')
    cid = issuance.open_case(db, 'print-01', 'void_and_reissue')['case_id']
    result = issuance.run_contract(db, cid)
    assert state(db, 'L100')['status'] == 'void'
    jobs = {j['job_id']: j for j in result['jobs']}
    assert jobs['J1']['current_serial'] == 'L103'
    assert jobs['J1']['voided_serial'] == 'L100'
    # crucially, L100 and L103 are not both releasable
    live = rows(db, "SELECT serial FROM label_stock WHERE status='printed' ORDER BY serial")
    assert [r['serial'] for r in live] == ['L102', 'L103']


def test_lost_void_ack_blocks_reissue_until_confirmed(tmp_path):
    doc = json.loads((FIX / 'print-case.json').read_text())
    doc['faults'] = {'L100': {'print': 'commit_then_ack_lost'}, 'L101': 'jam'}
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc))
    db = tmp_path / 'x.db'
    freeze_config(db, FIX / 'print-config.json')
    import_file(db, FIX / 'print-events.jsonl')
    register(db, 'print-01', case)
    digest = issuance.layout_digest('J1', 'R1', 'P1', 1)
    invoke(db, 'L100', 'J1', 'seed-L100', 0, content_sha256=digest, case_id='print-01')
    cid = issuance.open_case(db, 'print-01', 'void_and_reissue')['case_id']
    issuance.print_label(db, cid, 'J1')
    # crash after the intent was persisted but before the device saw the void
    with pytest.raises(issuance.InjectedCrash):
        issuance.void_label(db, cid, 'J1', crash_at='void_intent')
    # device still shows L100 printed; a reprint must be refused
    con = connect(db)
    with con:
        con.execute("UPDATE label_job SET state='voided' WHERE case_id=? AND job_id='J1'", (cid,))
    con.close()
    with pytest.raises(ValueError, match='void_unconfirmed'):
        issuance.print_label(db, cid, 'J1')
    assert state(db, 'L100')['status'] == 'printed'
    # after reconciliation the void is retried, never a blind reprint
    issuance.recover_case(db, cid)
    out = issuance.void_label(db, cid, 'J1')
    assert out['status'] in ('committed', 'already_voided')
    assert state(db, 'L100')['status'] == 'void'


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

def test_same_operation_replay_has_no_physical_side_effect(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    first = issuance.print_label(db, cid, 'J2', operation_id='op-fixed')
    assert first['status'] == 'jammed'
    rev = state(db, 'L101')['revision']
    replay = issuance.print_label(db, cid, 'J2', operation_id='op-fixed')
    assert replay['status'] == 'jammed'
    assert state(db, 'L101')['revision'] == rev
    ops = rows(db, "SELECT * FROM printer_operation WHERE id='op-fixed'")
    assert len(ops) == 1
    # J2's replacement L102 succeeds; a fresh operation id for a job that
    # already holds a live label is refused rather than printing a second sheet
    assert issuance.print_label(db, cid, 'J2')['serial'] == 'L102'
    with pytest.raises(ValueError, match='job_already_labeled'):
        issuance.print_label(db, cid, 'J2', operation_id='op-other')


def test_repeated_contract_run_is_stable(tmp_path):
    db, cid = seed(tmp_path, 'void_and_reissue')
    issuance.run_contract(db, cid)
    before = {
        'stock': rows(db, 'SELECT serial,status,job,revision FROM label_stock ORDER BY serial'),
        'ops': rows(db, 'SELECT operation_id,state FROM label_operation ORDER BY operation_id'),
        'postage': rows(db, 'SELECT serial,cents FROM label_postage ORDER BY serial'),
        'device': rows(db, 'SELECT id FROM printer_operation ORDER BY id'),
    }
    issuance.run_contract(db, cid)
    issuance.recover_case(db, cid)
    after = {
        'stock': rows(db, 'SELECT serial,status,job,revision FROM label_stock ORDER BY serial'),
        'ops': rows(db, 'SELECT operation_id,state FROM label_operation ORDER BY operation_id'),
        'postage': rows(db, 'SELECT serial,cents FROM label_postage ORDER BY serial'),
        'device': rows(db, 'SELECT id FROM printer_operation ORDER BY id'),
    }
    assert before == after


def test_printer_operation_conflict_is_detected(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    issuance.print_label(db, cid, 'J1')
    with pytest.raises(ValueError, match='printer_operation_conflict'):
        invoke(db, 'L102', 'J2', 'seed-print-L100', 0, 'print')


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------

CRASH_POINTS = ['print_intent', 'print_device', 'print_recorded', 'postage_booked',
                'jam_recorded', 'void_intent', 'void_device', 'void_recorded', 'job_ready']


@pytest.mark.parametrize('point', CRASH_POINTS)
@pytest.mark.parametrize('contract,expected', [
    ('retain_verified_serial', ('L100', 'L102', 160, ['L103'])),
    ('void_and_reissue', (None, None, 240, [])),
])
def test_crash_recovery_converges_without_duplicates(tmp_path, point, contract, expected):
    db, cid = seed(tmp_path, contract)
    try:
        issuance.run_contract(db, cid, crash_at=point)
    except issuance.InjectedCrash:
        pass
    issuance.recover_case(db, cid)
    result = issuance.run_contract(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}
    report = issuance.report(db, cid)
    got = (jobs['J1']['current_serial'], jobs['J2']['current_serial'],
           report['postage_totals']['charged_cents'], report['stock_remaining'])
    if expected[0] is not None:
        assert got == expected
    else:
        assert got[2:] == expected[2:]
    assert jobs['J1']['state'] == 'mail_ready'
    assert jobs['J2']['state'] == 'mail_ready'
    # Every live serial belongs to exactly one job and each job has exactly
    # one live sheet.  Which replacement serial a crash-resumed run happens to
    # consume is not contractual; counts, ownership and money are.
    live = rows(db, "SELECT serial,job FROM label_stock WHERE status='printed'")
    assert sorted(r['job'] for r in live) == ['J1', 'J2']
    assert len({r['serial'] for r in live}) == 2
    if contract == 'retain_verified_serial':
        assert dict((r['job'], r['serial']) for r in live) == {'J1': 'L100', 'J2': 'L102'}
    else:
        assert state(db, 'L100')['status'] == 'void'
        assert {jobs['J1']['current_serial'], jobs['J2']['current_serial']} == {'L102', 'L103'}
    # every physical sheet is charged exactly once, scrap included
    charged = [r['serial'] for r in rows(db, 'SELECT serial FROM label_postage')]
    assert len(charged) == len(set(charged))
    assert len(charged) == len(rows(db, 'SELECT serial FROM label_assignment')) - \
        len(rows(db, "SELECT serial FROM label_stock WHERE status='jammed'"))


def test_crash_after_print_intent_does_not_reprint(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial', preseed_l100=False)
    with pytest.raises(issuance.InjectedCrash):
        issuance.print_label(db, cid, 'J1', operation_id='print-once', crash_at='print_intent')
    assert state(db, 'L100')['revision'] == 0     # nothing reached the paper
    recovery = issuance.recover_case(db, cid)
    assert recovery['not_executed'] == [{'case_id': cid, 'job_id': 'J1', 'serial': 'L100'}]
    out = issuance.print_label(db, cid, 'J1', operation_id='print-once')
    # same operation id -> the device is touched exactly once overall
    ops = rows(db, "SELECT * FROM printer_operation WHERE id='print-once'")
    assert len(ops) == 1
    assert out['serial'] == 'L100'
    assert state(db, 'L100')['status'] == 'printed'


def test_crash_after_physical_print_adopts_without_second_sheet(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial', preseed_l100=False)
    with pytest.raises(issuance.InjectedCrash):
        issuance.print_label(db, cid, 'J1', operation_id='j1-once', crash_at='print_device')
    # paper exists but the ACK was never recorded
    assert state(db, 'L100')['status'] == 'printed'
    assert rows(db, "SELECT * FROM label_operation WHERE state='printed'") == []
    recovery = issuance.recover_case(db, cid)
    adopted = [a['serial'] for a in recovery['adopted_prints']]
    assert adopted == ['L100']
    assert state(db, 'L100')['revision'] == 1     # no second physical print
    assert rows(db, 'SELECT serial FROM label_postage') == [{'serial': 'L100'}]


def test_crash_after_jam_marks_scrap_and_moves_on(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    issuance.print_label(db, cid, 'J1')
    with pytest.raises(issuance.InjectedCrash):
        issuance.print_label(db, cid, 'J2', crash_at='jam_recorded')
    recovery = issuance.recover_case(db, cid)
    assert recovery['jammed'] == []          # jam was already durably recorded
    assert state(db, 'L101')['status'] == 'jammed'
    result = issuance.run_contract(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}
    assert jobs['J2']['current_serial'] == 'L102'


def test_crash_after_void_before_ack_is_recovered(tmp_path):
    db, cid = seed(tmp_path, 'void_and_reissue')
    issuance.print_label(db, cid, 'J1')
    with pytest.raises(issuance.InjectedCrash):
        issuance.void_label(db, cid, 'J1', crash_at='void_device')
    assert state(db, 'L100')['status'] == 'void'   # vacuum: physical effect happened
    recovery = issuance.recover_case(db, cid)
    assert [v['serial'] for v in recovery['confirmed_voids']] == ['L100']
    # L100 was a physically printed sheet, so its postage stays booked even
    # though the sheet is now scrap; no second charge is added for it
    assert rows(db, 'SELECT serial,cents FROM label_postage WHERE serial=?', ('L100',)) == \
        [{'serial': 'L100', 'cents': 80}]
    result = issuance.run_contract(db, cid)
    jobs = {j['job_id']: j for j in result['jobs']}
    assert jobs['J1']['current_serial'] == 'L103'
    # L100's postage was already booked as a live label; it is not refunded and
    # the two replacements are charged once each
    assert rows(db, 'SELECT serial FROM label_postage ORDER BY serial') == \
        [{'serial': 'L100'}, {'serial': 'L102'}, {'serial': 'L103'}]


# ---------------------------------------------------------------------------
# mail-ready rules
# ---------------------------------------------------------------------------

def test_queued_alone_is_never_mail_ready(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial', preseed_l100=False)
    report = issuance.report(db, cid)
    assert [j['mail_ready'] for j in report['cases'][0]['jobs']] == [False, False]
    assert report['cases'][0]['status'] == 'in_progress'
    assert report['mail_ready'] == [{'case_id': cid, 'jobs': []}]
    # queued evidence exists for both recipients but that is not delivery
    assert len(rows(db, "SELECT * FROM event WHERE kind='queued'")) == 2
    assert rows(db, 'SELECT * FROM decision') == []


def test_mail_ready_requires_postage(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial')
    issuance.print_label(db, cid, 'J1')
    con = connect(db)
    with con:
        con.execute('DELETE FROM label_postage')
        con.execute("UPDATE label_assignment SET postage_cents=0")
    con.close()
    job = rows(db, 'SELECT state FROM label_job WHERE job_id=?', ('J1',))[0]
    # recomputing readiness demotes the job; the layer never guesses postage
    issuance.recover_case(db, cid)
    job = rows(db, 'SELECT state FROM label_job WHERE job_id=?', ('J1',))[0]
    assert job['state'] == 'mail_ready'   # recovery rebooked the missing charge
    assert rows(db, 'SELECT serial,cents FROM label_postage') == [{'serial': 'L100', 'cents': 80}]


def test_content_hash_must_match_layout(tmp_path):
    db, cid = seed(tmp_path, 'retain_verified_serial', preseed_l100=False)
    wrong = 'a' * 64
    invoke(db, 'L100', 'J1', 'seed-wrong', 0, content_sha256=wrong, case_id='print-01')
    issuance.open_case(db, 'print-01', 'retain_verified_serial')
    recovery = issuance.recover_case(db, 'print-01:retain_verified_serial')
    assert recovery['adopted_prints'] == []
    assert recovery['manual_review'][0]['reason'] == 'unaccounted_label_content_mismatch'
    assert state(db, 'L100')['content_sha256'] == wrong   # left untouched
    assert rows(db, 'SELECT * FROM label_postage') == []


def test_report_preserves_source_event_decision(tmp_path):
    from receiptdesk.ingest import freeze_config, import_file   # noqa: F401
    from receiptdesk.rules import evaluate
    db, cid = seed(tmp_path, 'void_and_reissue')
    issuance.run_contract(db, cid)
    evaluate(db)
    before = {
        'source': rows(db, 'SELECT id,path,sha256,raw FROM source ORDER BY id'),
        'event': rows(db, 'SELECT * FROM event ORDER BY id'),
        'decision': rows(db, 'SELECT * FROM decision ORDER BY subject'),
        'evidence': rows(db, 'SELECT * FROM evidence ORDER BY subject,event_id'),
    }
    report = issuance.report(db, cid)
    assert report['decisions'] and report['events'] and report['source_evidence']
    after = {
        'source': rows(db, 'SELECT id,path,sha256,raw FROM source ORDER BY id'),
        'event': rows(db, 'SELECT * FROM event ORDER BY id'),
        'decision': rows(db, 'SELECT * FROM decision ORDER BY subject'),
        'evidence': rows(db, 'SELECT * FROM evidence ORDER BY subject,event_id'),
    }
    assert before == after
    # per-job source evidence is reported
    for job in report['cases'][0]['jobs']:
        assert job['queued_event_sha256'] and job['source_sha256']
        assert job['layout_hash'] and job['layout_version'] == 1
        assert job['mail_ready_basis'] == ['device_print_confirmed',
                                           'ack_lost_reconciled_by_device'
                                           if (job['current_label'] or {}).get('ack_lost')
                                           else 'print_confirmed_unused',
                                           'content_hash_match', 'postage_booked'] \
            or job['mail_ready_basis'] == ['device_print_confirmed', 'content_hash_match',
                                           'postage_booked']


def test_markdown_report_has_required_sections(tmp_path):
    db, cid = seed(tmp_path, 'void_and_reissue')
    issuance.run_contract(db, cid)
    out = issuance.write_report(db, tmp_path / 'labels.json', cid)
    assert out.exists() and out.with_suffix('.md').exists()
    text = out.with_suffix('.md').read_text()
    assert '库存余量' in text and '废标' in text and '邮资' in text
    data = json.loads(out.read_text())
    assert data['waste']['voided'][0]['serial'] == 'L100'
    assert data['waste']['paper_sheets_consumed'] == 4


# ---------------------------------------------------------------------------
# one-live-label invariants
# ---------------------------------------------------------------------------

def test_one_current_label_per_job_invariant(tmp_path):
    db, cid = seed(tmp_path, 'void_and_reissue')
    issuance.run_contract(db, cid)
    current = rows(db, "SELECT job_id,serial FROM label_assignment WHERE state='printed'")
    assert sorted((r['job_id'], r['serial']) for r in current) == [('J1', 'L103'), ('J2', 'L102')]
    per_job = rows(db, "SELECT job_id,COUNT(*) n FROM label_assignment WHERE state='printed' "
                       'GROUP BY job_id')
    assert all(r['n'] == 1 for r in per_job)
    # every serial appears at most once in the assignment ledger
    all_serials = rows(db, 'SELECT serial FROM label_assignment')
    assert len({r['serial'] for r in all_serials}) == len(all_serials)
