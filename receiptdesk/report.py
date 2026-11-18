"""Internal evidence reports and external channel summaries.

``export`` is the internal auditor path and intentionally keeps the complete
evidence set (including addresses, channels, payloads and source paths).

The ``publish_disclosure`` path is the external-partner path.  It never reads
or re-decides receipts/revokes, delivery decisions or message attempts: it
only turns the frozen roster channel for an authorised cohort into a count.
Its files and its ledger therefore contain no per-person channel and no raw
event data.
"""

import hashlib
import itertools
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .store import connect, connect_immediate

POLICIES = ('deny_unsafe', 'minimum_safe_coarsening')
_DEFAULT_OUT_DIRNAME = 'disclosures'
_RELEASE_FILE_RE = re.compile(r'-[0-9a-f]{64}\.json$')


# ---------------------------------------------------------------------------
# Internal evidence report (unchanged behaviour)
# ---------------------------------------------------------------------------

def export(db_path, out):
    db = connect(db_path)
    data = {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('batch', 'subject', 'event', 'issue', 'decision', 'evidence', 'run')}
    data['source'] = [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash'] = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = target.with_suffix('.md')
    md.write_text('# 决策报告\n\n' + '\n'.join(
        f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']) + '\n',
        encoding='utf-8')
    db.close()
    return target


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _utc_now(now=None):
    if now is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        dt = now
    else:
        dt = datetime.fromisoformat(str(now).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _safe_component(value):
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value)).strip('._')
    return cleaned or 'item'


def _load_request(request):
    if isinstance(request, (str, os.PathLike)):
        return json.loads(Path(request).read_text(encoding='utf-8'))
    return request


def _json_list(raw):
    value = json.loads(raw)
    return list(value)


def _metric_channel(metric):
    suffix = '_recipient_count'
    if not isinstance(metric, str) or not metric.endswith(suffix) or len(metric) == len(suffix):
        raise ValueError('unsupported_metric')
    return metric[:-len(suffix)]


# ---------------------------------------------------------------------------
# Inference safety
#
# Every fact visible to an audience is an exact equation
#     sum(x[p] for p in cohort) == count
# where x[p] in {0, 1} says whether p uses the metric channel.  A release is
# unsafe if the visible equations (including the proposed one) force any one
# x[p] to a single value.  Checking every fact together (not just the newest
# pair) handles overlapping groups and all-zero / all-one cohorts.  Repeated
# facts, single-person cohorts and incompatible facts are covered too.
# ---------------------------------------------------------------------------

def _normalise_fact(fact):
    if isinstance(fact, dict):
        members = fact.get('cohort', fact.get('recipients', fact.get('members', [])))
        count = fact.get('count')
    else:
        members, count = fact
    return tuple(sorted({str(member) for member in members})), int(count)


def _domains_for(facts):
    domains = {}
    for members, _ in facts:
        for member in members:
            domains.setdefault(member, {0, 1})
    return domains


def _propagate(facts, domains):
    domains = {key: set(value) for key, value in domains.items()}
    while True:
        changed = False
        for members, total in facts:
            ones = sum(1 for member in members if domains[member] == {1})
            possible = sum(1 for member in members if 1 in domains[member])
            if ones > total or possible < total:
                return None
            if ones == total:
                for member in members:
                    if len(domains[member]) > 1:
                        domains[member] = {0}
                        changed = True
            if possible == total:
                for member in members:
                    if len(domains[member]) > 1:
                        domains[member] = {1}
                        changed = True
        if not changed:
            return domains


def _feasible_domains(facts, domains):
    domains = _propagate(facts, domains)
    if domains is None:
        return False
    unassigned = [member for member, values in domains.items() if len(values) > 1]
    if not unassigned:
        return True
    # Branch on the most constrained variable; the propagator prunes the rest.
    member = max(unassigned, key=lambda key: sum(1 for group, _ in facts if key in group))
    for value in (0, 1):
        branch = {key: set(values) for key, values in domains.items()}
        branch[member] = {value}
        if _feasible_domains(facts, branch):
            return True
    return False


def _feasible(facts, fixed=None):
    domains = _domains_for(facts)
    for member, value in (fixed or {}).items():
        domains[member] = {int(value)}
    return _feasible_domains(facts, domains)


def forced_individual_channels(facts, universe=None):
    """Return ``{person: 0|1}`` for every channel the facts pin down.

    ``facts`` entries are ``(members, count)`` pairs or mappings with a
    cohort/recipients list and a count.  ``None`` as a value marks an
    inconsistent fact set (which must also block publication).
    """
    normalised = [_normalise_fact(fact) for fact in facts]
    candidates = set(universe or ())
    for members, _ in normalised:
        candidates.update(members)
    if not candidates:
        return {}
    if not _feasible(normalised):
        # Inconsistent visible facts cannot be used to prove safety.
        return {member: None for member in sorted(candidates)}
    forced = {}
    for member in sorted(candidates):
        can_zero = _feasible(normalised, {member: 0})
        can_one = _feasible(normalised, {member: 1})
        if can_zero != can_one:
            forced[member] = 0 if can_zero else 1
    return forced


def is_disclosure_safe(released_facts, cohort, count, universe=None):
    facts = list(released_facts) + [(tuple(cohort), int(count))]
    return not forced_individual_channels(facts, universe)


# ---------------------------------------------------------------------------
# Frozen roster and ledger helpers
# ---------------------------------------------------------------------------

def _frozen_roster(db):
    row = db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
    if row is None:
        raise ValueError('config_not_frozen')
    config = json.loads(row['value'])
    items = config.get('boxes', config.get('stations', config.get('recipients', [])))
    roster = {}
    for item in items:
        subject = item.get('id', item.get('box'))
        if subject:
            roster[str(subject)] = item
    return roster


def _batch_version(db):
    row = db.execute('SELECT id, config_hash FROM batch ORDER BY id LIMIT 1').fetchone()
    meta = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
    if row is None or meta is None:
        raise ValueError('batch_not_frozen')
    return row['id'], meta['value']


def _actual_count(roster, channel, cohort):
    return sum(1 for member in cohort if roster[member].get('channel') == channel)


def _validate_cohort(cohort, roster, universe, label):
    if not isinstance(cohort, (list, tuple)) or not cohort:
        raise ValueError(f'{label}_empty')
    members = [str(member) for member in cohort]
    if len(members) != len(set(members)):
        raise ValueError(f'{label}_duplicate')
    for member in members:
        if member not in roster:
            raise ValueError(f'{label}_unknown_recipient')
        if member not in universe:
            raise ValueError(f'{label}_outside_universe')
    return members


def _visible_facts(db, audience, metric):
    rows = db.execute(
        "SELECT cohort, recipient_count FROM disclosure_release "
        "WHERE audience=? AND metric=? AND status='published' ORDER BY rowid",
        (audience, metric)).fetchall()
    return [(set(json.loads(row['cohort'])), row['recipient_count']) for row in rows]


def _external_record(row):
    # Strict external contract: actual cohort, the metric-channel count, the
    # batch/config version and the release ID.  No metric/audience/policy field
    # (that stays in the internal ledger), and never any personal or raw data.
    return {
        'release_id': row['release_id'],
        'cohort': _json_list(row['cohort']) if isinstance(row['cohort'], str) else list(row['cohort']),
        'count': row['recipient_count'],
        'batch_id': row['batch_id'],
        'config_hash': row['config_hash'],
    }


def _render_external(row):
    record = _external_record(row)
    return (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _release_path(out_dir, audience, release_id, digest):
    folder = Path(out_dir) / _safe_component(audience)
    return folder / f"{_safe_component(release_id)}-{digest}.json"


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _row_dict(row):
    return dict(row)


def _stored_path(row, out_dir):
    if row['file_path']:
        return Path(row['file_path'])
    digest = row['file_sha256'] or _sha256(_render_external(row))
    return _release_path(out_dir, row['audience'], row['release_id'], digest)


def _ensure_release_file(row, out_dir):
    """Make the file and its recorded digest agree, repairing either side.

    Returns ``(path, digest, data, rewritten)``.
    """
    row = _row_dict(row)
    data = _render_external(row)
    digest = _sha256(data)
    path = _stored_path(row, out_dir)
    current = None
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        pass
    rewritten = current != data
    if rewritten:
        _atomic_write(path, data)
    if _sha256(path.read_bytes()) != digest:
        raise RuntimeError('disclosure_file_integrity')
    return path, digest, data, rewritten


def _recover_locked(db, out_dir):
    """Repair file/ledger divergence under an already-held write lock.

    * a published or pending ledger row always ends up with the exact file
      described by its recorded fields;
    * generated files that have no ledger row are removed, so an interrupted
      publish cannot leave an externally visible file without a record.
    """
    out_dir = Path(out_dir)
    changed = 0
    known = set()
    now = _utc_now()
    for row in db.execute('SELECT * FROM disclosure_release ORDER BY audience, release_id').fetchall():
        row = _row_dict(row)
        previous_path = Path(row['file_path']).resolve() if row['file_path'] else None
        path, digest, _, rewritten = _ensure_release_file(row, out_dir)
        resolved = str(path.resolve())
        known.add(resolved)
        values = {
            'file_path': resolved,
            'file_sha256': digest,
            'status': 'published' if row['status'] in ('pending', 'published') else row['status'],
            'updated_at': now,
        }
        if (rewritten or
                previous_path != Path(values['file_path']) or
                row['file_sha256'] != values['file_sha256'] or
                row['status'] != values['status']):
            db.execute(
                'UPDATE disclosure_release SET file_path=?, file_sha256=?, status=?, updated_at=? '
                'WHERE audience=? AND release_id=?',
                (values['file_path'], values['file_sha256'], values['status'],
                 values['updated_at'], row['audience'], row['release_id']))
            changed += 1
    removed = 0
    if out_dir.exists():
        for path in out_dir.rglob('*'):
            if not path.is_file():
                continue
            if path.name.startswith('.tmp-'):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            elif _RELEASE_FILE_RE.search(path.name) and str(path.resolve()) not in known:
                path.unlink()
                removed += 1
    return {'repaired': changed, 'removed_orphans': removed}


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _seed_previous_release(db, request, audience, roster, channel, universe, out_dir, now,
                           metric=None, request_id=None):
    previous = request.get('previous_release')
    if not previous:
        return None
    release_id = str(previous['id'])
    previous_request_id = str(previous.get('request_id', release_id))
    if request_id is not None and previous_request_id == request_id:
        raise ValueError('previous_release_id_conflict')
    cohort = _validate_cohort(previous.get('recipients', previous.get('cohort', [])),
                              roster, universe, 'previous_release')
    actual = _actual_count(roster, channel, cohort)
    if 'count' in previous and int(previous['count']) != actual:
        raise ValueError('previous_release_count_mismatch')
    existing = db.execute(
        'SELECT * FROM disclosure_release WHERE audience=? AND release_id=?',
        (audience, release_id)).fetchone()
    if existing:
        if (set(_json_list(existing['cohort'])) != set(cohort) or
                existing['recipient_count'] != actual or
                (metric is not None and existing['metric'] != metric)):
            raise ValueError('release_conflict')
        _ensure_release_file(existing, out_dir)
        return release_id
    # The request carries the prior publication as an already-visible fact.
    # Refuse to import a "history" that is itself inconsistent or pins somebody;
    # otherwise unverified request data would silently enter the safety proof.
    history = _visible_facts(db, audience, metric) if metric is not None else []
    forced = forced_individual_channels(history + [(set(cohort), actual)], universe)
    if forced:
        raise ValueError('previous_release_unsafe')
    batch_id, config_hash = _batch_version(db)
    policy = str(previous.get('policy', 'historical'))
    row = {
        'audience': audience,
        'release_id': release_id,
        'request_id': previous_request_id,
        'policy': policy,
        'status': 'pending',
        'requested_cohort': json.dumps(cohort, ensure_ascii=False, separators=(',', ':')),
        'cohort': json.dumps(cohort, ensure_ascii=False, separators=(',', ':')),
        'recipient_count': actual,
        'metric': request.get('metric', ''),
        'batch_id': batch_id,
        'config_hash': config_hash,
        'file_path': None,
        'file_sha256': None,
        'origin': 'imported',
        'created_at': now,
        'updated_at': now,
    }
    data = _render_external(row)
    digest = _sha256(data)
    path = _release_path(out_dir, audience, release_id, digest)
    row['file_path'] = str(path.resolve())
    row['file_sha256'] = digest
    db.execute(
        'INSERT INTO disclosure_release(audience,release_id,request_id,policy,status,'
        'requested_cohort,cohort,recipient_count,metric,batch_id,config_hash,file_path,'
        'file_sha256,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (audience, release_id, previous_request_id, policy, 'pending',
         row['requested_cohort'], row['cohort'], actual, row['metric'], batch_id,
         config_hash, row['file_path'], digest, 'imported', now, now))
    _atomic_write(path, data)
    if _sha256(path.read_bytes()) != digest:
        raise RuntimeError('disclosure_file_integrity')
    db.execute(
        "UPDATE disclosure_release SET status='published', updated_at=? "
        "WHERE audience=? AND release_id=?", (now, audience, release_id))
    return release_id


def _publish_release(db, audience, request_id, policy, requested, cohort, count, metric,
                     batch_id, config_hash, out_dir, now):
    release_id = f'REL-{request_id}'
    conflict = db.execute(
        'SELECT request_id FROM disclosure_release WHERE audience=? AND release_id=?',
        (audience, release_id)).fetchone()
    if conflict and conflict['request_id'] != request_id:
        raise ValueError('release_id_conflict')
    row = {
        'release_id': release_id,
        'cohort': json.dumps(cohort, ensure_ascii=False, separators=(',', ':')),
        'recipient_count': count,
        'metric': metric,
        'batch_id': batch_id,
        'config_hash': config_hash,
    }
    data = _render_external(row)
    digest = _sha256(data)
    path = _release_path(out_dir, audience, release_id, digest)
    db.execute(
        'INSERT INTO disclosure_release(audience,release_id,request_id,policy,status,'
        'requested_cohort,cohort,recipient_count,metric,batch_id,config_hash,file_path,'
        'file_sha256,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (audience, release_id, request_id, policy, 'pending',
         json.dumps(requested, ensure_ascii=False, separators=(',', ':')),
         row['cohort'], count, metric, batch_id, config_hash, str(path.resolve()),
         digest, 'published', now, now))
    _atomic_write(path, data)
    if _sha256(path.read_bytes()) != digest:
        raise RuntimeError('disclosure_file_integrity')
    db.execute(
        "UPDATE disclosure_release SET status='published', updated_at=? "
        "WHERE audience=? AND release_id=?", (now, audience, release_id))
    return release_id, path, digest


