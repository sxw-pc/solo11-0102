"""Local physical label printer and stock with deterministic faults, not a planner.

The endpoint is deliberately small: it owns only the physical label stock and a
compare-and-swap revision per serial.  It never decides which job may use a
serial; that is the issuance layer's responsibility.  Faults are declared by
the frozen print case and are applied per action so that a print can lose its
ACK while a later void can also lose its receipt.
"""
import hashlib
import json
from pathlib import Path
from .store import connect


def _fault_for(doc, serial, action):
    """Faults may be a per-action map or a single string for the print action.

    A string fault (the seed fixture format) describes the *print* only, so the
    documented seed behaviour of ``L100`` and a later successful void is kept.
    A per-action map is how a case declares a lost *void* ACK.
    """
    fault = (doc.get('faults') or {}).get(serial)
    if isinstance(fault, dict):
        return fault.get(action)
    if action == 'print':
        return fault
    return None


def register(db_path, case_id, fixture):
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('jobs') or not doc.get('stock') or not doc.get('contracts'):
        raise ValueError('print_case_invalid')
    db = connect(db_path)
    try:
        observed = []
        with db:
            for job in doc['jobs']:
                subject = job['subject']
                if db.execute('SELECT 1 FROM subject WHERE id=?', (subject,)).fetchone() is None:
                    raise ValueError('print_subject_unknown')
                events = [dict(row) for row in db.execute(
                    'SELECT e.id,e.identity,e.time,s.sha256 AS source_sha256 '
                    'FROM event e JOIN source s ON s.id=e.source_id '
                    'WHERE e.subject=? AND e.kind=? ORDER BY e.id', (subject, 'queued'))]
                if not events:
                    raise ValueError('print_subject_unobserved')
                observed.append({'job': job, 'events': events})
            for serial in doc['stock']:
                db.execute('INSERT OR IGNORE INTO label_stock VALUES (?,?,?,?,?)',
                           (serial, 'blank', None, 0, None))
            payload = json.dumps({'doc': doc, 'path': str(path),
                                  'sha256': hashlib.sha256(raw).hexdigest(),
                                  'observed': observed}, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM print_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('print_case_conflict')
            db.execute('INSERT OR IGNORE INTO print_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'jobs': len(observed), 'stock': len(doc['stock'])}


def state(db_path, serial):
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM label_stock WHERE serial=?', (serial,)).fetchone()
        if row is None:
            raise ValueError('label_unknown')
        return dict(row)
    finally:
        db.close()


def _case_doc(db, case_id):
    if case_id is not None:
        row = db.execute('SELECT payload FROM print_case WHERE id=?', (case_id,)).fetchone()
    else:
        row = db.execute('SELECT payload FROM print_case LIMIT 1').fetchone()
    if row is None:
        raise ValueError('print_case_missing')
    return json.loads(row['payload'])['doc']


def invoke(db_path, serial, job, operation_id, expected_revision, action='print',
           content_sha256=None, case_id=None):
    """Apply one physical action.

    ``operation_id`` is the idempotency key: replaying the same call returns the
    recorded device outcome without touching the physical stock again.  A
    ``commit_then_ack_lost`` fault commits the physical effect and reports a
    timeout, which is exactly the state the caller must reconcile rather than
    blindly reprint.
    """
    db = connect(db_path)
    try:
        with db:
            prior = db.execute('SELECT * FROM printer_operation WHERE id=?', (operation_id,)).fetchone()
            if prior is not None:
                if (prior['serial'], prior['job'], prior['action']) != (serial, job, action):
                    raise ValueError('printer_operation_conflict')
                return {'status': prior['result'], 'revision': prior['revision_after']}
            stock = db.execute('SELECT * FROM label_stock WHERE serial=?', (serial,)).fetchone()
            if stock is None:
                raise ValueError('label_unknown')
            if stock['revision'] != expected_revision:
                raise ValueError('label_revision_conflict')
            doc = _case_doc(db, case_id)
            if job not in {item['id'] for item in doc['jobs']}:
                raise ValueError('job_unknown')
            fault = _fault_for(doc, serial, action)
            if action == 'print' and stock['status'] == 'blank':
                new_status = 'jammed' if fault == 'jam' else 'printed'
                result = 'jammed' if fault == 'jam' else (
                    'committed_ack_lost' if fault == 'commit_then_ack_lost' else 'committed')
            elif action == 'void' and stock['status'] == 'printed' and stock['job'] == job:
                new_status = 'void'
                result = ('committed_ack_lost' if fault == 'commit_then_ack_lost' else 'committed')
            else:
                raise ValueError('label_action_invalid')
            revision = stock['revision'] + 1
            content = stock['content_sha256']
            if action == 'print' and new_status == 'printed':
                content = content_sha256
            db.execute('UPDATE label_stock SET status=?,job=?,revision=?,content_sha256=? WHERE serial=?',
                       (new_status, job, revision, content, serial))
            db.execute('INSERT INTO printer_operation VALUES (?,?,?,?,?,?,?)',
                       (operation_id, serial, job, action, result, expected_revision, revision))
        return {'status': 'timeout' if result == 'committed_ack_lost' else result,
                'revision': revision, 'device_status': new_status,
                'device_result': result}
    finally:
        db.close()


def operation(db_path, operation_id):
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM printer_operation WHERE id=?', (operation_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        db.close()
