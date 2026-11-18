import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

from receiptdesk.__main__ import audit
from receiptdesk.ingest import DEFAULT_CHUNK_SIZE, freeze_config, import_file
from receiptdesk.store import connect

BASE = Path(__file__).parents[1] / 'fixtures'
CASE = json.loads((BASE / 'snapshot_mutation.json').read_text(encoding='utf-8'))
ORIGINAL = (BASE / CASE['original_file']).read_bytes()
REVISED = (BASE / CASE['revised_file']).read_bytes()
ORIGINAL_SHA = CASE['original_sha256']
REVISED_SHA = CASE['revised_sha256']


def _prepare(tmp_path, name='batch.db'):
    db = tmp_path / name
    freeze_config(db, BASE / 'config.json')
    return db


def _expected_payloads(raw):
    """Canonical payload per physical line, mirroring the ingest contract."""
    expected = {}
    for line_no, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        expected[line_no] = json.dumps(
            row, sort_keys=True, ensure_ascii=False, separators=(',', ':')
        )
    return expected


def _messages(con):
    return {
        row['identity']: json.loads(row['payload']).get('message_id')
        for row in con.execute('SELECT identity,payload FROM event')
    }


def _staging_counts(con):
    return tuple(
        con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table in ('stage_snapshot', 'stage_chunk', 'stage_event', 'stage_issue')
    )


def _write_copy(tmp_path, name, raw):
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _atomic_replace_hook(path, replacement, at_chunks=20):
    """Deterministically atomically replace the inbound path mid-read."""
    state = {'replaced': False, 'chunk': None, 'bytes': None, 'chunks_seen': 0}

    def hook(_path, progress):
        state['chunks_seen'] = progress['chunks']
        if not state['replaced'] and progress['chunks'] >= at_chunks:
            os.replace(replacement, path)
            state['replaced'] = True
            state['chunk'] = progress['chunks']
            state['bytes'] = progress['bytes_read']

    hook.state = state
    return hook


def _run_contract_case(tmp_path, contract, chunk_size=8192):
    db = _prepare(tmp_path, f'{contract}.db')
    incoming = tmp_path / 'incoming'
    incoming.mkdir(exist_ok=True)
    path = incoming / 'receipt_batch.jsonl'
    path.write_bytes(ORIGINAL)
    replacement = _write_copy(tmp_path, f'{contract}-replacement.jsonl', REVISED)
    hook = _atomic_replace_hook(path, replacement, at_chunks=20)
    result = import_file(db, path, contract=contract, chunk_size=chunk_size,
                         midread_hook=hook)
    return db, result, hook.state


