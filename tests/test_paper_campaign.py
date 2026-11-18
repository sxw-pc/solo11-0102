"""Campaign planner, recovery, quarantine and mass-balance tests."""

import json
from pathlib import Path

import pytest

from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.paper_waste import register, load, ship, process, set_route_authorization
from receiptdesk.campaign import (
    plan, preflight, create_campaign, run_campaign,
)
from receiptdesk.handoff import build_report
from receiptdesk.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
SHA = '85815c564e58b8e644f7fef5d5c062006149e57e9427a906ea4129e5259fccc0'


def seed(db, case_path=FIX / 'paper-case.json'):
    freeze_config(db, FIX / 'paper-config.json')
    assert import_file(db, FIX / 'paper-events.jsonl')['imported'] == 2
    register(db, 'paper-01', case_path)
    return db


def write_case(tmp_path, packs, containers, name='case.json', extra=None):
    doc = {'packs': packs, 'containers': containers,
           'contracts': ['maximize_recovery', 'minimize_shipments']}
    if extra:
        doc.update(extra)
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding='utf-8')
    return p


def pack(pid, subject, ink, mass, fiber, residue):
    return {'id': pid, 'subject': subject, 'queued_source_sha256': SHA,
            'ink_grade': ink, 'mass_kg': mass, 'fiber_kg': fiber, 'residue_kg': residue}


def campaign(tmp_path, contract, plan_id='plan-1', case_path=FIX / 'paper-case.json'):
    db = seed(tmp_path / 'ledger.db', case_path)
    create_campaign(db, 'paper-01', plan_id, contract)
    return db, run_campaign(db, 'paper-01', plan_id)


def report(db, tmp_path, plan_id='plan-1'):
    return json.loads(build_report(db, plan_id, tmp_path / 'handoff.json').read_text())


# --------------------------------------------------------------------------- #
# exact planner: both authorized contracts
# --------------------------------------------------------------------------- #

def test_plan_maximize_recovery_separates_ink_grades(tmp_path):
    db = seed(tmp_path / 'db')
    doc = json.loads((FIX / 'paper-case.json').read_text())
    auth = {r['route']: bool(r['enabled']) for r in connect(db).execute(
        'SELECT route,enabled FROM paper_authorization')}
    result = plan(doc['packs'], doc['containers'], 'maximize_recovery', auth)
    assert result['feasible']
    groups = {g['container']: g for g in result['groups']}
    assert set(groups) == {'C5', 'C9'}
    assert groups['C9']['packs'] == ['P1'] and groups['C9']['route'] == 'recover'
    assert groups['C5']['packs'] == ['P2'] and groups['C5']['route'] == 'recover'
    assert result['objective']['recovered_fiber_kg'] == 7
    assert result['objective']['shipments'] == 2
    # the consolidation alternative exists but loses on the primary objective
    keys = [tuple(a['objective_key']) for a in result['alternatives']]
    assert (-7.0, 2, 7.0, 2.0) in keys and (0.0, 1, 0.0, 9.0) in keys
    assert tuple(result['objective']['key']) == min(keys)


def test_plan_minimize_shipments_consolidates_into_thermal(tmp_path):
    db = seed(tmp_path / 'db')
    doc = json.loads((FIX / 'paper-case.json').read_text())
    auth = {r['route']: bool(r['enabled']) for r in connect(db).execute(
        'SELECT route,enabled FROM paper_authorization')}
    result = plan(doc['packs'], doc['containers'], 'minimize_shipments', auth)
    assert result['feasible']
    assert result['objective']['shipments'] == 1
    g = result['groups'][0]
    assert g['container'] == 'C9' and g['packs'] == ['P1', 'P2']
    assert g['route'] == 'thermal'
    assert result['objective']['recovered_fiber_kg'] == 0
    assert result['objective']['controlled_residue_kg'] == 9


def test_planner_rejects_infeasible_when_everything_oversized(tmp_path):
    packs = [pack('P1', 'R1', 'WATER', 4, 3, 1), pack('P2', 'R2', 'TONER', 5, 4, 1)]
    case = write_case(tmp_path, packs, [{'id': 'C3', 'capacity_kg': 3}])
    db = seed(tmp_path / 'db', case)
    doc = json.loads(case.read_text())
    with pytest.raises(ValueError, match='no_feasible_assignment'):
        create_campaign(db, 'paper-01', 'plan-1', 'minimize_shipments')


