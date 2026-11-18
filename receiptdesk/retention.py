"""Internal retention and erasure engine.

Two authorized contracts operate on the same mixed-recipient source blob and
produce genuinely different on-disk bytes:

* ``shared_source_hold`` keeps the whole R1+R2 blob (and its original hash)
  while an R1 hold is alive; R2's erasure request is recorded as *pending* and
  R2 content is masked on every normal read/report path. Releasing the hold
  re-processes the pending work — the bytes are never merely hidden.
* ``subject_erasure_with_rebase`` rebuilds, inside one transaction, a source
  containing only the retained subject's original lines (with original line
  numbers carried in a lineage record and a tombstone over the old hash),
  migrates the retained events to the complete new source and physically
  removes the erase subject's events/payloads and the old mixed blob.

Every erasure is checked against file hash, original line number, event
attribution and hold credential. A line that cannot be fully attested — bad
JSON, an identity not owned by an event, or a subject that does not match the
stored event — fails closed; it is never guessed to belong to the erase
subject and deleted.
"""
import json
import re
from datetime import datetime, timezone

from .store import connect, hash_bytes, begin_immediate
from .ingest import event_identity

CONTRACTS = ('shared_source_hold', 'subject_erasure_with_rebase')

# Test hook: when set, the named step raises before the surrounding
# transaction commits, proving rollback restores the complete old state.
FAULT_POINTS = {}


class RetentionError(ValueError):
    """A retention request failed verification; no state was changed."""


def set_fault(name):
    FAULT_POINTS[name] = True


def clear_faults():
    FAULT_POINTS.clear()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _audit(db, request_id, action, details):
    db.execute(
        'INSERT INTO retention_audit(request_id,action,at,details) VALUES (?,?,?,?)',
        (request_id, action, _now(), json.dumps(details, ensure_ascii=False, sort_keys=True)))


def _parse_hold_until(text):
    m = re.search(r'through\s+(\d{4}-\d{2}-\d{2})', text)
    if not m:
        raise RetentionError('hold_credential_missing_until')
    return m.group(1)


def _split_lines(raw):
    """Split original bytes into lines, keeping each line's original terminator."""
    return raw.decode('utf-8-sig').splitlines(keepends=True)


def _load_source(db, source_sha256):
    row = db.execute('SELECT * FROM source WHERE sha256=?', (source_sha256,)).fetchone()
    if row is None:
        raise RetentionError('unknown_source_hash')
    return dict(row)


def _classify_lines(db, source, erase_subject):
    """Attest every non-blank line and partition it into retain/erase.

    A line is attributed to the erase subject only when all of the following
    hold: it parses as JSON; its derived identity matches an event actually
    stored; that event belongs to this source and this exact line number; its
    canonical hash matches; and its subject equals the event's stored subject.
    Anything else raises RetentionError and blocks the whole request.
    """
    sid = source['id']
    raw = source['raw']
    actual_hash = hash_bytes(raw)
    if actual_hash != source['sha256']:
        raise RetentionError('source_hash_does_not_match_blob')
    retain, erase, unattested = [], [], []
    erase_events, retain_events = [], []
    for line_no, line in enumerate(_split_lines(raw), 1):
        if not line.strip():
            retain.append((line_no, line))  # blank separator stays byte-identical
            continue
        try:
            row = json.loads(line)
            identity = event_identity(row)
            subject = str(row['subject'])
            canonical = json.dumps(row, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':')).encode('utf-8')
        except (ValueError, TypeError, KeyError) as exc:
            unattested.append({'line_no': line_no, 'error': f'parse:{exc}'})
            continue
        ev = db.execute('SELECT * FROM event WHERE identity=?', (identity,)).fetchone()
        if ev is None:
            unattested.append({'line_no': line_no, 'error': 'identity_not_in_event_table'})
            continue
        if ev['source_id'] != sid or ev['line_no'] != line_no:
            unattested.append({'line_no': line_no,
                               'error': 'event_attribution_mismatch'})
            continue
        if ev['sha256'] != hash_bytes(canonical):
            unattested.append({'line_no': line_no, 'error': 'event_hash_mismatch'})
            continue
        if ev['subject'] != subject:
            # Cross-subject identity misuse: never guess this is the erase row.
            unattested.append({'line_no': line_no,
                               'error': 'cross_subject_identity_mismatch'})
            continue
        entry = (line_no, line, dict(ev))
        if subject == erase_subject:
            erase.append(entry)
            erase_events.append(dict(ev))
        else:
            retain.append(entry)
            retain_events.append(dict(ev))
    if unattested:
        raise RetentionError('unattested_line_blocked:' + json.dumps(
            unattested, ensure_ascii=False, sort_keys=True))
    if not erase_events:
        raise RetentionError('no_attested_event_for_subject')
    return {
        'retain_lines': retain,
        'erase_lines': erase,
        'erase_events': erase_events,
        'retain_events': retain_events,
    }


