"""Local courier movement and site inspection primitives; no route planner."""

import hashlib
import json
from pathlib import Path

from .store import connect


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('recipients') or not doc.get('edges'):
        raise ValueError('bad_field_case')
    db = connect(db_path)
    try:
        with db:
            for spec in doc['recipients']:
                decision = db.execute('SELECT status FROM decision WHERE subject=?', (spec['id'],)).fetchone()
                if not decision or decision['status'] != spec['decision_status'] or decision['status'] not in ('undelivered', 'insufficient'):
                    raise ValueError('recipient_not_field_eligible')
                source = db.execute(
                    '''SELECT s.sha256 FROM event e JOIN source s ON s.id=e.source_id
                       WHERE e.subject=? ORDER BY e.id LIMIT 1''', (spec['id'],)
                ).fetchone()
                if not source or source['sha256'] != spec['event_source_sha256']:
                    raise ValueError('field_event_source_missing')
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM field_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('field_case_conflict')
            db.execute('INSERT OR IGNORE INTO field_case VALUES (?,?,?)',
                       (case_id, json.dumps(doc, sort_keys=True), digest))
            db.execute('INSERT OR IGNORE INTO field_courier VALUES (?,?,0)',
                       (case_id, doc['depot']))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'recipients': len(doc['recipients'])}


def state(db_path, case_id):
    db = connect(db_path)
    try:
        row = db.execute('SELECT node,minute FROM field_courier WHERE case_id=?', (case_id,)).fetchone()
        if not row:
            raise ValueError('unknown_field_case')
        visits = [dict(x) for x in db.execute(
            'SELECT recipient,minute,observation FROM field_inspection WHERE case_id=? ORDER BY minute',
            (case_id,),
        )]
        return {'node': row['node'], 'minute': row['minute'], 'inspections': visits}
    finally:
        db.close()


def move(db_path, case_id, destination, operation_id):
    db = connect(db_path)
    try:
        with db:
            old = db.execute('SELECT origin,destination,minutes,arrival FROM field_move WHERE id=?', (operation_id,)).fetchone()
            if old:
                return {'status': 'replayed', 'record': dict(old)}
            case = db.execute('SELECT payload FROM field_case WHERE id=?', (case_id,)).fetchone()
            courier = db.execute('SELECT node,minute FROM field_courier WHERE case_id=?', (case_id,)).fetchone()
            if not case or not courier:
                raise ValueError('unknown_field_case')
            doc = json.loads(case['payload'])
            edge = next((e for e in doc['edges'] if e['from'] == courier['node'] and e['to'] == destination), None)
            if not edge:
                raise ValueError('road_unavailable')
            arrival = courier['minute'] + edge['minutes']
            if arrival > doc['shift_end_minute']:
                raise ValueError('shift_expired')
            db.execute('UPDATE field_courier SET node=?,minute=? WHERE case_id=?',
                       (destination, arrival, case_id))
            db.execute('INSERT INTO field_move VALUES (?,?,?,?,?,?)',
                       (operation_id, case_id, courier['node'], destination, edge['minutes'], arrival))
            return {'status': 'arrived', 'origin': courier['node'], 'destination': destination, 'minute': arrival}
    finally:
        db.close()


def inspect(db_path, case_id, recipient, operation_id):
    db = connect(db_path)
    try:
        with db:
            old = db.execute('SELECT recipient,minute,observation FROM field_inspection WHERE id=?', (operation_id,)).fetchone()
            if old:
                return {'status': 'replayed', 'record': dict(old)}
            case = db.execute('SELECT payload FROM field_case WHERE id=?', (case_id,)).fetchone()
            courier = db.execute('SELECT node,minute FROM field_courier WHERE case_id=?', (case_id,)).fetchone()
            if not case or not courier:
                raise ValueError('unknown_field_case')
            spec = next((x for x in json.loads(case['payload'])['recipients'] if x['id'] == recipient), None)
            if not spec or courier['node'] != spec['node']:
                raise ValueError('not_at_recipient_site')
            if db.execute('SELECT 1 FROM field_inspection WHERE case_id=? AND recipient=?', (case_id, recipient)).fetchone():
                raise ValueError('site_already_inspected')
            db.execute('INSERT INTO field_inspection VALUES (?,?,?,?,?)',
                       (operation_id, case_id, recipient, courier['minute'], spec['observation']))
            return {'status': 'inspected', 'recipient': recipient, 'minute': courier['minute'],
                    'late_minutes': max(0, courier['minute'] - spec['requested_minute']),
                    'observation': spec['observation']}
    finally:
        db.close()