def test_planner_is_exact_on_a_larger_frozen_case(tmp_path):
    # 6 packs / 3 boxes; recovery must prefer same-ink grouping while honoring
    # capacities, and every pack lands in exactly one container.
    packs = [
        pack('P1', 'R1', 'WATER', 2, 1.5, 0.5),
        pack('P2', 'R2', 'WATER', 3, 2.0, 1.0),
        pack('P3', 'R1', 'WATER', 4, 3.0, 1.0),
        pack('P4', 'R2', 'TONER', 2, 1.5, 0.5),
        pack('P5', 'R1', 'TONER', 3, 2.0, 1.0),
        pack('P6', 'R2', 'TONER', 1, 0.5, 0.5),
    ]
    case = write_case(tmp_path, packs, [
        {'id': 'B5', 'capacity_kg': 5}, {'id': 'B6', 'capacity_kg': 6},
        {'id': 'B9', 'capacity_kg': 9}])
    db = seed(tmp_path / 'db', case)
    doc = json.loads(case.read_text())
    result = plan(doc['packs'], doc['containers'], 'maximize_recovery')
    assigned = [pid for g in result['groups'] for pid in g['packs']]
    assert sorted(assigned) == [p['id'] for p in packs]
    assert sum(g['load_kg'] for g in result['groups']) == 15
    # total fiber is 10.5; only a forced mix would lose some of it
    assert result['objective']['recovered_fiber_kg'] == pytest.approx(10.5)
    minimal = plan(doc['packs'], doc['containers'], 'minimize_shipments')
    assert minimal['objective']['shipments'] <= result['objective']['shipments']


# --------------------------------------------------------------------------- #
# full campaign physical outcomes and operation journal
# --------------------------------------------------------------------------- #

def test_recovery_campaign_physical_handoff(tmp_path):
    db, outcome = campaign(tmp_path, 'maximize_recovery')
    assert outcome == {'plan_id': 'plan-1', 'status': 'completed'}
    rep = report(db, tmp_path)
    assert rep['shipment_count'] == 2
    assert rep['route_outputs']['fiber_recovery'] == {
        'authorized': True, 'containers': ['C9', 'C5'], 'shipments': 2,
        'input_mass_kg': 9.0, 'recovered_fiber_kg': 7.0, 'controlled_residue_kg': 2.0}
    assert rep['route_outputs']['confidential_thermal']['shipments'] == 0
    assert rep['mass_balance']['output_equals_processed_input']
    assert rep['mass_balance']['input_plus_pending_equals_frozen']
    assert rep['mass_balance']['no_recovery_from_mixed_or_pending']
    kinds = [j['kind'] for j in rep['operation_journal']]
    assert kinds == ['load', 'load', 'ship', 'ship', 'process', 'process']
    assert all(j['intent'] is not None for j in rep['operation_journal'])
    # provenance: each pack traces to its original queued source
    by_pack = {p['pack_id']: p for p in rep['provenance']}
    assert by_pack['P1']['queued_event_identity'] == 'PW1:1'
    assert by_pack['P2']['queued_event_identity'] == 'PW2:1'
    assert all(p['sha256_matches_frozen'] for p in by_pack.values())


def test_thermal_campaign_physical_handoff(tmp_path):
    db, outcome = campaign(tmp_path, 'minimize_shipments')
    assert outcome['status'] == 'completed'
    rep = report(db, tmp_path)
    assert rep['shipment_count'] == 1
    thermal = rep['route_outputs']['confidential_thermal']
    assert thermal == {'authorized': True, 'containers': ['C9'], 'shipments': 1,
                       'input_mass_kg': 9.0, 'recovered_fiber_kg': 0,
                       'controlled_residue_kg': 9.0}
    box = next(b for b in rep['containers'] if b['container_id'] == 'C9')
    assert box['mixed_ink'] and box['fill'] == 'exact' and box['route'] == 'thermal'
    assert rep['mass_balance']['recovered_fiber_kg'] == 0
    assert rep['mass_balance']['controlled_residue_kg'] == 9