def _active_hold(db, source_id, now):
    return db.execute(
        "SELECT * FROM retention_request WHERE source_id=? AND request_type='hold' "
        "AND status='active' ORDER BY created_at,id", (source_id,)).fetchone()


def _existing_request(db, request_id, expected):
    row = db.execute('SELECT * FROM retention_request WHERE id=?', (request_id,)).fetchone()
    if row is None:
        return None
    for key, value in expected.items():
        if str(row[key]) != str(value):
            raise RetentionError(f'request_id_reused_with_different_{key}')
    return dict(row)


def _do_rebase(db, req, source, classified, now):
    """Physical per-subject erasure; caller already holds BEGIN IMMEDIATE."""
    old_sid = source['id']
    old_hash = source['sha256']
    new_text = ''.join(text for _, text, *_ in classified['retain_lines'])
    new_raw = new_text.encode('utf-8')
    new_hash = hash_bytes(new_raw)

    existing = db.execute('SELECT id FROM source WHERE sha256=?', (new_hash,)).fetchone()
    if existing:
        new_sid = existing['id']
        new_raw_stored = db.execute('SELECT raw FROM source WHERE id=?',
                                    (new_sid,)).fetchone()['raw']
        if new_raw_stored != new_raw:
            raise RetentionError('rebuilt_hash_collision_with_different_bytes')
    else:
        rebased_path = source['path'] + '#' + req['id'] + '.rebased'
        new_sid = db.execute(
            'INSERT INTO source(path,sha256,raw) VALUES (?,?,?)',
            (rebased_path, new_hash, new_raw)).lastrowid
    if FAULT_POINTS.get('after_new_source_insert'):
        raise RuntimeError('injected_fault_after_new_source_insert')

    line_map = []
    for new_line_no, item in enumerate(
            (li for li in classified['retain_lines'] if len(li) == 3), 1):
        old_line_no, _text, ev = item
        db.execute('UPDATE event SET source_id=?,line_no=? WHERE id=?',
                   (new_sid, new_line_no, ev['id']))
        line_map.append({'old_line': old_line_no, 'new_line': new_line_no,
                         'event_id': ev['id'], 'identity': ev['identity']})

    erase_events = classified['erase_events']
    erase_event_ids = [ev['id'] for ev in erase_events]
    erase_line_nos = [ev['line_no'] for ev in erase_events]
    erased_addresses = []
    for ev in erase_events:
        try:
            addr = json.loads(ev['payload']).get('address')
            if addr:
                erased_addresses.append(addr)
        except (ValueError, TypeError):
            pass
    if erase_event_ids:
        # Drop decision/evidence belonging to the erased subject and detach
        # issue rows before removing the events themselves.
        db.execute('DELETE FROM evidence WHERE subject=?', (req['subject'],))
        db.execute('DELETE FROM decision WHERE subject=?', (req['subject'],))
        db.execute('UPDATE issue SET event_id=NULL WHERE event_id IN (%s)'
                   % ','.join('?' * len(erase_event_ids)), erase_event_ids)
        db.execute('DELETE FROM event WHERE id IN (%s)'
                   % ','.join('?' * len(erase_event_ids)), erase_event_ids)

    residual = db.execute('SELECT 1 FROM event WHERE subject=? LIMIT 1',
                          (req['subject'],)).fetchone()
    if residual:
        raise RetentionError('erased_subject_still_present')
    # No erased address may survive in any active event payload or active blob.
    for addr in erased_addresses:
        if db.execute('SELECT 1 FROM event WHERE payload LIKE ? LIMIT 1',
                      ('%' + addr + '%',)).fetchone():
            raise RetentionError('erased_address_still_in_event_payload')
        if db.execute('SELECT 1 FROM source WHERE raw LIKE ? LIMIT 1',
                      (b'%' + addr.encode('utf-8') + b'%',)).fetchone():
            raise RetentionError('erased_address_still_in_source_blob')
    if old_hash == new_hash:
        raise RetentionError('rebase_did_not_change_source_hash')

    db.execute('DELETE FROM source WHERE id=?', (old_sid,))
    gone = db.execute('SELECT 1 FROM source WHERE id=?', (old_sid,)).fetchone()
    if gone:
        raise RetentionError('old_source_not_removed')

    db.execute(
        'INSERT INTO source_lineage(new_source_id,old_source_id,old_sha256,'
        'new_sha256,request_id,reason,line_map) VALUES (?,?,?,?,?,?,?)',
        (new_sid, old_sid, old_hash, new_hash, req['id'], req['reason'],
         json.dumps(line_map, ensure_ascii=False, sort_keys=True)))
    db.execute(
        'INSERT INTO source_hash_tombstone(sha256,source_id,request_id,reason,'
        'replaced_by_sha256,at) VALUES (?,?,?,?,?,?)',
        (old_hash, old_sid, req['id'], req['reason'], new_hash, now))
    details = json.dumps({
        'old_source_id': old_sid, 'new_source_id': new_sid,
        'old_sha256': old_hash, 'new_sha256': new_hash,
        'erased_event_ids': erase_event_ids, 'erased_line_nos': erase_line_nos,
        'line_map': line_map,
    }, ensure_ascii=False, sort_keys=True)
    existing_req = db.execute('SELECT 1 FROM retention_request WHERE id=?',
                              (req['id'],)).fetchone()
    if existing_req:
        # Release flow: the pending placeholder becomes the completed record.
        db.execute(
            'UPDATE retention_request SET contract=?,request_type=?,source_id=?,'
            'source_sha256=?,status=?,updated_at=?,details=? WHERE id=?',
            (req['contract'], 'erasure', new_sid, old_hash, 'completed',
             now, details, req['id']))
    else:
        db.execute(
            'INSERT INTO retention_request(id,contract,request_type,source_id,'
            'source_sha256,subject,credential_version,credential_sha256,hold_until,'
            'status,reason,created_at,updated_at,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (req['id'], req['contract'], 'erasure', new_sid, old_hash, req['subject'],
             req['credential_version'], None, None, 'completed', req['reason'], now, now,
             details))
    _audit(db, req['id'], 'erasure_completed', {
        'old_source_id': old_sid, 'new_source_id': new_sid,
        'old_sha256': old_hash, 'new_sha256': new_hash,
        'erased_event_ids': erase_event_ids, 'erased_line_nos': erase_line_nos,
        'credential_version': req['credential_version'], 'reason': req['reason']})
    return {'request_id': req['id'], 'status': 'completed',
            'old_source_id': old_sid, 'new_source_id': new_sid,
            'old_sha256': old_hash, 'new_sha256': new_hash,
            'erased_event_ids': erase_event_ids, 'erased_line_nos': erase_line_nos}


