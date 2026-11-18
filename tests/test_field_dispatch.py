"""Field dispatch tests: exact planning, execution, closures and recovery.

All fixtures are local and synthetic.  The field ledger never mutates the
mail decision/evidence tables: an observation is not a delivery receipt.
"""
import json
from pathlib import Path

import pytest

from receiptdesk import field_visit
from receiptdesk.field_visit import (
    close_road, inspect, move, plan, recover, register, recheck, reopen_road,
    state, sync_eligibility,
)
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.rules import evaluate
from receiptdesk.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
SOURCE_SHA = 'df0bd3218116f3e98646e8aa22646f2410954f4114a92361f00f50e1eedb31f2'


def prepared(tmp_path, name='field.db'):
    db = tmp_path / name
    freeze_config(db, FIX / 'field_config.json')
    assert import_file(db, FIX / 'field_events.jsonl')['imported'] == 5
    assert evaluate(db) == {'R1': 'undelivered', 'R2': 'insufficient'}
    assert register(db, 'route-1', FIX / 'field_case.json')['recipients'] == 2
    return db


def write_case(tmp_path, doc, name='case.json'):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def base_doc(**overrides):
    doc = json.loads((FIX / 'field_case.json').read_text(encoding='utf-8'))
    doc.update(overrides)
    return doc


def walk(db, path, visits, courier='c1'):
    """Execute a full route with distinct operation ids and return inspections."""
    results = []
    for index, (destination, recipient) in enumerate(zip(path[1:], visits)):
        move(db, 'route-1', destination, f'{courier}-move-{index}',
             courier_id=courier)
        if recipient:
            results.append(inspect(db, 'route-1', recipient,
                                   f'{courier}-visit-{index}',
                                   courier_id=courier))
    return results


# ----------------------------------------------------------------- objectives

def test_min_total_travel_matches_fixture_route(tmp_path):
    db = prepared(tmp_path)
    planned = plan(db, 'route-1', 'min_total_travel')
    courier = planned['couriers'][0]
    assert [s['recipient'] for s in courier['stops']] == ['R1', 'R2']
    assert [s['planned_arrival'] for s in courier['stops']] == [1, 9]
    assert [s['late_minutes'] for s in courier['stops']] == [0, 4]
    assert planned['metrics']['total_travel_minutes'] == 16
    assert planned['metrics']['max_late_minutes'] == 4
    assert planned['optimality']['no_better_plan'] is True
    assert planned['optimality']['complete_search_space'] is True


def test_min_max_lateness_prefers_prompt_route(tmp_path):
    db = prepared(tmp_path)
    planned = plan(db, 'route-1', 'min_max_lateness')
    courier = planned['couriers'][0]
    assert [s['recipient'] for s in courier['stops']] == ['R2', 'R1']
    assert [s['planned_arrival'] for s in courier['stops']] == [7, 17]
    assert planned['metrics']['max_late_minutes'] == 2
    assert planned['metrics']['total_travel_minutes'] == 18
    # The trade-off is exactly the one the prompt describes: same stops,
    # different physical order, travel and lateness.
    short = plan(db, 'route-1', 'min_total_travel')
    assert short['metrics']['total_travel_minutes'] == 16
    assert short['metrics']['max_late_minutes'] == 4


def test_planning_is_versioned_and_does_not_execute(tmp_path):
    db = prepared(tmp_path)
    first = plan(db, 'route-1', 'min_total_travel')
    second = plan(db, 'route-1', 'min_max_lateness')
    assert (first['version'], second['version']) == (1, 2)
    live = state(db, 'route-1')
    assert live['moves'] == [] and live['inspections'] == []
    assert live['couriers'][0] == {'courier_id': 'c1', 'node': 'D', 'minute': 0}
    assert [s['recipient'] for s in live['remaining_stops']] == ['R2', 'R1']


def test_objective_key_is_recorded_for_review(tmp_path):
    db = prepared(tmp_path)
    planned = plan(db, 'route-1', 'min_max_lateness')
    optimality = planned['optimality']
    assert optimality['best_metric_key'][:2] == [2, 18]
    assert optimality['runner_up_metric_key'] is not None
    assert optimality['runner_up_metric_key'] > optimality['best_metric_key']
    assert optimality['directed_edges_only'] is True