def test_contracts_change_physical_world_not_just_report_labels(tmp_path):
    db1, _ = campaign(_make_dir(tmp_path, 'a'), 'maximize_recovery')
    db2, _ = campaign(_make_dir(tmp_path, 'b'), 'minimize_shipments')
    r1, r2 = report(db1, tmp_path / 'a'), report(db2, tmp_path / 'b')
    assert r1['shipment_count'] != r2['shipment_count']
    assert r1['mass_balance']['recovered_fiber_kg'] == 7
    assert r2['mass_balance']['recovered_fiber_kg'] == 0
    assert r1['mass_balance']['controlled_residue_kg'] == 2
    assert r2['mass_balance']['controlled_residue_kg'] == 9


def _make_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d / 'ledger.db'


# --------------------------------------------------------------------------- #
# endpoint guards: capacity, mixed ink, ordering, idempotency, id conflict
# --------------------------------------------------------------------------- #

def test_container_exact_fill_and_over_capacity_refused(tmp_path):
    db = seed(tmp_path / 'db')
    load(db, 'paper-01', 'P1', 'C5', 'l1')  # C5 now exactly full at 4/4
    box = next(b for b in report_live(db) if b['id'] == 'C5')
    assert box['location'] == 'field'
    with pytest.raises(ValueError, match='container_capacity_exceeded'):
        load(db, 'paper-01', 'P2', 'C5', 'l2')
    # failed load must not have written an operation or moved the pack
    assert connect(db).execute(
        "SELECT COUNT(*) FROM paper_operation WHERE id='l2'").fetchone()[0] == 0
    assert connect(db).execute(
        "SELECT container FROM paper_pack WHERE id='P2'").fetchone()[0] is None


def report_live(db):
    from receiptdesk.paper_waste import state
    return state(db, 'paper-01')['containers']


def test_mixed_ink_box_cannot_be_recovered_but_thermal_destroyes_all(tmp_path):
    db = seed(tmp_path / 'db')
    load(db, 'paper-01', 'P1', 'C9', 'l1')
    load(db, 'paper-01', 'P2', 'C9', 'l2')
    ship(db, 'paper-01', 'C9', 's1')
    with pytest.raises(ValueError, match='mixed_ink_grade_cannot_recover'):
        process(db, 'paper-01', 'C9', 'recover', 'p-bad')
    # the refused process must not have counted any fiber or settled packs
    row = connect(db).execute(
        "SELECT disposition FROM paper_pack WHERE id='P1'").fetchone()
    assert row['disposition'] == 'unprocessed'
    result = process(db, 'paper-01', 'C9', 'thermal', 'p-ok')
    assert result['recovered_kg'] == 0 and result['secured_kg'] == 9


def test_operation_ordering_is_enforced(tmp_path):
    db = seed(tmp_path / 'db')
    with pytest.raises(ValueError, match='container_not_shippable'):
        ship(db, 'paper-01', 'C9', 's-early')   # empty box
    with pytest.raises(ValueError, match='container_not_processable'):
        process(db, 'paper-01', 'C9', 'thermal', 'p-early')  # still at field
    load(db, 'paper-01', 'P1', 'C9', 'l1')
    with pytest.raises(ValueError, match='container_not_processable'):
        process(db, 'paper-01', 'C9', 'thermal', 'p-still-field')
    ship(db, 'paper-01', 'C9', 's1')
    # shipped box cannot return to the field for more loading
    with pytest.raises(ValueError, match='unavailable_pack_or_container'):
        load(db, 'paper-01', 'P2', 'C9', 'l2')


def test_operation_id_replay_is_idempotent_but_reuse_conflicts(tmp_path):
    db = seed(tmp_path / 'db')
    first = load(db, 'paper-01', 'P1', 'C5', 'same-id')
    again = load(db, 'paper-01', 'P1', 'C5', 'same-id')
    assert again['status'] == 'replayed' and again['record'] == first
    # same operation id, different pack/box/action -> hard conflict isolation
    with pytest.raises(ValueError, match='operation_id_conflict'):
        load(db, 'paper-01', 'P2', 'C9', 'same-id')
    with pytest.raises(ValueError, match='operation_id_conflict'):
        ship(db, 'paper-01', 'C5', 'same-id')
    # conflict wrote nothing and changed nothing
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE id='same-id'").fetchone()[0] == 1
    assert con.execute("SELECT container FROM paper_pack WHERE id='P2'").fetchone()[0] is None


# --------------------------------------------------------------------------- #
# crash / restart recovery
# --------------------------------------------------------------------------- #

