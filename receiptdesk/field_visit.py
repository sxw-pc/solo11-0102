"""Offline field-verification dispatch: planning, execution, recovery.

This module deliberately keeps two ledgers apart:

* ``field_plan`` / ``field_plan_stop`` / ``field_plan_leg`` — what was
  *planned*, versioned and never rewritten by walking.
* ``field_move`` / ``field_inspection`` — what a courier *actually did*, append
  only and replay-safe through the caller supplied operation id.

The planner is an exact, exhaustive search over every courier/site assignment
and every visit permutation (at most eight sites and two couriers), implemented
as subset dynamic programming with a dominance rule that discards only routes
proved interchangeable.  It only follows directed roads and honours
time-window closures, so a reverse road or a straight-line shortcut is never
assumed.  Field visits never touch ``decision``/``evidence``: a site
observation is not a mail receipt.
"""

import hashlib
import heapq
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import connect

OBJECTIVES = ('min_total_travel', 'min_max_lateness')
ELIGIBLE_STATUSES = ('undelivered', 'insufficient')
DEFAULT_COURIER = 'c1'
MAX_SITES = 8
MAX_COURIERS = 2


# ----------------------------------------------------------------- utilities

def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _audit(db, case_id, action, details):
    db.execute('INSERT INTO field_audit(case_id,action,at,details) VALUES (?,?,?,?)',
               (case_id, action, _now(), _canonical(details)))


