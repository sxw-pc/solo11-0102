"""Recoverable physical label issuance on top of the frozen printer fixture.

``print_lab`` owns only the physical stock and a compare-and-swap revision per
serial.  This module owns the business protocol:

* a work order is pinned to one frozen ``queued`` event, its source SHA-256, its
  message id, and an immutable layout digest;
* every physical action is preceded by a persisted intent and an idempotent
  operation id, and is followed by a reconciliation against the real device
  state;
* a serial is consumed the first time it is assigned, even when the sheet jams
  or the ACK is lost, so one physical sheet can never be mailed twice;
* a job becomes ``mail_ready`` only after the device reports the expected
  content hash and postage has been booked.  A ``queued`` event is never read
  as delivery.

Crash recovery always reads the device first: a persisted intent may or may not
have reached the paper, and the only trustworthy answer is what the stock says.
"""
import hashlib
import json
from datetime import datetime, timezone
from .store import connect
from . import print_lab

TEMPLATE = 'label-v1'
CONTRACTS = ('retain_verified_serial', 'void_and_reissue')

# Assignment states that occupy the job's single "current label" slot.  Jammed
# and voided sheets are scrap/void: the slot is free for a fresh serial, while
# the serial itself stays consumed forever.
CURRENT_STATES = ('allocated', 'intent_print', 'printed', 'void_intent',
                  'void_ack_lost', 'manual_review')
COMPLETED_PRINT_OPS = ('printed', 'ack_lost', 'jammed')


class LabelReviewRequired(Exception):
    """A human must inspect the physical stock before the flow may continue."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class InjectedCrash(Exception):
    """Test-only crash injection point, raised after a durable commit."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def layout_digest(job_id, subject, message_id, layout_version, template=TEMPLATE):
    return hashlib.sha256(_canonical({
        'template': template,
        'job': job_id,
        'subject': subject,
        'message_id': message_id,
        'layout_version': layout_version,
    }).encode('utf-8')).hexdigest()


def _point(crash_at, name):
    if crash_at == name:
        raise InjectedCrash(name)


def _row(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def _audit(db, case_id, job_id, serial, event, detail=None):
    db.execute('INSERT OR IGNORE INTO label_audit(case_id,job_id,serial,event,detail,created_at) '
               'VALUES (?,?,?,?,?,?)',
               (case_id, job_id, serial, event,
                _canonical(detail) if detail is not None else None, _now()))


def _print_case_doc(db, print_case_id):
    if print_case_id is None:
        rows = db.execute('SELECT id,payload FROM print_case ORDER BY id').fetchall()
        if len(rows) != 1:
            raise ValueError('print_case_ambiguous' if rows else 'print_case_missing')
        row = rows[0]
    else:
        row = db.execute('SELECT id,payload FROM print_case WHERE id=?', (print_case_id,)).fetchone()
        if row is None:
            raise ValueError('print_case_missing')
    return row['id'], json.loads(row['payload'])['doc']


def _load_case(db, case_id):
    row = _row(db, 'SELECT * FROM label_case WHERE case_id=?', (case_id,))
    if row is None:
        raise ValueError('label_case_unknown')
    return row


def _load_job(db, case_id, job_id):
    row = _row(db, 'SELECT * FROM label_job WHERE case_id=? AND job_id=?', (case_id, job_id))
    if row is None:
        raise ValueError('label_job_unknown')
    return row


def _jobs(db, case_id):
    return [dict(r) for r in db.execute(
        'SELECT * FROM label_job WHERE case_id=? ORDER BY ordinal', (case_id,))]


def _assignments(db, case_id, job_id=None):
    if job_id is None:
        rows = db.execute('SELECT * FROM label_assignment WHERE case_id=? '
                          'ORDER BY job_id,sequence', (case_id,))
    else:
        rows = db.execute('SELECT * FROM label_assignment WHERE case_id=? AND job_id=? '
                          'ORDER BY sequence', (case_id, job_id))
    return [dict(r) for r in rows]


def _current_assignment(db, case_id, job_id):
    marks = ','.join('?' for _ in CURRENT_STATES)
    return _row(db, 'SELECT * FROM label_assignment WHERE case_id=? AND job_id=? '
                    f'AND state IN ({marks}) ORDER BY sequence DESC LIMIT 1',
                (case_id, job_id) + CURRENT_STATES)


def _stock(db, serial):
    return _row(db, 'SELECT * FROM label_stock WHERE serial=?', (serial,))


def _set_case_status(db, case_id):
    jobs = _jobs(db, case_id)
    if any(j['state'] == 'manual_review' for j in jobs):
        status = 'needs_review'
    elif jobs and all(j['state'] == 'mail_ready' for j in jobs):
        status = 'mail_ready'
    else:
        status = 'in_progress'
    db.execute('UPDATE label_case SET status=?,updated_at=? WHERE case_id=?',
               (status, _now(), case_id))
    return status


def _block_job(db, case, job, code, detail=None):
    """Freeze the job and its current sheet for human review; never overwrite."""
    db.execute('UPDATE label_assignment SET state=?,reason=?,updated_at=? '
               'WHERE case_id=? AND job_id=? AND state IN '
               "('allocated','intent_print','printed','void_intent','void_ack_lost')",
               ('manual_review', code, _now(), case['case_id'], job['job_id']))
    db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
               'WHERE case_id=? AND job_id=?',
               ('manual_review', code, _now(), case['case_id'], job['job_id']))
    _audit(db, case['case_id'], job['job_id'], job.get('current_serial'), 'manual_review',
           {'code': code, 'detail': detail})
    db.execute('UPDATE label_case SET status=?,updated_at=? WHERE case_id=?',
               ('needs_review', _now(), case['case_id']))


# ---------------------------------------------------------------------------
# opening a case: freeze work orders from the registered print case
# ---------------------------------------------------------------------------

def _queued_event(db, subject, message_id):
    rows = [dict(r) for r in db.execute(
        'SELECT e.id,e.identity,e.subject,e.kind,e.time,e.payload,e.sha256 AS event_sha256,'
        'e.source_id,s.sha256 AS source_sha256 '
        'FROM event e JOIN source s ON s.id=e.source_id '
        "WHERE e.subject=? AND e.kind='queued' ORDER BY e.time,e.id", (subject,))]
    matches = [r for r in rows if json.loads(r['payload']).get('message_id') == message_id]
    if not matches:
        raise ValueError('print_message_unobserved')
    if len(matches) > 1:
        raise ValueError('print_message_duplicate')
    return matches[0]


