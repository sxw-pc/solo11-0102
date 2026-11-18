import json
import threading
from pathlib import Path

import pytest

from receiptdesk.bank import Bank
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.settlement import (payout, register, report, reverse_receipt,
                                    settle, solve_matching)
from receiptdesk.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def fresh_db(tmp_path):
    db = tmp_path / 'settlement.db'
    freeze_config(db, FIX / 'settlement-config.json')
    assert import_file(db, FIX / 'settlement-events.jsonl')['imported'] == 2
    return db


def write_case(tmp_path, invoices, receipts, name='case', commission=1600,
               contracts=('merchant_gross', 'carrier_commission')):
    doc = {'invoices': invoices, 'bank_ids': [r['bank_id'] for r in receipts],
           'beneficiaries': {'merchant': 'NOTICE-MERCHANT', 'carrier': 'MAIL-CARRIER'},
           'commission_cents': commission, 'contracts': list(contracts)}
    case_file = tmp_path / f'{name}.json'
    case_file.write_text(json.dumps(doc), encoding='utf-8')
    bank_file = tmp_path / f'{name}.jsonl'
    bank_file.write_text('\n'.join(json.dumps(r) for r in receipts) + '\n', encoding='utf-8')
    return case_file, bank_file


def fixture_case(tmp_path, case_id='settlement-01'):
    db = fresh_db(tmp_path)
    register(db, case_id, FIX / 'settlement-case.json', FIX / 'settlement-bank.jsonl')
    return db


def test_global_matching_resolves_overlapping_candidates(tmp_path):
    """C1 may clear INV-1 or INV-2, C2 only INV-1: greedy file order would
    strand C2, the global optimum clears both invoices for 16000 cents."""
    db = fixture_case(tmp_path)
    result = settle(db, 'settlement-01')
    assert result['cleared_cents'] == 16000
    assert result['suspense_cents'] == 0
    got = {(a['bank_id'], a['invoice_id']): a['amount_cents'] for a in result['allocations']}
    assert got == {('C2', 'INV-1'): 8000, ('C1', 'INV-2'): 8000}
    assert {i['status'] for i in result['invoices']} == {'cleared'}
    # matching evidence is reviewable: bank file SHA, serial, payer, amount
    con = connect(db)
    rows = con.execute("SELECT ref,amount_cents,detail FROM settlement_ledger "
                       "WHERE kind='allocation' ORDER BY ref").fetchall()
    con.close()
    assert [r['ref'] for r in rows] == ['C1->INV-2', 'C2->INV-1']
    for row in rows:
        detail = json.loads(row['detail'])
        assert len(detail['bank_sha256']) == 64 and detail['payer'].startswith('P-')


def test_solve_matching_is_exact_not_greedy():
    invoices = [{'id': 'INV-1', 'amount_cents': 8000}, {'id': 'INV-2', 'amount_cents': 8000}]
    receipts = [{'bank_id': 'C1', 'amount_cents': 8000, 'candidate_invoices': ['INV-1', 'INV-2']},
                {'bank_id': 'C2', 'amount_cents': 8000, 'candidate_invoices': ['INV-1']}]
    result = solve_matching(invoices, receipts)
    got = {(a['bank_id'], a['invoice_id']): a['amount_cents'] for a in result['allocations']}
    assert got == {('C1', 'INV-2'): 8000, ('C2', 'INV-1'): 8000}
    assert result['cleared_cents'] == 16000


