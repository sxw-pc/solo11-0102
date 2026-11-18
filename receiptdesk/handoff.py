"""Physical handoff report: provenance trail, operation journal and mass balance.

The report is built exclusively from persisted ledger state — frozen case
records, live pack/container rows, real ``paper_operation`` results and the
campaign journal. Planned numbers are labeled as plans; recovered fiber and
controlled residue are only counted where an endpoint actually produced them.
"""

import json
from pathlib import Path

from .store import connect

ROUTE_LABEL = {'recover': 'fiber_recovery', 'thermal': 'confidential_thermal'}


def build_report(db_path, plan_id, out_path):
    db = connect(db_path)
    try:
        plan_row = db.execute('SELECT * FROM paper_plan WHERE id=?', (plan_id,)).fetchone()
        if not plan_row:
            raise ValueError('unknown_plan')
        case_id = plan_row['case_id']
        case = db.execute('SELECT * FROM paper_case WHERE id=?', (case_id,)).fetchone()
        doc = json.loads(case['payload'])
        payload = json.loads(plan_row['payload'])
        packs = [dict(r) for r in db.execute(
            'SELECT * FROM paper_pack WHERE case_id=? ORDER BY id', (case_id,))]
        containers = [dict(r) for r in db.execute(
            'SELECT * FROM paper_container WHERE case_id=? ORDER BY id', (case_id,))]
        operations = [dict(r) for r in db.execute(
            'SELECT * FROM paper_operation WHERE case_id=? ORDER BY rowid', (case_id,))]
        steps = [dict(r) for r in db.execute(
            'SELECT * FROM paper_plan_step WHERE plan_id=? ORDER BY seq', (plan_id,))]
        quarantines = [dict(r) for r in db.execute(
            'SELECT * FROM paper_quarantine WHERE plan_id=? ORDER BY id', (plan_id,))]
        authorizations = {r['route']: bool(r['enabled']) for r in db.execute(
            'SELECT route,enabled FROM paper_authorization WHERE case_id=?', (case_id,))}
        queued_events = {}
        for frozen in doc['packs']:
            ev = db.execute(
                '''SELECT e.identity,e.kind,e.time,s.sha256 FROM event e
                   JOIN source s ON s.id=e.source_id
                   WHERE e.subject=? AND e.kind='queued' ORDER BY e.id LIMIT 1''',
                (frozen['subject'],),
            ).fetchone()
            queued_events[frozen['subject']] = dict(ev) if ev else None

        process_results = [json.loads(o['result']) for o in operations if o['kind'] == 'process']
        load_results = [json.loads(o['result']) for o in operations if o['kind'] == 'load']
        ship_results = [json.loads(o['result']) for o in operations if o['kind'] == 'ship']

        # -------------------------------------------------------------- packs
        live_pack = {p['id']: p for p in packs}
        pack_rows = []
        for frozen in doc['packs']:
            live = live_pack.get(frozen['id'])
            disposition = live['disposition'] if live else 'missing'
            if disposition == 'recover':
                fiber, residue, thermal = frozen['fiber_kg'], frozen['residue_kg'], 0.0
            elif disposition == 'thermal':
                fiber, residue, thermal = 0.0, 0.0, frozen['mass_kg']
            else:
                fiber = residue = thermal = 0.0
            settled = disposition in ('recover', 'thermal')
            pack_rows.append({
                'pack_id': frozen['id'],
                'queued_subject': frozen['subject'],
                'queued_source_sha256': frozen['queued_source_sha256'],
                'ink_grade': frozen['ink_grade'],
                'container': live['container'] if live else None,
                'location': live['location'] if live else 'absent',
                'disposition': disposition,
                'mass_kg': frozen['mass_kg'],
                'recovered_fiber_kg': fiber,
                'controlled_residue_kg': residue,
                'thermal_destroyed_kg': thermal,
                'accounted_output_kg': fiber + residue + thermal,
                'mass_balanced': (not settled) or abs(
                    fiber + residue + thermal - frozen['mass_kg']) < 1e-9,
            })

        # ----------------------------------------------------------- containers
        live_box = {c['id']: c for c in containers}
        process_by_box = {r['container']: r for r in process_results}
        box_rows = []
        planned_groups = {g['container']: g for g in payload['groups']}
        for frozen_c in doc['containers']:
            cid = frozen_c['id']
            live = live_box[cid]
            inside = [p for p in packs if p['container'] == cid]
            actual_load = sum(p['mass_kg'] for p in inside)
            inks = sorted({p['ink_grade'] for p in inside})
            proc = process_by_box.get(cid)
            box_rows.append({
                'container_id': cid,
                'capacity_kg': frozen_c['capacity_kg'],
                'location': live['location'],
                'packs': [p['id'] for p in inside],
                'ink_grades': inks,
                'mixed_ink': len(inks) > 1,
                'actual_load_kg': actual_load,
                'fill': ('empty' if actual_load == 0 else
                         'exact' if abs(actual_load - frozen_c['capacity_kg']) < 1e-9 else
                         'over' if actual_load > frozen_c['capacity_kg'] + 1e-9 else 'under'),
                'route': proc['route'] if proc else None,
                'recovered_fiber_kg': proc['recovered_kg'] if proc else 0.0,
                'controlled_residue_kg': proc['secured_kg'] if proc else 0.0,
                'planned_route': planned_groups.get(cid, {}).get('route'),
                'load_balanced': actual_load <= frozen_c['capacity_kg'] + 1e-9,
            })

        # -------------------------------------------------------- operation log
        journal = []
        for o in operations:
            journal.append({
                'operation_id': o['id'],
                'kind': o['kind'],
                'intent': json.loads(o['intent']) if o['intent'] else None,
                'record': json.loads(o['result']),
            })
        step_rows = [{
            'seq': s['seq'], 'operation_id': s['operation_id'], 'kind': s['kind'],
            'intent': json.loads(s['intent']), 'status': s['status'],
            'result': json.loads(s['result']) if s['result'] else None,
        } for s in steps]

        # --------------------------------------------------------- route outputs
        routes = {}
        for route in ('recover', 'thermal'):
            rs = [r for r in process_results if r['route'] == route]
            cids = [r['container'] for r in rs]
            routes[ROUTE_LABEL[route]] = {
                'authorized': authorizations.get(route, True),
                'containers': cids,
                'shipments': len(rs),
                'input_mass_kg': round(
                    sum(b['actual_load_kg'] for b in box_rows
                        if b['container_id'] in cids), 9),
                'recovered_fiber_kg': sum(r['recovered_kg'] for r in rs),
                'controlled_residue_kg': sum(r['secured_kg'] for r in rs),
            }

        # --------------------------------------------------------- mass balance
        total_mass = sum(f['mass_kg'] for f in doc['packs'])
        settled_mass = sum(p['mass_kg'] for p in pack_rows
                           if p['disposition'] in ('recover', 'thermal'))
        recovered = sum(p['recovered_fiber_kg'] for p in pack_rows)
        residue_total = sum(p['controlled_residue_kg'] for p in pack_rows)
        thermal_total = sum(p['thermal_destroyed_kg'] for p in pack_rows)
        pending_mass = sum(p['mass_kg'] for p in pack_rows
                           if p['disposition'] == 'unprocessed')
        quarantine_mass = sum(p['mass_kg'] for p in pack_rows
                              if p['disposition'] == 'quarantined')
        missing_mass = sum(f['mass_kg'] for f in doc['packs'] if f['id'] not in live_pack)
        batch_balance = {
            'frozen_total_mass_kg': total_mass,
            'processed_input_mass_kg': settled_mass,
            'recovered_fiber_kg': recovered,
            'controlled_residue_kg': residue_total + thermal_total,
            'of_which_thermal_destroyed_kg': thermal_total,
            'unprocessed_mass_kg': pending_mass,
            'quarantined_mass_kg': quarantine_mass,
            'missing_on_site_mass_kg': missing_mass,
            'output_equals_processed_input': abs(
                recovered + residue_total + thermal_total - settled_mass) < 1e-9,
            'input_plus_pending_equals_frozen': abs(
                settled_mass + pending_mass + quarantine_mass + missing_mass - total_mass) < 1e-9,
            'no_recovery_from_mixed_or_pending': all(
                not (b['mixed_ink'] and b['recovered_fiber_kg'] > 0) for b in box_rows),
        }

        # Provenance chain per pack, joined through the original queued event.
        provenance = []
        for frozen in doc['packs']:
            ev = queued_events.get(frozen['subject'])
            live = live_pack.get(frozen['id'])
            provenance.append({
                'pack_id': frozen['id'],
                'subject': frozen['subject'],
                'queued_event_identity': ev['identity'] if ev else None,
                'queued_event_time': ev['time'] if ev else None,
                'source_sha256': ev['sha256'] if ev else None,
                'sha256_matches_frozen': bool(
                    ev and ev['sha256'] == frozen['queued_source_sha256']),
                'container': live['container'] if live else None,
                'disposition': live['disposition'] if live else 'absent',
            })

        report = {
            'report_type': 'paper_waste_physical_handoff',
            'case_id': case_id,
            'case_digest': case['digest'],
            'plan_id': plan_id,
            'plan_digest': plan_row['digest'],
            'contract': plan_row['contract'],
            'status': plan_row['status'],
            'revision': payload['revision'],
            'planned_groups': payload['groups'],
            'optimality': {
                'objective_key': payload['objective']['key'],
                'shipments': payload['objective']['shipments'],
                'recovered_fiber_kg': payload['objective']['recovered_fiber_kg'],
                'controlled_residue_kg': payload['objective']['controlled_residue_kg'],
                'states_evaluated': payload['states_evaluated'],
                'distinct_objective_values_considered': payload['alternative_count'],
                'alternatives': payload['alternatives'],
                'tie_break': payload['objective']['tie'],
            },
            'provenance': provenance,
            'packs': pack_rows,
            'containers': box_rows,
            'shipment_count': len(ship_results),
            'operation_journal': journal,
            'step_journal': step_rows,
            'quarantine': [{'scope': q['scope'], 'ref_id': q['ref_id'], 'stage': q['stage'],
                            'reason': q['reason'], 'detail': q['detail']} for q in quarantines],
            'route_outputs': routes,
            'mass_balance': batch_balance,
        }
    finally:
        db.close()
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return target