def open_case(db_path, print_case_id=None, contract='retain_verified_serial', case_id=None):
    """Freeze immutable work orders from the registered printer fixture.

    Re-opening the same case id is idempotent and never rewrites frozen
    evidence fields; the same case id with a different contract is a conflict.
    """
    if contract not in CONTRACTS:
        raise ValueError('contract_unknown')
    db = connect(db_path)
    try:
        print_case_id, doc = _print_case_doc(db, print_case_id)
        if contract not in doc.get('contracts', []):
            raise ValueError('contract_not_authorized')
        case_id = case_id or f'{print_case_id}:{contract}'
        existing = _row(db, 'SELECT * FROM label_case WHERE case_id=?', (case_id,))
        if existing is not None:
            if (existing['print_case_id'], existing['contract']) != (print_case_id, contract):
                raise ValueError('label_case_conflict')
            return {'case_id': case_id, 'contract': contract, 'status': existing['status'],
                    'created': False, 'jobs': len(_jobs(db, case_id))}
        postage = doc.get('postage_cents_per_live_label')
        if not isinstance(postage, int) or isinstance(postage, bool) or postage < 0:
            raise ValueError('postage_invalid')
        seen_jobs, seen_subjects, seen_messages = set(), set(), set()
        prepared = []
        for ordinal, job in enumerate(doc['jobs']):
            job_id, subject, message_id = job.get('id'), job.get('subject'), job.get('message_id')
            if not job_id or not subject or not message_id:
                raise ValueError('print_job_invalid')
            if job_id in seen_jobs:
                raise ValueError('duplicate_job')
            if message_id in seen_messages:
                raise ValueError('duplicate_notification')
            if subject in seen_subjects:
                raise ValueError('duplicate_subject')
            seen_jobs.add(job_id)
            seen_messages.add(message_id)
            seen_subjects.add(subject)
            if db.execute('SELECT 1 FROM subject WHERE id=?', (subject,)).fetchone() is None:
                raise ValueError('print_subject_unknown')
            event = _queued_event(db, subject, message_id)
            version, declared = job.get('layout_version'), job.get('layout_hash')
            if not isinstance(version, int) or isinstance(version, bool) or not declared:
                raise ValueError('layout_invalid')
            if layout_digest(job_id, subject, message_id, version) != declared:
                raise ValueError('layout_hash_mismatch')
            prepared.append((ordinal, job_id, subject, message_id, event, version, declared))
        now = _now()
        with db:
            db.execute('INSERT INTO label_case(case_id,print_case_id,contract,status,'
                       'created_at,updated_at) VALUES (?,?,?,?,?,?)',
                       (case_id, print_case_id, contract, 'in_progress', now, now))
            for ordinal, job_id, subject, message_id, event, version, declared in prepared:
                db.execute(
                    'INSERT INTO label_job(case_id,job_id,ordinal,subject,message_id,'
                    'queued_event_id,queued_identity,queued_event_sha256,source_id,source_sha256,'
                    'layout_version,layout_hash,postage_cents,state,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (case_id, job_id, ordinal, subject, message_id, event['id'],
                     event['identity'], event['event_sha256'], event['source_id'],
                     event['source_sha256'], version, declared, postage, 'pending', now, now))
            _audit(db, case_id, None, None, 'case_opened',
                   {'print_case_id': print_case_id, 'contract': contract})
        return {'case_id': case_id, 'contract': contract, 'status': 'in_progress',
                'created': True, 'jobs': len(prepared)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# source evidence checks
# ---------------------------------------------------------------------------

def _verify_source(db, job):
    """Re-check pinned queued evidence against the current source rows."""
    if db.execute('SELECT 1 FROM subject WHERE id=?', (job['subject'],)).fetchone() is None:
        return False, 'subject_no_longer_frozen'
    row = _row(db, 'SELECT e.id,e.identity,e.subject,e.kind,e.payload,'
                   'e.sha256 AS event_sha256,e.source_id,s.sha256 AS source_sha256 '
                   'FROM event e JOIN source s ON s.id=e.source_id WHERE e.id=?',
               (job['queued_event_id'],))
    if row is None:
        return False, 'queued_event_missing'
    if (row['identity'], row['subject'], row['kind']) != (
            job['queued_identity'], job['subject'], 'queued'):
        return False, 'queued_event_changed'
    if row['event_sha256'] != job['queued_event_sha256']:
        return False, 'queued_event_sha_changed'
    if row['source_id'] != job['source_id'] or row['source_sha256'] != job['source_sha256']:
        return False, 'source_sha_changed'
    if json.loads(row['payload']).get('message_id') != job['message_id']:
        return False, 'queued_message_changed'
    if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict' AND detail=?",
                  (row['identity'],)).fetchone() is not None:
        return False, 'identity_conflict'
    return True, 'ok'


def _require_source(db, case, job):
    ok, reason = _verify_source(db, job)
    if not ok:
        with db:
            _block_job(db, case, job, reason, {'job_id': job['job_id']})
        raise LabelReviewRequired(reason, {'job_id': job['job_id']})


# ---------------------------------------------------------------------------
# allocation and postage
# ---------------------------------------------------------------------------

def _allocate(db, case, job, doc):
    """Reserve the next untouched physical serial in fixture declaration order."""
    used = {r['serial'] for r in db.execute('SELECT serial FROM label_assignment')}
    for serial in doc.get('stock', []):
        if serial in used:
            continue
        stock = _stock(db, serial)
        if stock is None or stock['status'] != 'blank' or stock['content_sha256'] is not None:
            continue
        seq = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM label_assignment '
                         'WHERE case_id=? AND job_id=?',
                         (case['case_id'], job['job_id'])).fetchone()[0]
        db.execute('INSERT INTO label_assignment(serial,case_id,job_id,sequence,state,'
                   'layout_hash,device_revision,paper_consumed,postage_cents,ack_lost,retained,'
                   'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (serial, case['case_id'], job['job_id'], seq, 'allocated',
                    job['layout_hash'], stock['revision'], 0, 0, 0, 0, _now(), _now()))
        _audit(db, case['case_id'], job['job_id'], serial, 'allocated',
               {'sequence': seq, 'device_revision': stock['revision']})
        return serial, stock['revision']
    raise ValueError('label_stock_exhausted')


def _next_print_operation(db, case_id, job_id):
    pending = _row(db, "SELECT operation_id FROM label_operation WHERE case_id=? AND job_id=? "
                        "AND action='print' AND state IN ('intent','not_executed') "
                       'ORDER BY created_at DESC,rowid DESC LIMIT 1', (case_id, job_id))
    if pending is not None:
        return pending['operation_id']
    count = db.execute("SELECT COUNT(*) FROM label_operation WHERE case_id=? AND job_id=? "
                       "AND action='print'", (case_id, job_id)).fetchone()[0]
    return f'{case_id}:{job_id}:print:{count + 1}'


def _next_void_operation(db, case_id, job_id):
    pending = _row(db, "SELECT operation_id FROM label_operation WHERE case_id=? AND job_id=? "
                       "AND action='void' AND state IN ('intent','void_failed') "
                       'ORDER BY created_at DESC,rowid DESC LIMIT 1', (case_id, job_id))
    if pending is not None:
        return pending['operation_id']
    count = db.execute("SELECT COUNT(*) FROM label_operation WHERE case_id=? AND job_id=? "
                       "AND action='void'", (case_id, job_id)).fetchone()[0]
    return f'{case_id}:{job_id}:void:{count + 1}'