def _record_attempt(db, audience, request_id, policy, status, requested, cohort=None,
                    count=None, release_id=None, reason=None, now=None, identifiable=None):
    db.execute(
        'INSERT INTO disclosure_attempt(audience,request_id,policy,status,requested_cohort,'
        'cohort,recipient_count,release_id,reason,identifiable,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (audience, request_id, policy, status,
         json.dumps(requested, ensure_ascii=False, separators=(',', ':')),
         json.dumps(cohort, ensure_ascii=False, separators=(',', ':')) if cohort is not None else None,
         count, release_id, reason,
         json.dumps(identifiable or {}, ensure_ascii=False, separators=(',', ':'), sort_keys=True),
         now or _utc_now()))


def _result(row, replayed=False):
    path = row['file_path']
    data = {
        'status': 'replayed' if replayed else 'published',
        'replayed': replayed,
        'audience': row['audience'],
        'request_id': row['request_id'],
        'policy': row['policy'],
        'release_id': row['release_id'],
        'requested_cohort': _json_list(row['requested_cohort']),
        'cohort': _json_list(row['cohort']),
        'count': row['recipient_count'],
        'metric': row['metric'],
        'batch_id': row['batch_id'],
        'config_hash': row['config_hash'],
        'file': path,
        'file_sha256': row['file_sha256'],
    }
    return data


