"""Local paper inventory and treatment primitives, without a campaign planner."""

import hashlib
import json
from pathlib import Path

from .store import connect

TREATMENT_ROUTES = ('recover', 'thermal')


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('packs') or not doc.get('containers'):
        raise ValueError('bad_paper_case')
    db = connect(db_path)
    try:
        with db:
            for pack in doc['packs']:
                source = db.execute(
                    '''SELECT s.sha256 FROM event e JOIN source s ON s.id=e.source_id
                       WHERE e.subject=? AND e.kind='queued' ORDER BY e.id LIMIT 1''',
                    (pack['subject'],),
                ).fetchone()
                if not source or source['sha256'] != pack['queued_source_sha256']:
                    raise ValueError('paper_notice_source_missing')
                if pack['mass_kg'] != pack['fiber_kg'] + pack['residue_kg']:
                    raise ValueError('pack_mass_mismatch')
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM paper_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('paper_case_conflict')
            db.execute('INSERT OR IGNORE INTO paper_case VALUES (?,?,?)', (case_id, json.dumps(doc, sort_keys=True), digest))
            for item in doc['packs']:
                db.execute(
                    'INSERT OR IGNORE INTO paper_pack VALUES (?,?,?,?,?,?,?,?,?)',
                    (item['id'], case_id, item['ink_grade'], item['mass_kg'],
                     item['fiber_kg'], item['residue_kg'], None, 'field', 'unprocessed'),
                )
            for item in doc['containers']:
                db.execute(
                    'INSERT OR IGNORE INTO paper_container VALUES (?,?,?,?)',
                    (item['id'], case_id, item['capacity_kg'], 'field'),
                )
            # Frozen treatment-plant authorization. A case fixture may declare it
            # explicitly; otherwise both authorized routes are enabled by default.
            declared = doc.get('authorizations', {})
            if declared:
                unknown = set(declared) - set(TREATMENT_ROUTES)
                if unknown:
                    raise ValueError('bad_treatment_route')
            for route in TREATMENT_ROUTES:
                enabled = 1 if declared.get(route, True) else 0
                db.execute(
                    'INSERT OR IGNORE INTO paper_authorization VALUES (?,?,?)',
                    (case_id, route, enabled),
                )
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest}


def set_route_authorization(db_path, case_id, route, enabled):
    """Freeze or revoke a treatment-plant route (offline operational control)."""
    if route not in TREATMENT_ROUTES:
        raise ValueError('bad_treatment_route')
    db = connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM paper_case WHERE id=?', (case_id,)).fetchone():
                raise ValueError('unknown_paper_case')
            db.execute(
                'INSERT INTO paper_authorization VALUES (?,?,?) '
                'ON CONFLICT(case_id,route) DO UPDATE SET enabled=excluded.enabled',
                (case_id, route, 1 if enabled else 0),
            )
    finally:
        db.close()
    return {'case_id': case_id, 'route': route, 'enabled': bool(enabled)}


def state(db_path, case_id):
    db = connect(db_path)
    try:
        packs = [dict(x) for x in db.execute('SELECT * FROM paper_pack WHERE case_id=? ORDER BY id', (case_id,))]
        containers = [dict(x) for x in db.execute('SELECT * FROM paper_container WHERE case_id=? ORDER BY id', (case_id,))]
        if not packs:
            raise ValueError('unknown_paper_case')
        authorizations = {
            r['route']: bool(r['enabled'])
            for r in db.execute('SELECT route,enabled FROM paper_authorization WHERE case_id=?', (case_id,))
        }
        return {'packs': packs, 'containers': containers, 'authorizations': authorizations}
    finally:
        db.close()


def _canonical_intent(kind, payload):
    return json.dumps({'kind': kind, **payload}, sort_keys=True, ensure_ascii=False)


def _replay(db, operation_id, intent):
    old = db.execute('SELECT result,intent FROM paper_operation WHERE id=?', (operation_id,)).fetchone()
    if not old:
        return None
    # An operation id reused for a different physical action is a conflict, never
    # a replay: it must not silently confirm a pack/box/action it never performed.
    if old['intent'] is not None and intent is not None and old['intent'] != intent:
        raise ValueError('operation_id_conflict')
    return {'status': 'replayed', 'record': json.loads(old['result'])}