def _book_postage(db, case, job, serial, reason):
    """Book one integer-cent charge per physical serial, exactly once."""
    if db.execute('SELECT 1 FROM label_postage WHERE serial=?', (serial,)).fetchone():
        return False
    db.execute('INSERT INTO label_postage(serial,case_id,job_id,cents,reason,operation_id,'
               'created_at) VALUES (?,?,?,?,?,?,?)',
               (serial, case['case_id'], job['job_id'], job['postage_cents'], reason,
                f'postage:{serial}', _now()))
    db.execute('UPDATE label_assignment SET postage_cents=?,updated_at=? WHERE serial=?',
               (job['postage_cents'], _now(), serial))
    _audit(db, case['case_id'], job['job_id'], serial, 'postage_booked',
           {'cents': job['postage_cents'], 'reason': reason})
    return True


# ---------------------------------------------------------------------------
# device reconciliation
# ---------------------------------------------------------------------------

def _device_matches(stock, job, serial=None):
    return (stock is not None
            and stock['status'] == 'printed'
            and stock['job'] == job['job_id']
            and stock['content_sha256'] == job['layout_hash']
            and (serial is None or stock['serial'] == serial))


def _ready_basis(db, case, job):
    ok, reason = _verify_source(db, job)
    if not ok:
        return False, [reason]
    assignment = _current_assignment(db, case['case_id'], job['job_id'])
    if assignment is None:
        return False, ['no_current_label']
    if assignment['state'] != 'printed':
        return False, [f'label_{assignment["state"]}']
    stock = _stock(db, assignment['serial'])
    basis = []
    if not _device_matches(stock, job, assignment['serial']):
        return False, ['device_print_unverified']
    basis.append('device_print_confirmed')
    if assignment['ack_lost']:
        basis.append('ack_lost_reconciled_by_device')
    basis.append('content_hash_match')
    if assignment['device_revision'] is not None and stock['revision'] != assignment['device_revision']:
        return False, ['label_revision_changed']
    if job['current_serial'] != assignment['serial']:
        return False, ['label_not_current']
    if db.execute('SELECT 1 FROM label_postage WHERE serial=?',
                  (assignment['serial'],)).fetchone() is None:
        return False, ['postage_missing']
    basis.append('postage_booked')
    return True, basis


def _refresh_job(db, case, job):
    job = _load_job(db, case['case_id'], job['job_id'])
    if job['state'] == 'manual_review':
        return job
    ok, basis = _ready_basis(db, case, job)
    if ok:
        db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                   'WHERE case_id=? AND job_id=?',
                   ('mail_ready', None, _now(), case['case_id'], job['job_id']))
    elif job['state'] == 'mail_ready':
        db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                   'WHERE case_id=? AND job_id=?',
                   ('printed', 'readiness_lost', _now(), case['case_id'], job['job_id']))
    return _load_job(db, case['case_id'], job['job_id'])


def _resolve_device(db, job, serial):
    """Describe the actual sheet: blank, printed-for-this-job, jammed, or conflict."""
    stock = _stock(db, serial)
    if stock is None:
        return 'conflict', stock
    if stock['status'] == 'blank' and stock['content_sha256'] is None and stock['job'] is None:
        return 'blank', stock
    if stock['status'] == 'jammed' and stock['job'] == job['job_id']:
        return 'jammed', stock
    if _device_matches(stock, job, serial):
        return 'printed', stock
    return 'conflict', stock


def _record_printed(db, case, job, assignment, stock, ack_lost, note):
    db.execute('UPDATE label_assignment SET state=?,device_revision=?,paper_consumed=1,'
               'ack_lost=?,reason=?,updated_at=? WHERE serial=?',
               ('printed', stock['revision'], 1 if ack_lost else 0, note, _now(),
                assignment['serial']))
    db.execute('UPDATE label_job SET state=?,current_serial=?,reason=?,updated_at=? '
               'WHERE case_id=? AND job_id=?',
               ('printed', assignment['serial'], note, _now(), case['case_id'], job['job_id']))
    if assignment['print_operation_id']:
        db.execute('UPDATE label_operation SET state=?,result=?,revision_after=?,updated_at=? '
                   'WHERE operation_id=?',
                   ('ack_lost' if ack_lost else 'printed', 'printed', stock['revision'],
                    _now(), assignment['print_operation_id']))
    _audit(db, case['case_id'], job['job_id'], assignment['serial'],
           'print_ack_lost' if ack_lost else 'print_confirmed',
           {'revision': stock['revision'], 'content_sha256': stock['content_sha256']})


def _record_jammed(db, case, job, assignment, stock):
    db.execute('UPDATE label_assignment SET state=?,device_revision=?,paper_consumed=1,'
               'ack_lost=0,reason=?,updated_at=? WHERE serial=?',
               ('jammed', stock['revision'], 'paper_jam', _now(), assignment['serial']))
    db.execute('UPDATE label_job SET state=?,current_serial=NULL,reason=?,updated_at=? '
               'WHERE case_id=? AND job_id=?',
               ('paper_jam', 'paper_jam', _now(), case['case_id'], job['job_id']))
    if assignment['print_operation_id']:
        db.execute('UPDATE label_operation SET state=?,result=?,revision_after=?,updated_at=? '
                   'WHERE operation_id=?',
                   ('jammed', 'jammed', stock['revision'], _now(),
                    assignment['print_operation_id']))
    _audit(db, case['case_id'], job['job_id'], assignment['serial'], 'label_jammed',
           {'revision': stock['revision'], 'paper_consumed': 1})


def _confirm_void(db, case, job, serial, note):
    db.execute("UPDATE label_assignment SET state='voided',reason=?,updated_at=? WHERE serial=?",
               (note, _now(), serial))
    db.execute('UPDATE label_job SET state=?,current_serial=NULL,voided_serial=?,reason=?,'
               'updated_at=? WHERE case_id=? AND job_id=?',
               ('voided', serial, note, _now(), case['case_id'], job['job_id']))
    op = _row(db, "SELECT operation_id FROM label_operation WHERE case_id=? AND job_id=? "
                  "AND action='void' AND serial=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
              (case['case_id'], job['job_id'], serial))
    if op is not None:
        db.execute("UPDATE label_operation SET state='voided',result=?,updated_at=? "
                   'WHERE operation_id=?', (note, _now(), op['operation_id']))
    _audit(db, case['case_id'], job['job_id'], serial, 'label_voided', {'note': note})


# ---------------------------------------------------------------------------
# print
# ---------------------------------------------------------------------------