def _denied(audience, request_id, policy, requested, reason):
    # Kept separate from _result so no count/inference field can leak into a
    # refusal response by accident.
    return {
        'status': 'denied',
        'replayed': False,
        'audience': audience,
        'request_id': request_id,
        'policy': policy,
        'requested_cohort': list(requested),
        'reason': reason,
        'file': None,
        'file_sha256': None,
    }


def _publish_locked(db, request, policy, out_dir, now):
    audience = str(request['audience'])
    new_request = request['new_request']
    request_id = str(new_request['id'])
    metric = request.get('metric', 'email-v2_recipient_count')
    channel = _metric_channel(metric)
    roster = _frozen_roster(db)
    batch_id, config_hash = _batch_version(db)
    universe = {str(member) for member in request.get('universe', sorted(roster))}

    requested = _validate_cohort(new_request.get('recipients', new_request.get('cohort', [])),
                                 roster, universe, 'request')
    expansion_pool = [str(member) for member in request.get('allowed_expansion', [])]
    for member in expansion_pool:
        if member not in roster:
            raise ValueError('allowed_expansion_unknown_recipient')
        if member not in universe:
            raise ValueError('allowed_expansion_outside_universe')

    # Repair any interrupted earlier publication before trusting the ledger.
    _recover_locked(db, out_dir)

    existing = db.execute(
        'SELECT * FROM disclosure_release WHERE audience=? AND request_id=?',
        (audience, request_id)).fetchone()
    if existing:
        if json.loads(existing['requested_cohort']) != requested:
            raise ValueError('request_id_conflict')
        row = _row_dict(existing)
        _ensure_release_file(row, out_dir)
        return _result(_row_dict(db.execute(
            'SELECT * FROM disclosure_release WHERE audience=? AND release_id=?',
            (audience, row['release_id'])).fetchone()), replayed=True)

    prior = db.execute(
        'SELECT * FROM disclosure_attempt WHERE audience=? AND request_id=? AND policy=?',
        (audience, request_id, policy)).fetchone()
    if prior:
        if json.loads(prior['requested_cohort']) != requested:
            raise ValueError('request_id_conflict')
        if prior['status'] == 'published' and prior['release_id']:
            row = db.execute(
                'SELECT * FROM disclosure_release WHERE audience=? AND release_id=?',
                (audience, prior['release_id'])).fetchone()
            if row:
                _ensure_release_file(row, out_dir)
                return _result(_row_dict(row), replayed=True)
        if prior['status'] == 'denied':
            return _denied(audience, request_id, policy,
                           json.loads(prior['requested_cohort']),
                           prior['reason'] or 'denied')

    _seed_previous_release(db, request, audience, roster, channel, universe, out_dir, now,
                           metric=metric, request_id=request_id)
    history = _visible_facts(db, audience, metric)

    def assess(cohort):
        count = _actual_count(roster, channel, cohort)
        forced = forced_individual_channels(history + [(set(cohort), count)], universe)
        return count, forced

    chosen = None
    forced = {}
    if policy == 'deny_unsafe':
        count, forced = assess(requested)
        if not forced:
            chosen = requested
    elif policy == 'minimum_safe_coarsening':
        count, forced = assess(requested)
        if not forced:
            chosen = requested
        else:
            pool = [member for member in dict.fromkeys(expansion_pool) if member not in requested]
            for size in range(1, len(pool) + 1):
                for extra in itertools.combinations(pool, size):
                    candidate = requested + list(extra)
                    candidate_count, candidate_forced = assess(candidate)
                    if not candidate_forced:
                        chosen = candidate
                        count = candidate_count
                        forced = {}
                        break
                if chosen:
                    break
    else:  # pragma: no cover - validated earlier
        raise ValueError('unknown_policy')

    if chosen is None:
        # A refusal must not itself disclose: the response carries no actual
        # count and no per-person inference.  The internal attempt row keeps
        # that audit detail for the auditor, not for the audience.
        reason = ('individual_channel_inferable' if policy == 'deny_unsafe'
                  else 'no_safe_coarsening')
        identifiable = {member: value for member, value in forced.items() if value is not None}
        _record_attempt(db, audience, request_id, policy, 'denied', requested,
                        cohort=requested, count=_actual_count(roster, channel, requested),
                        reason=reason, now=now, identifiable=identifiable)
        return _denied(audience, request_id, policy, requested, reason)

    release_id, path, digest = _publish_release(
        db, audience, request_id, policy, requested, chosen, count, metric,
        batch_id, config_hash, out_dir, now)
    _record_attempt(db, audience, request_id, policy, 'published', requested,
                    cohort=chosen, count=count, release_id=release_id, now=now)
    row = db.execute(
        'SELECT * FROM disclosure_release WHERE audience=? AND release_id=?',
        (audience, release_id)).fetchone()
    return _result(_row_dict(row))


