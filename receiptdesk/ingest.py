import codecs
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .store import connect

# Fixed-size streaming: neither the whole source, its decoded text, nor its
# line list is ever materialised in the Python process.
DEFAULT_CHUNK_SIZE = 64 * 1024
SOURCE_CONTRACTS = ('freeze_on_open', 'verify_then_retry_latest')
MAX_SOURCE_ATTEMPTS = 8

# str.splitlines() boundaries other than '\r' (handled separately so that a
# CRLF pair can straddle two chunks).
_LINE_BREAKS = frozenset('\n\x0b\x0c\x1c\x1d\x1e\x85  ')

_PARSE_ERRORS = (ValueError, TypeError, KeyError, json.JSONDecodeError)


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(text):
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone_required')
    return dt


def freeze_config(db_path, config):
    raw = Path(config).read_bytes()
    digest = hash_bytes(raw)
    db = connect(db_path)
    old = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
    if old and old['value'] != digest:
        db.close()
        raise ValueError('frozen_config_conflict')
    parsed = json.loads(raw)
    subjects = parsed.get('boxes', parsed.get('stations', parsed.get('recipients', [])))
    ids = [item.get('box', item.get('id')) for item in subjects]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError('duplicate_or_missing_subject')
    aliases = [alias for item in subjects for alias in item.get('aliases', [])]
    if len(aliases) != len(set(aliases)) or set(aliases) & set(ids):
        raise ValueError('ambiguous_alias')
    with db:
        old = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
        if old and old['value'] != digest:
            raise ValueError('frozen_config_conflict')
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_hash',?)", (digest,))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_json',?)", (raw.decode('utf-8'),))
        db.execute("INSERT OR IGNORE INTO batch VALUES ('batch-1',?)", (digest,))
        for item in subjects:
            db.execute("INSERT OR IGNORE INTO subject VALUES (?,?)", (item.get('box', item.get('id')), 'batch-1'))
    db.close()
    return digest


def _split_lines(text_parts):
    """Incrementally apply str.splitlines() semantics across chunk parts.

    Only an unfinished trailing line (plus a possible pending '\\r') is kept in
    memory, so the buffer is bounded by the longest line rather than the file.
    """
    buf = ''
    cursor = 0
    for text in text_parts:
        if not text:
            continue
        buf += text
        # A trailing '\r' may pair with a '\n' in the next chunk, so hold it
        # back instead of treating it as a boundary yet.
        limit = len(buf) - 1 if buf.endswith('\r') else len(buf)
        start = 0
        i = cursor
        while i < limit:
            ch = buf[i]
            if ch == '\r':
                yield buf[start:i]
                if i + 1 < len(buf) and buf[i + 1] == '\n':
                    i += 2
                else:
                    i += 1
                start = i
            elif ch in _LINE_BREAKS:
                yield buf[start:i]
                i += 1
                start = i
            else:
                i += 1
        buf = buf[start:]
        cursor = max(0, limit - start)
    if buf:
        # splitlines() does not emit an extra empty line after a final
        # terminator; a held-back '\r' terminates the last line.
        yield buf[:-1] if buf.endswith('\r') else buf


def _parse_line(line):
    row = json.loads(line)
    kind = str(row['kind'])
    subject = str(row['subject'])
    ts = str(row['time'])
    normalized = timestamp(ts).astimezone(timezone.utc).isoformat()
    identity = str(row.get('identity') or f"{row['device']}:{int(row['seq'])}")
    if not kind or not subject:
        raise ValueError('missing_kind_or_subject')
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return identity, subject, kind, normalized, canonical, hash_bytes(canonical.encode())


@dataclass
class _StagedSnapshot:
    id: str
    path: str
    digest: str
    size: int
    line_count: int
    path_changed: bool
    resumed: bool = False