def print_label(db_path, case_id, job_id, operation_id=None, crash_at=None):
    """Issue one physical label for a frozen work order.

    The intent, operation id and serial are persisted before the device is
    touched.  Any restart either replays the recorded outcome or reconciles the
    observed stock; it never prints a second copy.
    """
    db = connect(db_path)
    try:
        case = _load_case(db, case_id)
        job = _load_job(db, case_id, job_id)
        if job['state'] == 'manual_review':
            raise LabelReviewRequired(job['reason'] or 'job_manual_review')
        operation_id = operation_id or _next_print_operation(db, case_id, job_id)
        prior = _row(db, 'SELECT * FROM label_operation WHERE operation_id=?', (operation_id,))
        if prior is not None:
            if (prior['case_id'], prior['job_id'], prior['action']) != (case_id, job_id, 'print'):
                raise ValueError('label_operation_conflict')
            if prior['state'] in COMPLETED_PRINT_OPS:
                return _print_replay(db, case, job, prior)
            if prior['state'] == 'manual_review':
                raise LabelReviewRequired(prior['result'] or 'print_operation_review')
            assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?',
                              (prior['serial'],))
            if assignment is None:
                raise LabelReviewRequired('print_assignment_missing')
            serial = prior['serial']
        else:
            assignment = _current_assignment(db, case_id, job_id)
            if assignment is not None and assignment['state'] == 'printed':
                raise ValueError('job_already_labeled')
            if assignment is not None and assignment['state'] in ('void_intent', 'void_ack_lost'):
                raise ValueError('void_unconfirmed')
            if assignment is not None and assignment['state'] == 'manual_review':
                raise LabelReviewRequired('job_manual_review')
            _require_source(db, case, job)
            adopted, status = _adopt_job_device(db, case, job)
            if status == 'manual_review':
                raise LabelReviewRequired('job_manual_review')
            if any(a['state'] == 'printed' for a in adopted):
                job = _load_job(db, case_id, job_id)
                printed = next(a for a in adopted if a['state'] == 'printed')
                _set_case_status(db, case_id)
                return _print_result(db, case, job, printed['serial'], 'timeout', operation_id)
            with db:
                if assignment is not None and assignment['state'] in ('allocated', 'intent_print'):
                    serial = assignment['serial']
                    expected = assignment['device_revision'] or 0
                else:
                    _, doc = _print_case_doc(db, case['print_case_id'])
                    serial, expected = _allocate(db, case, job, doc)
                db.execute('INSERT INTO label_operation(operation_id,case_id,job_id,serial,action,'
                           'state,expected_revision,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?)',
                           (operation_id, case_id, job_id, serial, 'print', 'intent',
                            expected, _now(), _now()))
                db.execute('UPDATE label_assignment SET state=?,print_operation_id=?,'
                           'ack_lost=0,reason=NULL,device_revision=?,updated_at=? WHERE serial=?',
                           ('intent_print', operation_id, expected, _now(), serial))
                _audit(db, case_id, job_id, serial, 'print_intent',
                       {'operation_id': operation_id, 'expected_revision': expected})
        _point(crash_at, 'print_intent')

        assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?', (serial,))
        kind, stock = _resolve_device(db, job, serial)
        if kind == 'conflict':
            with db:
                _block_job(db, case, job, 'device_state_mismatch',
                           {'serial': serial, 'stock': stock})
            raise LabelReviewRequired('device_state_mismatch', {'job_id': job_id})
        if kind == 'printed':
            ack_lost = bool(assignment['ack_lost']) or (prior is not None and
                                                        prior['state'] == 'ack_lost')
            with db:
                _record_printed(db, case, job, assignment, stock, ack_lost,
                                'print_confirmed_after_restart')
                _book_postage(db, case, job, serial, 'live_label')
                _refresh_job(db, case, job)
                _set_case_status(db, case_id)
            return _print_result(db, case, job, serial,
                                 'timeout' if ack_lost else 'committed')
        if kind == 'jammed':
            with db:
                _record_jammed(db, case, job, assignment, stock)
                _set_case_status(db, case_id)
            return _print_result(db, case, job, serial, 'jammed')

        # Still blank: the intent has not reached the paper yet.  Re-verify the
        # frozen evidence immediately before the device is touched, so a stale
        # source can never take a physical sheet even on a resumed intent.
        _require_source(db, case, job)
        assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?', (serial,))
        expected = assignment['device_revision'] or 0
        try:
            out = print_lab.invoke(db_path, serial, job_id, operation_id, expected,
                                   'print', content_sha256=job['layout_hash'],
                                   case_id=case['print_case_id'])
        except ValueError as exc:
            code = str(exc)
            if code == 'label_revision_conflict':
                with db:
                    _block_job(db, case, job, 'label_revision_conflict',
                               {'serial': serial, 'expected_revision': expected})
                raise LabelReviewRequired('label_revision_conflict', {'job_id': job_id})
            raise
        _point(crash_at, 'print_device')
        stock = _stock(db, serial)
        ack_lost = out.get('device_result') == 'committed_ack_lost'
        if out.get('device_status') == 'jammed':
            with db:
                _record_jammed(db, case, job, assignment, stock)
                _set_case_status(db, case_id)
            _point(crash_at, 'jam_recorded')
            return _print_result(db, case, job, serial, 'jammed')
        if out.get('device_status') != 'printed':
            with db:
                _block_job(db, case, job, 'device_result_unknown', {'result': out})
            raise LabelReviewRequired('device_result_unknown', {'job_id': job_id})

        with db:
            _record_printed(db, case, job, assignment, stock, ack_lost,
                            'ack_lost_reconciled' if ack_lost else 'printed')
        _point(crash_at, 'print_recorded')
        with db:
            _book_postage(db, case, job, serial, 'live_label')
        _point(crash_at, 'postage_booked')
        with db:
            _refresh_job(db, case, job)
            _set_case_status(db, case_id)
        _point(crash_at, 'job_ready')
        return _print_result(db, case, job, serial,
                             'timeout' if ack_lost else 'committed')
    finally:
        db.close()


def _print_result(db, case, job, serial, status, operation_id=None):
    job = _load_job(db, case['case_id'], job['job_id'])
    assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?', (serial,))
    return {'case_id': case['case_id'], 'job_id': job['job_id'], 'serial': serial,
            'operation_id': operation_id or (assignment or {}).get('print_operation_id'),
            'status': status, 'job_state': job['state'],
            'assignment_state': assignment['state'] if assignment else None,
            'ack_lost': bool(assignment['ack_lost']) if assignment else False,
            'paper_consumed': assignment['paper_consumed'] if assignment else 0,
            'postage_cents': db.execute('SELECT COALESCE(SUM(cents),0) FROM label_postage '
                                        'WHERE case_id=? AND job_id=?',
                                        (case['case_id'], job['job_id'])).fetchone()[0],
            'mail_ready': job['state'] == 'mail_ready'}


def _print_replay(db, case, job, prior):
    status = {'printed': 'committed', 'ack_lost': 'timeout', 'jammed': 'jammed'}[prior['state']]
    return _print_result(db, case, job, prior['serial'], status, prior['operation_id'])


# ---------------------------------------------------------------------------
# void
# ---------------------------------------------------------------------------

