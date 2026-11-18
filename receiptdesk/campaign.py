"""Exact batch planner, resumable campaign executor and physical handoff support.

The frozen physical world is made of at most 12 packs and 6 containers. Every
pack goes into exactly one container, every container has a load limit, and a
container holding more than one ink grade is barred from fiber recovery and must
take confidential thermal treatment. Planning is therefore solved by an exact
subset dynamic program; nothing about recovery is inferred from planned weights —
fiber and secured residue only exist once the real ``load/ship/process``
endpoints have persisted their results.
"""

import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache

from .paper_waste import load, ship, process
from .store import connect

CONTRACTS = ('maximize_recovery', 'minimize_shipments')
MAX_PACKS = 12
MAX_CONTAINERS = 6
ROUTES = ('recover', 'thermal')


class CampaignError(RuntimeError):
    """A programming-level campaign failure (unknown plan, intent drift, ...)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


# --------------------------------------------------------------------------- #
# exact planner
# --------------------------------------------------------------------------- #

def _effective_route(ink_grades, authorizations):
    homogeneous = len(ink_grades) == 1
    if homogeneous and authorizations.get('recover', True):
        return 'recover'
    if not authorizations.get('thermal', True):
        return None
    return 'thermal'


def plan(packs, containers, contract, authorizations=None):
    """Return the globally exact assignment for one frozen case.

    Groups are returned in container order; unused containers do not appear.
    Objective keys are compared lexicographically, which implements the
    contracted priority order directly. Deterministic tie-breaking prefers the
    lexicographically smallest (container id, pack id) grouping.
    """
    if contract not in CONTRACTS:
        raise ValueError('bad_contract')
    authorizations = dict(authorizations or {'recover': True, 'thermal': True})
    if not any(authorizations.get(r) for r in ROUTES):
        raise ValueError('no_authorized_route')
    if not packs or not containers or len(packs) > MAX_PACKS or len(containers) > MAX_CONTAINERS:
        raise ValueError('bad_planning_input')
    ids = [p['id'] for p in packs]
    cids = [c['id'] for c in containers]
    if len(set(ids)) != len(ids) or len(set(cids)) != len(cids):
        raise ValueError('duplicate_pack_or_container')
    for p in packs:
        if p['mass_kg'] <= 0 or p['mass_kg'] != p['fiber_kg'] + p['residue_kg']:
            raise ValueError('pack_mass_mismatch')
    for c in containers:
        if c['capacity_kg'] <= 0:
            raise ValueError('bad_container_capacity')

    n, m = len(packs), len(containers)
    full = (1 << n) - 1
    mass = [float(p['mass_kg']) for p in packs]
    fiber = [float(p['fiber_kg']) for p in packs]
    residue = [float(p['residue_kg']) for p in packs]
    inks = [p['ink_grade'] for p in packs]

    subset_mass = [0.0] * (1 << n)
    subset_fiber = [0.0] * (1 << n)
    subset_residue = [0.0] * (1 << n)
    # Bitmask of distinct ink grades in each subset (grades are short strings).
    grade_index = {g: k for k, g in enumerate(sorted(set(inks)))}
    subset_grade_bits = [0] * (1 << n)
    for mask in range(1, 1 << n):
        bit = mask & -mask
        i = bit.bit_length() - 1
        prev = mask ^ bit
        subset_mass[mask] = subset_mass[prev] + mass[i]
        subset_fiber[mask] = subset_fiber[prev] + fiber[i]
        subset_residue[mask] = subset_residue[prev] + residue[i]
        subset_grade_bits[mask] = subset_grade_bits[prev] | (1 << grade_index[inks[i]])

    # Per-subset physical contribution under the frozen authorizations.
    # A homogeneous box may recover fiber; any mixed box must go thermal.
    recover_ok = authorizations.get('recover', True)
    thermal_ok = authorizations.get('thermal', True)
    mask_recovered = [0.0] * (1 << n)
    mask_secured = [0.0] * (1 << n)
    mask_routable = [True] * (1 << n)
    mask_route = [None] * (1 << n)
    for mask in range(1, 1 << n):
        homogeneous = subset_grade_bits[mask] & (subset_grade_bits[mask] - 1) == 0
        if homogeneous and recover_ok:
            mask_recovered[mask] = subset_fiber[mask]
            mask_secured[mask] = subset_residue[mask]
            mask_route[mask] = 'recover'
        elif thermal_ok:
            mask_secured[mask] = subset_mass[mask]
            mask_route[mask] = 'thermal'
        else:
            mask_routable[mask] = False

    # Feasible non-empty subsets per container, in ascending mask order so the
    # first retained optimum is the deterministic tie winner. Containers may
    # carry already-committed packs, whose mass reserves part of capacity.
    feasible = []
    for c in containers:
        used = float(c.get('used_kg', 0.0))
        feasible.append([mask for mask in range(1, 1 << n)
                         if mask_routable[mask]
                         and used + subset_mass[mask] <= float(c['capacity_kg']) + 1e-9])

    def make_key(shipments, recovered, secured):
        if contract == 'maximize_recovery':
            return (-recovered, shipments, recovered, secured)
        return (shipments, -recovered, recovered, secured)

    # Precompute each subset's member tuple once; tie construction otherwise
    # dominates runtime at the 12-pack limit.
    members_tuple = [()] * (1 << n)
    for mask in range(1, 1 << n):
        bit = mask & -mask
        i = bit.bit_length() - 1
        members_tuple[mask] = members_tuple[mask ^ bit] + (ids[i],)

    states_evaluated = 0

    @lru_cache(maxsize=None)
    def solve(ci, remaining):
        """Best (score, tie_tuple, group_tuple, objective_key) for containers[ci:]."""
        nonlocal states_evaluated
        states_evaluated += 1
        if remaining == 0:
            return (0.0, 0.0, 0), (), (), make_key(0, 0.0, 0.0)
        if ci == m:
            return None
        best = solve(ci + 1, remaining)  # leave container ci empty
        for sub in feasible[ci]:
            if sub & remaining != sub:
                continue
            tail = solve(ci + 1, remaining ^ sub)
            if tail is None:
                continue
            t_rec, t_sec, t_ship = tail[0]
            rec = t_rec + mask_recovered[sub]
            sec = t_sec + mask_secured[sub]
            ship = t_ship + 1
            key = make_key(ship, rec, sec)
            if best is not None and key > best[3]:
                continue
            tie = (members_tuple[sub],) + tail[1]
            groups = (sub,) + tail[2]
            candidate = ((rec, sec, ship), tie, groups, key)
            if best is None or (key, tie) < (best[3], best[1]):
                best = candidate
        return best

    optimum = solve(0, full)
    if optimum is None:
        return {'feasible': False, 'contract': contract, 'states_evaluated': states_evaluated,
                'groups': [], 'containers': [], 'objective': None,
                'alternatives': [], 'alternative_count': 0}

    score, tie, groups, key = optimum
    total_recovered, total_secured, shipments = score
    chosen_containers = []
    gi = 0
    for ci in range(m):
        if gi < len(groups):  # recursion fixes groups to container order
            chosen_containers.append(cids[ci])
            gi += 1
    grade_names = {v: k for k, v in grade_index.items()}
    assignment = []
    for cid, mask in zip(chosen_containers, groups):
        members = [ids[i] for i in range(n) if mask & (1 << i)]
        route = mask_route[mask]
        grade_set = sorted(grade_names[k] for k in range(len(grade_index))
                           if subset_grade_bits[mask] & (1 << k))
        load_kg = subset_mass[mask]
        recovered_kg = subset_fiber[mask] if route == 'recover' else 0.0
        secured_kg = subset_residue[mask] if route == 'recover' else subset_mass[mask]
        assignment.append({
            'container': cid,
            'packs': members,
            'ink_grades': grade_set,
            'homogeneous': len(grade_set) == 1,
            'route': route,
            'load_kg': load_kg,
            'planned_recovered_fiber_kg': recovered_kg,
            'planned_controlled_residue_kg': secured_kg,
        })

    # Distinct objective values reachable by complete assignments, as
    # optimality evidence for the physical handoff report. Every complete
    # assignment branches on container 0; cached tails carry additive scores.
    seen = {}
    tail0 = solve(1, full)
    if tail0 is not None and tail0[2]:
        seen[tail0[3]] = seen.get(tail0[3], 0) + 1
    for sub in feasible[0]:
        tail = solve(1, full ^ sub)
        if tail is None:
            continue
        rec = tail[0][0] + mask_recovered[sub]
        sec = tail[0][1] + mask_secured[sub]
        k = make_key(tail[0][2] + 1, rec, sec)
        seen[k] = seen.get(k, 0) + 1
    alternatives = sorted(
        ({'objective_key': list(k), 'assignments': cnt} for k, cnt in seen.items()),
        key=lambda a: a['objective_key'],
    )
    return {
        'feasible': True,
        'contract': contract,
        'states_evaluated': states_evaluated,
        'groups': assignment,
        'containers': chosen_containers,
        'objective': {
            'key': list(key),
            'tie': [list(t) for t in tie],
            'shipments': len(groups),
            'recovered_fiber_kg': total_recovered,
            'controlled_residue_kg': total_secured,
        },
        'alternatives': alternatives[:20],
        'alternative_count': len(seen),
    }


# --------------------------------------------------------------------------- #
# live verification
# --------------------------------------------------------------------------- #

def preflight(db_path, case_id):
    """Re-verify frozen provenance and mass integrity against live ledger state."""
    db = connect(db_path)
    try:
        case = db.execute('SELECT payload FROM paper_case WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise ValueError('unknown_paper_case')
        doc = json.loads(case['payload'])
        problems = []
        live = {r['id']: dict(r) for r in db.execute(
            'SELECT * FROM paper_pack WHERE case_id=?', (case_id,))}
        for expected in doc['packs']:
            pid = expected['id']
            row = live.get(pid)
            if row is None:
                problems.append({'scope': 'pack', 'ref_id': pid, 'reason': 'pack_missing_on_site',
                                 'detail': 'frozen pack is not present in the live ledger'})
                continue
            source = db.execute(
                '''SELECT s.sha256 FROM event e JOIN source s ON s.id=e.source_id
                   WHERE e.subject=? AND e.kind='queued' ORDER BY e.id LIMIT 1''',
                (expected['subject'],),
            ).fetchone()
            if not source or source['sha256'] != expected['queued_source_sha256']:
                problems.append({'scope': 'pack', 'ref_id': pid,
                                 'reason': 'queued_source_mismatch',
                                 'detail': {'subject': expected['subject'],
                                            'expected_sha256': expected['queued_source_sha256'],
                                            'live_sha256': source['sha256'] if source else None}})
            if row['mass_kg'] != row['fiber_kg'] + row['residue_kg']:
                problems.append({'scope': 'pack', 'ref_id': pid, 'reason': 'pack_mass_mismatch',
                                 'detail': {'mass_kg': row['mass_kg'], 'fiber_kg': row['fiber_kg'],
                                            'residue_kg': row['residue_kg']}})
            for field in ('ink_grade', 'mass_kg', 'fiber_kg', 'residue_kg'):
                if row[field] != expected[field]:
                    problems.append({'scope': 'pack', 'ref_id': pid,
                                     'reason': 'frozen_record_drift',
                                     'detail': {'field': field, 'frozen': expected[field],
                                                'live': row[field]}})
        if not db.execute('SELECT 1 FROM paper_authorization WHERE case_id=? AND enabled=1',
                          (case_id,)).fetchone():
            problems.append({'scope': 'batch', 'ref_id': case_id,
                             'reason': 'no_authorized_treatment_route',
                             'detail': 'every treatment route is currently revoked'})
        return problems
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# campaign persistence
# --------------------------------------------------------------------------- #

def _steps_from_plan(plan_id, revision, groups):
    steps = []
    seq = 0
    for g in groups:
        cid = g['container']
        for pid in g['packs']:
            op = f'{plan_id}.r{revision}.L{pid}@{cid}'
            intent = _canonical({'kind': 'load', 'pack': pid, 'container': cid})
            steps.append((seq, op, 'load', intent)); seq += 1
        op = f'{plan_id}.r{revision}.S{cid}'
        intent = _canonical({'kind': 'ship', 'container': cid})
        steps.append((seq, op, 'ship', intent)); seq += 1
        op = f'{plan_id}.r{revision}.P{cid}:{g["route"]}'
        intent = _canonical({'kind': 'process', 'container': cid, 'route': g['route']})
        steps.append((seq, op, 'process', intent)); seq += 1
    return steps


def create_campaign(db_path, case_id, plan_id, contract):
    if contract not in CONTRACTS:
        raise ValueError('bad_contract')
    db = connect(db_path)
    try:
        case = db.execute('SELECT payload,digest FROM paper_case WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise ValueError('unknown_paper_case')
        if db.execute('SELECT 1 FROM paper_plan WHERE id=?', (plan_id,)).fetchone():
            raise ValueError('plan_id_conflict')
        doc = json.loads(case['payload'])
        if contract not in doc.get('contracts', CONTRACTS):
            raise ValueError('contract_not_authorized')
        authorizations = {r['route']: bool(r['enabled']) for r in db.execute(
            'SELECT route,enabled FROM paper_authorization WHERE case_id=?', (case_id,))}
        result = plan(doc['packs'], doc['containers'], contract, authorizations)
        if not result['feasible']:
            raise ValueError('no_feasible_assignment')
        payload = {'revision': 0, 'contract': contract, 'groups': result['groups'],
                   'objective': result['objective'], 'states_evaluated': result['states_evaluated'],
                   'alternatives': result['alternatives'],
                   'alternative_count': result['alternative_count']}
        digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        now = _now()
        with db:
            db.execute('INSERT INTO paper_plan VALUES (?,?,?,?,?,?,?,?)',
                       (plan_id, case_id, contract, 'planning', _canonical(payload), digest, now, now))
            for seq, op, kind, intent in _steps_from_plan(plan_id, 0, result['groups']):
                db.execute('INSERT INTO paper_plan_step VALUES (?,?,?,?,?,?,?)',
                           (plan_id, seq, op, kind, intent, 'pending', None))
        return {'plan_id': plan_id, 'status': 'planning', 'digest': digest,
                'objective': result['objective']}
    finally:
        db.close()


def _quarantine(db, plan_id, scope, ref_id, stage, reason, detail):
    db.execute('INSERT INTO paper_quarantine(plan_id,scope,ref_id,stage,reason,detail,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (plan_id, scope, ref_id, stage, reason,
                _canonical(detail) if not isinstance(detail, str) else detail, _now()))
    if scope == 'pack':
        # Reflect the physical quarantine on the pack itself; settled packs
        # keep their recover/thermal disposition and are never re-quarantined.
        db.execute(
            "UPDATE paper_pack SET disposition='quarantined' WHERE id=? AND disposition='unprocessed'",
            (ref_id,))


def _quarantined_ids(db, plan_id, scope):
    return {r['ref_id'] for r in db.execute(
        'SELECT ref_id FROM paper_quarantine WHERE plan_id=? AND scope=?', (plan_id, scope))}


def _stranded_packs(db, cid):
    return [r['id'] for r in db.execute(
        'SELECT id FROM paper_pack WHERE container=? ORDER BY id', (cid,))]


def _isolate_box(db, plan_id, cid, stage, reason, detail):
    if cid not in _quarantined_ids(db, plan_id, 'container'):
        _quarantine(db, plan_id, 'container', cid, stage, reason, detail)
        for stranded in _stranded_packs(db, cid):
            if stranded not in _quarantined_ids(db, plan_id, 'pack'):
                _quarantine(db, plan_id, 'pack', stranded, stage,
                            'stranded_with_quarantined_container', {'container': cid})


def _append_steps(db, plan_id, revision, groups, include_loads=True):
    next_seq = db.execute(
        'SELECT COALESCE(MAX(seq)+1,0) FROM paper_plan_step WHERE plan_id=?',
        (plan_id,)).fetchone()[0]
    seq = 0
    for g in groups:
        cid = g['container']
        if include_loads:
            for pid in g['packs']:
                op = f'{plan_id}.r{revision}.L{pid}@{cid}'
                intent = _canonical({'kind': 'load', 'pack': pid, 'container': cid})
                db.execute('INSERT INTO paper_plan_step VALUES (?,?,?,?,?,?,?)',
                           (plan_id, next_seq + seq, op, 'load', intent, 'pending', None))
                seq += 1
        op = f'{plan_id}.r{revision}.S{cid}'
        intent = _canonical({'kind': 'ship', 'container': cid})
        db.execute('INSERT INTO paper_plan_step VALUES (?,?,?,?,?,?,?)',
                   (plan_id, next_seq + seq, op, 'ship', intent, 'pending', None))
        seq += 1
        op = f'{plan_id}.r{revision}.P{cid}:{g["route"]}'
        intent = _canonical({'kind': 'process', 'container': cid, 'route': g['route']})
        db.execute('INSERT INTO paper_plan_step VALUES (?,?,?,?,?,?,?)',
                   (plan_id, next_seq + seq, op, 'process', intent, 'pending', None))
        seq += 1


def _committed_box_groups(db, case_id, q_boxes, authorizations):
    """Build planner-style groups for field boxes already holding packs.

    These packs were physically loaded before the failure, so the groups are
    only used to emit ship/process steps — never duplicate load steps.
    """
    groups = []
    for r in db.execute(
            'SELECT * FROM paper_container WHERE case_id=? ORDER BY id', (case_id,)):
        if r['id'] in q_boxes or r['location'] != 'field':
            continue
        inside = [dict(p) for p in db.execute(
            'SELECT * FROM paper_pack WHERE container=? ORDER BY id', (r['id'],))
            if p['disposition'] == 'unprocessed']
        if not inside:
            continue
        grades = {p['ink_grade'] for p in inside}
        route = _effective_route(grades, authorizations)
        if route is None:
            continue
        groups.append({'container': r['id'], 'packs': [p['id'] for p in inside],
                       'ink_grades': sorted(grades), 'homogeneous': len(grades) == 1,
                       'route': route, 'load_kg': sum(p['mass_kg'] for p in inside),
                       'planned_recovered_fiber_kg': sum(p['fiber_kg'] for p in inside)
                       if route == 'recover' else 0.0,
                       'planned_controlled_residue_kg': (
                           sum(p['residue_kg'] for p in inside) if route == 'recover'
                           else sum(p['mass_kg'] for p in inside))})
    return groups


def _revise_committed_only(db, plan_row):
    """Quarantine every free pack; only finish boxes already physically loaded."""
    plan_id = plan_row['id']
    case_id = plan_row['case_id']
    payload = json.loads(plan_row['payload'])
    q_packs = _quarantined_ids(db, plan_id, 'pack')
    q_boxes = _quarantined_ids(db, plan_id, 'container')
    db.execute("UPDATE paper_plan_step SET status='superseded' WHERE plan_id=? AND status='pending'",
               (plan_id,))
    for r in db.execute(
            "SELECT id FROM paper_pack WHERE case_id=? AND disposition='unprocessed' "
            'AND container IS NULL', (case_id,)):
        if r['id'] not in q_packs:
            _quarantine(db, plan_id, 'pack', r['id'], 'preflight',
                        'no_authorized_treatment_route', 'batch route freeze')
    authorizations = {r['route']: bool(r['enabled']) for r in db.execute(
        'SELECT route,enabled FROM paper_authorization WHERE case_id=?', (case_id,))}
    committed = _committed_box_groups(db, case_id, q_boxes, authorizations)
    if not committed:
        return {'revised': True, 'feasible': False}
    revision = payload['revision'] + 1
    payload['revision'] = revision
    payload['groups'] = payload.get('groups', []) + committed
    _append_steps(db, plan_id, revision, committed, include_loads=False)
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    db.execute('UPDATE paper_plan SET payload=?,digest=?,updated_at=? WHERE id=?',
               (_canonical(payload), digest, _now(), plan_id))
    return {'revised': True, 'feasible': True}


def _revise(db, plan_row, reason_detail):
    """Replan everything still physically free after a quarantine event."""
    plan_id = plan_row['id']
    case_id = plan_row['case_id']
    payload = json.loads(plan_row['payload'])
    q_packs = _quarantined_ids(db, plan_id, 'pack')
    q_boxes = _quarantined_ids(db, plan_id, 'container')
    remaining_rows = [dict(r) for r in db.execute(
        'SELECT * FROM paper_pack WHERE case_id=? ORDER BY id', (case_id,))
        if r['id'] not in q_packs and r['disposition'] == 'unprocessed'
        and r['location'] == 'field' and r['container'] is None]
    # Containers already holding committed (but unshipped) packs stay in
    # service with their consumed capacity reserved; the planner only adds
    # still-free packs and never reassigns what was physically loaded.
    free_boxes = []
    for r in db.execute('SELECT * FROM paper_container WHERE case_id=? ORDER BY id', (case_id,)):
        if r['id'] in q_boxes or r['location'] != 'field':
            continue
        used = db.execute(
            'SELECT COALESCE(SUM(mass_kg),0) FROM paper_pack WHERE container=?',
            (r['id'],)).fetchone()[0]
        free_boxes.append({'id': r['id'], 'capacity_kg': r['capacity_kg'], 'used_kg': used})
    authorizations = {r['route']: bool(r['enabled']) for r in db.execute(
        'SELECT route,enabled FROM paper_authorization WHERE case_id=?', (case_id,))}
    db.execute("UPDATE paper_plan_step SET status='superseded' WHERE plan_id=? AND status='pending'",
               (plan_id,))

    def finish_committed():
        """Ship/process boxes whose packs were already physically loaded."""
        committed = _committed_box_groups(db, case_id, q_boxes, authorizations)
        if committed:
            revision = payload['revision'] + 1
            payload['revision'] = revision
            payload['groups'] = payload.get('groups', []) + committed
            _append_steps(db, plan_id, revision, committed, include_loads=False)
            digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
            db.execute('UPDATE paper_plan SET payload=?,digest=?,updated_at=? WHERE id=?',
                       (_canonical(payload), digest, _now(), plan_id))

    if not remaining_rows or not free_boxes:
        if remaining_rows:
            # No physical container remains to carry the free inventory.
            for r in remaining_rows:
                if r['id'] not in q_packs:
                    _quarantine(db, plan_id, 'pack', r['id'], 'replan',
                                'no_container_available', reason_detail)
            _quarantine(db, plan_id, 'batch', case_id, 'replan',
                        'no_feasible_assignment', reason_detail)
            finish_committed()
            return {'revised': True, 'feasible': False}
        finish_committed()
        return {'revised': True, 'feasible': True, 'groups': []}
    try:
        result = plan(
            [{'id': r['id'], 'ink_grade': r['ink_grade'], 'mass_kg': r['mass_kg'],
              'fiber_kg': r['fiber_kg'], 'residue_kg': r['residue_kg']} for r in remaining_rows],
            free_boxes, payload['contract'], authorizations)
    except ValueError as exc:
        for r in remaining_rows:
            if r['id'] not in q_packs:
                _quarantine(db, plan_id, 'pack', r['id'], 'replan',
                            'no_feasible_assignment', {'error': str(exc)})
        _quarantine(db, plan_id, 'batch', case_id, 'replan', 'no_feasible_assignment', reason_detail)
        finish_committed()
        return {'revised': True, 'feasible': False}
    if not result['feasible']:
        for r in remaining_rows:
            if r['id'] not in q_packs:
                _quarantine(db, plan_id, 'pack', r['id'], 'replan',
                            'no_feasible_assignment', reason_detail)
        _quarantine(db, plan_id, 'batch', case_id, 'replan', 'no_feasible_assignment', reason_detail)
        finish_committed()
        return {'revised': True, 'feasible': False}
    revision = payload['revision'] + 1
    payload['revision'] = revision
    payload['groups'] = result['groups']
    payload['objective'] = result['objective']
    payload['states_evaluated'] = result['states_evaluated']
    payload['alternatives'] = result['alternatives']
    payload['alternative_count'] = result['alternative_count']
    _append_steps(db, plan_id, revision, result['groups'])
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    db.execute('UPDATE paper_plan SET payload=?,digest=?,updated_at=? WHERE id=?',
               (_canonical(payload), digest, _now(), plan_id))
    return {'revised': True, 'feasible': True, 'groups': result['groups']}


def _mark_done(db, plan_id, seq, result):
    db.execute('UPDATE paper_plan_step SET status=?,result=? WHERE plan_id=? AND seq=?',
               ('done', _canonical(result), plan_id, seq))


def _reconcile(db, plan_id):
    """Mark pending steps done when their operation was already persisted."""
    for row in db.execute(
            "SELECT seq,operation_id,intent FROM paper_plan_step WHERE plan_id=? AND status='pending' "
            'ORDER BY seq', (plan_id,)):
        op = db.execute('SELECT intent,result FROM paper_operation WHERE id=?',
                        (row['operation_id'],)).fetchone()
        if op is None:
            continue
        if op['intent'] != row['intent']:
            raise CampaignError('operation_intent_drift')
        db.execute('UPDATE paper_plan_step SET status=?,result=? WHERE plan_id=? AND seq=?',
                   ('done', op['result'], plan_id, row['seq']))


def _transaction(db_path, fn):
    """Run ``fn(db)`` in one committed transaction on a short-lived connection."""
    db = connect(db_path)
    try:
        with db:
            return fn(db)
    finally:
        db.close()


def finalize_campaign(db_path, plan_id):
    def work(db):
        plan_row = db.execute('SELECT * FROM paper_plan WHERE id=?', (plan_id,)).fetchone()
        if not plan_row:
            raise ValueError('unknown_plan')
        case_id = plan_row['case_id']
        pending = db.execute(
            "SELECT COUNT(*) FROM paper_plan_step WHERE plan_id=? AND status='pending'",
            (plan_id,)).fetchone()[0]
        processed = db.execute(
            "SELECT COUNT(*) FROM paper_pack WHERE case_id=? AND disposition IN ('recover','thermal')",
            (case_id,)).fetchone()[0]
        loaded = db.execute(
            "SELECT COUNT(*) FROM paper_pack WHERE case_id=? AND container IS NOT NULL",
            (case_id,)).fetchone()[0]
        quarantined = len(_quarantined_ids(db, plan_id, 'pack'))
        if pending == 0 and quarantined == 0:
            status = 'completed'
        elif processed > 0 or loaded > 0:
            # Some physical obligation was fulfilled (processed output, or
            # packs safely loaded into sealed field boxes); the rest is
            # explicitly quarantined with the reason delivery cannot continue.
            status = 'partial'
        else:
            status = 'infeasible'
        db.execute('UPDATE paper_plan SET status=?,updated_at=? WHERE id=?',
                   (status, _now(), plan_id))
        return {'plan_id': plan_id, 'status': status}
    return _transaction(db_path, work)


def handle_preflight(db, problems, plan_row, plan_id):
    """Quarantine provenance/mass failures and replan the free inventory."""
    case_id = plan_row['case_id']
    batch_blocked = False
    for prob in problems:
        if prob['scope'] == 'batch':
            _quarantine(db, plan_id, 'batch', case_id, 'preflight',
                        prob['reason'], prob['detail'])
            batch_blocked = True
            continue
        pid = prob['ref_id']
        row = db.execute('SELECT container FROM paper_pack WHERE id=?', (pid,)).fetchone()
        _quarantine(db, plan_id, 'pack', pid, 'preflight', prob['reason'], prob['detail'])
        if row and row['container']:
            _isolate_box(db, plan_id, row['container'], 'preflight',
                         'container_tainted_by_pack',
                         {'pack': pid, 'container': row['container'], 'reason': prob['reason']})
    if batch_blocked:
        # Routes are frozen/revoked for new work, but packs already sealed in
        # field boxes still complete their physical ship/process finish line.
        return _revise_committed_only(db, plan_row)
    return _revise(db, plan_row, {'preflight': problems})


def run_campaign(db_path, case_id, plan_id, inject=None):
    """Execute (or resume) a campaign through the real physical endpoints.

    Every state transition is one persisted endpoint call. Restarts reconcile
    from ``paper_operation`` + ``paper_plan_step`` first, so a box is never
    shipped twice and a pack is never processed twice. ``inject`` is an
    optional callback receiving each successful endpoint result; raising from
    it simulates a process crash after the physical action was committed.
    Each loop iteration uses a fresh short-lived connection, so no write
    transaction is held while an endpoint opens its own connection.
    """
    def bootstrap(db):
        row = db.execute('SELECT * FROM paper_plan WHERE id=? AND case_id=?',
                         (plan_id, case_id)).fetchone()
        if not row:
            raise ValueError('unknown_plan')
        db.execute("UPDATE paper_plan SET status='running',updated_at=? WHERE id=?",
                   (_now(), plan_id))
        _reconcile(db, plan_id)
        return dict(row)

    plan_row = _transaction(db_path, bootstrap)
    seen_pending = set()

    while True:
        pending = _transaction(db_path, lambda db: [dict(r) for r in db.execute(
            "SELECT * FROM paper_plan_step WHERE plan_id=? AND status='pending' "
            "ORDER BY CASE kind WHEN 'load' THEN 0 WHEN 'ship' THEN 1 ELSE 2 END, seq",
            (plan_id,))])
        if not pending:
            break
        step = pending[0]
        # Every iteration must consume, supersede or add steps; an unchanged
        # pending front would otherwise loop forever on an unhandled condition.
        # Revisions replace the pending set, so the guard resets afterwards.
        front = tuple(s['operation_id'] for s in pending)
        if front in seen_pending:
            raise CampaignError('campaign_made_no_progress:' + step['operation_id'])
        seen_pending.add(front)
        intent = json.loads(step['intent'])
        kind = step['kind']
        cid = intent['container']

        # Live provenance / mass / authorization verification before acting.
        known_q = _transaction(db_path, lambda db: _quarantined_ids(db, plan_id, 'pack'))
        problems = [p for p in preflight(db_path, case_id) if p['ref_id'] not in known_q]
        if problems:
            _transaction(db_path, lambda db: handle_preflight(db, problems, plan_row, plan_id))
            seen_pending.clear()
            continue

        # Eligibility guards: only ask the endpoint for actions the live ledger
        # says are physically possible right now.
        def check_eligible(db):
            if kind == 'load':
                pack = db.execute('SELECT * FROM paper_pack WHERE id=? AND case_id=?',
                                  (intent['pack'], case_id)).fetchone()
                box = db.execute('SELECT * FROM paper_container WHERE id=? AND case_id=?',
                                 (cid, case_id)).fetchone()
                return (pack is not None and box is not None
                        and pack['location'] == 'field' and pack['container'] is None
                        and box['location'] == 'field')
            if kind == 'ship':
                box = db.execute(
                    "SELECT * FROM paper_container WHERE id=? AND case_id=? AND location='field'",
                    (cid, case_id)).fetchone()
                inside = db.execute(
                    'SELECT COUNT(*) FROM paper_pack WHERE container=?',
                    (cid,)).fetchone()[0] if box else 0
                return box is not None and inside > 0
            box = db.execute(
                "SELECT * FROM paper_container WHERE id=? AND case_id=? AND location='facility'",
                (cid, case_id)).fetchone()
            inside = db.execute(
                "SELECT COUNT(*) FROM paper_pack WHERE container=? AND disposition='unprocessed'",
                (cid,)).fetchone()[0] if box else 0
            return box is not None and inside > 0

        if not _transaction(db_path, check_eligible):
            def handle_ineligible(db):
                _isolate_box(db, plan_id, cid, kind, 'action_no_longer_eligible', intent)
                return _revise(db, plan_row, {'ineligible_step': intent})
            _transaction(db_path, handle_ineligible)
            seen_pending.clear()
            continue

        # Endpoint calls open their own connection and commit internally. A
        # refusal isolates the box; the remaining free inventory is replanned.
        try:
            if kind == 'load':
                result = load(db_path, case_id, intent['pack'], cid, step['operation_id'])
            elif kind == 'ship':
                result = ship(db_path, case_id, cid, step['operation_id'])
            else:
                result = process(db_path, case_id, cid, intent['route'], step['operation_id'])
        except ValueError as exc:
            def handle_refusal(db):
                _isolate_box(db, plan_id, cid, kind, 'endpoint_refused:' + str(exc), intent)
                return _revise(db, plan_row, {'endpoint_error': str(exc), 'step': intent})
            _transaction(db_path, handle_refusal)
            seen_pending.clear()
            continue

        if result.get('status') == 'replayed':
            _transaction(db_path, lambda db: _reconcile(db, plan_id))
        else:
            _transaction(db_path, lambda db: _mark_done(db, plan_id, step['seq'], result))
        if inject is not None:
            inject(kind, result)

    return finalize_campaign(db_path, plan_id)
