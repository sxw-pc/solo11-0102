import json
import shutil
import sys
from pathlib import Path

from receiptdesk.__main__ import main
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.packet import allocate
from receiptdesk.rules import evaluate
from receiptdesk.store import connect

BASE = Path(__file__).parents[1] / 'fixtures'
FORM_A_SHA = 'f4571575f755cab65b35fd99a70fc863747f69350c7cc98f2d9fd6051c13dbc7'
FORM_B_SHA = 'ce14c628ef11febc23c7ffbb81107ce8e83602b676ab62dd802ec690c3a8e06a'


def frozen_db(tmp_path):
    db = tmp_path / 'batch.db'
    freeze_config(db, BASE / 'packet_config.json')
    return db


def write_signing(tmp_path, **overrides):
    spec = json.loads((BASE / 'packet_signing.json').read_text(encoding='utf-8'))
    spec.update(overrides)
    path = tmp_path / 'signing.json'
    path.write_text(json.dumps(spec), encoding='utf-8')
    return path


def copy_files(tmp_path):
    dest = tmp_path / 'packet_files'
    shutil.copytree(BASE / 'packet_files', dest)
    return dest


def reasons(result):
    return {r['recipient']: r['reason'] for r in result['rejected']}


def test_oldest_contract_issues_r1_complete_packet(tmp_path):
    db = frozen_db(tmp_path)
    result = allocate(db, BASE / 'packet_signing.json')
    assert result['already_applied'] is False
    assert [p['recipient'] for p in result['packets']] == ['R1']
    items = {i['form']: i for i in result['packets'][0]['items']}
    assert set(items) == {'A', 'B'}
    assert items['A'] == {'form': 'A', 'version': 'FORM-A-v1.txt',
                          'sha256': FORM_A_SHA, 'signer': 'S-A'}
    assert items['B'] == {'form': 'B', 'version': 'FORM-B-v2.txt',
                          'sha256': FORM_B_SHA, 'signer': 'S-AB'}
    assert reasons(result) == {'R2': 'insufficient_seals', 'R3': 'insufficient_seals'}
    assert result['seals'] == {'S-A': {'remaining': 0, 'consumed': 1},
                               'S-AB': {'remaining': 0, 'consumed': 1}}
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0] == 2
    # 未获配者不得标成已发
    assert con.execute(
        "SELECT COUNT(*) FROM packet WHERE status='issued' AND recipient!='R1'"
    ).fetchone()[0] == 0
    # 原始通知台账不被配发改写
    for table in ('source', 'event', 'decision', 'evidence'):
        assert con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_max_completed_contract_serves_two_recipients(tmp_path):
    db = frozen_db(tmp_path)
    result = allocate(db, BASE / 'packet_signing.json',
                      contract='max_completed_recipients')
    assert [p['recipient'] for p in result['packets']] == ['R2', 'R3']
    signers = {}
    for packet in result['packets']:
        assert [i['form'] for i in packet['items']] == ['A']
        signers[packet['recipient']] = packet['items'][0]['signer']
    assert signers == {'R2': 'S-A', 'R3': 'S-AB'}
    assert reasons(result) == {'R1': 'insufficient_seals'}
    assert all(s['remaining'] == 0 for s in result['seals'].values())


def test_signer_authorization_conflict_rejects_packet(tmp_path):
    db = frozen_db(tmp_path)
    signing = write_signing(
        tmp_path,
        signers=[{'id': 'S-A', 'remaining_seals': 2, 'authorized_forms': ['A']}],
        requests=[{'recipient': 'R2', 'requested_at': '2026-01-01T09:00:00Z',
                   'required_forms': ['B']}])
    result = allocate(db, signing, files_dir=BASE / 'packet_files')
    assert result['packets'] == []
    assert reasons(result) == {'R2': 'unauthorized_form'}
    assert result['seals']['S-A'] == {'remaining': 2, 'consumed': 0}
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0] == 0
    # 只能签 A 的签章员绝不能出现在 B 的配发行里
    assert con.execute(
        "SELECT COUNT(*) FROM packet_item WHERE form_id='B' AND signer_id='S-A'"
    ).fetchone()[0] == 0


def test_attachment_content_change_rejects_packets(tmp_path):
    files = copy_files(tmp_path)
    (files / 'FORM-A-v1.txt').write_bytes(b'tampered attachment bytes')
    db = frozen_db(tmp_path)
    result = allocate(db, BASE / 'packet_signing.json', files_dir=files)
    assert result['packets'] == []
    assert reasons(result) == {'R1': 'attachment_hash_mismatch',
                               'R2': 'attachment_hash_mismatch',
                               'R3': 'attachment_hash_mismatch'}
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0] == 0
    assert all(s['consumed'] == 0 for s in result['seals'].values())


def test_missing_attachment_rejects_only_affected_packets(tmp_path):
    files = copy_files(tmp_path)
    (files / 'FORM-B-v2.txt').unlink()
    db = frozen_db(tmp_path)
    result = allocate(db, BASE / 'packet_signing.json', files_dir=files)
    assert [p['recipient'] for p in result['packets']] == ['R2', 'R3']
    assert reasons(result) == {'R1': 'missing_attachment'}
    assert con_ledger_count(db) == 2


def con_ledger_count(db):
    return connect(db).execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0]