def void_label(db_path, case_id, job_id, operation_id=None, reason='superseded',
               crash_at=None):
    """Void the job's currently printed physical sheet.

    A lost void ACK is resolved by reading the device: only a device-confirmed
    ``void`` frees the slot for a replacement serial.  Until then the job keeps
    its printed sheet and printing is refused, so L100 and a reprint can never
    be released together.
    """
    db = connect(db_path)
    try:
        case = _load_case(db, case_id)
        job = _load_job(db, case_id, job_id)
        operation_id = operation_id or _next_void_operation(db, case_id, job_id)
        prior = _row(db, 'SELECT * FROM label_operation WHERE operation_id=?', (operation_id,))
        if prior is not None:
            if (prior['case_id'], prior['job_id'], prior['action']) != (case_id, job_id, 'void'):
                raise ValueError('label_operation_conflict')
            if prior['state'] == 'voided':
                return _void_result(db, case, job, prior['serial'], 'already_voided')
            if prior['state'] == 'manual_review':
                raise LabelReviewRequired(prior['result'] or 'void_operation_review')
            serial = prior['serial']
        else:
            assignment = _current_assignment(db, case_id, job_id)
            if assignment is None or assignment['state'] != 'printed':
                raise ValueError('label_not_printed')
            ok, code = _ready_basis(db, case, job)
            if not ok and code != ['postage_missing']:
                with db:
                    _block_job(db, case, job, code[0], {'serial': assignment['serial']})
                raise LabelReviewRequired(code[0], {'job_id': job_id})
            serial = assignment['serial']
            with db:
                db.execute('INSERT INTO label_operation(operation_id,case_id,job_id,serial,action,'
                           'state,expected_revision,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?)',
                           (operation_id, case_id, job_id, serial, 'void', 'intent',
                            assignment['device_revision'], _now(), _now()))
                db.execute("UPDATE label_assignment SET state='void_intent',void_operation_id=?,"
                           'reason=?,updated_at=? WHERE serial=?',
                           (operation_id, reason, _now(), serial))
                db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                           'WHERE case_id=? AND job_id=?',
                           ('void_pending', reason, _now(), case_id, job_id))
                _audit(db, case_id, job_id, serial, 'void_intent',
                       {'operation_id': operation_id, 'reason': reason})
        _point(crash_at, 'void_intent')

        assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?', (serial,))
        stock = _stock(db, serial)
        if stock is None:
            with db:
                _block_job(db, case, job, 'label_missing', {'serial': serial})
            raise LabelReviewRequired('label_missing', {'job_id': job_id})
        if stock['status'] == 'void' and stock['job'] == job_id:
            with db:
                _confirm_void(db, case, job, serial, 'void_confirmed_after_restart')
                _set_case_status(db, case_id)
            return _void_result(db, case, job, serial, 'already_voided')
        if not _device_matches(stock, job, serial):
            with db:
                _block_job(db, case, job, 'void_device_state_mismatch',
                           {'serial': serial, 'stock': stock})
            raise LabelReviewRequired('void_device_state_mismatch', {'job_id': job_id})
        try:
            out = print_lab.invoke(db_path, serial, job_id, operation_id,
                                   assignment['device_revision'], 'void',
                                   case_id=case['print_case_id'])
        except ValueError as exc:
            code = str(exc)
            with db:
                db.execute("UPDATE label_operation SET state='void_failed',result=?,updated_at=? "
                           'WHERE operation_id=?', (code, _now(), operation_id))
                db.execute("UPDATE label_assignment SET state='printed',reason=?,updated_at=? "
                           'WHERE serial=?', (code, _now(), serial))
                db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                           'WHERE case_id=? AND job_id=?',
                           ('void_failed', code, _now(), case_id, job_id))
                _audit(db, case_id, job_id, serial, 'void_failed', {'code': code})
                _set_case_status(db, case_id)
            if code == 'label_revision_conflict':
                with db:
                    _block_job(db, case, job, 'label_revision_conflict',
                               {'serial': serial})
                raise LabelReviewRequired('label_revision_conflict', {'serial': serial})
            raise LabelReviewRequired('void_failed', {'serial': serial, 'code': code})
        _point(crash_at, 'void_device')
        stock = _stock(db, serial)
        if stock is not None and stock['status'] == 'void' and stock['job'] == job_id:
            note = 'void_ack_lost' if out.get('status') == 'timeout' else 'void_confirmed'
            with db:
                _confirm_void(db, case, job, serial, note)
                _set_case_status(db, case_id)
            _point(crash_at, 'void_recorded')
            return _void_result(db, case, job, serial,
                                'committed_ack_lost' if out.get('status') == 'timeout'
                                else 'committed')
        with db:
            _block_job(db, case, job, 'void_not_observed', {'serial': serial, 'device': out})
        raise LabelReviewRequired('void_not_observed', {'serial': serial})
    finally:
        db.close()


def _void_result(db, case, job, serial, status):
    job = _load_job(db, case['case_id'], job['job_id'])
    assignment = _row(db, 'SELECT * FROM label_assignment WHERE serial=?', (serial,))
    return {'case_id': case['case_id'], 'job_id': job['job_id'], 'serial': serial,
            'status': status, 'job_state': job['state'],
            'assignment_state': assignment['state'] if assignment else None,
            'voided_serial': job['voided_serial']}


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------