def _tombstoned_resolution(db, request_id, subject, source_sha256, now):
    tomb = db.execute('SELECT * FROM source_hash_tombstone WHERE sha256=?',
                      (source_sha256,)).fetchone()
    if tomb is None:
        return None
    prior = db.execute('SELECT * FROM retention_request WHERE id=?',
                       (tomb['request_id'],)).fetchone()
    if prior is not None and prior['subject'] != subject:
        raise RetentionError('source_already_rebased_for_other_subject')
    if request_id == tomb['request_id']:
        return {'request_id': request_id, 'status': 'completed',
                'idempotent_rerun': True}
    # Repeated request for the same subject on an already-rebuilt source:
    # record the duplicate as completed against the existing tombstone.
    db.execute(
        'INSERT INTO retention_request(id,contract,request_type,source_id,'
        'source_sha256,subject,credential_version,credential_sha256,hold_until,'
        'status,reason,created_at,updated_at,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (request_id, prior['contract'] if prior else 'subject_erasure_with_rebase',
         'erasure', prior['source_id'] if prior else -1, source_sha256, subject,
         'duplicate-request', None, None, 'completed',
         'duplicate request against already erased source', now, now,
         json.dumps({'superseded_by': tomb['request_id'],
                     'replaced_by_sha256': tomb['replaced_by_sha256']},
                    ensure_ascii=False, sort_keys=True)))
    _audit(db, request_id, 'erasure_already_done',
           {'superseded_by': tomb['request_id'], 'source_sha256': source_sha256})
    return {'request_id': request_id, 'status': 'completed',
            'idempotent_rerun': True, 'superseded_by': tomb['request_id']}