@pytest.mark.parametrize(
    'contract,chosen_raw,chosen_sha,message_id,attempts,path_changed,other_message_id',
    [
        ('freeze_on_open', ORIGINAL, ORIGINAL_SHA, 'Q-1024', 1, True, 'Q-1024-REVISED'),
        ('verify_then_retry_latest', REVISED, REVISED_SHA, 'Q-1024-REVISED', 2, False, 'Q-1024'),
    ],
)
def test_source_snapshot_contracts_are_complete_and_never_mixed(
    tmp_path, contract, chosen_raw, chosen_sha, message_id, attempts, path_changed,
    other_message_id,
):
    db, result, hook_state = _run_contract_case(tmp_path, contract)
    assert hook_state['replaced'], 'the deterministic mid-read replacement must fire'
    # The replacement happened after the open, before the read finished.
    assert hook_state['chunk'] == 20
    assert 0 < hook_state['bytes'] < len(ORIGINAL)

    assert result['imported'] == 2048
    assert result['duplicate_file'] is False
    assert result['sha256'] == chosen_sha
    assert result['attempts'] == attempts
    assert result['path_changed_during_read'] is path_changed
    assert result['resumed'] is False

    con = connect(db)
    source = con.execute('SELECT id,sha256,raw FROM source').fetchone()
    # source.raw is bit-for-bit the bytes the recorded hash was computed over.
    assert source['raw'] == chosen_raw
    assert source['sha256'] == hashlib.sha256(source['raw']).hexdigest() == chosen_sha

    expected = _expected_payloads(chosen_raw)
    rows = con.execute(
        'SELECT source_id,line_no,identity,payload,sha256 FROM event ORDER BY line_no'
    ).fetchall()
    assert {row['line_no']: row['payload'] for row in rows} == expected
    assert len(rows) == 2048
    assert {row['source_id'] for row in rows} == {source['id']}
    for row in rows:
        assert hashlib.sha256(row['payload'].encode()).hexdigest() == row['sha256']

    messages = _messages(con)
    assert messages['SNAP:1024'] == message_id
    assert messages['SNAP:1024'] != other_message_id
    # No event or byte from the other source version leaked in.
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 1
    assert other_message_id not in {
        json.loads(row['payload']).get('message_id')
        for row in con.execute('SELECT payload FROM event')
    }
    assert _staging_counts(con) == (0, 0, 0, 0)

    audit_row = con.execute('SELECT * FROM import_audit').fetchone()
    assert audit_row['source_id'] == source['id']
    assert audit_row['contract'] == contract
    assert audit_row['attempts'] == attempts
    assert audit_row['resumed'] == 0
    assert audit_row['path_changed'] == int(path_changed)
    assert audit_row['line_count'] == 2048
    assert audit_row['imported'] == 2048
    assert audit_row['chunk_size'] == 8192
    assert audit_row['snapshot_id'] == result['snapshot_id']
    con.close()

    # The same snapshot re-entered by source hash is idempotent, even from a
    # different path holding bit-identical content.
    replay = _write_copy(tmp_path, f'{contract}-replay.jsonl', chosen_raw)
    again = import_file(db, replay, chunk_size=8192)
    assert again['duplicate_file'] is True and again['imported'] == 0
    assert again['sha256'] == chosen_sha
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 2048
    assert con.execute('SELECT COUNT(*) FROM import_audit').fetchone()[0] == 1
    con.close()


def test_contract_difference_is_only_the_permutation_of_versions(tmp_path):
    """Both contracts are internally consistent but commit different snapshots."""
    freeze_db, freeze_result, _ = _run_contract_case(tmp_path, 'freeze_on_open')
    retry_db, retry_result, _ = _run_contract_case(tmp_path, 'verify_then_retry_latest')
    assert freeze_result['sha256'] == ORIGINAL_SHA
    assert retry_result['sha256'] == REVISED_SHA
    assert freeze_result['sha256'] != retry_result['sha256']

    con = connect(freeze_db)
    freeze_events = {(r['identity'], r['payload']) for r in con.execute('SELECT * FROM event')}
    con.close()
    con = connect(retry_db)
    retry_events = {(r['identity'], r['payload']) for r in con.execute('SELECT * FROM event')}
    con.close()
    # Exactly one event identity differs; the rest are byte-identical.
    assert freeze_events - retry_events == {(
        'SNAP:1024',
        _expected_payloads(ORIGINAL)[1025],
    )}
    assert retry_events - freeze_events == {(
        'SNAP:1024',
        _expected_payloads(REVISED)[1025],
    )}