def _adopt_job_device(db, case, job, summary=None):
    """Reconcile physical sheets that belong to this job but have no ledger row.

    This is how a restarted process notices that a previous attempt already
    reached the paper (the fixture's ``L100 printed J1 / ACK lost`` state).
    Adoption demands both job ownership and the exact frozen layout digest; a
    second physical sheet for a job that already owns a label is a duplicate and
    blocks for review.
    """
    adopted = []
    stocks = [dict(r) for r in db.execute(
        'SELECT * FROM label_stock WHERE job=? ORDER BY serial', (job['job_id'],))]
    for stock in stocks:
        serial = stock['serial']
        if db.execute('SELECT 1 FROM label_assignment WHERE serial=?',
                      (serial,)).fetchone() is not None:
            continue
        current = _current_assignment(db, case['case_id'], job['job_id'])
        if stock['status'] == 'printed' and stock['content_sha256'] != job['layout_hash']:
            with db:
                _block_job(db, case, job, 'unaccounted_label_content_mismatch',
                           {'serial': serial, 'content_sha256': stock['content_sha256']})
            if summary is not None:
                summary['manual_review'].append(
                    {'case_id': case['case_id'], 'job_id': job['job_id'],
                     'reason': 'unaccounted_label_content_mismatch'})
            return adopted, 'manual_review'
        if current is not None:
            if stock['status'] in ('printed', 'jammed'):
                with db:
                    _block_job(db, case, job, 'duplicate_physical_label',
                               {'serial': serial, 'current_serial': current['serial']})
                if summary is not None:
                    summary['manual_review'].append(
                        {'case_id': case['case_id'], 'job_id': job['job_id'],
                         'reason': 'duplicate_physical_label'})
                return adopted, 'manual_review'
            continue
        state = 'printed' if stock['status'] == 'printed' else (
            'jammed' if stock['status'] == 'jammed' else None)
        if state is None:
            continue
        seq = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM label_assignment '
                         'WHERE case_id=? AND job_id=?',
                         (case['case_id'], job['job_id'])).fetchone()[0]
        now = _now()
        ack_lost = 1 if state == 'printed' else 0
        with db:
            db.execute('INSERT INTO label_assignment(serial,case_id,job_id,sequence,state,'
                       'layout_hash,device_revision,paper_consumed,postage_cents,ack_lost,'
                       'retained,reason,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (serial, case['case_id'], job['job_id'], seq, state,
                        job['layout_hash'], stock['revision'], 1, 0, ack_lost, 0,
                        'orphan_print_adopted' if state == 'printed' else 'orphan_jam_adopted',
                        now, now))
            if state == 'printed':
                db.execute('UPDATE label_job SET state=?,current_serial=?,reason=?,updated_at=? '
                           'WHERE case_id=? AND job_id=?',
                           ('printed', serial, 'ack_lost', now, case['case_id'],
                            job['job_id']))
                _book_postage(db, case, job, serial, 'live_label')
            else:
                db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                           'WHERE case_id=? AND job_id=?',
                           ('paper_jam', 'paper_jam', now, case['case_id'], job['job_id']))
            _audit(db, case['case_id'], job['job_id'], serial,
                   'orphan_print_adopted' if state == 'printed' else 'orphan_jam_adopted',
                   {'revision': stock['revision'],
                    'content_sha256': stock['content_sha256'], 'ack_lost': bool(ack_lost)})
            _refresh_job(db, case, job)
            _set_case_status(db, case['case_id'])
        adopted.append({'serial': serial, 'state': state, 'ack_lost': bool(ack_lost)})
        if summary is not None and state == 'printed':
            summary['adopted_prints'].append(
                {'case_id': case['case_id'], 'job_id': job['job_id'],
                 'serial': serial, 'ack_lost': True})
        if summary is not None and state == 'jammed':
            summary['jammed'].append({'case_id': case['case_id'], 'job_id': job['job_id'],
                                      'serial': serial})
    return adopted, 'ok'


def _adopt_orphans(db, case, summary, crash_at=None):
    """Run device adoption for every work order in the case."""
    for job in _jobs(db, case['case_id']):
        if job['state'] == 'manual_review':
            continue
        _adopt_job_device(db, case, job, summary)
        _point(crash_at, 'recover')


def recover_case(db_path, case_id=None, crash_at=None):
    """Reconcile every persisted intent against the real device and stock."""
    db = connect(db_path)
    try:
        if case_id is None:
            cases = [dict(r) for r in db.execute('SELECT * FROM label_case ORDER BY case_id')]
        else:
            cases = [_load_case(db, case_id)]
        summary = {'cases': [], 'adopted_prints': [], 'confirmed_voids': [], 'jammed': [],
                   'not_executed': [], 'manual_review': [], 'postage_booked': []}
        for case in cases:
            cid = case['case_id']
            description = {'case_id': cid, 'contract': case['contract'], 'jobs': []}
            _adopt_orphans(db, case, summary)
            for job in _jobs(db, cid):
                if job['state'] == 'manual_review':
                    summary['manual_review'].append({'case_id': cid, 'job_id': job['job_id'],
                                                     'reason': job['reason']})
                    description['jobs'].append({'job_id': job['job_id'],
                                                'state': job['state'],
                                                'reason': job['reason']})
                    continue
                assignment = _current_assignment(db, cid, job['job_id'])
                if assignment is not None and assignment['state'] == 'intent_print':
                    kind, stock = _resolve_device(db, job, assignment['serial'])
                    if kind == 'printed':
                        with db:
                            _record_printed(db, case, job, assignment, stock,
                                            ack_lost=bool(assignment['ack_lost']),
                                            note='crash_recovery_adopted')
                            booked = _book_postage(db, case, job, assignment['serial'],
                                                   'live_label')
                        summary['adopted_prints'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'serial': assignment['serial'],
                             'ack_lost': bool(assignment['ack_lost'])})
                        if booked:
                            summary['postage_booked'].append(
                                {'case_id': cid, 'job_id': job['job_id'],
                                 'serial': assignment['serial']})
                    elif kind == 'jammed':
                        with db:
                            _record_jammed(db, case, job, assignment, stock)
                        summary['jammed'].append({'case_id': cid, 'job_id': job['job_id'],
                                                  'serial': assignment['serial']})
                    elif kind == 'blank':
                        with db:
                            db.execute("UPDATE label_assignment SET state='allocated',"
                                       'updated_at=? WHERE serial=?',
                                       (_now(), assignment['serial']))
                            if assignment['print_operation_id']:
                                db.execute("UPDATE label_operation SET state='not_executed',"
                                           'updated_at=? WHERE operation_id=?',
                                           (_now(), assignment['print_operation_id']))
                            db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                                       'WHERE case_id=? AND job_id=?',
                                       ('pending', 'print_not_executed', _now(), cid,
                                        job['job_id']))
                            _audit(db, cid, job['job_id'], assignment['serial'],
                                   'print_intent_not_executed')
                        summary['not_executed'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'serial': assignment['serial']})
                    else:
                        with db:
                            _block_job(db, case, job, 'crash_state_mismatch',
                                       {'serial': assignment['serial'], 'stock': stock})
                        summary['manual_review'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'reason': 'crash_state_mismatch'})
                        continue
                elif assignment is not None and assignment['state'] in ('void_intent',
                                                                        'void_ack_lost'):
                    stock = _stock(db, assignment['serial'])
                    if stock is not None and stock['status'] == 'void' and \
                            stock['job'] == job['job_id']:
                        with db:
                            _confirm_void(db, case, job, assignment['serial'],
                                          'void_confirmed_after_restart')
                        summary['confirmed_voids'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'serial': assignment['serial']})
                    elif _device_matches(stock, job, assignment['serial']):
                        # The void never reached the paper: keep the sheet live
                        # and leave the job blocked until the void is retried.
                        with db:
                            db.execute("UPDATE label_assignment SET state='printed',"
                                       'updated_at=? WHERE serial=?',
                                       (_now(), assignment['serial']))
                            db.execute('UPDATE label_job SET state=?,reason=?,updated_at=? '
                                       'WHERE case_id=? AND job_id=?',
                                       ('void_failed', 'void_unconfirmed', _now(), cid,
                                        job['job_id']))
                        summary['manual_review'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'reason': 'void_unconfirmed'})
                    else:
                        with db:
                            _block_job(db, case, job, 'void_state_mismatch',
                                       {'serial': assignment['serial'], 'stock': stock})
                        summary['manual_review'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'reason': 'void_state_mismatch'})
                    continue
                elif assignment is not None and assignment['state'] == 'printed':
                    ok, basis = _ready_basis(db, case, job)
                    if not ok and basis == ['postage_missing']:
                        with db:
                            _book_postage(db, case, job, assignment['serial'], 'live_label')
                        summary['postage_booked'].append(
                            {'case_id': cid, 'job_id': job['job_id'],
                             'serial': assignment['serial']})
                    elif not ok:
                        with db:
                            _block_job(db, case, job, basis[0],
                                       {'serial': assignment['serial'], 'basis': basis})
                        summary['manual_review'].append(
                            {'case_id': cid, 'job_id': job['job_id'], 'reason': basis[0]})
                        continue
                    else:
                        src_ok, src_reason = _verify_source(db, job)
                        if not src_ok:
                            with db:
                                _block_job(db, case, job, src_reason,
                                           {'serial': assignment['serial']})
                            summary['manual_review'].append(
                                {'case_id': cid, 'job_id': job['job_id'],
                                 'reason': src_reason})
                            continue
                    with db:
                        _refresh_job(db, case, job)
                        _set_case_status(db, cid)
                else:
                    with db:
                        _refresh_job(db, case, job)
                _point(crash_at, 'recover')
                job = _load_job(db, cid, job['job_id'])
                description['jobs'].append({'job_id': job['job_id'], 'state': job['state'],
                                            'current_serial': job['current_serial'],
                                            'reason': job['reason']})
            with db:
                _set_case_status(db, cid)
            description['status'] = _load_case(db, cid)['status']
            summary['cases'].append(description)
        return summary
    finally:
        db.close()