def publish_disclosure(db_path, request, policy=None, out_dir=None, now=None):
    """Publish an external channel summary, or record why it is refused.

    ``request`` is the JSON request document (or a path to it).  ``out_dir``
    defaults to ``<db dir>/disclosures``.  Publishing uses an immediate SQLite
    transaction so all requests for one audience are assessed serially against
    the audience's complete publication history.
    """
    request = _load_request(request)
    policies = request.get('policies') or list(POLICIES)
    if policy is None:
        policy = policies[0] if policies else 'deny_unsafe'
    if policy not in POLICIES:
        raise ValueError('unknown_policy')
    if policy not in policies:
        raise ValueError('policy_not_permitted')

    db_path = Path(db_path)
    out_dir = Path(out_dir) if out_dir is not None else db_path.resolve().parent / _DEFAULT_OUT_DIRNAME
    timestamp = _utc_now(now)
    db = connect_immediate(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            result = _publish_locked(db, request, policy, out_dir, timestamp)
            db.execute('COMMIT')
            return result
        except BaseException:
            try:
                db.execute('ROLLBACK')
            except Exception:
                pass
            raise
    finally:
        db.close()


# Friendly aliases for callers that describe the operation differently.
publish_request = publish_disclosure
publish = publish_disclosure


def recover_disclosures(db_path, out_dir=None):
    """Repair interrupted file/ledger publication after a crash."""
    db_path = Path(db_path)
    out_dir = Path(out_dir) if out_dir is not None else db_path.resolve().parent / _DEFAULT_OUT_DIRNAME
    db = connect_immediate(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            summary = _recover_locked(db, out_dir)
            db.execute('COMMIT')
            return summary
        except BaseException:
            try:
                db.execute('ROLLBACK')
            except Exception:
                pass
            raise
    finally:
        db.close()


def disclosure_ledger(db_path, audience=None):
    """Internal view of releases and attempts, scoped by audience on demand."""
    db = connect(db_path)
    if audience is None:
        releases = [dict(row) for row in db.execute(
            'SELECT * FROM disclosure_release ORDER BY audience, release_id')]
        attempts = [dict(row) for row in db.execute(
            'SELECT * FROM disclosure_attempt ORDER BY audience, request_id, policy')]
    else:
        releases = [dict(row) for row in db.execute(
            'SELECT * FROM disclosure_release WHERE audience=? ORDER BY release_id',
            (str(audience),))]
        attempts = [dict(row) for row in db.execute(
            'SELECT * FROM disclosure_attempt WHERE audience=? ORDER BY request_id, policy',
            (str(audience),))]
    db.close()
    for row in releases:
        row['requested_cohort'] = _json_list(row['requested_cohort'])
        row['cohort'] = _json_list(row['cohort'])
    for row in attempts:
        row['requested_cohort'] = _json_list(row['requested_cohort'])
        row['cohort'] = _json_list(row['cohort']) if row['cohort'] else None
        row['identifiable'] = json.loads(row['identifiable']) if row['identifiable'] else {}
    return {'releases': releases, 'attempts': attempts}


audience_ledger = disclosure_ledger