def _record(db, operation_id, case_id, kind, result, intent):
    db.execute('INSERT INTO paper_operation VALUES (?,?,?,?,?)',
               (operation_id, case_id, kind, json.dumps(result, sort_keys=True), intent))
    return result


def load(db_path, case_id, pack_id, container_id, operation_id):
    intent = _canonical_intent('load', {'pack': pack_id, 'container': container_id})
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, intent)
            if prior:
                return prior
            pack = db.execute('SELECT * FROM paper_pack WHERE id=? AND case_id=?', (pack_id, case_id)).fetchone()
            box = db.execute('SELECT * FROM paper_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            if not pack or not box or pack['location'] != 'field' or pack['container'] or box['location'] != 'field':
                raise ValueError('unavailable_pack_or_container')
            used = db.execute('SELECT COALESCE(SUM(mass_kg),0) FROM paper_pack WHERE container=?', (container_id,)).fetchone()[0]
            if used + pack['mass_kg'] > box['capacity_kg']:
                raise ValueError('container_capacity_exceeded')
            db.execute('UPDATE paper_pack SET container=? WHERE id=?', (container_id, pack_id))
            return _record(db, operation_id, case_id, 'load', {'status': 'loaded', 'pack': pack_id, 'container': container_id, 'mass_kg': pack['mass_kg']}, intent)
    finally:
        db.close()


def ship(db_path, case_id, container_id, operation_id):
    intent = _canonical_intent('ship', {'container': container_id})
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, intent)
            if prior:
                return prior
            box = db.execute('SELECT * FROM paper_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            packs = [dict(x) for x in db.execute('SELECT * FROM paper_pack WHERE container=? AND case_id=?', (container_id, case_id))]
            if not box or box['location'] != 'field' or not packs:
                raise ValueError('container_not_shippable')
            db.execute("UPDATE paper_container SET location='facility' WHERE id=?", (container_id,))
            db.execute("UPDATE paper_pack SET location='facility' WHERE container=?", (container_id,))
            return _record(db, operation_id, case_id, 'ship', {'status': 'shipped', 'container': container_id, 'packs': [p['id'] for p in packs], 'mass_kg': sum(p['mass_kg'] for p in packs)}, intent)
    finally:
        db.close()


def process(db_path, case_id, container_id, route, operation_id):
    if route not in TREATMENT_ROUTES:
        raise ValueError('bad_treatment_route')
    intent = _canonical_intent('process', {'container': container_id, 'route': route})
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, intent)
            if prior:
                return prior
            box = db.execute('SELECT * FROM paper_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            packs = [dict(x) for x in db.execute('SELECT * FROM paper_pack WHERE container=? AND case_id=?', (container_id, case_id))]
            if not box or box['location'] != 'facility' or not packs or any(p['disposition'] != 'unprocessed' for p in packs):
                raise ValueError('container_not_processable')
            if route == 'recover' and len({p['ink_grade'] for p in packs}) != 1:
                raise ValueError('mixed_ink_grade_cannot_recover')
            auth = db.execute(
                'SELECT enabled FROM paper_authorization WHERE case_id=? AND route=?',
                (case_id, route),
            ).fetchone()
            if auth is not None and not auth['enabled']:
                raise ValueError('treatment_route_not_authorized')
            for pack in packs:
                db.execute('UPDATE paper_pack SET disposition=? WHERE id=?', (route, pack['id']))
            recovered = sum(p['fiber_kg'] for p in packs) if route == 'recover' else 0
            secured = sum(p['residue_kg'] for p in packs) if route == 'recover' else sum(p['mass_kg'] for p in packs)
            return _record(db, operation_id, case_id, 'process', {'status': 'processed', 'container': container_id, 'route': route, 'recovered_kg': recovered, 'secured_kg': secured}, intent)
    finally:
        db.close()