# ---------------------------------------------------------------------------
# contract execution
# ---------------------------------------------------------------------------

def _needs_print(db, case, job):
    if job['state'] == 'manual_review':
        return False
    current = _current_assignment(db, case['case_id'], job['job_id'])
    if current is None:
        return True
    return current['state'] in ('allocated', 'intent_print')


def _verify_and_retain(db, case, job, summary):
    ok, basis = _ready_basis(db, case, job)
    if not ok:
        with db:
            _block_job(db, case, job, basis[0], {'job_id': job['job_id']})
        raise LabelReviewRequired(basis[0], {'job_id': job['job_id']})
    current = _current_assignment(db, case['case_id'], job['job_id'])
    with db:
        db.execute('UPDATE label_assignment SET retained=1,reason=?,updated_at=? WHERE serial=?',
                   ('retained_after_verification', _now(), current['serial']))
        _audit(db, case['case_id'], job['job_id'], current['serial'], 'label_retained',
               {'basis': basis})
        job = _refresh_job(db, case, job)
        _set_case_status(db, case['case_id'])
    summary['retained'].append({'job_id': job['job_id'], 'serial': current['serial'],
                                'basis': basis})
    return job


def _print_until_labeled(db_path, case_id, job_id, summary, crash_at=None):
    """Print for one job until it owns a confirmed label or stock runs out."""
    for _ in range(64):
        out = print_label(db_path, case_id, job_id, crash_at=crash_at)
        summary['prints'].append(out)
        if out['status'] != 'jammed':
            return out
    raise ValueError('label_stock_exhausted')


def _handle_ack_lost(db_path, case_id, summary, crash_at=None):
    """Resolve every printed-but-unacknowledged sheet per the contract."""
    db = connect(db_path)
    try:
        case = _load_case(db, case_id)
        jobs = _jobs(db, case_id)
    finally:
        db.close()
    for job in jobs:
        db = connect(db_path)
        try:
            case = _load_case(db, case_id)
            job = _load_job(db, case_id, job['job_id'])
            current = _current_assignment(db, case_id, job['job_id'])
        finally:
            db.close()
        if job['state'] == 'manual_review':
            continue
        if current is None or current['state'] != 'printed' or not current['ack_lost']:
            continue
        if case['contract'] == 'retain_verified_serial':
            if current['retained']:
                continue
            db = connect(db_path)
            try:
                case = _load_case(db, case_id)
                job = _load_job(db, case_id, job['job_id'])
                _verify_and_retain(db, case, job, summary)
            finally:
                db.close()
        else:
            # In the void-and-reissue contract an unacknowledged sheet is
            # never trusted even though the device says it printed: void it,
            # then print a replacement serial.
            summary['voids'].append(
                void_label(db_path, case_id, job['job_id'], crash_at=crash_at))


def _jobs_needing_print(db_path, case_id, states):
    db = connect(db_path)
    try:
        case = _load_case(db, case_id)
        result = []
        for job in _jobs(db, case_id):
            if job['state'] in states and _needs_print(db, case, job):
                result.append(job['job_id'])
        return result
    finally:
        db.close()


def run_contract(db_path, case_id, crash_at=None):
    """Execute an authorised disposition contract end to end.

    Rounds keep the fixture deterministic while still converging from a fresh
    case: resolve lost-ACK sheets first (retain after device verification, or
    void and reissue), then issue labels to jobs that never had one.  A job is
    never printed twice because the device is reconciled before every round.
    """
    summary = {'case_id': case_id, 'contract': None, 'prints': [], 'voids': [],
               'retained': [], 'recover': None}
    summary['recover'] = recover_case(db_path, case_id)
    db = connect(db_path)
    try:
        case = _load_case(db, case_id)
        summary['contract'] = case['contract']
    finally:
        db.close()
    if any(j['state'] == 'manual_review' for j in recover_jobs(summary['recover'], case_id)):
        raise LabelReviewRequired('case_needs_review')

    for _ in range(8):
        _handle_ack_lost(db_path, case_id, summary, crash_at)
        fresh = _jobs_needing_print(db_path, case_id, ('pending',))
        if not fresh:
            break
        for job_id in fresh:
            _print_until_labeled(db_path, case_id, job_id, summary, crash_at)
    _handle_ack_lost(db_path, case_id, summary, crash_at)
    for _ in range(8):
        reissue = _jobs_needing_print(db_path, case_id,
                                      ('voided', 'paper_jam', 'void_failed', 'voided_reissue'))
        if not reissue:
            break
        for job_id in reissue:
            _print_until_labeled(db_path, case_id, job_id, summary, crash_at)
    _handle_ack_lost(db_path, case_id, summary, crash_at)

    db = connect(db_path)
    try:
        with db:
            _set_case_status(db, case_id)
        summary['status'] = _load_case(db, case_id)['status']
        summary['jobs'] = [{'job_id': j['job_id'], 'state': j['state'],
                            'current_serial': j['current_serial'],
                            'voided_serial': j['voided_serial'],
                            'reason': j['reason']} for j in _jobs(db, case_id)]
    finally:
        db.close()
    return summary