def _case_doc(db, case_id):
    row = db.execute('SELECT payload FROM field_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('unknown_field_case')
    return json.loads(row['payload'])


def _courier_specs(doc):
    raw = doc.get('couriers')
    if not raw:
        raw = [{'id': DEFAULT_COURIER, 'depot': doc.get('depot'),
                'finish': doc.get('finish')}]
    specs = []
    for index, item in enumerate(raw):
        specs.append({
            'id': str(item.get('id') or f'c{index + 1}'),
            'start': item.get('start') or item.get('depot') or doc.get('depot'),
            'finish': item.get('finish') or item.get('end') or doc.get('finish'),
            'start_minute': int(item.get('start_minute', 0)),
        })
    if len(specs) > MAX_COURIERS:
        raise ValueError('too_many_couriers')
    if len({s['id'] for s in specs}) != len(specs):
        raise ValueError('duplicate_courier')
    for spec in specs:
        if not spec['start'] or not spec['finish']:
            raise ValueError('bad_courier_depot')
    return specs


def _edge_map(db, case_id):
    edges = {}
    for row in db.execute(
            'SELECT origin,destination,minutes FROM field_edge WHERE case_id=?',
            (case_id,)):
        edges[(row['origin'], row['destination'])] = row['minutes']
    return edges


def _closure_list(db, case_id):
    return [
        {'origin': r['origin'], 'destination': r['destination'],
         'start_minute': r['start_minute'], 'end_minute': r['end_minute']}
        for r in db.execute(
            'SELECT origin,destination,start_minute,end_minute FROM field_closure '
            'WHERE case_id=? ORDER BY origin,destination,start_minute', (case_id,))
    ]
def _adjacency(edges):
    adj = {}
    for (origin, destination), minutes in edges.items():
        adj.setdefault(origin, []).append((destination, minutes))
    for values in adj.values():
        values.sort()
    return adj


def _closure_index(closures):
    index = {}
    for item in closures:
        index.setdefault((item['origin'], item['destination']), []).append(
            (item['start_minute'], item['end_minute']))
    for values in index.values():
        values.sort()
    return index


def _closed(index, origin, destination, minute):
    for start, end in index.get((origin, destination), ()):
        if start <= minute < end:
            return True
    return False


# ------------------------------------------------------------- route solving

def _earliest(adj, closed, start, depart, target, horizon, cache):
    """Earliest arrival from ``start`` at ``depart`` over open directed roads.

    Returns ``(arrival, path, road_minutes, segments)`` or ``None``.  Arrival
    time accumulates road minutes exactly as the edge weights state.  A road
    whose departure minute falls inside one of its closure windows is not
    passable at that moment, so the planner must find another ordered path;
    reverse roads and straight-line shortcuts are never considered.
    """
    if start == target:
        return depart, (start,), 0, []
    key = (start, depart, target, horizon)
    if key in cache:
        return cache[key]
    heap = [(depart, start, (start,), 0, ())]
    seen = set()
    result = None
    while heap:
        minute, node, path, road, segments = heapq.heappop(heap)
        if (node, minute) in seen:
            continue
        seen.add((node, minute))
        if node == target:
            result = (minute, path, road, list(segments))
            break
        for destination, minutes in adj.get(node, ()):
            if _closed(closed, node, destination, minute):
                continue
            arrival = minute + minutes
            if arrival > horizon:
                continue
            heapq.heappush(heap, (
                arrival, destination, path + (destination,), road + minutes,
                segments + ((node, destination, minute, arrival, minutes),)))
    cache[key] = result
    return result


def _leg(origin, destination, depart, arrive, path, segments, recipient, sequence):
    road = sum(minutes for _, _, _, _, minutes in segments)
    return {'sequence': sequence, 'origin': origin, 'destination': destination,
            'depart_minute': depart, 'arrive_minute': arrive,
            'minutes': arrive - depart, 'road_minutes': road,
            'wait_minutes': (arrive - depart) - road,
            'path': list(path), 'recipient': recipient,
            'segments': [
                {'from': a, 'to': b, 'depart_minute': left,
                 'arrive_minute': reached, 'minutes': minutes}
                for a, b, left, reached, minutes in segments]}


def _arrival_of(label):
    return label.get('arrival', label.get('finish_minute', 0))


def _label_key(label, objective):
    order = tuple(s['recipient'] for s in label['stops'])
    arrival = _arrival_of(label)
    if objective == 'min_total_travel':
        return (label['travel_minutes'], label['max_late_minutes'],
                arrival, order)
    return (label['max_late_minutes'], label['travel_minutes'],
            arrival, order)


def _prune(labels, objective):
    """Keep exactly the labels an optimum can still start from.

    Couriers never stand still: arrival at a state equals departure plus the
    accumulated road minutes.  Two labels at the same (mask, last site) with
    the same accumulated road minutes therefore arrive at the same minute, so
    every continuation available to one is available to the other; only the
    smaller accumulated lateness can matter.  Different accumulated road
    minutes arrive at different minutes, and a time-window closure can make
    the later arrival pass where the earlier one cannot, so those labels are
    all retained.  This keeps the frontier exact for both objectives while
    collapsing equivalent routes deterministically.
    """
    best = {}
    for label in labels:
        key = label['travel_minutes']
        current = best.get(key)
        order = tuple(s['recipient'] for s in label['stops'])
        if current is None:
            best[key] = label
            continue
        current_order = tuple(s['recipient'] for s in current['stops'])
        if (label['max_late_minutes'], _arrival_of(label), order) < \
                (current['max_late_minutes'], _arrival_of(current), current_order):
            best[key] = label
    return sorted(best.values(), key=lambda x: _label_key(x, objective))


def _empty_label(courier, adj, closed, horizon, cache):
    found = _earliest(adj, closed, courier['start'], courier['start_minute'],
                      courier['finish'], horizon, cache)
    if not found:
        return None
    arrival, path, road, segments = found
    if arrival > horizon:
        return None
    leg = _leg(origin=courier['start'], destination=courier['finish'],
               depart=courier['start_minute'], arrive=arrival, path=path,
               segments=segments, recipient=None, sequence=1)
    return {'travel_minutes': road,
            'max_late_minutes': 0, 'arrival': arrival,
            'finish_minute': arrival, 'stops': [], 'legs': [leg]}


def _subset_options(courier, sites, adj, closed, horizon, objective, cache):
    """Exact route options for every site subset of one courier.

    Subset dynamic programming over ``(visited_mask, last_site)`` with exact
    directed, closure-aware legs.  The courier split then enumerates these
    options across all subsets, so the global comparison sees every feasible
    assignment and every feasible visit order.  ``_prune`` keeps only routes
    that are provably interchangeable with a better one.
    """
    n = len(sites)
    states = {}
    for j, site in enumerate(sites):
        found = _earliest(adj, closed, courier['start'], courier['start_minute'],
                          site['node'], horizon, cache)
        if not found:
            continue
        arrival, path, road, segments = found
        if arrival > horizon:
            continue
        leg = _leg(origin=courier['start'], destination=site['node'],
                   depart=courier['start_minute'], arrive=arrival, path=path,
                   segments=segments, recipient=site['recipient'], sequence=1)
        label = {
            'travel_minutes': road,
            'max_late_minutes': max(0, arrival - site['requested_minute']),
            'arrival': arrival,
            'stops': [{'sequence': 1, 'recipient': site['recipient'],
                       'node': site['node'],
                       'requested_minute': site['requested_minute'],
                       'planned_arrival': arrival,
                       'late_minutes': max(0, arrival - site['requested_minute'])}],
            'legs': [leg],
        }
        states.setdefault((1 << j, j), []).append(label)
    for mask in range(1, 1 << n):
        for last in range(n):
            labels = states.get((mask, last))
            if not labels:
                continue
            labels = _prune(labels, objective)
            states[(mask, last)] = labels
            for label in labels:
                node, minute = sites[last]['node'], label['arrival']
                for j in range(n):
                    if mask & (1 << j):
                        continue
                    found = _earliest(adj, closed, node, minute, sites[j]['node'],
                                      horizon, cache)
                    if not found:
                        continue
                    arrival, path, road, segments = found
                    if arrival > horizon:
                        continue
                    late = max(0, arrival - sites[j]['requested_minute'])
                    sequence = len(label['stops']) + 1
                    leg = _leg(origin=node, destination=sites[j]['node'],
                               depart=minute, arrive=arrival, path=path,
                               segments=segments,
                               recipient=sites[j]['recipient'],
                               sequence=sequence)
                    new_label = {
                        'travel_minutes': label['travel_minutes'] + road,
                        'max_late_minutes': max(label['max_late_minutes'], late),
                        'arrival': arrival,
                        'stops': label['stops'] + [{
                            'sequence': sequence,
                            'recipient': sites[j]['recipient'],
                            'node': sites[j]['node'],
                            'requested_minute': sites[j]['requested_minute'],
                            'planned_arrival': arrival,
                            'late_minutes': late}],
                        'legs': label['legs'] + [leg],
                    }
                    states.setdefault((mask | (1 << j), j), []).append(new_label)
    options = {}
    for mask in range(1 << n):
        candidates = []
        if mask == 0:
            empty = _empty_label(courier, adj, closed, horizon, cache)
            candidates = [empty] if empty else []
        else:
            for last in range(n):
                for label in states.get((mask, last), ()):
                    found = _earliest(adj, closed, sites[last]['node'],
                                      label['arrival'], courier['finish'],
                                      horizon, cache)
                    if not found:
                        continue
                    arrival, path, road, segments = found
                    if arrival > horizon:
                        continue
                    leg = _leg(origin=sites[last]['node'],
                               destination=courier['finish'],
                               depart=label['arrival'], arrive=arrival,
                               path=path, segments=segments, recipient=None,
                               sequence=len(label['stops']) + 1)
                    candidates.append({
                        'travel_minutes': label['travel_minutes'] + road,
                        'max_late_minutes': label['max_late_minutes'],
                        'finish_minute': arrival,
                        'stops': label['stops'],
                        'legs': label['legs'] + [leg],
                    })
        options[mask] = _prune(candidates, objective)
    return options


def _option_signature(courier_id, option):
    return (courier_id, tuple(s['recipient'] for s in option['stops']))


def solve_dispatch(couriers, sites, edges, closures, shift_end, objective):
    """Exact joint assignment/order/route search for up to 8 sites.

    ``couriers`` entries: ``{'id','start','finish','start_minute'}``.
    Returns ``{'couriers': [...], 'metrics': ..., 'optimality': ...}``.
    Raises ``no_feasible_route`` when no plan visits every site in shift.

    Every site subset and visit order is represented by exact subset DP over
    directed roads; the courier split is then enumerated over all subsets, so
    the result is the global optimum of the finite problem.
    """
    if objective not in OBJECTIVES:
        raise ValueError('unknown_objective')
    if len(sites) > MAX_SITES:
        raise ValueError('too_many_sites')
    if len(couriers) > MAX_COURIERS or not couriers:
        raise ValueError('bad_courier_count')
    if not sites:
        raise ValueError('no_remaining_recipients')
    ordered = sorted(sites, key=lambda s: s['recipient'])
    adj = _adjacency(edges)
    closed = _closure_index(closures)
    cache = {}
    per_courier = [
        _subset_options(courier, ordered, adj, closed,
                        shift_end, objective, cache)
        for courier in couriers
    ]
    full = (1 << len(ordered)) - 1
    best = None
    best_key = None
    second_key = None
    partitions = 0
    combinations = 0

    def note_key(key):
        nonlocal best_key, second_key
        if best_key is None or key < best_key:
            if best_key is not None and (second_key is None or best_key < second_key):
                second_key = best_key
            best_key = key
        elif key != best_key and (second_key is None or key < second_key):
            second_key = key

    def consider(mask, picks):
        nonlocal best, partitions, combinations
        partitions += 1
        total_travel = sum(p['travel_minutes'] for p in picks)
        max_late = max(p['max_late_minutes'] for p in picks)
        signature = tuple(sorted(
            _option_signature(couriers[i]['id'], picks[i])
            for i in range(len(couriers))))
        if objective == 'min_total_travel':
            key = (total_travel, max_late, signature)
        else:
            key = (max_late, total_travel, signature)
        if best_key is not None and key >= best_key:
            note_key(key)
            return
        note_key(key)
        best = {
            'couriers': [
                {
                    'courier_id': couriers[i]['id'],
                    'start': {'node': couriers[i]['start'],
                              'minute': couriers[i]['start_minute']},
                    'finish_node': couriers[i]['finish'],
                    'travel_minutes': picks[i]['travel_minutes'],
                    'max_late_minutes': picks[i]['max_late_minutes'],
                    'finish_minute': picks[i]['finish_minute'],
                    'stops': picks[i]['stops'],
                    'legs': picks[i]['legs'],
                }
                for i in range(len(couriers))
            ],
            'metrics': {
                'total_travel_minutes': total_travel,
                'max_late_minutes': max_late,
                'max_finish_minute': max(p['finish_minute'] for p in picks),
                'stops': len(ordered),
                'per_courier_travel': {
                    couriers[i]['id']: picks[i]['travel_minutes']
                    for i in range(len(couriers))
                },
            },
        }

    if len(couriers) == 1:
        for option in per_courier[0].get(full, ()):
            combinations += 1
            consider(full, [option])
    else:
        for mask in range(1 << len(ordered)):
            complement = full ^ mask
            left = per_courier[0].get(mask, ())
            right = per_courier[1].get(complement, ())
            for a in left:
                for b in right:
                    combinations += 1
                    consider(mask, [a, b])
    if best is None:
        raise ValueError('no_feasible_route')
    best['optimality'] = {
        'method': 'exact_subset_dp_over_assignments_and_permutations',
        'complete_search_space': True,
        'partitions_checked': partitions,
        'candidate_route_combinations': combinations,
        'objective': objective,
        'best_metric_key': list(best_key),
        'runner_up_metric_key': list(second_key) if second_key else None,
        'no_better_plan': True,
        'why_no_better': ('every split of the registered sites between the '
                          'authorised couriers and every visit order was solved '
                          'with exact directed-road legs; dominated partial '
                          'routes were discarded only when another route '
                          'reached the same place, mask and last site with no '
                          'more road minutes, no more lateness and no later '
                          'arrival; the reported plan has the lexicographically '
                          'smallest objective key among all feasible plans'),
        'semantics': 'total_travel then max_lateness' if objective == 'min_total_travel'
                     else 'max_lateness then total_travel',
        'directed_edges_only': True,
        'closures_applied': True,
        'shift_end_minute': shift_end,
        'site_count': len(ordered),
        'courier_count': len(couriers),
    }
    return best


# ------------------------------------------------------- eligibility snapshot

def _current_snapshot(db, subject):
    decision = db.execute(
        'SELECT status,details,rule_version,run_id FROM decision WHERE subject=?',
        (subject,)).fetchone()
    events = db.execute(
        '''SELECT e.id,e.sha256,s.sha256 AS source_sha
           FROM event e JOIN source s ON s.id=e.source_id
           WHERE e.subject=? ORDER BY e.id''', (subject,)).fetchall()
    payload = {
        'status': decision['status'] if decision else None,
        'details': decision['details'] if decision else None,
        'rule_version': decision['rule_version'] if decision else None,
        'run_id': decision['run_id'] if decision else None,
        'event_ids': [e['id'] for e in events],
        'event_shas': [e['sha256'] for e in events],
        'source_shas': sorted({e['source_sha'] for e in events}),
    }
    payload['signature'] = _sha({k: payload[k] for k in
                                 ('status', 'details', 'rule_version',
                                  'event_shas', 'source_shas')})
    return payload


def _snapshot_fields(prefix, snapshot):
    return {
        prefix + '_status': snapshot['status'],
        prefix + '_details': snapshot['details'],
        prefix + '_rule_version': snapshot['rule_version'],
        prefix + '_run_id': snapshot['run_id'],
        prefix + '_event_ids': _canonical(snapshot['event_ids']),
        prefix + '_event_shas': _canonical(snapshot['event_shas']),
        prefix + '_source_shas': _canonical(snapshot['source_shas']),
    }


def _recipient_row(db, case_id, recipient):
    return db.execute('SELECT * FROM field_recipient WHERE case_id=? AND recipient=?',
                      (case_id, recipient)).fetchone()


def _diff_reason(row, snapshot):
    if snapshot['status'] is None:
        return 'decision_missing'
    if snapshot['status'] not in ELIGIBLE_STATUSES:
        return 'decision_status_changed'
    if row['snapshot_signature'] is None or snapshot['signature'] != row['snapshot_signature']:
        if snapshot['event_shas'] != json.loads(row['snapshot_event_shas'] or '[]'):
            return 'event_source_changed'
        if snapshot['source_shas'] != json.loads(row['snapshot_source_shas'] or '[]'):
            return 'source_snapshot_changed'
        return 'decision_changed'
    return None


def sync_eligibility(db_path, case_id):
    """Invalidate registered targets whose decision/source snapshot moved.

    Visited facts are never deleted; instead the case freezes and every open
    plan is cancelled so a human recheck happens before more dispatch.
    """
    db = connect(db_path)
    try:
        with db:
            return _sync_eligibility(db, case_id)
    finally:
        db.close()


def _sync_eligibility(db, case_id):
    _case_doc(db, case_id)
    changed = []
    rows = list(db.execute(
        'SELECT * FROM field_recipient WHERE case_id=? ORDER BY recipient',
        (case_id,)))
    for row in rows:
        if row['eligibility'] != 'active':
            continue
        snapshot = _current_snapshot(db, row['recipient'])
        reason = _diff_reason(row, snapshot)
        if reason is None:
            continue
        db.execute(
            '''UPDATE field_recipient SET eligibility='invalidated',
               invalidation_reason=?,rechecked_at=? WHERE case_id=? AND recipient=?''',
            (reason, _now(), case_id, row['recipient']))
        changed.append({'recipient': row['recipient'], 'reason': reason,
                        'old_signature': row['snapshot_signature'],
                        'new_signature': snapshot['signature']})
    if changed:
        db.execute(
            '''UPDATE field_case_state SET status='frozen',recheck_required=1,
               updated_at=? WHERE case_id=?''', (_now(), case_id))
        db.execute(
            '''UPDATE field_plan SET status='invalidated'
               WHERE case_id=? AND status IN ('active','interrupted')''',
            (case_id,))
        _audit(db, case_id, 'eligibility_invalidated', {'changed': changed})
    return {'case_id': case_id, 'changed': changed, 'frozen': bool(changed)}


def recheck(db_path, case_id):
    """Re-read the decision/source snapshot after an invalidation.

    Still non-effective targets resume with the new snapshot; a now-effective
    target is excluded (never visited).  Facts already observed stay frozen in
    the inspection ledger and are never reinterpreted as a delivery receipt.
    """
    db = connect(db_path)
    try:
        with db:
            _case_doc(db, case_id)
            rows = list(db.execute(
                'SELECT * FROM field_recipient WHERE case_id=? ORDER BY recipient',
                (case_id,)))
            results = []
            for row in rows:
                snapshot = _current_snapshot(db, row['recipient'])
                if row['eligibility'] == 'active' \
                        and snapshot['signature'] == row['snapshot_signature']:
                    results.append({'recipient': row['recipient'],
                                    'eligibility': 'active', 'changed': False})
                    continue
                eligible = snapshot['status'] in ELIGIBLE_STATUSES
                eligibility = 'active' if eligible else 'excluded'
                reason = None if eligible else f"decision_{snapshot['status']}"
                fields = _snapshot_fields('snapshot', snapshot)
                db.execute(
                    '''UPDATE field_recipient SET snapshot_status=?,snapshot_details=?,
                       snapshot_rule_version=?,snapshot_run_id=?,snapshot_event_ids=?,
                       snapshot_event_shas=?,snapshot_source_shas=?,snapshot_signature=?,
                       eligibility=?,invalidation_reason=?,rechecked_at=?
                       WHERE case_id=? AND recipient=?''',
                    (fields['snapshot_status'], fields['snapshot_details'],
                     fields['snapshot_rule_version'], fields['snapshot_run_id'],
                     fields['snapshot_event_ids'], fields['snapshot_event_shas'],
                     fields['snapshot_source_shas'], snapshot['signature'],
                     eligibility, reason, _now(), case_id, row['recipient']))
                results.append({'recipient': row['recipient'],
                                'eligibility': eligibility,
                                'changed': row['eligibility'] != eligibility
                                or snapshot['signature'] != row['snapshot_signature'],
                                'previous_reason': row['invalidation_reason'],
                                'current_status': snapshot['status']})
            db.execute(
                '''UPDATE field_case_state SET status='ready',recheck_required=0,
                   updated_at=? WHERE case_id=?''', (_now(), case_id))
            _audit(db, case_id, 'eligibility_rechecked', {'recipients': results})
            return {'case_id': case_id, 'status': 'ready',
                    'recipients': results,
                    'visited_facts_preserved': True}
    finally:
        db.close()


# ------------------------------------------------------------------ register

def _parse_closures(doc):
    closures = []
    for item in doc.get('closures') or doc.get('closed_edges') or []:
        closures.append({
            'origin': item.get('from') or item.get('origin'),
            'destination': item.get('to') or item.get('destination'),
            'start_minute': int(item.get('start_minute', item.get('start', 0))),
            'end_minute': int(item.get('end_minute', item.get('end', 0))),
        })
    for edge in doc['edges']:
        for window in edge.get('closed') or []:
            if isinstance(window, dict):
                closures.append({
                    'origin': edge['from'], 'destination': edge['to'],
                    'start_minute': int(window.get('start_minute', window.get('start', 0))),
                    'end_minute': int(window.get('end_minute', window.get('end', 0))),
                })
            else:
                closures.append({
                    'origin': edge['from'], 'destination': edge['to'],
                    'start_minute': int(window[0]), 'end_minute': int(window[1]),
                })
    for item in closures:
        if not item['origin'] or not item['destination'] \
                or item['start_minute'] >= item['end_minute'] or item['start_minute'] < 0:
            raise ValueError('bad_field_closure')
    return closures


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('recipients') or not doc.get('edges'):
        raise ValueError('bad_field_case')
    if len(doc['recipients']) > MAX_SITES:
        raise ValueError('too_many_sites')
    couriers = _courier_specs(doc)
    if not doc.get('shift_end_minute') or int(doc['shift_end_minute']) <= 0:
        raise ValueError('bad_shift_end')
    nodes = {e['from'] for e in doc['edges']} | {e['to'] for e in doc['edges']}
    seen_nodes = set()
    for spec in doc['recipients']:
        if not spec.get('id') or not spec.get('node') or not spec.get('decision_status') \
                or not spec.get('event_source_sha256'):
            raise ValueError('bad_field_case')
        if spec['node'] in seen_nodes:
            raise ValueError('duplicate_recipient_site')
        seen_nodes.add(spec['node'])
        if spec['node'] not in nodes:
            raise ValueError('recipient_site_missing')
    for courier in couriers:
        if courier['start'] not in nodes or courier['finish'] not in nodes:
            raise ValueError('courier_site_missing')
    closures = _parse_closures(doc)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        with db:
            for spec in doc['recipients']:
                decision = db.execute(
                    'SELECT * FROM decision WHERE subject=?', (spec['id'],)).fetchone()
                if not decision or decision['status'] != spec['decision_status'] \
                        or decision['status'] not in ELIGIBLE_STATUSES:
                    raise ValueError('recipient_not_field_eligible')
                source = db.execute(
                    '''SELECT s.sha256 FROM event e JOIN source s ON s.id=e.source_id
                       WHERE e.subject=? ORDER BY e.id LIMIT 1''', (spec['id'],)
                ).fetchone()
                if not source or source['sha256'] != spec['event_source_sha256']:
                    raise ValueError('field_event_source_missing')
            old = db.execute('SELECT digest FROM field_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('field_case_conflict')
            db.execute('INSERT OR IGNORE INTO field_case VALUES (?,?,?)',
                       (case_id, json.dumps(doc, sort_keys=True), digest))
            db.execute("INSERT OR IGNORE INTO field_case_state VALUES (?,?,?,?,?)",
                       (case_id, 'ready', 1, 0, _now()))
            for edge in doc['edges']:
                minutes = int(edge['minutes'])
                if minutes <= 0:
                    raise ValueError('bad_edge_minutes')
                db.execute(
                    '''INSERT OR IGNORE INTO field_edge(case_id,origin,destination,minutes)
                       VALUES (?,?,?,?)''',
                    (case_id, edge['from'], edge['to'], minutes))
            for closure in closures:
                db.execute(
                    '''INSERT OR IGNORE INTO field_closure
                       (id,case_id,origin,destination,start_minute,end_minute)
                       VALUES (?,?,?,?,?,?)''',
                    (f"closure-{case_id}-{closure['origin']}-{closure['destination']}-"
                     f"{closure['start_minute']}-{closure['end_minute']}",
                     case_id, closure['origin'], closure['destination'],
                     closure['start_minute'], closure['end_minute']))
            for courier in couriers:
                db.execute(
                    '''INSERT OR IGNORE INTO field_courier(case_id,courier_id,node,minute)
                       VALUES (?,?,?,0)''',
                    (case_id, courier['id'], courier['start']))
            for spec in doc['recipients']:
                snapshot = _current_snapshot(db, spec['id'])
                if snapshot['signature'] is None:
                    raise ValueError('field_event_source_missing')
                existing = _recipient_row(db, case_id, spec['id'])
                original = _snapshot_fields('original', snapshot)
                snap = _snapshot_fields('snapshot', snapshot)
                if existing is None:
                    db.execute(
                        '''INSERT INTO field_recipient(
                           case_id,recipient,node,requested_minute,observation,
                           original_status,original_details,original_rule_version,
                           original_run_id,original_event_ids,original_event_shas,
                           original_source_shas,
                           snapshot_status,snapshot_details,snapshot_rule_version,
                           snapshot_run_id,snapshot_event_ids,snapshot_event_shas,
                           snapshot_source_shas,snapshot_signature,
                           eligibility,invalidation_reason,snapshot_at,rechecked_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (case_id, spec['id'], spec['node'],
                         int(spec['requested_minute']), str(spec['observation']),
                         original['original_status'], original['original_details'],
                         original['original_rule_version'], original['original_run_id'],
                         original['original_event_ids'], original['original_event_shas'],
                         original['original_source_shas'],
                         snap['snapshot_status'], snap['snapshot_details'],
                         snap['snapshot_rule_version'], snap['snapshot_run_id'],
                         snap['snapshot_event_ids'], snap['snapshot_event_shas'],
                         snap['snapshot_source_shas'], snapshot['signature'],
                         'active', None, _now(), None))
                else:
                    db.execute(
                        '''UPDATE field_recipient SET node=?,requested_minute=?,
                           observation=? WHERE case_id=? AND recipient=?''',
                        (spec['node'], int(spec['requested_minute']),
                         str(spec['observation']), case_id, spec['id']))
            _audit(db, case_id, 'registered',
                   {'digest': digest, 'couriers': [c['id'] for c in couriers],
                    'recipients': [r['id'] for r in doc['recipients']]})
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest,
            'recipients': len(doc['recipients']),
            'couriers': len(couriers)}


# --------------------------------------------------------------------- state

def _latest_plan(db, case_id):
    return db.execute(
        'SELECT * FROM field_plan WHERE case_id=? ORDER BY version DESC LIMIT 1',
        (case_id,)).fetchone()


def _courier_state(db, case_id):
    return [dict(row) for row in db.execute(
        'SELECT courier_id,node,minute FROM field_courier WHERE case_id=? '
        'ORDER BY courier_id', (case_id,))]


def _inspection_rows(db, case_id):
    return [dict(row) for row in db.execute(
        'SELECT rowid AS ordinal,id,courier_id,recipient,minute,observation,'
        'late_minutes,plan_id,plan_version FROM field_inspection '
        'WHERE case_id=? ORDER BY minute,rowid', (case_id,))]


def _move_rows(db, case_id):
    return [dict(row) for row in db.execute(
        'SELECT rowid AS ordinal,id,courier_id,origin,destination,minutes,arrival,'
        'plan_id,plan_version FROM field_move WHERE case_id=? ORDER BY rowid',
        (case_id,))]


def _already_inspected(db, case_id):
    return {row['recipient'] for row in db.execute(
        'SELECT recipient FROM field_inspection WHERE case_id=?', (case_id,))}


def _plan_view(db, plan_row):
    if not plan_row:
        return None
    payload = json.loads(plan_row['payload'])
    return {
        'plan_id': plan_row['id'], 'version': plan_row['version'],
        'objective': plan_row['objective'], 'status': plan_row['status'],
        'created_at': plan_row['created_at'],
        'metrics': json.loads(plan_row['metrics']),
        'optimality': json.loads(plan_row['optimality']),
        'couriers': payload['couriers'],
        'base_state': json.loads(plan_row['base_state']) if plan_row['base_state'] else None,
    }


def _remaining_from_plan(db, case_id, plan_row):
    if not plan_row:
        return []
    inspected = _already_inspected(db, case_id)
    stops = []
    for row in db.execute(
            'SELECT * FROM field_plan_stop WHERE plan_id=? ORDER BY courier_id,sequence',
            (plan_row['id'],)):
        item = dict(row)
        item['completed'] = item['recipient'] in inspected
        if not item['completed']:
            stops.append(item)
    return stops


def state(db_path, case_id):
    sync = sync_eligibility(db_path, case_id)
    db = connect(db_path)
    try:
        couriers = _courier_state(db, case_id)
        if not couriers:
            raise ValueError('unknown_field_case')
        case_state = db.execute(
            'SELECT * FROM field_case_state WHERE case_id=?', (case_id,)).fetchone()
        inspections = _inspection_rows(db, case_id)
        moves = _move_rows(db, case_id)
        latest = _latest_plan(db, case_id)
        first = couriers[0]
        return {
            'case_id': case_id,
            'node': first['node'], 'minute': first['minute'],
            'courier_id': first['courier_id'],
            'couriers': couriers,
            'inspections': inspections,
            'moves': moves,
            'status': case_state['status'] if case_state else 'ready',
            'recheck_required': bool(case_state['recheck_required']) if case_state else False,
            'eligibility': [
                {'recipient': row['recipient'], 'eligibility': row['eligibility'],
                 'reason': row['invalidation_reason']}
                for row in db.execute(
                    'SELECT recipient,eligibility,invalidation_reason FROM field_recipient '
                    'WHERE case_id=? ORDER BY recipient', (case_id,))],
            'plan': _plan_view(db, latest),
            'remaining_stops': _remaining_from_plan(db, case_id, latest),
            'eligibility_sync': sync,
        }
    finally:
        db.close()


# ------------------------------------------------------------------ planning

def _planning_inputs(db, case_id):
    doc = _case_doc(db, case_id)
    couriers = []
    for spec in _courier_specs(doc):
        row = db.execute(
            'SELECT node,minute FROM field_courier WHERE case_id=? AND courier_id=?',
            (case_id, spec['id'])).fetchone()
        if not row:
            raise ValueError('unknown_field_courier')
        couriers.append({'id': spec['id'], 'start': row['node'],
                         'start_minute': row['minute'], 'finish': spec['finish']})
    inspected = _already_inspected(db, case_id)
    sites = []
    for row in db.execute(
            'SELECT * FROM field_recipient WHERE case_id=? ORDER BY recipient',
            (case_id,)):
        if row['eligibility'] != 'active' or row['recipient'] in inspected:
            continue
        sites.append({'recipient': row['recipient'], 'node': row['node'],
                      'requested_minute': row['requested_minute']})
    return doc, couriers, sites


def plan(db_path, case_id, objective='min_total_travel'):
    """Create a new exact plan version for all remaining eligible sites."""
    if objective not in OBJECTIVES:
        raise ValueError('unknown_objective')
    db = connect(db_path)
    try:
        doc = _case_doc(db, case_id)
        case_state = db.execute(
            'SELECT * FROM field_case_state WHERE case_id=?', (case_id,)).fetchone()
        if case_state and case_state['recheck_required']:
            raise ValueError('case_requires_recheck')
        sync = _sync_eligibility(db, case_id)
        db.commit()
        if sync['frozen']:
            raise ValueError('case_requires_recheck')
        couriers, sites = _planning_inputs(db, case_id)[1:]
        edges = _edge_map(db, case_id)
        closures = _closure_list(db, case_id)
        try:
            solution = solve_dispatch(couriers, sites, edges, closures,
                                      int(doc['shift_end_minute']), objective)
        except ValueError as exc:
            with db:
                _audit(db, case_id, 'plan_failed',
                       {'objective': objective, 'error': str(exc),
                        'remaining': [s['recipient'] for s in sites]})
            raise
        with db:
            state_row = db.execute(
                'SELECT next_plan_version FROM field_case_state WHERE case_id=?',
                (case_id,)).fetchone()
            version = state_row['next_plan_version'] if state_row else 1
            db.execute(
                '''UPDATE field_plan SET status='superseded'
                   WHERE case_id=? AND status IN ('active','interrupted')''',
                (case_id,))
            base_state = {
                'couriers': [{'courier_id': c['id'], 'node': c['start'],
                              'minute': c['start_minute']} for c in couriers],
                'visited': sorted(_already_inspected(db, case_id)),
                'closures': closures,
                'source_signatures': {
                    row['recipient']: row['snapshot_signature']
                    for row in db.execute(
                        'SELECT recipient,snapshot_signature FROM field_recipient '
                        'WHERE case_id=?', (case_id,))},
            }
            payload = {'couriers': solution['couriers'],
                       'metrics': solution['metrics']}
            cursor = db.execute(
                '''INSERT INTO field_plan(case_id,version,objective,status,created_at,
                   payload,metrics,optimality,digest,base_state,request_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (case_id, version, objective, 'active', _now(),
                 _canonical(payload), _canonical(solution['metrics']),
                 _canonical(solution['optimality']),
                 _sha({'payload': payload, 'optimality': solution['optimality']}),
                 _canonical(base_state), None))
            plan_id = cursor.lastrowid
            for courier in solution['couriers']:
                for stop in courier['stops']:
                    db.execute(
                        '''INSERT INTO field_plan_stop(
                           plan_id,courier_id,sequence,recipient,node,requested_minute,
                           planned_arrival,late_minutes) VALUES (?,?,?,?,?,?,?,?)''',
                        (plan_id, courier['courier_id'], stop['sequence'],
                         stop['recipient'], stop['node'], stop['requested_minute'],
                         stop['planned_arrival'], stop['late_minutes']))
                for leg in courier['legs']:
                    db.execute(
                        '''INSERT INTO field_plan_leg(
                           plan_id,courier_id,sequence,origin,destination,minutes,
                           depart_minute,arrive_minute,path,recipient)
                           VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (plan_id, courier['courier_id'], leg['sequence'],
                         leg['origin'], leg['destination'], leg['minutes'],
                         leg['depart_minute'], leg['arrive_minute'],
                         _canonical(leg['path']), leg['recipient']))
            db.execute(
                '''UPDATE field_case_state SET next_plan_version=?,status='planned',
                   updated_at=? WHERE case_id=?''',
                (version + 1, _now(), case_id))
            _audit(db, case_id, 'planned',
                   {'version': version, 'objective': objective,
                    'metrics': solution['metrics'],
                    'optimality': solution['optimality']})
            view = _plan_view(db, db.execute(
                'SELECT * FROM field_plan WHERE id=?', (plan_id,)).fetchone())
            return view
    finally:
        db.close()


dispatch = plan


# --------------------------------------------------------------- execution

def _check_frozen(db, case_id):
    case_state = db.execute(
        'SELECT status,recheck_required FROM field_case_state WHERE case_id=?',
        (case_id,)).fetchone()
    if case_state and case_state['recheck_required']:
        raise ValueError('case_requires_recheck')


def _closure_error(db, case_id, origin, destination, minute):
    row = db.execute(
        '''SELECT 1 FROM field_closure WHERE case_id=? AND origin=? AND destination=?
           AND start_minute<=? AND ?<end_minute''',
        (case_id, origin, destination, minute, minute)).fetchone()
    return row is not None


def _courier_row(db, case_id, courier_id):
    return db.execute(
        'SELECT node,minute FROM field_courier WHERE case_id=? AND courier_id=?',
        (case_id, courier_id)).fetchone()


def _maybe_complete(db, case_id):
    """Mark the case finished only when every eligible site is inspected and
    every courier has reached its finish point inside the shift."""
    doc = _case_doc(db, case_id)
    state_row = db.execute(
        'SELECT status,recheck_required FROM field_case_state WHERE case_id=?',
        (case_id,)).fetchone()
    if not state_row or state_row['recheck_required']:
        return False
    pending = db.execute(
        "SELECT 1 FROM field_recipient WHERE case_id=? AND eligibility='active' "
        "AND recipient NOT IN (SELECT recipient FROM field_inspection WHERE case_id=?) "
        "LIMIT 1", (case_id, case_id)).fetchone()
    if pending:
        return False
    for spec in _courier_specs(doc):
        row = _courier_row(db, case_id, spec['id'])
        if not row or row['node'] != spec['finish']:
            return False
        if row['minute'] > int(doc['shift_end_minute']):
            return False
    db.execute("UPDATE field_case_state SET status='completed',updated_at=? "
               "WHERE case_id=?", (_now(), case_id))
    _audit(db, case_id, 'completed', {'inspected': sorted(_already_inspected(db, case_id))})
    return True


def move(db_path, case_id, destination, operation_id, courier_id=DEFAULT_COURIER,
         path=None):
    """Advance a courier over one or more directed, currently open roads.

    ``path`` (optional) is the full node sequence; every road segment gets its
    own append-only execution row, all keyed under the caller's operation id.
    Replaying the id returns the recorded facts without moving again.
    """
    db = connect(db_path)
    try:
        prefix = f'{operation_id}:'
        existing = [dict(r) for r in db.execute(
            'SELECT * FROM field_move WHERE id=? OR id LIKE ? ORDER BY rowid',
            (operation_id, prefix + '%'))]
        if existing:
            last = existing[-1]
            if last['destination'] != destination:
                raise ValueError('operation_conflict')
            return {'status': 'replayed', 'courier_id': last['courier_id'],
                    'origin': existing[0]['origin'],
                    'destination': last['destination'],
                    'minute': last['arrival'],
                    'segments': existing,
                    'record': existing[0]}
        # Eligibility sync commits on its own connection so a freeze survives
        # the rejection of this move.
        sync = sync_eligibility(db_path, case_id)
        if sync['frozen']:
            raise ValueError('case_requires_recheck')
        with db:
            _check_frozen(db, case_id)
            _case_doc(db, case_id)
            courier = _courier_row(db, case_id, courier_id)
            if not courier:
                raise ValueError('unknown_field_courier')
            route = ([courier['node']] if destination == courier['node']
                     else [courier['node'], destination]) if path is None else list(path)
            if not route or route[0] != courier['node'] or route[-1] != destination:
                raise ValueError('bad_move_path')
            if len(route) == 1:
                return {'status': 'arrived', 'courier_id': courier_id,
                        'origin': courier['node'], 'destination': destination,
                        'minute': courier['minute'], 'segments': []}
            edges = _edge_map(db, case_id)
            doc = _case_doc(db, case_id)
            minute = courier['minute']
            node = courier['node']
            segments = []
            for index, nxt in enumerate(route[1:]):
                if (node, nxt) not in edges:
                    raise ValueError('road_unavailable')
                if _closure_error(db, case_id, node, nxt, minute):
                    raise ValueError('road_closed')
                arrival = minute + edges[(node, nxt)]
                if arrival > int(doc['shift_end_minute']):
                    raise ValueError('shift_expired')
                segments.append({'id': f'{operation_id}:{index}', 'origin': node,
                                 'destination': nxt, 'minutes': edges[(node, nxt)],
                                 'depart_minute': minute, 'arrival': arrival})
                minute, node = arrival, nxt
            db.execute(
                'UPDATE field_courier SET node=?,minute=? WHERE case_id=? AND courier_id=?',
                (destination, minute, case_id, courier_id))
            plan_row = _latest_plan(db, case_id)
            plan_id = plan_row['id'] if plan_row and plan_row['status'] in (
                'active', 'interrupted') else None
            plan_version = plan_row['version'] if plan_id else None
            for segment in segments:
                db.execute(
                    '''INSERT INTO field_move(id,case_id,courier_id,origin,destination,
                       minutes,arrival,plan_id,plan_version)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (segment['id'], case_id, courier_id, segment['origin'],
                     segment['destination'], segment['minutes'], segment['arrival'],
                     plan_id, plan_version))
            _audit(db, case_id, 'moved',
                   {'courier_id': courier_id, 'operation_id': operation_id,
                    'path': route, 'minute': minute,
                    'plan_version': plan_version})
            completed = _maybe_complete(db, case_id)
            return {'status': 'arrived', 'courier_id': courier_id,
                    'origin': route[0], 'destination': destination,
                    'minute': minute, 'path': route,
                    'plan_version': plan_version,
                    'case_completed': completed,
                    'segments': segments}
    finally:
        db.close()


def inspect(db_path, case_id, recipient, operation_id, courier_id=DEFAULT_COURIER):
    """Record a site observation only when the courier is physically there.

    The observation is a field fact, never an email receipt: ``decision`` and
    ``evidence`` are untouched.  Each site is inspectable exactly once.
    """
    db = connect(db_path)
    try:
        old = db.execute(
            'SELECT * FROM field_inspection WHERE id=?', (operation_id,)).fetchone()
        if old:
            if old['recipient'] != recipient:
                raise ValueError('operation_conflict')
            return {'status': 'replayed', 'record': dict(old)}
        sync = sync_eligibility(db_path, case_id)
        if sync['frozen']:
            raise ValueError('case_requires_recheck')
        with db:
            _check_frozen(db, case_id)
            _case_doc(db, case_id)
            courier = _courier_row(db, case_id, courier_id)
            target = db.execute(
                'SELECT * FROM field_recipient WHERE case_id=? AND recipient=?',
                (case_id, recipient)).fetchone()
            if not courier or not target:
                raise ValueError('unknown_field_recipient')
            if target['eligibility'] != 'active':
                raise ValueError('recipient_not_field_eligible')
            if courier['node'] != target['node']:
                raise ValueError('not_at_recipient_site')
            if db.execute(
                    'SELECT 1 FROM field_inspection WHERE case_id=? AND recipient=?',
                    (case_id, recipient)).fetchone():
                raise ValueError('site_already_inspected')
            late = max(0, courier['minute'] - target['requested_minute'])
            plan_row = _latest_plan(db, case_id)
            plan_id = plan_row['id'] if plan_row and plan_row['status'] in (
                'active', 'interrupted') else None
            plan_version = plan_row['version'] if plan_id else None
            db.execute(
                '''INSERT INTO field_inspection(id,case_id,courier_id,recipient,minute,
                   observation,late_minutes,plan_id,plan_version)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (operation_id, case_id, courier_id, recipient, courier['minute'],
                 target['observation'], late, plan_id, plan_version))
            _audit(db, case_id, 'inspected',
                   {'recipient': recipient, 'courier_id': courier_id,
                    'minute': courier['minute'], 'late_minutes': late,
                    'operation_id': operation_id})
            completed = _maybe_complete(db, case_id)
            return {'status': 'inspected', 'recipient': recipient,
                    'courier_id': courier_id, 'minute': courier['minute'],
                    'late_minutes': late, 'observation': target['observation'],
                    'plan_version': plan_version,
                    'case_completed': completed}
    finally:
        db.close()


# ---------------------------------------------------------- closures/recovery

def _plan_uses_closure(payload, origin, destination, start_minute, end_minute):
    hits = []
    for courier in payload['couriers']:
        for leg in courier['legs']:
            for segment in leg['segments']:
                if segment['from'] == origin and segment['to'] == destination \
                        and start_minute <= segment['depart_minute'] < end_minute:
                    hits.append({'courier_id': courier['courier_id'],
                                 'leg_sequence': leg['sequence'],
                                 'depart_minute': segment['depart_minute']})
    return hits


def close_road(db_path, case_id, origin, destination, start_minute, end_minute,
               closure_id=None, replan=False, objective='min_total_travel'):
    """Persist a directed time-window closure and flag open plans.

    With ``replan=True`` and an open plan that actually used the closed road,
    a new plan version is computed from real courier positions and completed
    inspections; sites visited already are never re-added.
    """
    if start_minute >= end_minute or start_minute < 0:
        raise ValueError('bad_field_closure')
    db = connect(db_path)
    try:
        with db:
            _case_doc(db, case_id)
            if (origin, destination) not in _edge_map(db, case_id):
                raise ValueError('road_unavailable')
            cid = closure_id or (f"closure-{case_id}-{origin}-{destination}-"
                                 f"{start_minute}-{end_minute}")
            db.execute(
                '''INSERT OR IGNORE INTO field_closure
                   (id,case_id,origin,destination,start_minute,end_minute)
                   VALUES (?,?,?,?,?,?)''',
                (cid, case_id, origin, destination, start_minute, end_minute))
            interrupted = []
            for row in db.execute(
                    "SELECT * FROM field_plan WHERE case_id=? AND status='active'",
                    (case_id,)).fetchall():
                hit = _plan_uses_closure(json.loads(row['payload']), origin,
                                         destination, start_minute, end_minute)
                if hit:
                    db.execute("UPDATE field_plan SET status='interrupted' WHERE id=?",
                               (row['id'],))
                    interrupted.append({'plan_id': row['id'],
                                        'version': row['version'], 'legs': hit})
            _audit(db, case_id, 'road_closed',
                   {'origin': origin, 'destination': destination,
                    'start_minute': start_minute, 'end_minute': end_minute,
                    'interrupted_plans': interrupted})
            result = {'case_id': case_id, 'closure_id': cid,
                      'interrupted_plans': interrupted,
                      'replan_required': bool(interrupted)}
    finally:
        db.close()
    if replan and result['replan_required']:
        try:
            result['replacement_plan'] = plan(db_path, case_id, objective)
        except ValueError as exc:
            # No alternative route exists for the remaining sites: report it
            # instead of quietly dropping stops.
            result['replacement_plan'] = None
            result['replan_error'] = str(exc)
    return result


def reopen_road(db_path, case_id, closure_id):
    db = connect(db_path)
    try:
        with db:
            cursor = db.execute(
                'DELETE FROM field_closure WHERE case_id=? AND id=?',
                (case_id, closure_id))
            if cursor.rowcount == 0:
                raise ValueError('unknown_closure')
            _audit(db, case_id, 'road_reopened', {'closure_id': closure_id})
            return {'case_id': case_id, 'closure_id': closure_id, 'removed': True}
    finally:
        db.close()


def recover(db_path, case_id):
    """Rebuild courier positions from the executed ledger after a restart.

    Planned routes are not evidence of movement: the last recorded directed
    segment wins.  Inspections already recorded are treated as complete and
    can never be repeated; the caller should then replan only remaining sites.
    """
    db = connect(db_path)
    try:
        with db:
            doc = _case_doc(db, case_id)
            specs = _courier_specs(doc)
            recovered = []
            for spec in specs:
                rows = db.execute(
                    '''SELECT * FROM field_move WHERE case_id=? AND courier_id=?
                       ORDER BY rowid DESC LIMIT 1''',
                    (case_id, spec['id'])).fetchall()
                if rows:
                    node, minute = rows[0]['destination'], rows[0]['arrival']
                    source = 'last_executed_move'
                else:
                    existing = _courier_row(db, case_id, spec['id'])
                    if existing:
                        node, minute = existing['node'], existing['minute']
                    else:
                        node, minute = spec['start'], 0
                    source = 'courier_record'
                db.execute(
                    '''INSERT INTO field_courier(case_id,courier_id,node,minute)
                       VALUES (?,?,?,?) ON CONFLICT(case_id,courier_id)
                       DO UPDATE SET node=excluded.node,minute=excluded.minute''',
                    (case_id, spec['id'], node, minute))
                recovered.append({'courier_id': spec['id'], 'node': node,
                                  'minute': minute, 'source': source})
            inspections = _inspection_rows(db, case_id)
            visited = {row['recipient'] for row in inspections}
            latest = _latest_plan(db, case_id)
            remaining = _remaining_from_plan(db, case_id, latest)
            _audit(db, case_id, 'recovered',
                   {'couriers': recovered, 'inspected': sorted(visited)})
            return {'case_id': case_id, 'couriers': recovered,
                    'inspected': sorted(visited),
                    'remaining_stops': remaining,
                    'plan_version': latest['version'] if latest else None,
                    'plan_status': latest['status'] if latest else None}
    finally:
        db.close()


# ------------------------------------------------------------------- report

def _decision_view(db, subject):
    row = db.execute(
        'SELECT status,details,rule_version,run_id FROM decision WHERE subject=?',
        (subject,)).fetchone()
    return dict(row) if row else None


def _actual_route(db, case_id):
    moves = _move_rows(db, case_id)
    inspections = _inspection_rows(db, case_id)
    return {
        'visit_order': [
            {'sequence': index, 'recipient': row['recipient'],
             'courier_id': row['courier_id'], 'minute': row['minute'],
             'observation': row['observation'],
             'late_minutes': row['late_minutes']}
            for index, row in enumerate(inspections, 1)
        ],
        'segments': moves,
        'total_travel_minutes': sum(row['minutes'] for row in moves),
        'max_late_minutes': max((row['late_minutes'] for row in inspections), default=0),
        'by_courier': {
            courier: {
                'segments': [m for m in moves if m['courier_id'] == courier],
                'inspections': [i for i in inspections if i['courier_id'] == courier],
                'travel_minutes': sum(m['minutes'] for m in moves
                                      if m['courier_id'] == courier),
            }
            for courier in sorted({m['courier_id'] for m in moves}
                                  | {i['courier_id'] for i in inspections})
        },
    }


def build_report(db_path, case_id):
    sync_eligibility(db_path, case_id)
    db = connect(db_path)
    try:
        doc = _case_doc(db, case_id)
        case = db.execute('SELECT * FROM field_case WHERE id=?', (case_id,)).fetchone()
        case_state = db.execute(
            'SELECT * FROM field_case_state WHERE case_id=?', (case_id,)).fetchone()
        recipients = []
        for row in db.execute(
                'SELECT * FROM field_recipient WHERE case_id=? ORDER BY recipient',
                (case_id,)):
            original_events = json.loads(row['original_event_ids'])
            original_sources = json.loads(row['original_source_shas'])
            recipients.append({
                'recipient': row['recipient'], 'node': row['node'],
                'requested_minute': row['requested_minute'],
                'observation': row['observation'],
                'eligibility': row['eligibility'],
                'invalidation_reason': row['invalidation_reason'],
                'original_decision': {
                    'status': row['original_status'],
                    'details': row['original_details'],
                    'rule_version': row['original_rule_version'],
                    'event_ids': original_events,
                    'source_sha256': original_sources,
                },
                'eligibility_snapshot': {
                    'status': row['snapshot_status'],
                    'details': row['snapshot_details'],
                    'rule_version': row['snapshot_rule_version'],
                    'event_ids': json.loads(row['snapshot_event_ids']),
                    'source_sha256': json.loads(row['snapshot_source_shas']),
                    'signature': row['snapshot_signature'],
                    'snapshot_at': row['snapshot_at'],
                    'rechecked_at': row['rechecked_at'],
                },
                'current_decision': _decision_view(db, row['recipient']),
            })
        plans = []
        for row in db.execute(
                'SELECT * FROM field_plan WHERE case_id=? ORDER BY version',
                (case_id,)):
            plans.append(_plan_view(db, row))
        latest = _latest_plan(db, case_id)
        return {
            'report': 'field_verification',
            'case_id': case_id,
            'case_digest': case['digest'],
            'shift_end_minute': doc['shift_end_minute'],
            'status': case_state['status'] if case_state else 'ready',
            'recheck_required': bool(case_state['recheck_required']) if case_state else False,
            'couriers': _courier_state(db, case_id),
            'recipients': recipients,
            'plans': plans,
            'latest_plan': _plan_view(db, latest),
            'remaining_stops': _remaining_from_plan(db, case_id, latest),
            'actual': _actual_route(db, case_id),
            'inspections': _inspection_rows(db, case_id),
            'closures': _closure_list(db, case_id),
            'audit': [dict(row) for row in db.execute(
                'SELECT action,at,details FROM field_audit WHERE case_id=? ORDER BY id',
                (case_id,))],
            'original_delivery_unchanged': True,
            'inspections_are_not_delivery_receipts': True,
            'note': ('现场观察仅记录核验事实；原投递结论与证据表未被修改，'
                     '也不代替邮寄标签打印或金融核销。'),
        }
    finally:
        db.close()


def write_report(db_path, out, case_id):
    data = build_report(db_path, case_id)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    actual = data['actual']
    lines = [
        '# 现场核验派工报告', '',
        f"- 任务: {case_id}",
        f"- 状态: {data['status']}",
        f"- 实际到访顺序: {', '.join(v['recipient'] + '@' + str(v['minute']) + '分' for v in actual['visit_order']) or '无'}",
        f"- 道路分钟合计: {actual['total_travel_minutes']}",
        f"- 最大迟到: {actual['max_late_minutes']}",
        f"- 最新计划版本: {data['latest_plan']['version'] if data['latest_plan'] else '无'}",
        '',
        '> 现场核验不改变原投递结论，也不代替邮寄标签打印或金融核销。',
    ]
    target.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return target