def _stat_identity(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _signature(stat):
    return '|'.join(str(part) for part in _stat_identity(stat))


def _delete_staging(db, snapshot_id):
    db.execute('DELETE FROM stage_chunk WHERE snapshot_id=?', (snapshot_id,))
    db.execute('DELETE FROM stage_event WHERE snapshot_id=?', (snapshot_id,))
    db.execute('DELETE FROM stage_issue WHERE snapshot_id=?', (snapshot_id,))
    db.execute('DELETE FROM stage_snapshot WHERE id=?', (snapshot_id,))


def _discard_staging(db, snapshot_id):
    with db:
        _delete_staging(db, snapshot_id)


def _find_resumable(db, file_key, signature):
    """Return a fully staged snapshot for the same path signature, if any.

    Staging only reaches state='staged' after the complete file version has
    been read and hashed in one transaction, so resuming it preserves exactly
    one snapshot identity.  Incomplete 'writing' rows are crash debris and are
    removed instead of being trusted.
    """
    rows = list(db.execute(
        'SELECT * FROM stage_snapshot WHERE file_key=? ORDER BY created_at', (file_key,)
    ))
    resumable = None
    debris = []
    for row in rows:
        if row['state'] == 'staged' and row['file_sig'] == signature and resumable is None:
            resumable = row
        else:
            debris.append(row['id'])
    if debris:
        with db:
            for snapshot_id in debris:
                _delete_staging(db, snapshot_id)
    return resumable


def _stage_attempt(db, path, chunk_size, midread_hook):
    """Read one complete version of `path` into durable, disposable staging.

    The open file handle pins the inode: an atomic rename of the logical path
    cannot change the bytes, hash or events of this attempt.  The staging
    transaction commits as a unit, so an interrupted read leaves nothing but
    an empty/incomplete attempt that the next run discards.
    """
    fh = open(path, 'rb')
    try:
        initial = os.fstat(fh.fileno())
        snapshot_id = uuid4().hex
        hasher = hashlib.sha256()
        decoder = codecs.getincrementaldecoder('utf-8-sig')()
        state = {'bytes': 0, 'seq': 0, 'lines': 0, 'events': 0, 'issues': 0}

        def text_parts():
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                state['bytes'] += len(chunk)
                hasher.update(chunk)
                db.execute(
                    'INSERT INTO stage_chunk(snapshot_id,seq,data) VALUES (?,?,?)',
                    (snapshot_id, state['seq'], chunk),
                )
                state['seq'] += 1
                if midread_hook is not None:
                    midread_hook(path, {
                        'bytes_read': state['bytes'],
                        'lines': state['lines'],
                        'chunks': state['seq'],
                    })
                # The BOM decoder only strips U+FEFF at the very start and
                # buffers split multi-byte sequences, even across chunks.
                text = decoder.decode(chunk)
                if text:
                    yield text
            tail = decoder.decode(b'', True)
            if tail:
                yield tail

        with db:
            db.execute(
                "INSERT INTO stage_snapshot(id,path,file_key,file_sig,sha256,size,line_count,state,created_at) "
                "VALUES (?,?,?,?,NULL,0,0,'writing',?)",
                (snapshot_id, os.fspath(path), os.path.abspath(os.fspath(path)),
                 _signature(initial), datetime.now(timezone.utc).isoformat()),
            )
            for line_no, line in enumerate(_split_lines(text_parts()), 1):
                state['lines'] = line_no
                if not line.strip():
                    continue
                try:
                    identity, subject, kind, normalized, canonical, sha = _parse_line(line)
                except _PARSE_ERRORS as exc:
                    db.execute(
                        'INSERT OR REPLACE INTO stage_issue(snapshot_id,line_no,error,raw_line) VALUES (?,?,?,?)',
                        (snapshot_id, line_no, str(exc), line),
                    )
                    state['issues'] += 1
                    continue
                db.execute(
                    'INSERT OR REPLACE INTO stage_event('
                    'snapshot_id,line_no,identity,subject,kind,time,payload,sha256) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (snapshot_id, line_no, identity, subject, kind, normalized, canonical, sha),
                )
                state['events'] += 1
            digest = hasher.hexdigest()
            db.execute(
                "UPDATE stage_snapshot SET sha256=?,size=?,line_count=?,state='staged' WHERE id=?",
                (digest, state['bytes'], state['lines'], snapshot_id),
            )
        open_end = os.fstat(fh.fileno())
    finally:
        fh.close()

    try:
        path_end = os.stat(path)
    except OSError:
        path_end = None
    changed = (
        path_end is None
        or _stat_identity(initial) != _stat_identity(open_end)
        or _stat_identity(open_end) != _stat_identity(path_end)
    )
    return _StagedSnapshot(snapshot_id, os.fspath(path), digest, state['bytes'], state['lines'], changed)


def _commit_staged(db, staged, contract, attempts, chunk_size):
    """Promote one staged snapshot to source/event/issue in a single transaction.

    Raw bytes are copied from the same staged chunks that produced the digest,
    and the digest is recomputed while copying; any mismatch rolls the whole
    transaction back.  A crash therefore leaves either nothing or the complete
    source plus its events — never a half source or duplicated events.
    """
    with db:
        existing = db.execute('SELECT id FROM source WHERE sha256=?', (staged.digest,)).fetchone()
        if existing:
            _delete_staging(db, staged.id)
            return {
                'imported': 0,
                'duplicate_file': True,
                'sha256': staged.digest,
                'source_id': existing['id'],
                'contract': contract,
                'attempts': attempts,
                'resumed': staged.resumed,
            }

        size = int(db.execute(
            'SELECT COALESCE(SUM(LENGTH(data)),0) AS size FROM stage_chunk WHERE snapshot_id=?',
            (staged.id,),
        ).fetchone()['size'])
        if size != staged.size:
            raise RuntimeError('staged_snapshot_size_mismatch')

        source_id = db.execute(
            'INSERT INTO source(path,sha256,raw) VALUES (?,?,zeroblob(?))',
            (staged.path, staged.digest, size),
        ).lastrowid
        hasher = hashlib.sha256()
        blob = db.blobopen('source', 'raw', source_id)
        try:
            for row in db.execute(
                'SELECT data FROM stage_chunk WHERE snapshot_id=? ORDER BY seq', (staged.id,)
            ):
                data = row['data']
                hasher.update(data)
                blob.write(data)
        finally:
            blob.close()
        if hasher.hexdigest() != staged.digest:
            raise RuntimeError('staged_snapshot_corrupt')

        imported = 0
        for row in db.execute(
            'SELECT * FROM stage_event WHERE snapshot_id=? ORDER BY line_no', (staged.id,)
        ):
            old = db.execute('SELECT sha256 FROM event WHERE identity=?', (row['identity'],)).fetchone()
            if old:
                if old['sha256'] != row['sha256']:
                    db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',
                               ('identity_conflict', row['identity']))
                continue
            db.execute(
                'INSERT INTO event(source_id,line_no,identity,subject,kind,time,payload,sha256) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (source_id, row['line_no'], row['identity'], row['subject'], row['kind'],
                 row['time'], row['payload'], row['sha256']),
            )
            imported += 1

        parse_errors = 0
        for row in db.execute(
            'SELECT * FROM stage_issue WHERE snapshot_id=? ORDER BY line_no', (staged.id,)
        ):
            detail = json.dumps({
                'source_id': source_id,
                'line_no': row['line_no'],
                'error': row['error'],
                'raw_line': row['raw_line'],
            }, ensure_ascii=False)
            db.execute('INSERT INTO issue(code,detail) VALUES (?,?)', ('parse_error', detail))
            parse_errors += 1

        db.execute(
            'INSERT INTO import_audit('
            'source_id,snapshot_id,contract,attempts,resumed,path_changed,chunk_size,'
            'line_count,imported,parse_errors,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (source_id, staged.id, contract, attempts, int(staged.resumed),
             int(staged.path_changed), chunk_size, staged.line_count, imported, parse_errors,
             datetime.now(timezone.utc).isoformat()),
        )
        _delete_staging(db, staged.id)

    return {
        'imported': imported,
        'duplicate_file': False,
        'sha256': staged.digest,
        'source_id': source_id,
        'contract': contract,
        'attempts': attempts,
        'resumed': staged.resumed,
        'path_changed_during_read': staged.path_changed,
        'line_count': staged.line_count,
        'parse_errors': parse_errors,
        'snapshot_id': staged.id,
    }