# ------------------------------------------------------------------ execution

def test_actual_route_differs_from_plan_when_walked_otherwise(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_max_lateness')
    results = walk(db, ['D', 'A', 'B', 'E'], ['R1', 'R2', None])
    assert [(r['recipient'], r['minute'], r['late_minutes']) for r in results] == [
        ('R1', 1, 0), ('R2', 9, 4)]
    live = state(db, 'route-1')
    # Planned route remains version 1 (R2 then R1); execution is separate.
    assert [s['recipient'] for s in live['plan']['couriers'][0]['stops']] == ['R2', 'R1']
    assert [s['recipient'] for s in live['remaining_stops']] == []
    report = field_visit.build_report(db, 'route-1')
    assert [v['recipient'] for v in report['actual']['visit_order']] == ['R1', 'R2']
    assert report['actual']['total_travel_minutes'] == 16


def test_operation_ids_replay_without_duplicating(tmp_path):
    db = prepared(tmp_path)
    assert move(db, 'route-1', 'A', 'm1')['minute'] == 1
    assert inspect(db, 'route-1', 'R1', 'v1')['status'] == 'inspected'
    replay = move(db, 'route-1', 'A', 'm1')
    assert replay['status'] == 'replayed' and replay['minute'] == 1
    assert inspect(db, 'route-1', 'R1', 'v1')['status'] == 'replayed'
    assert len(state(db, 'route-1')['moves']) == 1
    with pytest.raises(ValueError, match='operation_conflict'):
        move(db, 'route-1', 'B', 'm1')


def test_cannot_inspect_off_site_or_twice(tmp_path):
    db = prepared(tmp_path)
    with pytest.raises(ValueError, match='not_at_recipient_site'):
        inspect(db, 'route-1', 'R1', 'early')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    with pytest.raises(ValueError, match='site_already_inspected'):
        inspect(db, 'route-1', 'R1', 'v2')


def test_reverse_road_is_not_a_path(tmp_path):
    db = prepared(tmp_path)
    # Only D->A exists, so a move A->D must fail despite the straight line.
    move(db, 'route-1', 'A', 'm1')
    with pytest.raises(ValueError, match='road_unavailable'):
        move(db, 'route-1', 'D', 'back')
    assert state(db, 'route-1')['couriers'][0]['node'] == 'A'


def test_shift_boundary_blocks_late_finish(tmp_path):
    db = prepared(tmp_path)
    with pytest.raises(ValueError, match='road_unavailable'):
        move(db, 'route-1', 'E', 'direct')  # no D->E road exists
    move(db, 'route-1', 'A', 'm1')          # minute 1
    move(db, 'route-1', 'B', 'm2')          # minute 9
    move(db, 'route-1', 'A', 'm3')          # 9 + 10 = 19, inside the shift
    assert state(db, 'route-1')['couriers'][0]['minute'] == 19
    move(db, 'route-1', 'B', 'm4')          # 19 + 8 = 27, still inside
    # B->E would arrive at minute 34, past the shift end of 30.
    with pytest.raises(ValueError, match='shift_expired'):
        move(db, 'route-1', 'E', 'm5')
    assert state(db, 'route-1')['couriers'][0]['node'] == 'B'


# ------------------------------------------------------------------- closures

def test_closed_road_blocks_finishing(tmp_path):
    db = prepared(tmp_path)
    # B->E is the only direct way out of B; with A->E also closed there is no
    # directed path from the R2 site to the finish point during the shift.
    close_road(db, 'route-1', 'B', 'E', 0, 30, closure_id='close-BE')
    close_road(db, 'route-1', 'A', 'E', 0, 30, closure_id='close-AE')
    with pytest.raises(ValueError, match='no_feasible_route'):
        plan(db, 'route-1', 'min_total_travel')
    with pytest.raises(ValueError, match='bad_move_path'):
        move(db, 'route-1', 'E', 'not-at-B', path=['B', 'E'])
    move(db, 'route-1', 'B', 'to-B')
    with pytest.raises(ValueError, match='road_closed'):
        move(db, 'route-1', 'E', 'through-closed', path=['B', 'E'])
    # Reopening A->E restores a feasible plan from the courier's real
    # position (B at minute 7): R2 is inspected on the spot, then A and E.
    reopen_road(db, 'route-1', 'close-AE')
    replacement = plan(db, 'route-1', 'min_total_travel')
    assert replacement['base_state']['couriers'][0] == {
        'courier_id': 'c1', 'node': 'B', 'minute': 7}
    courier = replacement['couriers'][0]
    assert [s['recipient'] for s in courier['stops']] == ['R2', 'R1']
    assert [s['planned_arrival'] for s in courier['stops']] == [7, 17]


def test_off_hours_closure_does_not_block_planning(tmp_path):
    db = prepared(tmp_path)
    close_road(db, 'route-1', 'A', 'B', 20, 30, closure_id='close-late')
    planned = plan(db, 'route-1', 'min_total_travel')
    # A->B departs at minute 1, outside the window.
    assert [s['planned_arrival'] for s in planned['couriers'][0]['stops']] == [1, 9]


def test_midway_closure_interrupts_plan_and_replans_remaining(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    # Close A->B for the whole shift: R2 is still pending and must be reached
    # by another directed road after reopening or replanning.
    closed = close_road(db, 'route-1', 'A', 'B', 0, 30, closure_id='close-AB')
    assert closed['replan_required'] is True
    assert closed['interrupted_plans'][0]['plan_id'] == 1
    reopened = reopen_road(db, 'route-1', 'close-AB')
    assert reopened['removed'] is True
    replacement = plan(db, 'route-1', 'min_total_travel')
    assert replacement['version'] == 2
    assert [s['recipient'] for s in replacement['couriers'][0]['stops']] == ['R2']
    assert replacement['base_state']['visited'] == ['R1']


def test_closed_road_blocks_execution(tmp_path):
    db = prepared(tmp_path)
    close_road(db, 'route-1', 'D', 'A', 0, 2, closure_id='close-DA')
    with pytest.raises(ValueError, match='road_closed'):
        move(db, 'route-1', 'A', 'm1')
    assert state(db, 'route-1')['couriers'][0]['node'] == 'D'
    reopen_road(db, 'route-1', 'close-DA')
    assert move(db, 'route-1', 'A', 'm2')['minute'] == 1

# ---------------------------------------------------------------- two couriers

def two_courier_case(tmp_path):
    doc = base_doc(couriers=[
        {'id': 'c1', 'start': 'D', 'finish': 'E'},
        {'id': 'c2', 'start': 'D', 'finish': 'E'},
    ])
    return write_case(tmp_path, doc, 'two.json')


def test_two_couriers_split_sites(tmp_path):
    db = prepared(tmp_path)
    register(db, 'two', two_courier_case(tmp_path))
    planned = plan(db, 'two', 'min_total_travel')
    assignments = {c['courier_id']: [s['recipient'] for s in c['stops']]
                   for c in planned['couriers']}
    assert sorted(sum(assignments.values(), [])) == ['R1', 'R2']
    assert assignments == {'c1': ['R1'], 'c2': ['R2']}
    assert planned['metrics']['total_travel_minutes'] == 16
    # The two-courier split ties on road minutes with the single-courier plan
    # but wins the second criterion: R2 is reached at minute 7, not 9.
    assert planned['metrics']['max_late_minutes'] == 2
    single = plan(db, 'route-1', 'min_total_travel')
    assert single['metrics']['total_travel_minutes'] == 16
    assert single['metrics']['max_late_minutes'] == 4


def test_two_courier_execution_keeps_positions_separate(tmp_path):
    db = prepared(tmp_path)
    register(db, 'two', two_courier_case(tmp_path))
    plan(db, 'two', 'min_total_travel')
    move(db, 'two', 'A', 'c1-move', courier_id='c1')
    inspect(db, 'two', 'R1', 'c1-visit', courier_id='c1')
    move(db, 'two', 'B', 'c2-move', courier_id='c2')
    inspect(db, 'two', 'R2', 'c2-visit', courier_id='c2')
    live = state(db, 'two')
    positions = {c['courier_id']: c['node'] for c in live['couriers']}
    assert positions == {'c1': 'A', 'c2': 'B'}
    assert {i['courier_id'] for i in live['inspections']} == {'c1', 'c2'}


def test_single_courier_is_forced_when_only_one_registered(tmp_path):
    db = prepared(tmp_path)
    planned = plan(db, 'route-1', 'min_total_travel')
    assert len(planned['couriers']) == 1
    assert planned['metrics']['per_courier_travel'] == {'c1': 16}


# ------------------------------------------------------------------ recovery

def test_operation_replay_after_restart_is_idempotent(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    recovered = recover(db, 'route-1')
    assert recovered['inspected'] == ['R1']
    # Replaying both operations from a fresh process changes nothing.
    assert move(db, 'route-1', 'A', 'm1')['status'] == 'replayed'
    assert inspect(db, 'route-1', 'R1', 'v1')['status'] == 'replayed'
    live = state(db, 'route-1')
    assert len(live['moves']) == 1
    assert len(live['inspections']) == 1
    # A different destination under the same move id is a conflict, not a new
    # move.
    with pytest.raises(ValueError, match='operation_conflict'):
        move(db, 'route-1', 'B', 'm1')
    assert state(db, 'route-1')['couriers'][0]['node'] == 'A'


def test_restart_recovers_position_and_completed_visits(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    # Simulate a crash-repaired database whose courier row was rolled back
    # while the append-only ledgers survived.
    con = connect(db)
    con.execute("UPDATE field_courier SET node='D',minute=0 WHERE case_id='route-1'")
    con.commit()
    con.close()
    out = recover(db, 'route-1')
    assert out['couriers'][0]['node'] == 'A'
    assert out['couriers'][0]['minute'] == 1
    assert out['inspected'] == ['R1']
    assert [s['recipient'] for s in out['remaining_stops']] == ['R2']
    # No re-visit of R1 is possible.
    with pytest.raises(ValueError, match='site_already_inspected'):
        inspect(db, 'route-1', 'R1', 'v1-replay')


def test_replan_after_restart_only_schedules_unvisited(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    recover(db, 'route-1')
    replacement = plan(db, 'route-1', 'min_total_travel')
    stops = {s['recipient'] for c in replacement['couriers'] for s in c['stops']}
    assert stops == {'R2'}
    assert replacement['base_state']['visited'] == ['R1']
    assert replacement['base_state']['couriers'][0]['node'] == 'A'


# ------------------------------------------------------- eligibility changes

def add_effective_events(tmp_path, db):
    """Append a valid receipt for R1, making its decision effective."""
    path = tmp_path / 'extra.jsonl'
    events = [
        {'kind': 'queued', 'subject': 'R1', 'identity': 'R1-X:1',
         'time': '2026-01-01T09:00:00Z', 'message_id': 'R1-X',
         'address': 'r1@example.invalid', 'channel': 'email-v1'},
        {'kind': 'sent', 'subject': 'R1', 'identity': 'R1-X:2',
         'time': '2026-01-01T09:05:00Z', 'message_id': 'R1-X',
         'address': 'r1@example.invalid', 'channel': 'email-v1'},
        {'kind': 'receipt', 'subject': 'R1', 'identity': 'R1-X:3',
         'time': '2026-01-01T09:10:00Z', 'message_id': 'R1-X',
         'address': 'r1@example.invalid', 'channel': 'email-v1'},
    ]
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
    import_file(db, path)
    return evaluate(db)


def test_changed_decision_freezes_case_and_stops_dispatch(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    assert add_effective_events(tmp_path, db)['R1'] == 'effective'

    synced = sync_eligibility(db, 'route-1')
    assert synced['frozen'] is True
    assert synced['changed'][0]['recipient'] == 'R1'
    assert synced['changed'][0]['reason'] == 'decision_status_changed'

    # Freeze preserves the visit that already happened, but forbids new work.
    frozen = state(db, 'route-1')
    assert frozen['status'] == 'frozen'
    assert [i['recipient'] for i in frozen['inspections']] == ['R1']
    with pytest.raises(ValueError, match='case_requires_recheck'):
        move(db, 'route-1', 'B', 'm2')
    with pytest.raises(ValueError, match='case_requires_recheck'):
        plan(db, 'route-1', 'min_total_travel')

    # Recheck excludes the now-effective target and drops it from dispatch.
    out = recheck(db, 'route-1')
    assert out['visited_facts_preserved'] is True
    assert state(db, 'route-1')['status'] == 'ready'
    replacement = plan(db, 'route-1', 'min_total_travel')
    stops = {s['recipient'] for c in replacement['couriers'] for s in c['stops']}
    assert stops == {'R2'}
    assert all(r['recipient'] != 'R1' for r in
               state(db, 'route-1')['remaining_stops'])
    # Field observations did not rewrite the original mail decision history:
    # R1 is now effective because of new mail events, not because R1 was
    # physically visited.
    con = connect(db)
    assert con.execute("SELECT status FROM decision WHERE subject='R1'").fetchone()[0] == 'effective'
    assert con.execute("SELECT status FROM decision WHERE subject='R2'").fetchone()[0] == 'insufficient'
    assert con.execute(
        "SELECT count(*) FROM field_inspection WHERE recipient='R1'").fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM evidence WHERE subject='R1'").fetchone()[0] == 6
    con.close()


def test_source_snapshot_change_invalidates_without_deleting_visits(tmp_path):
    db = prepared(tmp_path)
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    # A new revoke event arrives for R2 and the rules are re-evaluated: the
    # eligibility snapshot it was registered under no longer matches.
    extra = tmp_path / 'extra-revoke.jsonl'
    extra.write_text(json.dumps({
        'kind': 'revoke', 'subject': 'R2', 'identity': 'R2-M:3',
        'time': '2026-01-01T08:20:00Z', 'message_id': 'R2-M',
        'address': 'r2@example.invalid', 'channel': 'email-v1'}) + '\n',
        encoding='utf-8')
    import_file(db, extra)
    assert evaluate(db)['R2'] == 'insufficient'
    out = sync_eligibility(db, 'route-1')
    assert out['frozen'] is True
    assert {c['recipient'] for c in out['changed']} == {'R2'}
    assert out['changed'][0]['reason'] == 'event_source_changed'
    report = field_visit.build_report(db, 'route-1')
    assert report['status'] == 'frozen'
    assert [i['recipient'] for i in report['inspections']] == ['R1']
    # After rechecking with the new source snapshot, R2 is dispatchable again
    # and R1's completed visit stays on record.
    rechecked = recheck(db, 'route-1')
    assert rechecked['status'] == 'ready'
    replacement = plan(db, 'route-1', 'min_total_travel')
    assert {s['recipient'] for c in replacement['couriers'] for s in c['stops']} == {'R2'}
    assert replacement['base_state']['visited'] == ['R1']


# --------------------------------------------------------------- no route

def test_closure_forces_detour_on_directed_graph(tmp_path):
    """A closed direct road makes the exact planner take the open detour."""
    doc = {
        'depot': 'D', 'finish': 'E', 'shift_end_minute': 30,
        'recipients': [
            {'id': 'R1', 'node': 'A', 'decision_status': 'undelivered',
             'event_source_sha256': SOURCE_SHA, 'requested_minute': 30,
             'observation': 'locked_mailbox'},
            {'id': 'R2', 'node': 'B', 'decision_status': 'insufficient',
             'event_source_sha256': SOURCE_SHA, 'requested_minute': 30,
             'observation': 'address_confirmed'},
        ],
        'edges': [
            {'from': 'D', 'to': 'A', 'minutes': 1},
            {'from': 'A', 'to': 'B', 'minutes': 1},
            {'from': 'A', 'to': 'C', 'minutes': 1},
            {'from': 'C', 'to': 'B', 'minutes': 1},
            {'from': 'B', 'to': 'E', 'minutes': 1},
            {'from': 'A', 'to': 'E', 'minutes': 1},
        ],
        'closures': [
            {'from': 'A', 'to': 'B', 'start_minute': 0, 'end_minute': 30},
        ],
    }
    db = prepared(tmp_path)
    register(db, 'detour', write_case(tmp_path, doc, 'detour.json'))
    planned = plan(db, 'detour', 'min_total_travel')
    courier = planned['couriers'][0]
    assert [s['recipient'] for s in courier['stops']] == ['R1', 'R2']
    paths = [leg['path'] for leg in courier['legs']]
    assert ['A', 'B'] not in paths
    assert paths[1] == ['A', 'C', 'B']
    assert planned['metrics']['total_travel_minutes'] == 4
    move(db, 'detour', 'A', 'm1')
    with pytest.raises(ValueError, match='road_closed'):
        move(db, 'detour', 'B', 'direct-closed')
    move(db, 'detour', 'B', 'detour-move', path=['A', 'C', 'B'])
    assert state(db, 'detour')['couriers'][0]['minute'] == 3
    assert state(db, 'detour')['couriers'][0]['node'] == 'B'
    # A closed road is not a straight line: the detour is two real edges.
    assert [seg['id'] for seg in state(db, 'detour')['moves'][-2:]] == [
        'detour-move:0', 'detour-move:1']


def test_no_feasible_route_when_shift_too_short(tmp_path):
    doc = base_doc(shift_end_minute=5)
    db = prepared(tmp_path)
    with pytest.raises(ValueError, match='no_feasible_route'):
        register(db, 'short', write_case(tmp_path, doc, 'short.json'))
        plan(db, 'short', 'min_total_travel')


def test_unreachable_site_reports_no_feasible_route(tmp_path):
    doc = base_doc(shift_end_minute=10)
    db = prepared(tmp_path)
    register(db, 'short', write_case(tmp_path, doc, 'short.json'))
    with pytest.raises(ValueError, match='no_feasible_route'):
        plan(db, 'short', 'min_total_travel')
    # A failed plan is audited and never stored as an active plan.
    live = state(db, 'short')
    assert live['plan'] is None
    assert any(a['action'] == 'plan_failed' for a in field_visit.build_report(db, 'short')['audit'])


def test_site_count_limit_is_enforced(tmp_path):
    doc = base_doc()
    doc['recipients'] = doc['recipients'] * 5
    doc['recipients'] = [dict(r, id=f"R{i}", node=f"N{i}") for i, r in
                         enumerate(doc['recipients'])]
    path = write_case(tmp_path, doc, 'many.json')
    db = tmp_path / 'many.db'
    freeze_config(db, FIX / 'field_config.json')
    import_file(db, FIX / 'field_events.jsonl')
    evaluate(db)
    with pytest.raises(ValueError, match='too_many_sites'):
        register(db, 'many', path)


# -------------------------------------------------------------------- reportdef test_report_carries_snapshots_plans_and_actuals(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_max_lateness')
    walk(db, ['D', 'B', 'A', 'E'], ['R2', 'R1', None])
    out = field_visit.write_report(db, tmp_path / 'field.json', 'route-1')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['report'] == 'field_verification'
    assert out.with_suffix('.md').exists()
    assert data['original_delivery_unchanged'] is True
    assert data['inspections_are_not_delivery_receipts'] is True
    by_recipient = {r['recipient']: r for r in data['recipients']}
    assert by_recipient['R1']['original_decision']['status'] == 'undelivered'
    assert by_recipient['R2']['original_decision']['status'] == 'insufficient'
    assert by_recipient['R1']['original_decision']['source_sha256'] == [SOURCE_SHA]
    assert data['plans'][0]['optimality']['no_better_plan'] is True
    assert [v['recipient'] for v in data['actual']['visit_order']] == ['R2', 'R1']
    assert [v['late_minutes'] for v in data['actual']['visit_order']] == [2, 0]
    assert data['status'] == 'completed'


def test_reverse_only_road_is_never_assumed(tmp_path):
    """A case whose only access is the directed road, never the reverse one."""
    doc = {
        'depot': 'D', 'finish': 'E', 'shift_end_minute': 30,
        'recipients': [
            {'id': 'R1', 'node': 'A', 'decision_status': 'undelivered',
             'event_source_sha256': SOURCE_SHA, 'requested_minute': 30,
             'observation': 'locked_mailbox'},
            {'id': 'R2', 'node': 'B', 'decision_status': 'insufficient',
             'event_source_sha256': SOURCE_SHA, 'requested_minute': 5,
             'observation': 'address_confirmed'},
        ],
        # No D->A and no A->B: only the directed B->A road exists.
        'edges': [
            {'from': 'D', 'to': 'B', 'minutes': 1},
            {'from': 'B', 'to': 'A', 'minutes': 3},
            {'from': 'A', 'to': 'E', 'minutes': 1},
        ],
    }
    db = prepared(tmp_path)
    register(db, 'reverse', write_case(tmp_path, doc, 'reverse.json'))
    planned = plan(db, 'reverse', 'min_total_travel')
    courier = planned['couriers'][0]
    assert [s['recipient'] for s in courier['stops']] == ['R2', 'R1']
    assert [leg['path'] for leg in courier['legs']] == [
        ['D', 'B'], ['B', 'A'], ['A', 'E']]
    assert planned['metrics']['total_travel_minutes'] == 5
    # Straight-line distance would be shorter but is not a road.
    with pytest.raises(ValueError, match='road_unavailable'):
        move(db, 'reverse', 'A', 'straight-line')


def test_midway_closure_replans_only_unvisited(tmp_path):
    db = prepared(tmp_path)
    planned = plan(db, 'route-1', 'min_total_travel')
    assert [s['recipient'] for s in planned['couriers'][0]['stops']] == ['R1', 'R2']
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    # A->B is closed for the rest of the shift: R2 can no longer be reached
    # from A on this directed network, so the remaining dispatch stops rather
    # than silently skipping R2.
    out = close_road(db, 'route-1', 'A', 'B', 0, 30,
                     closure_id='close-AB', replan=True)
    assert out['replan_required'] is True
    assert out['replacement_plan'] is None
    assert out['replan_error'] == 'no_feasible_route'
    with pytest.raises(ValueError, match='road_closed'):
        move(db, 'route-1', 'B', 'blocked')
    assert [i['recipient'] for i in state(db, 'route-1')['inspections']] == ['R1']
    # Reopening allows a new version covering only R2, from the real position.
    reopen_road(db, 'route-1', 'close-AB')
    replacement = plan(db, 'route-1', 'min_total_travel')
    assert replacement['version'] == 2
    assert [s['recipient'] for s in replacement['couriers'][0]['stops']] == ['R2']
    assert replacement['base_state']['visited'] == ['R1']
    move(db, 'route-1', 'B', 'm2')
    inspect(db, 'route-1', 'R2', 'v2')
    move(db, 'route-1', 'E', 'm3')
    visited = [i['recipient'] for i in state(db, 'route-1')['inspections']]
    assert visited == ['R1', 'R2']
    assert state(db, 'route-1')['status'] == 'completed'


def test_close_road_replan_flag_creates_replacement(tmp_path):
    db = prepared(tmp_path)
    plan(db, 'route-1', 'min_total_travel')
    move(db, 'route-1', 'A', 'm1')
    inspect(db, 'route-1', 'R1', 'v1')
    # A->B is only needed to reach R2; closing B->E instead leaves R2
    # reachable but forces a different remaining route once replanning runs.
    out = close_road(db, 'route-1', 'A', 'B', 12, 30,
                     closure_id='close-AB-late', replan=True)
    # The original plan walked A->B at minute 1, before the window, so no plan
    # leg is interrupted and no replacement is needed.
    assert out['replan_required'] is False
    assert 'replacement_plan' not in out
    assert plan(db, 'route-1', 'min_total_travel')['version'] == 2


def test_cli_field_roundtrip(tmp_path, capsys):
    from receiptdesk.__main__ import main
    db = tmp_path / 'cli.db'
    freeze_config(db, FIX / 'field_config.json')
    import_file(db, FIX / 'field_events.jsonl')
    evaluate(db)
    fixture = str(FIX / 'field_case.json')
    import sys
    old = sys.argv
    try:
        sys.argv = ['receiptdesk', 'field-register', '--db', str(db),
                    '--case', 'route-1', '--fixture', fixture]
        main()
        sys.argv = ['receiptdesk', 'field-plan', '--db', str(db),
                    '--case', 'route-1', '--objective', 'min_max_lateness']
        main()
        sys.argv = ['receiptdesk', 'field-state', '--db', str(db), '--case', 'route-1']
        main()
    finally:
        sys.argv = old
    output = capsys.readouterr().out
    assert 'no_better_plan' in output