def test_partial_payment_and_over_amount_suspense(tmp_path):
    db = fresh_db(tmp_path)
    case_file, bank_file = write_case(
        tmp_path,
        invoices=[{'id': 'INV-1', 'subject': 'R1', 'message_id': 'SET1', 'amount_cents': 8000},
                  {'id': 'INV-2', 'subject': 'R2', 'message_id': 'SET2', 'amount_cents': 8000}],
        receipts=[{'bank_id': 'P1', 'amount_cents': 5000, 'payer': 'P-R1',
                   'at': '2026-01-01T09:00:00Z', 'candidate_invoices': ['INV-1']},
                  {'bank_id': 'P2', 'amount_cents': 9000, 'payer': 'P-R2',
                   'at': '2026-01-01T09:01:00Z', 'candidate_invoices': ['INV-2']}])
    register(db, 'case-partial', case_file, bank_file)
    result = settle(db, 'case-partial')
    assert result['cleared_cents'] == 13000
    assert result['suspense'] == [{'bank_id': 'P2', 'amount_cents': 1000}]
    status = {i['invoice_id']: i for i in result['invoices']}
    assert status['INV-1']['status'] == 'partial' and status['INV-1']['paid_cents'] == 5000
    assert status['INV-2']['status'] == 'cleared'
    # suspense is frozen, never paid out
    bank = Bank()
    summary = payout(db, 'case-partial', 'merchant_gross', bank)
    assert summary['paid_cents'] == 13000
    assert bank.total_issued() == 13000
    assert bank.balance('NOTICE-MERCHANT') == 13000


def test_duplicate_serial_and_idempotent_settle(tmp_path):
    db = fixture_case(tmp_path)
    first = settle(db, 'settlement-01')
    second = settle(db, 'settlement-01')
    assert first['match_digest'] == second['match_digest']
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM settlement_alloc WHERE case_id='settlement-01'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM settlement_ledger WHERE kind='receipt'").fetchone()[0] == 2
    con.close()
    # the same bank serial cannot be settled again under another case
    register(db, 'settlement-02', FIX / 'settlement-case.json', FIX / 'settlement-bank.jsonl')
    with pytest.raises(ValueError, match='bank_receipt_already_settled'):
        settle(db, 'settlement-02')
    # duplicate serial inside one bank file is rejected at register time
    dup = tmp_path / 'dup.jsonl'
    lines = (FIX / 'settlement-bank.jsonl').read_text().splitlines()
    dup.write_text(lines[0] + '\n' + lines[0] + '\n')
    with pytest.raises(ValueError, match='bank_receipt_duplicate'):
        register(db, 'settlement-03', FIX / 'settlement-case.json', dup)