def recover_jobs(recovery, case_id):
    for case in recovery.get('cases', []):
        if case['case_id'] == case_id:
            return [{'state': j['state']} for j in case['jobs']]
    return []


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def report(db_path, case_id=None):
    """Build the label issuance report without touching source/event/decision."""
    db = connect(db_path)
    try:
        if case_id is None:
            cases = [dict(r) for r in db.execute('SELECT * FROM label_case ORDER BY case_id')]
        else:
            cases = [_load_case(db, case_id)]
        data = {'cases': [], 'label_stock': [], 'scrap': [], 'postage': {},
                'mail_ready': [], 'unaccounted_stock': []}
        for case in cases:
            cid = case['case_id']
            entry = {'case_id': cid, 'contract': case['contract'], 'status': case['status'],
                     'jobs': [], 'postage_total_cents': 0, 'postage_live_cents': 0,
                     'mail_ready': []}
            for job in _jobs(db, cid):
                current = _current_assignment(db, cid, job['job_id'])
                assignments = _assignments(db, cid, job['job_id'])
                stock = _stock(db, current['serial']) if current else None
                ok, basis = _ready_basis(db, case, job)
                charged = db.execute('SELECT COALESCE(SUM(cents),0) FROM label_postage '
                                     'WHERE case_id=? AND job_id=?',
                                     (cid, job['job_id'])).fetchone()[0]
                entry['jobs'].append({
                    'job_id': job['job_id'],
                    'subject': job['subject'],
                    'message_id': job['message_id'],
                    'queued_event_id': job['queued_event_id'],
                    'queued_identity': job['queued_identity'],
                    'queued_event_sha256': job['queued_event_sha256'],
                    'source_id': job['source_id'],
                    'source_sha256': job['source_sha256'],
                    'layout_version': job['layout_version'],
                    'layout_hash': job['layout_hash'],
                    'state': job['state'],
                    'current_serial': job['current_serial'],
                    'voided_serial': job['voided_serial'],
                    'current_label': ({
                        'serial': current['serial'], 'state': current['state'],
                        'device_status': stock['status'] if stock else None,
                        'device_revision': stock['revision'] if stock else None,
                        'job_on_device': stock['job'] if stock else None,
                        'content_sha256': stock['content_sha256'] if stock else None,
                        'ack_lost': bool(current['ack_lost']),
                        'retained': bool(current['retained']),
                        'paper_consumed': current['paper_consumed'],
                    } if current else None),
                    'assignments': assignments,
                    'postage_charged_cents': charged,
                    'postage_live_cents': (current['postage_cents']
                                           if current and current['state'] == 'printed'
                                           and current['postage_cents'] else 0),
                    'mail_ready': bool(job['state'] == 'mail_ready' and ok),
                    'mail_ready_basis': basis if ok else [],
                    'not_mail_ready_basis': [] if ok else basis,
                    'delivery_decision': _row(
                        db, 'SELECT status,rule_version,run_id FROM decision WHERE subject=?',
                        (job['subject'],)),
                })
                entry['postage_total_cents'] += charged
                if current and current['state'] == 'printed' and current['postage_cents']:
                    entry['postage_live_cents'] += current['postage_cents']
                if job['state'] == 'mail_ready' and ok:
                    entry['mail_ready'].append(job['job_id'])
                for a in assignments:
                    if a['state'] in ('jammed', 'voided'):
                        data['scrap'].append({
                            'case_id': cid, 'job_id': job['job_id'],
                            'serial': a['serial'], 'state': a['state'],
                            'paper_consumed': a['paper_consumed'],
                            'postage_cents': a['postage_cents']})
            data['mail_ready'].append({'case_id': cid, 'jobs': entry['mail_ready']})
            data['cases'].append(entry)
            data['postage'][cid] = [dict(row) for row in db.execute(
                'SELECT * FROM label_postage WHERE case_id=? ORDER BY serial', (cid,))]
            data.setdefault('label_operations', {})[cid] = [dict(row) for row in db.execute(
                'SELECT * FROM label_operation WHERE case_id=? ORDER BY created_at,rowid',
                (cid,))]
        for row in db.execute('SELECT * FROM label_stock ORDER BY serial'):
            item = dict(row)
            assignment = _row(db, 'SELECT case_id,job_id,sequence,state FROM label_assignment '
                                  'WHERE serial=?', (row['serial'],))
            item['assignment'] = assignment
            if assignment is None and row['status'] != 'blank':
                data['unaccounted_stock'].append(item)
            data['label_stock'].append(item)
        data['stock_remaining'] = [row['serial'] for row in db.execute(
            "SELECT serial FROM label_stock WHERE status='blank' ORDER BY serial")
            if row['serial'] not in {a['serial'] for a in db.execute(
                'SELECT serial FROM label_assignment')}]
        data['waste'] = {
            'jammed': [s for s in data['scrap'] if s['state'] == 'jammed'],
            'voided': [s for s in data['scrap'] if s['state'] == 'voided'],
            'paper_sheets_consumed': sum(
                1 for row in db.execute('SELECT paper_consumed FROM label_assignment')
                if row[0]),
        }
        data['postage_totals'] = {
            'charged_cents': db.execute('SELECT COALESCE(SUM(cents),0) FROM label_postage'
                                        ).fetchone()[0],
            'live_cents': db.execute(
                'SELECT COALESCE(SUM(p.cents),0) FROM label_postage p '
                "JOIN label_assignment a ON a.serial=p.serial WHERE a.state='printed'"
            ).fetchone()[0],
        }
        data['source_evidence'] = [
            dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
        data['events'] = [dict(r) for r in db.execute(
            'SELECT id,source_id,identity,subject,kind,time,sha256 FROM event ORDER BY id')]
        data['decisions'] = [dict(r) for r in db.execute('SELECT * FROM decision ORDER BY subject')]
        data['print_case'] = [dict(r) for r in db.execute(
            'SELECT id,payload FROM print_case ORDER BY id')]
        return data
    finally:
        db.close()


def write_report(db_path, out, case_id=None):
    from pathlib import Path
    data = report(db_path, case_id)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 实体标签发行与库存报告', '']
    for case in data['cases']:
        lines.append(f"## {case['case_id']} — {case['contract']} ({case['status']})")
        for job in case['jobs']:
            lines.append(
                f"- {job['job_id']} / {job['subject']} / {job['message_id']}: "
                f"{job['state']}，当前标签 {job['current_serial']}，"
                f"邮资 {job['postage_charged_cents']} 分，"
                f"交邮就绪={'是' if job['mail_ready'] else '否'}"
                + (f"（依据：{', '.join(job['mail_ready_basis'])}）"
                   if job['mail_ready'] else f"（{', '.join(job['not_mail_ready_basis'])}）"))
        lines.append(f"- 库存余量：{', '.join(data['stock_remaining']) or '无'}")
        lines.append(f"- 废标：{len(data['waste']['jammed'])} 枚卡纸 / "
                     f"{len(data['waste']['voided'])} 枚作废")
        lines.append(f"- 邮资：已入账 {case['postage_total_cents']} 分，"
                     f"有效标签 {case['postage_live_cents']} 分")
        lines.append('')
    target.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
    return target