def test_chunk_boundaries_preserve_bom_crlf_blank_and_bad_line_semantics(tmp_path):
    valid1 = {
        'kind': 'queued', 'subject': 'R1', 'identity': 'C:1',
        'time': '2026-01-01T00:00:00Z', 'message_id': 'C1', 'note': '中文测试',
    }
    valid2 = {
        'kind': 'sent', 'subject': 'R1', 'identity': 'C:2',
        'time': '2026-01-01T00:05:00Z', 'message_id': 'C1',
    }
    valid3 = {
        'kind': 'receipt', 'subject': 'R1', 'identity': 'C:3',
        'time': '2026-01-01T00:10:00Z', 'message_id': 'C1',
    }
    body = '\r\n'.join([
        json.dumps(valid1, ensure_ascii=False, separators=(',', ':')),
        json.dumps(valid2, ensure_ascii=False, separators=(',', ':')),
        '   ',
        '{not-json',
        json.dumps(valid3, ensure_ascii=False, separators=(',', ':')),
    ])
    raw = b'\xef\xbb\xbf' + body.encode('utf-8')
    assert not raw.endswith(b'\n')
    path = _write_copy(tmp_path, 'boundaries.jsonl', raw)

    # Include a chunk size that ends exactly after a '\r' so CRLF straddles
    # chunks, plus sizes that split the UTF-8 BOM and a Chinese code point.
    crlf_at = raw.index(b'\r\n')
    chunk_sizes = [1, 2, 3, 5, 7, 13, 64, 127, crlf_at + 1, len(raw), DEFAULT_CHUNK_SIZE]
    baselines = []
    for chunk_size in chunk_sizes:
        db = _prepare(tmp_path, f'boundaries-{chunk_size}.db')
        result = import_file(db, path, chunk_size=chunk_size)
        assert result['imported'] == 3
        con = connect(db)
        events = [tuple(row) for row in con.execute(
            'SELECT line_no,identity,payload,sha256 FROM event ORDER BY line_no'
        )]
        issues = [tuple(row) for row in con.execute(
            'SELECT line_no,error,raw_line FROM stage_issue ORDER BY line_no'
        )]
        issue_rows = [json.loads(row['detail']) for row in con.execute(
            "SELECT detail FROM issue WHERE code='parse_error' ORDER BY id"
        )]
        source_raw = con.execute('SELECT raw FROM source').fetchone()['raw']
        con.close()
        assert [e[0] for e in events] == [1, 2, 5]
        assert [e[1] for e in events] == ['C:1', 'C:2', 'C:3']
        assert source_raw == raw
        assert issues == []
        assert [(i['line_no'], i['raw_line']) for i in issue_rows] == [(4, '{not-json')]
        baselines.append((events, [(i['line_no'], i['error'], i['raw_line']) for i in issue_rows]))

    assert all(item == baselines[0] for item in baselines)


def test_crlf_and_lone_cr_line_boundaries_match_splitlines(tmp_path):
    raw = (b'\xef\xbb\xbf'
           b'{"kind":"queued","subject":"R1","identity":"L:1","time":"2026-01-01T00:00:00Z"}\r'
           b'{"kind":"queued","subject":"R1","identity":"L:2","time":"2026-01-01T00:00:00Z"}\r\n'
           b'\n'
           b'{"kind":"sent","subject":"R1","identity":"L:3","time":"2026-01-01T00:00:00Z"}')
    lines = raw.decode('utf-8-sig').splitlines()
    assert len(lines) == 4 and lines[2] == ''
    assert [json.loads(line)['identity'] for line in lines if line] == ['L:1', 'L:2', 'L:3']

    path = _write_copy(tmp_path, 'crlf.jsonl', raw)
    for chunk_size in (1, 2, 3, 4, 17, 71, 4096):
        db = _prepare(tmp_path, f'crlf-{chunk_size}.db')
        result = import_file(db, path, chunk_size=chunk_size)
        assert result['imported'] == 3
        con = connect(db)
        assert [tuple(r) for r in con.execute(
            'SELECT line_no,identity FROM event ORDER BY line_no')] == [
            (1, 'L:1'), (2, 'L:2'), (4, 'L:3')]
        assert con.execute('SELECT raw FROM source').fetchone()['raw'] == raw
        con.close()