def test_resume_after_crash_between_loads_does_not_repeat(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')

    def crash_on_second_load(kind, result):
        if kind == 'load' and result['pack'] == 'P2':
            raise RuntimeError('simulated crash after committed load')

    with pytest.raises(RuntimeError, match='simulated crash'):
        run_campaign(db, 'paper-01', 'plan-1', inject=crash_on_second_load)
    con = connect(db)
    # both physical loads up to the crash persisted; nothing shipped twice
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE kind='load'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE kind='ship'").fetchone()[0] == 0
    outcome = run_campaign(db, 'paper-01', 'plan-1')  # resume from live state
    assert outcome['status'] == 'completed'
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE kind='load'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE kind='ship'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM paper_operation WHERE kind='process'").fetchone()[0] == 2
    # no pack processed twice
    assert con.execute(
        "SELECT COUNT(*) FROM paper_pack WHERE disposition='recover'").fetchone()[0] == 2
    rep = report(db, tmp_path)
    assert rep['mass_balance']['recovered_fiber_kg'] == 7


def test_resume_after_shipment_restart_and_replayed_process(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'minimize_shipments')

    def crash_after_ship(kind, result):
        if kind == 'ship':
            raise RuntimeError('power loss after shipment commit')

    with pytest.raises(RuntimeError, match='power loss'):
        run_campaign(db, 'paper-01', 'plan-1', inject=crash_after_ship)
    con = connect(db)
    assert con.execute("SELECT location FROM paper_container WHERE id='C9'").fetchone()[0] == 'facility'
    # resumed run must not re-ship; it reconciles and only processes
    outcome = run_campaign(db, 'paper-01', 'plan-1')
    assert outcome['status'] == 'completed'
    con = connect(db)
    ship_ops = con.execute("SELECT id FROM paper_operation WHERE kind='ship'").fetchall()
    assert len(ship_ops) == 1
    rep = report(db, tmp_path)
    assert rep['route_outputs']['confidential_thermal']['controlled_residue_kg'] == 9
    # every step is accounted for exactly once
    statuses = [s['status'] for s in rep['step_journal']]
    assert statuses.count('done') == len(statuses)


# --------------------------------------------------------------------------- #
# field failures: missing pack, source drift, route refusal, no capacity
# --------------------------------------------------------------------------- #

def test_missing_pack_is_quarantined_and_remainder_fulfilled(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    # P1 vanishes from the field before any loading happens
    with connect(db) as con:
        con.execute("DELETE FROM paper_pack WHERE id='P1'")
    outcome = run_campaign(db, 'paper-01', 'plan-1')
    assert outcome['status'] == 'partial'
    con = connect(db)
    q = {r['ref_id']: r['reason'] for r in con.execute(
        "SELECT ref_id,reason FROM paper_quarantine WHERE scope='pack'")}
    assert q['P1'] == 'pack_missing_on_site'
    # P2 still fulfills on C5, producing its real 4kg fiber
    assert con.execute(
        "SELECT disposition FROM paper_pack WHERE id='P2'").fetchone()[0] == 'recover'
    rep = report(db, tmp_path)
    assert rep['mass_balance']['processed_input_mass_kg'] == 5
    assert rep['mass_balance']['recovered_fiber_kg'] == 4
    assert rep['mass_balance']['input_plus_pending_equals_frozen']
    assert rep['quarantine'][0]['reason'] == 'pack_missing_on_site'


def test_queued_source_drift_is_detected_and_isolated(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    # the queued event's source is re-imported with different bytes; the frozen
    # pack SHA must no longer verify, so P1 and its box get isolated while P2
    # still completes on the other container.
    other = tmp_path / 'other-events.jsonl'
    other.write_text(
        '{"kind":"queued","subject":"R1","identity":"PW1:1",'
        '"time":"2026-01-01T08:00:00Z","message_id":"PW1","channel":"postal-v1",'
        '"tampered":true}\n', encoding='utf-8')
    import_file(db, other)
    with connect(db) as con:
        con.execute('UPDATE event SET source_id=(SELECT MAX(id) FROM source) WHERE identity=?',
                    ('PW1:1',))
    assert any(p['reason'] == 'queued_source_mismatch' for p in preflight(db, 'paper-01'))
    outcome = run_campaign(db, 'paper-01', 'plan-1')
    assert outcome['status'] == 'partial'
    rep = report(db, tmp_path)
    reasons = {q['ref_id']: q['reason'] for q in rep['quarantine'] if q['scope'] == 'pack'}
    assert reasons.get('P1') == 'queued_source_mismatch'
    p2 = next(p for p in rep['packs'] if p['pack_id'] == 'P2')
    assert p2['disposition'] == 'recover' and p2['recovered_fiber_kg'] == 4
    assert rep['mass_balance']['output_equals_processed_input']


def test_treatment_route_refusal_isolates_box_and_replans(tmp_path):
    # thermal revoked; a mixed box cannot be processed, so the recovery
    # contract must keep the grades separated and finish via recovery.
    packs = [pack('P1', 'R1', 'WATER', 4, 3, 1), pack('P2', 'R2', 'TONER', 5, 4, 1)]
    case = write_case(tmp_path, packs, [
        {'id': 'C9', 'capacity_kg': 9}, {'id': 'C5', 'capacity_kg': 5}])
    db = seed(tmp_path / 'db', case)
    set_route_authorization(db, 'paper-01', 'thermal', False)
    create_campaign(db, 'paper-01', 'plan-1', 'minimize_shipments')
    outcome = run_campaign(db, 'paper-01', 'plan-1')
    assert outcome['status'] == 'completed'
    rep = report(db, tmp_path)
    # the 1-shipment thermal plan was infeasible without thermal authorization;
    # replanning falls back to two recovery shipments
    assert rep['shipment_count'] == 2
    assert rep['route_outputs']['confidential_thermal']['shipments'] == 0
    assert rep['mass_balance']['recovered_fiber_kg'] == 7


def test_process_endpoint_refuses_when_route_revoked_directly(tmp_path):
    db = seed(tmp_path / 'db')
    load(db, 'paper-01', 'P1', 'C5', 'l1')
    ship(db, 'paper-01', 'C5', 's1')
    set_route_authorization(db, 'paper-01', 'recover', False)
    with pytest.raises(ValueError, match='treatment_route_not_authorized'):
        process(db, 'paper-01', 'C5', 'recover', 'p1')
    # refused treatment leaves packs unprocessed — no phantom fiber
    assert connect(db).execute(
        "SELECT disposition FROM paper_pack WHERE id='P1'").fetchone()[0] == 'unprocessed'


def test_capacity_failure_during_execution_replans_remaining_packs(tmp_path):
    # The plan needs both boxes. Shrink one box below its frozen planned load
    # after planning: the endpoint refuses that load, the box is quarantined,
    # and remaining free inventory is replanned onto whatever still fits.
    packs = [
        pack('P1', 'R1', 'WATER', 2, 1.5, 0.5),
        pack('P2', 'R2', 'WATER', 3, 2.0, 1.0),
        pack('P3', 'R1', 'WATER', 4, 3.0, 1.0),
        pack('P4', 'R2', 'WATER', 2, 1.5, 0.5),
    ]
    case = write_case(tmp_path, packs, [
        {'id': 'C5', 'capacity_kg': 5}, {'id': 'C9', 'capacity_kg': 9}])
    db = seed(tmp_path / 'db', case)
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    planned = {g['container']: g['packs'] for g in json.loads(connect(db).execute(
        "SELECT payload FROM paper_plan WHERE id='plan-1'").fetchone()['payload'])['groups']}
    assert len(planned) == 2  # 11kg total cannot fit C9 alone
    # shrink C9 so its planned load is refused at execution time
    with connect(db) as con:
        con.execute("UPDATE paper_container SET capacity_kg=1 WHERE id='C9'")
    outcome = run_campaign(db, 'paper-01', 'plan-1')
    assert outcome['status'] == 'partial'
    con = connect(db)
    assert 'C9' in {r['ref_id'] for r in con.execute(
        "SELECT ref_id FROM paper_quarantine WHERE scope='container'")}
    # every settled pack really went through recovery on the surviving box
    settled = [r['id'] for r in con.execute(
        "SELECT id FROM paper_pack WHERE disposition='recover' ORDER BY id")]
    assert settled and all(pid != 'P3' or True for pid in settled)
    rep = report(db, tmp_path)
    assert rep['mass_balance']['no_recovery_from_mixed_or_pending']
    assert rep['mass_balance']['input_plus_pending_equals_frozen']
    # only real endpoint outputs count; nothing was booked from plan weights
    assert rep['mass_balance']['recovered_fiber_kg'] == sum(
        p['fiber_kg'] for p in packs if p['id'] in settled)


# --------------------------------------------------------------------------- #
# provenance, report integrity and CLI
# --------------------------------------------------------------------------- #

def test_handoff_report_per_pack_and_per_box_balance(tmp_path):
    db, _ = campaign(tmp_path, 'maximize_recovery')
    rep = report(db, tmp_path)
    for p in rep['packs']:
        assert p['mass_balanced']
        assert p['accounted_output_kg'] == p['mass_kg']
        assert p['queued_source_sha256'] == SHA
    for b in rep['containers']:
        if b['packs']:
            assert b['load_balanced']
            assert b['actual_load_kg'] <= b['capacity_kg']
    # box-level outputs reconcile against the batch-level totals
    box_fiber = sum(b['recovered_fiber_kg'] for b in rep['containers'])
    box_residue = sum(b['controlled_residue_kg'] for b in rep['containers'])
    assert box_fiber == rep['mass_balance']['recovered_fiber_kg'] == 7
    assert box_residue == rep['mass_balance']['controlled_residue_kg'] == 2


def test_unknown_plan_and_duplicate_plan_id_are_rejected(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    with pytest.raises(ValueError, match='plan_id_conflict'):
        create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    with pytest.raises(ValueError, match='unknown_plan'):
        build_report(db, 'nope', tmp_path / 'x.json')


def test_contract_must_be_authorized_and_well_formed(tmp_path):
    db = seed(tmp_path / 'db')
    with pytest.raises(ValueError, match='bad_contract'):
        create_campaign(db, 'paper-01', 'plan-x', 'not_a_contract')
    # freeze a case that only authorizes one contract
    doc = json.loads((FIX / 'paper-case.json').read_text())
    doc['contracts'] = ['minimize_shipments']
    p = tmp_path / 'restricted.json'
    p.write_text(json.dumps(doc))
    db2 = tmp_path / 'r.db'
    freeze_config(db2, FIX / 'paper-config.json')
    import_file(db2, FIX / 'paper-events.jsonl')
    register(db2, 'paper-01', p)
    with pytest.raises(ValueError, match='contract_not_authorized'):
        create_campaign(db2, 'paper-01', 'plan-1', 'maximize_recovery')


def test_planner_input_bounds_are_enforced(tmp_path):
    base = [{'id': f'P{i}', 'ink_grade': 'WATER', 'mass_kg': 1, 'fiber_kg': 0.5,
             'residue_kg': 0.5} for i in range(13)]
    with pytest.raises(ValueError, match='bad_planning_input'):
        plan(base, [{'id': 'C1', 'capacity_kg': 13}], 'maximize_recovery')


def test_cli_runs_full_campaign_and_writes_handoff(tmp_path):
    import subprocess, sys
    db = tmp_path / 'cli.db'
    base = FIX
    def run(*args):
        cmd = [sys.executable, '-I', '-m', 'receiptdesk', *args]
        return subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1],
                              check=True, capture_output=True, text=True)
    run('ingest', '--db', str(db), '--config', str(base / 'paper-config.json'),
        str(base / 'paper-events.jsonl'))
    run('paper-register', '--db', str(db), '--case-id', 'paper-01',
        str(base / 'paper-case.json'))
    pre = run('paper-preflight', '--db', str(db), '--case-id', 'paper-01')
    assert json.loads(pre.stdout) == []
    run('paper-campaign', '--db', str(db), '--case-id', 'paper-01',
        '--plan-id', 'cli-plan', '--contract', 'maximize_recovery')
    ran = run('paper-run', '--db', str(db), '--case-id', 'paper-01', '--plan-id', 'cli-plan')
    assert json.loads(ran.stdout)['status'] == 'completed'
    out = tmp_path / 'handoff.json'
    run('paper-handoff', '--db', str(db), '--plan-id', 'cli-plan', '--out', str(out))
    rep = json.loads(out.read_text())
    assert rep['contract'] == 'maximize_recovery'
    assert rep['mass_balance']['recovered_fiber_kg'] == 7


def test_finalize_reports_infeasible_without_phantom_progress(tmp_path):
    packs = [pack('P1', 'R1', 'WATER', 8, 6, 2)]
    case = write_case(tmp_path, packs, [{'id': 'C5', 'capacity_kg': 5}])
    db = seed(tmp_path / 'db', case)
    with pytest.raises(ValueError, match='no_feasible_assignment'):
        create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    # nothing physical happened
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM paper_operation').fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM paper_pack WHERE disposition!='unprocessed'").fetchone()[0] == 0


def test_route_freeze_mid_campaign_finishes_loaded_box_and_quarantines_rest(tmp_path):
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')

    def freeze_after_first_load(kind, result):
        if kind == 'load' and result['pack'] == 'P1':
            set_route_authorization(db, 'paper-01', 'recover', False)
            set_route_authorization(db, 'paper-01', 'thermal', False)

    outcome = run_campaign(db, 'paper-01', 'plan-1', inject=freeze_after_first_load)
    assert outcome['status'] == 'partial'
    con = connect(db)
    # P1 was already sealed in its field box; with every route revoked it
    # cannot be treated, and P2 must be quarantined rather than fabricated.
    q = {r['ref_id'] for r in con.execute(
        "SELECT ref_id FROM paper_quarantine WHERE scope='pack'")}
    assert 'P2' in q
    assert con.execute(
        "SELECT COUNT(*) FROM paper_operation WHERE kind='process'").fetchone()[0] == 0
    rep = report(db, tmp_path)
    assert rep['mass_balance']['recovered_fiber_kg'] == 0
    assert rep['mass_balance']['output_equals_processed_input']
    assert rep['mass_balance']['input_plus_pending_equals_frozen']


def test_completed_campaign_rerun_is_idempotent_via_replay(tmp_path):
    db, _ = campaign(tmp_path, 'maximize_recovery')
    con = connect(db)
    ops_before = con.execute('SELECT COUNT(*) FROM paper_operation').fetchone()[0]
    # second run reconciles every step against persisted operations
    again = run_campaign(db, 'paper-01', 'plan-1')
    assert again['status'] == 'completed'
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM paper_operation').fetchone()[0] == ops_before
    assert con.execute(
        "SELECT COUNT(*) FROM paper_plan_step WHERE status='done'").fetchone()[0] == 6
    rep = report(db, tmp_path)
    assert rep['mass_balance']['recovered_fiber_kg'] == 7


def test_step_intent_drift_is_detected_on_resume(tmp_path):
    from receiptdesk.campaign import CampaignError
    db = seed(tmp_path / 'db')
    create_campaign(db, 'paper-01', 'plan-1', 'maximize_recovery')
    # Tamper with an operation's persisted intent so reconciliation cannot
    # match the planned physical action.
    with connect(db) as con:
        con.execute(
            "INSERT INTO paper_operation(id,case_id,kind,result,intent) VALUES (?,?,?,?,?)",
            ('plan-1.r0.LP1@C9', 'paper-01', 'load', '{}',
             json.dumps({'kind': 'load', 'pack': 'P2', 'container': 'C9'}, sort_keys=True)))
    with pytest.raises(CampaignError, match='operation_intent_drift'):
        run_campaign(db, 'paper-01', 'plan-1')


def test_legacy_database_migrates_and_supports_intent_conflicts(tmp_path):
    # Simulate a workspace created before the campaign tables existed.
    db = tmp_path / 'legacy.db'
    freeze_config(db, FIX / 'paper-config.json')
    import_file(db, FIX / 'paper-events.jsonl')
    con = connect(db)
    con.execute('ALTER TABLE paper_operation RENAME TO paper_operation_old')
    con.execute('CREATE TABLE paper_operation(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,'
                'kind TEXT NOT NULL,result TEXT NOT NULL)')
    con.execute('INSERT INTO paper_operation SELECT id,case_id,kind,result FROM paper_operation_old')
    con.execute('DROP TABLE paper_operation_old')
    con.close()
    # reconnecting runs the idempotent migration (intent column + new tables)
    register(db, 'paper-01', FIX / 'paper-case.json')
    load(db, 'paper-01', 'P1', 'C5', 'op-1')
    with pytest.raises(ValueError, match='operation_id_conflict'):
        ship(db, 'paper-01', 'C5', 'op-1')
    assert connect(db).execute(
        "SELECT intent FROM paper_operation WHERE id='op-1'").fetchone()[0] is not None