def submit_shared_hold(db_path, *, request_id, source_sha256, hold_subject,
                       credential_path, credential_sha256, reason, now=None):
    now = now or _now()
    with open(credential_path, 'rb') as fh:
        credential = fh.read()
    actual = hash_bytes(credential)
    if actual != credential_sha256:
        raise RetentionError('hold_credential_hash_mismatch')
    hold_until = _parse_hold_until(credential.decode('utf-8'))
    if datetime.fromisoformat(hold_until).date() < datetime.fromisoformat(now).date():
        raise RetentionError('hold_credential_expired')
    db = connect(db_path)
    try:
        begin_immediate(db)
        source = _load_source(db, source_sha256)
        dup = _existing_request(db, request_id, {
            'contract': 'shared_source_hold', 'request_type': 'hold',
            'source_sha256': source_sha256, 'subject': hold_subject})
        if dup is not None:
            db.commit()
            return {'request_id': request_id, 'status': dup['status'],
                    'idempotent_rerun': True}
        owned = db.execute(
            "SELECT 1 FROM event WHERE source_id=? AND subject=? LIMIT 1",
            (source['id'], hold_subject)).fetchone()
        if owned is None:
            raise RetentionError('hold_subject_has_no_event_in_source')
        db.execute(
            'INSERT INTO retention_request(id,contract,request_type,source_id,'
            'source_sha256,subject,credential_version,credential_sha256,hold_until,'
            'status,reason,created_at,updated_at,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (request_id, 'shared_source_hold', 'hold', source['id'], source_sha256,
             hold_subject, 'sha256:' + actual[:12], actual, hold_until, 'active',
             reason, now, now, json.dumps({'hold_until': hold_until}, ensure_ascii=False)))
        _audit(db, request_id, 'hold_applied', {
            'source_id': source['id'], 'source_sha256': source_sha256,
            'hold_subject': hold_subject, 'credential_sha256': actual,
            'hold_until': hold_until, 'reason': reason})
        db.commit()
        return {'request_id': request_id, 'status': 'active',
                'source_id': source['id'], 'hold_until': hold_until}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def request_erasure(db_path, *, request_id, source_sha256, subject,
                    contract, credential_version, reason, now=None):
    if contract not in CONTRACTS:
        raise RetentionError('unknown_contract')
    now = now or _now()
    db = connect(db_path)
    try:
        begin_immediate(db)
        dup = _existing_request(db, request_id, {
            'contract': contract, 'request_type': 'erasure',
            'source_sha256': source_sha256, 'subject': subject})
        if dup is not None:
            db.commit()
            return {'request_id': request_id, 'status': dup['status'],
                    'idempotent_rerun': True}
        resolution = _tombstoned_resolution(db, request_id, subject,
                                            source_sha256, now)
        if resolution is not None:
            db.commit()
            return resolution
        source = _load_source(db, source_sha256)
        # Full attestation runs before any state change, in both contracts.
        classified = _classify_lines(db, source, subject)
        hold = _active_hold(db, source['id'], now)
        req = {'id': request_id, 'contract': contract, 'subject': subject,
               'credential_version': credential_version, 'reason': reason}
        if hold is not None:
            # Whole-source hold wins the serial decision: the mixed blob stays
            # byte-identical and this request waits until the hold is released.
            db.execute(
                'INSERT INTO retention_request(id,contract,request_type,source_id,'
                'source_sha256,subject,credential_version,credential_sha256,hold_until,'
                'status,reason,created_at,updated_at,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (request_id, contract, 'erasure', source['id'], source_sha256,
                 subject, credential_version, None, None, 'pending', reason, now, now,
                 json.dumps({'blocked_by_hold': hold['id'],
                             'erased_line_nos': [ev['line_no'] for ev in
                                                 classified['erase_events']],
                             'erased_event_ids': [ev['id'] for ev in
                                                  classified['erase_events']]},
                            ensure_ascii=False, sort_keys=True)))
            _audit(db, request_id, 'erasure_deferred_for_shared_hold', {
                'source_id': source['id'], 'source_sha256': source_sha256,
                'subject': subject, 'blocked_by_hold': hold['id'],
                'credential_version': credential_version, 'reason': reason})
            db.commit()
            return {'request_id': request_id, 'status': 'pending',
                    'blocked_by_hold': hold['id'], 'source_id': source['id']}
        result = _do_rebase(db, req, source, classified, now)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def release_hold(db_path, *, request_id, reason, now=None):
    now = now or _now()
    db = connect(db_path)
    try:
        begin_immediate(db)
        hold = db.execute('SELECT * FROM retention_request WHERE id=?',
                          (request_id,)).fetchone()
        if hold is None:
            raise RetentionError('unknown_hold_request')
        if hold['request_type'] != 'hold':
            raise RetentionError('not_a_hold_request')
        if hold['status'] == 'released':
            db.commit()
            return {'request_id': request_id, 'status': 'released',
                    'idempotent_rerun': True, 'processed': []}
        db.execute('UPDATE retention_request SET status=?,updated_at=?,details=? '
                   'WHERE id=?',
                   ('released', now, json.dumps({'release_reason': reason},
                                                ensure_ascii=False), request_id))
        _audit(db, request_id, 'hold_released', {'reason': reason})
        pending = db.execute(
            "SELECT * FROM retention_request WHERE request_type='erasure' "
            "AND status='pending' AND source_id=? ORDER BY created_at,id",
            (hold['source_id'],)).fetchall()
        processed = []
        for p in pending:
            still_present = db.execute(
                'SELECT 1 FROM source WHERE sha256=?', (p['source_sha256'],)).fetchone()
            if still_present is None:
                # An earlier pending erasure in this same release already
                # rebased the shared source: this request's subject is now
                # either retained in the rebuilt blob or already gone.
                tomb = db.execute('SELECT * FROM source_hash_tombstone WHERE sha256=?',
                                  (p['source_sha256'],)).fetchone()
                if tomb is None:
                    raise RetentionError('pending_source_vanished_without_tombstone')
                leftover = db.execute(
                    'SELECT 1 FROM event WHERE subject=? LIMIT 1',
                    (p['subject'],)).fetchone()
                if leftover:
                    raise RetentionError('pending_subject_survives_rebase')
                db.execute(
                    'UPDATE retention_request SET status=?,updated_at=?,details=? WHERE id=?',
                    ('completed', now, json.dumps({
                        'satisfied_by': tomb['request_id'],
                        'replaced_by_sha256': tomb['replaced_by_sha256']},
                        ensure_ascii=False), p['id']))
                _audit(db, p['id'], 'erasure_satisfied_by_prior_rebase', {
                    'satisfied_by': tomb['request_id'],
                    'source_sha256': p['source_sha256']})
                processed.append({'request_id': p['id'], 'status': 'completed',
                                  'satisfied_by': tomb['request_id']})
                continue
            source = _load_source(db, p['source_sha256'])
            classified = _classify_lines(db, source, p['subject'])
            req = {'id': p['id'], 'contract': p['contract'], 'subject': p['subject'],
                   'credential_version': p['credential_version'],
                   'reason': p['reason']}
            # The pending placeholder is updated to completed inside _do_rebase,
            # so the request identity and its audit trail stay continuous.
            result = _do_rebase(db, req, source, classified, now)
            processed.append(result)
        db.commit()
        return {'request_id': request_id, 'status': 'released',
                'processed': processed}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_requests(db_path):
    db = connect(db_path)
    try:
        return [dict(r) for r in db.execute(
            'SELECT * FROM retention_request ORDER BY created_at,id')]
    finally:
        db.close()


def request_status(db_path, request_id):
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM retention_request WHERE id=?',
                         (request_id,)).fetchone()
        if row is None:
            raise RetentionError('unknown_request')
        return dict(row)
    finally:
        db.close()


def lineage(db_path):
    db = connect(db_path)
    try:
        out = []
        for r in db.execute('SELECT * FROM source_lineage ORDER BY new_source_id'):
            d = dict(r)
            d['line_map'] = json.loads(d['line_map'])
            out.append(d)
        return out
    finally:
        db.close()


def tombstones(db_path):
    db = connect(db_path)
    try:
        return [dict(r) for r in db.execute(
            'SELECT * FROM source_hash_tombstone ORDER BY at')]
    finally:
        db.close()


def audits(db_path):
    db = connect(db_path)
    try:
        return [dict(r) for r in db.execute(
            'SELECT * FROM retention_audit ORDER BY id')]
    finally:
        db.close()