def test_same_identity_different_content_stays_an_isolated_conflict(tmp_path):
    db = _prepare(tmp_path)
    original_path = _write_copy(tmp_path, 'original.jsonl', ORIGINAL)
    revised_path = _write_copy(tmp_path, 'revised.jsonl', REVISED)
    assert import_file(db, original_path, chunk_size=4096)['imported'] == 2048
    result = import_file(db, revised_path, chunk_size=4096)
    assert result['imported'] == 0
    assert result['duplicate_file'] is False
    assert result['sha256'] == REVISED_SHA

    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 2
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 2048
    assert con.execute(
        "SELECT COUNT(*) FROM issue WHERE code='identity_conflict'").fetchone()[0] == 1
    conflict = con.execute(
        "SELECT detail FROM issue WHERE code='identity_conflict'").fetchone()['detail']
    assert conflict == 'SNAP:1024'
    assert _messages(con)['SNAP:1024'] == 'Q-1024'
    con.close()


def test_interrupted_read_leaves_nothing_and_retry_imports_complete_source(tmp_path):
    db = _prepare(tmp_path)
    path = _write_copy(tmp_path, 'incoming.jsonl', ORIGINAL)

    def exploding_hook(_path, progress):
        if progress['chunks'] == 3:
            raise RuntimeError('simulated crash mid-read')

    with pytest.raises(RuntimeError, match='simulated crash mid-read'):
        import_file(db, path, chunk_size=4096, midread_hook=exploding_hook)

    con = connect(db)
    assert _staging_counts(con) == (0, 0, 0, 0)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 0
    con.close()

    result = import_file(db, path, chunk_size=4096)
    assert result['imported'] == 2048 and result['resumed'] is False
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 2048
    assert con.execute('SELECT raw FROM source').fetchone()['raw'] == ORIGINAL
    con.close()


def test_interrupted_commit_resumes_the_same_snapshot_without_duplicates(tmp_path):
    db = _prepare(tmp_path)
    path = _write_copy(tmp_path, 'incoming.jsonl', ORIGINAL)

    con = connect(db)
    con.execute(
        "CREATE TRIGGER abort_commit BEFORE INSERT ON event "
        "WHEN NEW.identity='SNAP:1500' BEGIN SELECT RAISE(ABORT,'simulated crash'); END"
    )
    con.commit()
    con.close()

    with pytest.raises(sqlite3.IntegrityError, match='simulated crash'):
        import_file(db, path, chunk_size=4096)

    # The final source/event/issue transaction rolled back entirely, while the
    # completed staging snapshot survives for an exact retry.
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM issue').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM import_audit').fetchone()[0] == 0
    staged = con.execute('SELECT * FROM stage_snapshot').fetchall()
    assert len(staged) == 1
    assert staged[0]['state'] == 'staged'
    snapshot_id = staged[0]['id']
    assert staged[0]['sha256'] == ORIGINAL_SHA
    assert con.execute('SELECT COUNT(*) FROM stage_event').fetchone()[0] == 2048
    con.execute('DROP TRIGGER abort_commit')
    con.commit()
    con.close()

    result = import_file(db, path, chunk_size=4096)
    assert result['resumed'] is True
    assert result['snapshot_id'] == snapshot_id
    assert result['imported'] == 2048

    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 2048
    assert con.execute('SELECT COUNT(*) FROM issue').fetchone()[0] == 0
    source = con.execute('SELECT id,sha256,raw FROM source').fetchone()
    assert source['raw'] == ORIGINAL
    assert source['sha256'] == ORIGINAL_SHA
    audit_row = con.execute('SELECT * FROM import_audit').fetchone()
    assert audit_row['source_id'] == source['id']
    assert audit_row['snapshot_id'] == snapshot_id
    assert audit_row['resumed'] == 1
    assert _staging_counts(con) == (0, 0, 0, 0)
    con.close()


def _generate_jsonl(path, count, pad=160):
    with open(path, 'wb') as fh:
        for i in range(count):
            row = {
                'kind': 'queued', 'subject': 'R1', 'identity': f'G:{i}',
                'time': '2026-01-01T00:00:00Z', 'message_id': f'G-{i}',
                'attempt': 1, 'address': 'r1-alias@example.invalid',
                'channel': 'email-v1', 'pad': 'x' * pad,
            }
            fh.write(json.dumps(row, separators=(',', ':')).encode('utf-8') + b'\n')