def test_complete_packet_atomicity_no_partial_issue(tmp_path):
    db = frozen_db(tmp_path)
    signing = write_signing(
        tmp_path,
        signers=[{'id': 'S-AB', 'remaining_seals': 1, 'authorized_forms': ['A', 'B']}],
        requests=[{'recipient': 'R1', 'requested_at': '2026-01-01T08:00:00Z',
                   'required_forms': ['A', 'B']}])
    result = allocate(db, signing, files_dir=BASE / 'packet_files')
    assert result['packets'] == []
    assert reasons(result) == {'R1': 'insufficient_seals'}
    assert result['seals']['S-AB'] == {'remaining': 1, 'consumed': 0}
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM packet_item').fetchone()[0] == 0


def test_rerun_same_input_is_idempotent(tmp_path):
    db = frozen_db(tmp_path)
    first = allocate(db, BASE / 'packet_signing.json')
    second = allocate(db, BASE / 'packet_signing.json')
    assert first['already_applied'] is False
    assert second['already_applied'] is True
    assert second['packets'] == first['packets']
    assert second['rejected'] == first['rejected']
    assert second['seals'] == first['seals']
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM packet_run').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM seal_ledger').fetchone()[0] == 2
    assert con.execute(
        "SELECT COUNT(*) FROM packet WHERE status='issued'").fetchone()[0] == 1


def test_second_contract_run_sees_consumed_seals(tmp_path):
    db = frozen_db(tmp_path)
    allocate(db, BASE / 'packet_signing.json')
    result = allocate(db, BASE / 'packet_signing.json',
                      contract='max_completed_recipients')
    assert result['packets'] == []
    assert reasons(result) == {'R1': 'already_issued', 'R2': 'insufficient_seals',
                               'R3': 'insufficient_seals'}
    assert con_ledger_count(db) == 2


def test_unknown_alias_and_duplicate_requests(tmp_path):
    db = frozen_db(tmp_path)
    signing = write_signing(tmp_path, requests=[
        {'recipient': 'R1', 'requested_at': '2026-01-01T08:00:00Z',
         'required_forms': ['A']},
        {'recipient': 'r2@example.invalid', 'requested_at': '2026-01-01T09:00:00Z',
         'required_forms': ['A']},
        {'recipient': 'R2', 'requested_at': '2026-01-01T09:10:00Z',
         'required_forms': ['A']},
        {'recipient': 'R9', 'requested_at': '2026-01-01T09:20:00Z',
         'required_forms': ['A']},
        {'recipient': 'R1', 'requested_at': '2026-01-01T09:30:00Z',
         'required_forms': ['A']},
    ])
    result = allocate(db, signing, files_dir=BASE / 'packet_files')
    # 别名归并到冻结名单主体，重复与未知对象不造成额外耗材
    assert [p['recipient'] for p in result['packets']] == ['R1', 'R2']
    assert reasons(result) == {'R1': 'duplicate_request', 'R2': 'duplicate_request',
                               'R9': 'unknown_recipient'}
    assert con_ledger_count(db) == 2
    assert all(s['remaining'] == 0 for s in result['seals'].values())


def test_duplicate_form_version_rejected(tmp_path):
    db = frozen_db(tmp_path)
    signing = write_signing(
        tmp_path,
        forms=[
            {'id': 'A', 'version': 'FORM-A-v1.txt', 'sha256': FORM_A_SHA},
            {'id': 'A2', 'version': 'FORM-A-v1.txt', 'sha256': FORM_A_SHA},
            {'id': 'B', 'version': 'FORM-B-v2.txt', 'sha256': FORM_B_SHA},
        ],
        requests=[
            {'recipient': 'R2', 'requested_at': '2026-01-01T09:00:00Z',
             'required_forms': ['A']},
            {'recipient': 'R3', 'requested_at': '2026-01-01T09:30:00Z',
             'required_forms': ['A2']},
        ])
    result = allocate(db, signing, files_dir=BASE / 'packet_files')
    assert result['packets'] == []
    assert reasons(result) == {'R2': 'duplicate_form_version',
                               'R3': 'duplicate_form_version'}
    assert con_ledger_count(db) == 0


def test_packet_run_does_not_rewrite_decisions(tmp_path):
    db = frozen_db(tmp_path)
    import_file(db, BASE / 'events.jsonl')
    evaluate(db)
    con = connect(db)
    before = {t: con.execute(f'SELECT * FROM {t}').fetchall()
              for t in ('source', 'event', 'decision', 'evidence', 'issue')}
    allocate(db, BASE / 'packet_signing.json')
    after = {t: con.execute(f'SELECT * FROM {t}').fetchall()
             for t in ('source', 'event', 'decision', 'evidence', 'issue')}
    assert before == after


def test_cli_packet_command(tmp_path, capsys, monkeypatch):
    db = tmp_path / 'cli.db'
    out = tmp_path / 'manifest.json'
    monkeypatch.setattr(sys, 'argv', [
        'receiptdesk', 'packet', '--db', str(db),
        '--config', str(BASE / 'packet_config.json'),
        '--signing', str(BASE / 'packet_signing.json'),
        '--contract', 'max_completed_recipients', '--out', str(out)])
    main()
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out.read_text(encoding='utf-8'))
    assert printed == written
    assert [p['recipient'] for p in printed['packets']] == ['R2', 'R3']
    assert printed['rejected'][0]['reason'] == 'insufficient_seals'