def test_two_workers_race_same_receipts(tmp_path):
    db = fixture_case(tmp_path)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(settle(db, 'settlement-01'))
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({r['match_digest'] for r in results}) == 1
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM settlement_state').fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM settlement_ledger WHERE kind='receipt'").fetchone()[0] == 2
    con.close()
    # racing payouts also stay single-shot: the bank sees each nonce once
    bank = Bank()
    barrier2 = threading.Barrier(2)
    payouts, errors2 = [], []

    def pay_worker():
        barrier2.wait()
        try:
            payouts.append(payout(db, 'settlement-01', 'merchant_gross', bank))
        except Exception as exc:  # noqa: BLE001
            errors2.append(exc)

    threads = [threading.Thread(target=pay_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors2
    assert bank.total_issued() == 16000
    assert bank.balance('NOTICE-MERCHANT') == 16000
    assert len(bank.transfers()) == 1
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM settlement_ledger WHERE kind='payout'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM payment_order WHERE status='confirmed'").fetchone()[0] == 1
    con.close()


def test_merchant_gross_payout_full_amount(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    summary = payout(db, 'settlement-01', 'merchant_gross', bank)
    assert summary['paid_cents'] == 16000
    assert len(summary['orders']) == 1
    order = summary['orders'][0]
    assert order['payee'] == 'NOTICE-MERCHANT' and order['status'] == 'confirmed'
    assert order['bank_ref'].startswith('BK-')
    # idempotent retry: same nonce, no second transfer
    again = payout(db, 'settlement-01', 'merchant_gross', bank)
    assert again['orders'][0]['nonce'] == order['nonce']
    assert bank.total_issued() == 16000


def test_carrier_commission_split_and_partial_failure(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    bank.refuse_payees.add('MAIL-CARRIER')
    summary = payout(db, 'settlement-01', 'carrier_commission', bank)
    by_payee = {o['payee']: o for o in summary['orders']}
    assert by_payee['NOTICE-MERCHANT']['amount_cents'] == 14400
    assert by_payee['NOTICE-MERCHANT']['status'] == 'confirmed'
    assert by_payee['MAIL-CARRIER']['amount_cents'] == 1600
    assert by_payee['MAIL-CARRIER']['status'] == 'failed'
    assert bank.balance('NOTICE-MERCHANT') == 14400
    assert bank.balance('MAIL-CARRIER') == 0
    # mid-failure the books still balance: 1600 cents sit outstanding, not lost
    mid = json.loads(report(db, 'settlement-01', tmp_path / 'mid.json', bank).read_text())
    assert mid['conservation']['checks']['ok']
    assert mid['conservation']['outstanding_cents'] == 1600
    assert mid['conservation']['paid_cents'] == 14400
    # merchant stays confirmed; retry settles only the failed carrier leg
    bank.refuse_payees.clear()
    retry = payout(db, 'settlement-01', 'carrier_commission', bank)
    assert {o['status'] for o in retry['orders']} == {'confirmed'}
    assert bank.balance('NOTICE-MERCHANT') == 14400  # not paid twice
    assert bank.balance('MAIL-CARRIER') == 1600
    assert bank.total_issued() == 16000


def test_late_ack_recovers_via_bank_status_query(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    bank.drop_acks = True  # bank processes the transfer but the ACK is lost
    summary = payout(db, 'settlement-01', 'merchant_gross', bank)
    assert summary['orders'][0]['status'] == 'confirmed'
    assert bank.total_issued() == 16000
    con = connect(db)
    detail = json.loads(con.execute("SELECT detail FROM settlement_ledger WHERE kind='payout'").fetchone()[0])
    con.close()
    assert detail['via'] == 'ack_lost_recovered'
    bank.drop_acks = False
    again = payout(db, 'settlement-01', 'merchant_gross', bank)
    assert again['paid_cents'] == 16000
    assert bank.total_issued() == 16000  # no double payment


def test_contract_change_is_locked_after_payout(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    payout(db, 'settlement-01', 'merchant_gross', bank)
    with pytest.raises(ValueError, match='payout_contract_locked'):
        payout(db, 'settlement-01', 'carrier_commission', bank)
    con = connect(db)
    orders = con.execute('SELECT payee,amount_cents,status FROM payment_order').fetchall()
    con.close()
    assert [(o['payee'], o['amount_cents'], o['status']) for o in orders] == [
        ('NOTICE-MERCHANT', 16000, 'confirmed')]
    assert bank.balance('NOTICE-MERCHANT') == 16000
    assert bank.balance('MAIL-CARRIER') == 0  # paid history untouched


def test_refund_reversal_after_payout_creates_recovery_entries(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    payout(db, 'settlement-01', 'merchant_gross', bank)
    result = reverse_receipt(db, 'settlement-01', 'C1', reason='payer_chargeback')
    assert result['cleared_cents'] == 8000  # only C2->INV-1 survives
    status = {i['invoice_id']: i['status'] for i in result['invoices']}
    assert status == {'INV-1': 'cleared', 'INV-2': 'open'}
    con = connect(db)
    entries = {r['kind']: r['amount_cents'] for r in con.execute(
        "SELECT kind,amount_cents FROM settlement_ledger WHERE kind IN ('reversal','recovery_due')")}
    con.close()
    assert entries == {'reversal': 8000, 'recovery_due': 8000}
    # cash already left: paid 16000 == active clearing 8000 + recovery due 8000
    out = report(db, 'settlement-01', tmp_path / 'report.json', bank)
    data = json.loads(out.read_text())
    assert data['conservation']['checks']['orders_cover_cleared_plus_recovery']
    assert data['conservation']['checks']['ok']
    assert data['conservation']['paid_cents'] == 16000
    assert data['conservation']['recovery_due_cents'] == 8000
    with pytest.raises(ValueError, match='settlement_receipt_already_reversed'):
        reverse_receipt(db, 'settlement-01', 'C1')


def test_double_reversal_accumulates_recovery_incrementally(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    bank = Bank()
    payout(db, 'settlement-01', 'merchant_gross', bank)
    reverse_receipt(db, 'settlement-01', 'C1', reason='chargeback')
    result = reverse_receipt(db, 'settlement-01', 'C2', reason='chargeback')
    assert result['cleared_cents'] == 0
    con = connect(db)
    entries = [r['amount_cents'] for r in con.execute(
        "SELECT amount_cents FROM settlement_ledger WHERE kind='recovery_due' ORDER BY id")]
    con.close()
    assert entries == [8000, 8000]  # incremental, not 8000 + 16000
    data = json.loads(report(db, 'settlement-01', tmp_path / 'r.json', bank).read_text())
    assert data['conservation']['recovery_due_cents'] == 16000
    assert data['conservation']['checks']['ok']


def test_reversal_before_payout_shrinks_payout(tmp_path):
    db = fixture_case(tmp_path)
    settle(db, 'settlement-01')
    reverse_receipt(db, 'settlement-01', 'C2', reason='bank_clawback')
    bank = Bank()
    summary = payout(db, 'settlement-01', 'merchant_gross', bank)
    assert summary['paid_cents'] == 8000
    assert bank.total_issued() == 8000
    con = connect(db)
    kinds = [r[0] for r in con.execute('SELECT kind FROM settlement_ledger')]
    con.close()
    assert 'recovery_due' not in kinds  # nothing overpaid, no compensation needed


def test_fund_conservation_report_for_both_contracts(tmp_path):
    totals = {}
    for contract, expected in (('merchant_gross', {'NOTICE-MERCHANT': 16000}),
                               ('carrier_commission', {'NOTICE-MERCHANT': 14400,
                                                       'MAIL-CARRIER': 1600})):
        case_dir = tmp_path / contract
        case_dir.mkdir()
        db = fixture_case(case_dir)
        settle(db, 'settlement-01')
        bank = Bank(case_dir / 'bank.db')
        payout(db, 'settlement-01', contract, bank)
        out = report(db, 'settlement-01', case_dir / 'settlement-report.json', bank)
        data = json.loads(out.read_text())
        assert data['conservation']['checks']['ok']
        assert data['conservation']['paid_cents'] == 16000
        got = {p['payee']: p['amount_cents'] for p in data['payments']}
        assert got == expected
        totals[contract] = sum(got.values())
        md = out.with_suffix('.md').read_text()
        assert '资金守恒' in md and '✅' in md
        for payee, amount in expected.items():
            assert payee in md and str(amount) in md
    assert totals['merchant_gross'] == totals['carrier_commission'] == 16000


def test_settlement_validations(tmp_path):
    db = fixture_case(tmp_path)
    with pytest.raises(ValueError, match='settlement_case_unknown'):
        settle(db, 'no-such-case')
    with pytest.raises(ValueError, match='settlement_not_settled'):
        payout(db, 'settlement-01', 'merchant_gross', Bank())
    settle(db, 'settlement-01')
    with pytest.raises(ValueError, match='settlement_contract_unknown'):
        payout(db, 'settlement-01', 'carrier_free', Bank())
    # candidate invoices must reference frozen case invoices
    case_file, bank_file = write_case(
        tmp_path,
        invoices=[{'id': 'INV-1', 'subject': 'R1', 'message_id': 'SET1', 'amount_cents': 8000}],
        receipts=[{'bank_id': 'X1', 'amount_cents': 8000, 'payer': 'P-X',
                   'at': '2026-01-01T09:00:00Z', 'candidate_invoices': ['INV-9']}], name='bad')
    register(db, 'case-bad', case_file, bank_file)
    with pytest.raises(ValueError, match='settlement_candidate_unknown'):
        settle(db, 'case-bad')