def _streamed_digest(path, block=65536):
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _measure_import(db, path, chunk_size):
    tracemalloc.start()
    try:
        result = import_file(db, path, chunk_size=chunk_size)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, result


def test_large_file_peak_memory_follows_chunk_size_not_file_length(tmp_path):
    chunk_size = 16384
    small_lines, large_lines = 2000, 20000
    small = tmp_path / 'small.jsonl'
    large = tmp_path / 'large.jsonl'
    _generate_jsonl(small, small_lines)
    _generate_jsonl(large, large_lines)
    small_bytes = small.stat().st_size
    large_bytes = large.stat().st_size
    assert small_bytes > 4 * chunk_size
    assert large_bytes >= 8 * small_bytes

    small_db = _prepare(tmp_path, 'small.db')
    large_db = _prepare(tmp_path, 'large.db')
    small_peak, small_result = _measure_import(small_db, small, chunk_size)
    large_peak, large_result = _measure_import(large_db, large, chunk_size)
    assert small_result['imported'] == small_lines
    assert large_result['imported'] == large_lines

    # Both imports stay in the same bounded envelope; the ten-times-larger
    # file must not drag peak Python allocation up with it.
    assert large_peak < 2 * 1024 * 1024
    assert large_peak < small_peak * 1.5 + 256 * 1024
    assert large_result['line_count'] / max(small_result['line_count'], 1) == 10

    for db, path, expected_sha, expected_size in (
        (small_db, small, *_streamed_digest(small)),
        (large_db, large, *_streamed_digest(large)),
    ):
        con = connect(db)
        source = con.execute('SELECT id,sha256,raw FROM source').fetchone()
        assert source['sha256'] == expected_sha
        blob = con.blobopen('source', 'raw', source['id'], readonly=True)
        digest = hashlib.sha256()
        seen = 0
        try:
            while True:
                block = blob.read(65536)
                if not block:
                    break
                digest.update(block)
                seen += len(block)
        finally:
            blob.close()
        con.close()
        assert seen == expected_size == path.stat().st_size
        assert digest.hexdigest() == expected_sha


def test_unknown_contract_and_invalid_chunk_are_rejected(tmp_path):
    db = _prepare(tmp_path)
    path = _write_copy(tmp_path, 'incoming.jsonl', ORIGINAL)
    with pytest.raises(ValueError, match='unknown_source_contract'):
        import_file(db, path, contract='read_twice_and_hope')
    with pytest.raises(ValueError, match='invalid_chunk_size'):
        import_file(db, path, chunk_size=0)


def test_cli_offers_auditable_snapshot_contract_selection(tmp_path):
    repo = Path(__file__).parents[1]
    db = tmp_path / 'cli.db'
    incoming = _write_copy(tmp_path, 'incoming.jsonl', ORIGINAL)
    env = dict(os.environ, PYTHONPATH=str(repo))
    common = [sys.executable, '-B', '-m', 'receiptdesk']
    ingest = subprocess.run(
        common + ['ingest', '--db', str(db), '--config', str(BASE / 'config.json'),
                  '--contract', 'freeze_on_open', '--chunk-size', '4096', str(incoming)],
        cwd=repo, env=env, capture_output=True, text=True, check=True,
    )
    summary = json.loads(ingest.stdout)
    assert summary['imported'] == 2048
    assert summary['contract'] == 'freeze_on_open'
    assert summary['sha256'] == ORIGINAL_SHA

    listed = subprocess.run(
        common + ['audit', '--db', str(db)],
        cwd=repo, env=env, capture_output=True, text=True, check=True,
    )
    rows = json.loads(listed.stdout)
    assert len(rows) == 1
    assert rows[0]['sha256'] == ORIGINAL_SHA
    assert rows[0]['contract'] == 'freeze_on_open'
    assert rows[0]['snapshot_id'] == summary['snapshot_id']
    assert rows[0]['chunk_size'] == 4096
    assert audit(db)[0]['snapshot_id'] == summary['snapshot_id']