def import_file(db_path, path, contract='freeze_on_open', chunk_size=DEFAULT_CHUNK_SIZE,
                midread_hook=None):
    """Import one logical inbound JSONL path with bounded memory.

    contract='freeze_on_open' pins the bytes of the file opened at the start of
    the import even if the path is atomically replaced mid-read.
    contract='verify_then_retry_latest' discards an attempt whose path changed
    during the read and retries the now-stable version, so the committed
    source.raw, digest and events always belong to a single complete version.

    `midread_hook(path, progress)` is called after each chunk is read (before
    that chunk is parsed); it is the deterministic injection point tests use to
    atomically replace the inbound path mid-read.
    """
    if contract not in SOURCE_CONTRACTS:
        raise ValueError('unknown_source_contract')
    if int(chunk_size) <= 0:
        raise ValueError('invalid_chunk_size')
    chunk_size = int(chunk_size)
    path_str = os.fspath(path)
    file_key = os.path.abspath(path_str)
    db = connect(db_path)
    try:
        attempts = 0
        while True:
            attempts += 1
            try:
                current = os.stat(path_str)
            except OSError:
                current = None
            if current is not None:
                resumable = _find_resumable(db, file_key, _signature(current))
                if resumable is not None:
                    # A resumed snapshot was fully read before; its source
                    # identity is settled, so the retry contract need not
                    # re-check the path for changes.
                    staged = _StagedSnapshot(
                        resumable['id'], resumable['path'], resumable['sha256'],
                        resumable['size'], resumable['line_count'], False, resumed=True,
                    )
                    break
            staged = _stage_attempt(db, path_str, chunk_size, midread_hook)
            if contract == 'freeze_on_open' or not staged.path_changed:
                break
            # The path was replaced while this snapshot was being read.  Drop
            # every staged byte/event of the superseded version and read the
            # now-current version as a fresh, internally consistent attempt.
            _discard_staging(db, staged.id)
            if attempts >= MAX_SOURCE_ATTEMPTS:
                raise RuntimeError('source_unstable')
        return _commit_staged(db, staged, contract, attempts, chunk_size)
    finally:
        db.close()
