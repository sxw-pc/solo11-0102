"""Freeze notice identities, invoices and actual synthetic bank receipts; no allocation."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from .bank import AckLost, Bank, TransferRejected
from .store import connect


def register(db_path, case_id, fixture, bank_file):
    fixture_path = Path(fixture).resolve()
    bank_path = Path(bank_file).resolve()
    raw = fixture_path.read_bytes()
    bank_raw = bank_path.read_bytes()
    doc = json.loads(raw)
    receipts = [json.loads(line) for line in bank_raw.decode('utf-8-sig').splitlines() if line]
    if not case_id or not doc.get('invoices') or not receipts or not doc.get('contracts'):
        raise ValueError('settlement_case_invalid')
    if len({r['bank_id'] for r in receipts}) != len(receipts):
        raise ValueError('bank_receipt_duplicate')
    bank_by_id = {r['bank_id']: r for r in receipts}
    if {r['bank_id'] for r in receipts} != set(doc['bank_ids']):
        raise ValueError('bank_receipt_manifest_mismatch')
    db = connect(db_path)
    try:
        observed = []
        for invoice in doc['invoices']:
            subject = invoice['subject']
            if db.execute('SELECT 1 FROM subject WHERE id=?', (subject,)).fetchone() is None:
                raise ValueError('settlement_subject_unknown')
            events = [dict(row) for row in db.execute(
                'SELECT e.id,e.identity,e.time,s.sha256 AS source_sha256 '
                'FROM event e JOIN source s ON s.id=e.source_id '
                'WHERE e.subject=? AND e.kind=? ORDER BY e.id', (subject, 'queued'))]
            if not events:
                raise ValueError('settlement_subject_unobserved')
            observed.append({'invoice': invoice, 'queued': events})
        if any(not bank_by_id[rid]['amount_cents'] > 0 for rid in doc['bank_ids']):
            raise ValueError('bank_amount_invalid')
        payload = json.dumps({'doc': doc, 'invoices': observed,
                              'bank_receipts': receipts,
                              'fixture_path': str(fixture_path),
                              'fixture_sha256': hashlib.sha256(raw).hexdigest(),
                              'bank_path': str(bank_path),
                              'bank_sha256': hashlib.sha256(bank_raw).hexdigest()},
                             sort_keys=True, separators=(',', ':'))
        with db:
            old = db.execute('SELECT payload FROM settlement_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('settlement_case_conflict')
            db.execute('INSERT OR IGNORE INTO settlement_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'invoices': len(observed), 'bank_receipts': len(receipts)}


# ---------------------------------------------------------------------------
# Clearing: exact global matching of bank receipts to invoices
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _dinic(n, arcs, s, t):
    """Integer max-flow; arcs are (u, v, capacity). Returns (value, flow per arc)."""
    g = [[] for _ in range(n)]
    pos = []
    for u, v, c in arcs:
        pos.append((u, len(g[u])))
        g[u].append([v, c, 0, len(g[v])])
        g[v].append([u, 0, 0, len(g[u]) - 1])

    def dfs(u, t, f, level, it):
        if u == t:
            return f
        while it[u] < len(g[u]):
            e = g[u][it[u]]
            if e[1] - e[2] > 0 and level[e[0]] == level[u] + 1:
                d = dfs(e[0], t, min(f, e[1] - e[2]), level, it)
                if d:
                    e[2] += d
                    g[e[0]][e[3]][2] -= d
                    return d
            it[u] += 1
        return 0

    total = 0
    while True:
        level = [-1] * n
        level[s] = 0
        queue = [s]
        i = 0
        while i < len(queue):
            u = queue[i]
            i += 1
            for e in g[u]:
                if e[1] - e[2] > 0 and level[e[0]] < 0:
                    level[e[0]] = level[u] + 1
                    queue.append(e[0])
        if level[t] < 0:
            break
        it = [0] * n
        while True:
            pushed = dfs(s, t, 1 << 62, level, it)
            if not pushed:
                break
            total += pushed
    return total, [g[u][k][2] for u, k in pos]


def _max_flow(arcs, source, sink):
    """Max-flow over hashable node ids; flows align with the input arc order."""
    index = {}

    def node(x):
        if x not in index:
            index[x] = len(index)
        return index[x]

    iarcs = [(node(u), node(v), c) for u, v, c in arcs]
    s, t = node(source), node(sink)
    value, flows = _dinic(len(index), iarcs, s, t)
    return value, flows


def _feasible_circulation(arcs):
    """Feasibility of a circulation with per-arc (lower, upper) bounds."""
    balance = {}
    residual = []
    for u, v, lo, hi in arcs:
        if lo > hi:
            return False
        balance[u] = balance.get(u, 0) - lo
        balance[v] = balance.get(v, 0) + lo
        residual.append((u, v, hi - lo))
    super_s, super_t = object(), object()
    demand = 0
    for node, bal in balance.items():
        if bal > 0:
            residual.append((super_s, node, bal))
            demand += bal
        elif bal < 0:
            residual.append((node, super_t, -bal))
    value, _ = _max_flow(residual, super_s, super_t)
    return value == demand


def _circulation_ok(invoices, receipts, total, saturated, fixed, caps):
    arcs = []
    for r in receipts:
        rid = r['bank_id']
        arcs.append(('SRC', 'R:' + rid, 0, r['amount_cents']))
        for inv in r['candidate_invoices']:
            key = (rid, inv)
            if key in fixed:
                v = fixed[key]
                arcs.append(('R:' + rid, 'I:' + inv, v, v))
            else:
                arcs.append(('R:' + rid, 'I:' + inv, 0, min(r['amount_cents'], caps.get(key, r['amount_cents']))))
    for i in invoices:
        lo = i['amount_cents'] if i['id'] in saturated else 0
        arcs.append(('I:' + i['id'], 'SNK', lo, i['amount_cents']))
    arcs.append(('SNK', 'SRC', total, total))
    return _feasible_circulation(arcs)


def solve_matching(invoices, receipts):
    """Exact optimal clearing of bank receipts against invoices.

    Objectives, lexicographic: (1) maximize total cleared cents, (2) maximize
    the number of fully cleared invoices, (3) deterministic canonical
    tie-break: lexicographically smallest allocation vector over arcs ordered
    by (bank_id, invoice_id). A receipt may fund several candidate invoices;
    any unmatched remainder is frozen as suspense. Exact for the local scale
    (<= 12 invoices / 12 receipts) via integer flow plus complete search.
    """
    inv_ids = sorted(i['id'] for i in invoices)
    inv_amount = {i['id']: i['amount_cents'] for i in invoices}
    receipts = sorted(receipts, key=lambda r: r['bank_id'])
    receipts = [dict(r, candidate_invoices=sorted(set(r.get('candidate_invoices', []))))
                for r in receipts]
    arcs = []
    for r in receipts:
        arcs.append(('SRC', 'R:' + r['bank_id'], r['amount_cents']))
        for inv in r['candidate_invoices']:
            arcs.append(('R:' + r['bank_id'], 'I:' + inv, r['amount_cents']))
    for inv in inv_ids:
        arcs.append(('I:' + inv, 'SNK', inv_amount[inv]))
    total, _ = _max_flow(arcs, 'SRC', 'SNK')
    saturated = []
    for k in range(len(inv_ids), -1, -1):
        found = None
        for combo in combinations(inv_ids, k):
            if _circulation_ok(invoices, receipts, total, set(combo), {}, {}):
                found = list(combo)
                break
        if found is not None:
            saturated = found
            break
    fixed = {}
    for r in receipts:
        for inv in r['candidate_invoices']:
            key = (r['bank_id'], inv)
            hi = min(r['amount_cents'], inv_amount[inv])
            lo, span = 0, hi
            while lo < span:
                mid = (lo + span) // 2
                if _circulation_ok(invoices, receipts, total, set(saturated), fixed, {key: mid}):
                    span = mid
                else:
                    lo = mid + 1
            fixed[key] = lo
    allocations = [{'bank_id': rid, 'invoice_id': inv, 'amount_cents': amount}
                   for (rid, inv), amount in sorted(fixed.items()) if amount > 0]
    paid = {inv: 0 for inv in inv_ids}
    per_receipt = {r['bank_id']: 0 for r in receipts}
    for a in allocations:
        paid[a['invoice_id']] += a['amount_cents']
        per_receipt[a['bank_id']] += a['amount_cents']
    suspense = [{'bank_id': r['bank_id'], 'amount_cents': r['amount_cents'] - per_receipt[r['bank_id']]}
                for r in receipts if r['amount_cents'] - per_receipt[r['bank_id']] > 0]
    return {
        'allocations': allocations,
        'suspense': suspense,
        'cleared_cents': sum(a['amount_cents'] for a in allocations),
        'suspense_cents': sum(s['amount_cents'] for s in suspense),
        'invoices': [{'invoice_id': inv, 'amount_cents': inv_amount[inv], 'paid_cents': paid[inv],
                      'status': 'cleared' if paid[inv] == inv_amount[inv]
                                else ('partial' if paid[inv] else 'open')}
                     for inv in inv_ids],
    }


# ---------------------------------------------------------------------------
# Settlement lifecycle: settle -> payout -> (reversal) -> report
# ---------------------------------------------------------------------------

def _load_case(db, case_id):
    row = db.execute('SELECT payload FROM settlement_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError('settlement_case_unknown')
    return json.loads(row['payload'])


def _validate_case(payload):
    doc = payload['doc']
    invoices = doc['invoices']
    inv_ids = [i['id'] for i in invoices]
    if len(set(inv_ids)) != len(inv_ids):
        raise ValueError('settlement_invoice_duplicate')
    for invoice in invoices:
        if not isinstance(invoice['amount_cents'], int) or invoice['amount_cents'] <= 0:
            raise ValueError('settlement_invoice_amount_invalid')
    for r in payload['bank_receipts']:
        if not isinstance(r['amount_cents'], int) or r['amount_cents'] <= 0:
            raise ValueError('bank_amount_invalid')
        if set(r.get('candidate_invoices', [])) - set(inv_ids):
            raise ValueError('settlement_candidate_unknown')
    return doc


def _match_digest(payload, result):
    return _sha(_canon({
        'fixture_sha256': payload['fixture_sha256'],
        'bank_sha256': payload['bank_sha256'],
        'invoices': [[i['id'], i['amount_cents']] for i in payload['doc']['invoices']],
        'allocations': [[a['bank_id'], a['invoice_id'], a['amount_cents']] for a in result['allocations']],
        'suspense': [[s['bank_id'], s['amount_cents']] for s in result['suspense']],
    }))


def _ledger(db, case_id, kind, ref, amount, detail, now):
    db.execute('INSERT INTO settlement_ledger(case_id,kind,ref,amount_cents,detail,created_at) '
               'VALUES (?,?,?,?,?,?)', (case_id, kind, ref, amount, _canon(detail), now))


def _active_view(db, case_id, payload):
    """Live clearing state: active allocations, reversed serials, suspense."""
    allocs = [dict(r) for r in db.execute(
        "SELECT bank_id,invoice_id,amount_cents FROM settlement_alloc "
        "WHERE case_id=? AND status='active' ORDER BY bank_id,invoice_id", (case_id,))]
    reversed_ids = {r['ref'] for r in db.execute(
        "SELECT ref FROM settlement_ledger WHERE case_id=? AND kind='reversal'", (case_id,))}
    per_receipt = {}
    for a in allocs:
        per_receipt[a['bank_id']] = per_receipt.get(a['bank_id'], 0) + a['amount_cents']
    suspense = []
    for r in payload['bank_receipts']:
        if r['bank_id'] in reversed_ids:
            continue
        rest = r['amount_cents'] - per_receipt.get(r['bank_id'], 0)
        if rest > 0:
            suspense.append({'bank_id': r['bank_id'], 'amount_cents': rest})
    cleared = sum(a['amount_cents'] for a in allocs)
    return {'allocations': allocs, 'reversed': sorted(reversed_ids),
            'suspense': suspense, 'cleared_cents': cleared,
            'suspense_cents': sum(s['amount_cents'] for s in suspense)}


def _invoice_status(payload, allocs):
    paid = {}
    for a in allocs:
        paid[a['invoice_id']] = paid.get(a['invoice_id'], 0) + a['amount_cents']
    out = []
    for i in sorted(payload['doc']['invoices'], key=lambda x: x['id']):
        p = paid.get(i['id'], 0)
        out.append({'invoice_id': i['id'], 'subject': i['subject'],
                    'amount_cents': i['amount_cents'], 'paid_cents': p,
                    'status': 'cleared' if p == i['amount_cents'] else ('partial' if p else 'open')})
    return out


def _settlement_summary(db, case_id, payload):
    state = db.execute('SELECT * FROM settlement_state WHERE case_id=?', (case_id,)).fetchone()
    view = _active_view(db, case_id, payload)
    return {'case_id': case_id, 'match_digest': state['match_digest'],
            'cleared_cents': view['cleared_cents'], 'suspense_cents': view['suspense_cents'],
            'allocations': view['allocations'], 'suspense': view['suspense'],
            'reversed': view['reversed'],
            'invoices': _invoice_status(payload, view['allocations'])}


def settle(db_path, case_id):
    """Globally optimal clearing of the frozen bank receipts against invoices.

    Idempotent: re-running recomputes the deterministic optimum and compares
    the frozen match digest. A bank serial may be settled in exactly one case.
    """
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            payload = _load_case(db, case_id)
            _validate_case(payload)
            result = solve_matching(payload['doc']['invoices'], payload['bank_receipts'])
            digest = _match_digest(payload, result)
            old = db.execute('SELECT match_digest FROM settlement_state WHERE case_id=?',
                             (case_id,)).fetchone()
            if old is not None:
                if old['match_digest'] != digest:
                    raise ValueError('settlement_state_conflict')
                db.commit()
                return _settlement_summary(db, case_id, payload)
            for r in payload['bank_receipts']:
                try:
                    db.execute('INSERT INTO bank_serial_lock VALUES (?,?)', (r['bank_id'], case_id))
                except sqlite3.IntegrityError:
                    raise ValueError('bank_receipt_already_settled')
            now = _now()
            db.execute('INSERT INTO settlement_state VALUES (?,?,?,?,?)',
                       (case_id, digest, result['cleared_cents'], result['suspense_cents'], now))
            evidence = {'bank_sha256': payload['bank_sha256'],
                        'fixture_sha256': payload['fixture_sha256']}
            for r in sorted(payload['bank_receipts'], key=lambda x: x['bank_id']):
                _ledger(db, case_id, 'receipt', r['bank_id'], r['amount_cents'],
                        {'payer': r['payer'], 'at': r.get('at'), **evidence}, now)
            for a in result['allocations']:
                db.execute('INSERT INTO settlement_alloc VALUES (?,?,?,?,?)',
                           (case_id, a['bank_id'], a['invoice_id'], a['amount_cents'], 'active'))
                payer = next(r['payer'] for r in payload['bank_receipts']
                             if r['bank_id'] == a['bank_id'])
                _ledger(db, case_id, 'allocation', f"{a['bank_id']}->{a['invoice_id']}",
                        a['amount_cents'], {'payer': payer, **evidence}, now)
            for s in result['suspense']:
                _ledger(db, case_id, 'suspense', s['bank_id'], s['amount_cents'],
                        {'reason': 'unmatched_balance', **evidence}, now)
            db.commit()
            return _settlement_summary(db, case_id, payload)
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Payout under one frozen split contract
# ---------------------------------------------------------------------------

def split_payments(doc, contract, cleared_cents):
    """Payee split for one frozen contract; parts always sum to cleared_cents."""
    beneficiaries = doc['beneficiaries']
    if contract == 'merchant_gross':
        splits = [(beneficiaries['merchant'], cleared_cents)]
    elif contract == 'carrier_commission':
        commission = min(int(doc['commission_cents']), cleared_cents)
        splits = [(beneficiaries['carrier'], commission),
                  (beneficiaries['merchant'], cleared_cents - commission)]
    else:
        raise ValueError('settlement_contract_unknown')
    return sorted((p, a) for p, a in splits if a > 0)


def _lock_digest(payload, contract, allocs, splits):
    return _sha(_canon({
        'bank_sha256': payload['bank_sha256'],
        'fixture_sha256': payload['fixture_sha256'],
        'contract': contract,
        'invoices': [[i['id'], i['amount_cents'], i.get('message_id')]
                     for i in sorted(payload['doc']['invoices'], key=lambda x: x['id'])],
        'allocations': [[a['bank_id'], a['invoice_id'], a['amount_cents']] for a in allocs],
        'payees': [[p, a, _sha(p)] for p, a in splits],
    }))


def _payment_nonce(case_id, contract, payee, amount, lock_digest):
    return 'PAY-' + _sha(_canon(['payment', case_id, contract, payee, amount, lock_digest]))[:32]


def _payment_digest(case_id, contract, payee, amount, lock_digest):
    return _sha(_canon({'case_id': case_id, 'contract': contract, 'payee': payee,
                        'payee_digest': _sha(payee), 'amount_cents': amount,
                        'lock_digest': lock_digest}))


def _mark_order(db, case_id, nonce, status, bank_ref, detail):
    db.execute('BEGIN IMMEDIATE')
    try:
        cur = db.execute("UPDATE payment_order SET status=?,bank_ref=?,updated_at=? "
                         "WHERE nonce=? AND status!='confirmed'", (status, bank_ref, _now(), nonce))
        if cur.rowcount and status == 'confirmed':
            order = db.execute('SELECT * FROM payment_order WHERE nonce=?', (nonce,)).fetchone()
            _ledger(db, case_id, 'payout', nonce, order['amount_cents'],
                    {'payee': order['payee'], 'bank_ref': bank_ref, **detail}, _now())
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _execute_order(db, case_id, bank, order):
    nonce = order['nonce']
    rec = bank.status(nonce)
    if rec is not None and rec['status'] == 'confirmed':
        _mark_order(db, case_id, nonce, 'confirmed', rec['bank_ref'], {'via': 'status_query'})
        return
    try:
        rec = bank.submit(nonce, order['payee'], order['amount_cents'], order['digest'])
        _mark_order(db, case_id, nonce, 'confirmed', rec['bank_ref'], {'via': 'submit'})
    except AckLost:
        rec = bank.status(nonce)
        if rec is not None and rec['status'] == 'confirmed':
            _mark_order(db, case_id, nonce, 'confirmed', rec['bank_ref'],
                        {'via': 'ack_lost_recovered'})
        else:
            _mark_order(db, case_id, nonce, 'unknown', None, {'via': 'ack_lost_unconfirmed'})
    except TransferRejected:
        _mark_order(db, case_id, nonce, 'failed', None, {'via': 'bank_rejected'})


def payout(db_path, case_id, contract, bank):
    """Execute the frozen split contract as irrevocable bank payment orders.

    The first payout run locks bank serials, invoice versions, contract and
    payee digests into one lock digest; a later run under a different contract
    — or after the clearing changed under a confirmed payout (for example a
    receipt reversal) — is refused, and recovery rides on explicit
    reversal/recovery_due ledger entries instead. Payment nonces are stable,
    the bank is idempotent on them, and lost acknowledgements are recovered
    by querying the bank's final state.
    """
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            payload = _load_case(db, case_id)
            doc = _validate_case(payload)
            if db.execute('SELECT 1 FROM settlement_state WHERE case_id=?', (case_id,)).fetchone() is None:
                raise ValueError('settlement_not_settled')
            if contract not in doc.get('contracts', []):
                raise ValueError('settlement_contract_unknown')
            view = _active_view(db, case_id, payload)
            cleared = view['cleared_cents']
            splits = split_payments(doc, contract, cleared)
            lock = _lock_digest(payload, contract, view['allocations'], splits)
            run = db.execute('SELECT * FROM payout_run WHERE case_id=?', (case_id,)).fetchone()
            if run is None:
                try:
                    db.execute('INSERT INTO payout_run VALUES (?,?,?,?)',
                               (case_id, contract, lock, _now()))
                except sqlite3.IntegrityError:
                    run = db.execute('SELECT * FROM payout_run WHERE case_id=?', (case_id,)).fetchone()
            if run is not None and (run['contract'] != contract or run['lock_digest'] != lock):
                raise ValueError('payout_contract_locked')
            now = _now()
            for payee, amount in splits:
                nonce = _payment_nonce(case_id, contract, payee, amount, lock)
                digest = _payment_digest(case_id, contract, payee, amount, lock)
                db.execute('INSERT OR IGNORE INTO payment_order VALUES (?,?,?,?,?,?,?,?)',
                           (nonce, case_id, payee, amount, 'prepared', None, digest, now))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        pending = [dict(r) for r in db.execute(
            "SELECT * FROM payment_order WHERE case_id=? AND status!='confirmed' ORDER BY payee",
            (case_id,))]
        for order in pending:
            _execute_order(db, case_id, bank, order)
        return _payout_summary(db, case_id, payload, contract)
    finally:
        db.close()


def _payout_summary(db, case_id, payload, contract):
    run = db.execute('SELECT * FROM payout_run WHERE case_id=?', (case_id,)).fetchone()
    orders = [dict(r) for r in db.execute(
        'SELECT * FROM payment_order WHERE case_id=? ORDER BY payee', (case_id,))]
    view = _active_view(db, case_id, payload)
    paid = sum(o['amount_cents'] for o in orders if o['status'] == 'confirmed')
    return {'case_id': case_id, 'contract': run['contract'] if run else contract,
            'lock_digest': run['lock_digest'] if run else None,
            'cleared_cents': view['cleared_cents'], 'suspense_cents': view['suspense_cents'],
            'paid_cents': paid,
            'orders': [{'nonce': o['nonce'], 'payee': o['payee'], 'amount_cents': o['amount_cents'],
                        'status': o['status'], 'bank_ref': o['bank_ref']} for o in orders]}


# ---------------------------------------------------------------------------
# Receipt reversal: explicit reversal/compensation entries, never rollback
# ---------------------------------------------------------------------------

def reverse_receipt(db_path, case_id, bank_id, reason=''):
    """Reverse a bank receipt after settlement (refund/clawback).

    Cash already paid out cannot be recovered by rolling back the database, so
    the reversal writes explicit ledger entries: a `reversal` for the clawed
    receipt and a `recovery_due` compensation entry for any amount that was
    already paid out on those funds.
    """
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            payload = _load_case(db, case_id)
            receipts = {r['bank_id']: r for r in payload['bank_receipts']}
            if bank_id not in receipts:
                raise ValueError('settlement_bank_unknown')
            if db.execute('SELECT 1 FROM settlement_state WHERE case_id=?', (case_id,)).fetchone() is None:
                raise ValueError('settlement_not_settled')
            if db.execute("SELECT 1 FROM settlement_ledger WHERE case_id=? AND kind='reversal' "
                          'AND ref=?', (case_id, bank_id)).fetchone() is not None:
                raise ValueError('settlement_receipt_already_reversed')
            now = _now()
            db.execute("UPDATE settlement_alloc SET status='reversed' "
                       "WHERE case_id=? AND bank_id=? AND status='active'", (case_id, bank_id))
            receipt = receipts[bank_id]
            _ledger(db, case_id, 'reversal', bank_id, receipt['amount_cents'],
                    {'payer': receipt['payer'], 'reason': reason,
                     'bank_sha256': payload['bank_sha256']}, now)
            view = _active_view(db, case_id, payload)
            paid = db.execute("SELECT COALESCE(SUM(amount_cents),0) AS t FROM payment_order "
                              "WHERE case_id=? AND status='confirmed'", (case_id,)).fetchone()['t']
            recorded = db.execute("SELECT COALESCE(SUM(amount_cents),0) AS t FROM settlement_ledger "
                                  "WHERE case_id=? AND kind='recovery_due'", (case_id,)).fetchone()['t']
            recovery = paid - view['cleared_cents'] - recorded
            if recovery > 0:
                _ledger(db, case_id, 'recovery_due', bank_id, recovery,
                        {'reason': reason, 'note': 'paid out before reversal; recovery required'},
                        now)
            db.execute('UPDATE settlement_state SET cleared_cents=?,suspense_cents=? WHERE case_id=?',
                       (view['cleared_cents'], view['suspense_cents'], case_id))
            db.commit()
            return _settlement_summary(db, case_id, payload)
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Settlement and fund-conservation report (JSON + Markdown)
# ---------------------------------------------------------------------------

def _conservation(payload, view, orders, ledger_rows, bank, has_payout_run):
    receipts_active = sum(r['amount_cents'] for r in payload['bank_receipts']
                          if r['bank_id'] not in set(view['reversed']))
    allocated = view['cleared_cents']
    suspense = view['suspense_cents']
    paid = sum(o['amount_cents'] for o in orders if o['status'] == 'confirmed')
    outstanding = sum(o['amount_cents'] for o in orders if o['status'] != 'confirmed')
    recovery = sum(r['amount_cents'] for r in ledger_rows if r['kind'] == 'recovery_due')
    checks = {
        'receipts_equal_allocations_plus_suspense': receipts_active == allocated + suspense,
    }
    if has_payout_run:
        # locked payment instructions cover the clearing; a reversal after
        # payout turns the already-paid excess into recovery_due
        checks['orders_cover_cleared_plus_recovery'] = (
            sum(o['amount_cents'] for o in orders) == allocated + recovery)
    if bank is not None:
        checks['bank_issued_equals_confirmed_payouts'] = bank.total_issued() == paid
    checks['ok'] = all(checks.values())
    return {'receipts_active_cents': receipts_active, 'allocated_cents': allocated,
            'suspense_cents': suspense, 'paid_cents': paid, 'outstanding_cents': outstanding,
            'recovery_due_cents': recovery, 'checks': checks}


def report(db_path, case_id, out, bank=None):
    db = connect(db_path)
    try:
        payload = _load_case(db, case_id)
        view = _active_view(db, case_id, payload)
        run = db.execute('SELECT * FROM payout_run WHERE case_id=?', (case_id,)).fetchone()
        orders = [dict(r) for r in db.execute(
            'SELECT * FROM payment_order WHERE case_id=? ORDER BY payee', (case_id,))]
        ledger_rows = [dict(r) for r in db.execute(
            'SELECT kind,ref,amount_cents,detail,created_at FROM settlement_ledger '
            'WHERE case_id=? ORDER BY id', (case_id,))]
        conservation = _conservation(payload, view, orders, ledger_rows, bank,
                                     has_payout_run=run is not None)
        per_receipt = {}
        for a in view['allocations']:
            per_receipt.setdefault(a['bank_id'], []).append(a)
        data = {
            'case_id': case_id,
            'fixture_sha256': payload['fixture_sha256'],
            'bank_sha256': payload['bank_sha256'],
            'contract': run['contract'] if run else None,
            'lock_digest': run['lock_digest'] if run else None,
            'invoices': _invoice_status(payload, view['allocations']),
            'receipts': [{'bank_id': r['bank_id'], 'payer': r['payer'],
                          'amount_cents': r['amount_cents'],
                          'status': 'reversed' if r['bank_id'] in set(view['reversed']) else 'active',
                          'allocations': per_receipt.get(r['bank_id'], []),
                          'suspense_cents': next((s['amount_cents'] for s in view['suspense']
                                                  if s['bank_id'] == r['bank_id']), 0)}
                         for r in sorted(payload['bank_receipts'], key=lambda x: x['bank_id'])],
            'allocations': [{**a, 'evidence': {'bank_sha256': payload['bank_sha256'],
                                               'fixture_sha256': payload['fixture_sha256'],
                                               'payer': next(r['payer'] for r in payload['bank_receipts']
                                                             if r['bank_id'] == a['bank_id'])}}
                            for a in view['allocations']],
            'suspense': view['suspense'],
            'payments': [{'nonce': o['nonce'], 'payee': o['payee'],
                          'amount_cents': o['amount_cents'], 'status': o['status'],
                          'bank_ref': o['bank_ref']} for o in orders],
            'ledger': [{**r, 'detail': json.loads(r['detail'])} for r in ledger_rows],
            'conservation': conservation,
        }
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        target.with_suffix('.md').write_text(_markdown(data), encoding='utf-8')
        return target
    finally:
        db.close()


def _markdown(data):
    lines = [f"# 结算报告 {data['case_id']}", '']
    lines.append(f"- 案件 SHA：fixture `{data['fixture_sha256'][:16]}…` 银行 `{data['bank_sha256'][:16]}…`")
    if data['contract']:
        lines.append(f"- 分账合同：`{data['contract']}` 锁定摘要 `{data['lock_digest'][:16]}…`")
    lines += ['', '## 到账与核销', '',
              '| 流水 | 付款人 | 金额(分) | 状态 | 核销去向 | 待认款(分) |',
              '| --- | --- | ---: | --- | --- | ---: |']
    for r in data['receipts']:
        dest = '；'.join(f"{a['bank_id']}→{a['invoice_id']} {a['amount_cents']}"
                         for a in r['allocations']) or '—'
        lines.append(f"| {r['bank_id']} | {r['payer']} | {r['amount_cents']} | {r['status']} "
                     f"| {dest} | {r['suspense_cents']} |")
    lines += ['', '## 票据状态', '',
              '| 票据 | 服务对象 | 应收(分) | 已核(分) | 状态 |', '| --- | --- | ---: | ---: | --- |']
    for i in data['invoices']:
        lines.append(f"| {i['invoice_id']} | {i['subject']} | {i['amount_cents']} "
                     f"| {i['paid_cents']} | {i['status']} |")
    lines += ['', '## 分账与付款指令', '',
              '| 收款方 | 金额(分) | nonce | 状态 | 银行回执 |', '| --- | ---: | --- | --- | --- |']
    for p in data['payments']:
        lines.append(f"| {p['payee']} | {p['amount_cents']} | `{p['nonce']}` | {p['status']} "
                     f"| {p['bank_ref'] or '—'} |")
    if not data['payments']:
        lines.append('| — | 0 | — | 未发款 | — |')
    lines += ['', '## 待认款', '']
    if data['suspense']:
        for s in data['suspense']:
            lines.append(f"- 流水 {s['bank_id']} 冻结 {s['amount_cents']} 分")
    else:
        lines.append('- 无')
    reversals = [e for e in data['ledger'] if e['kind'] in ('reversal', 'recovery_due')]
    lines += ['', '## 冲正与补差', '']
    if reversals:
        for e in reversals:
            lines.append(f"- {e['kind']} 流水 {e['ref']}：{e['amount_cents']} 分"
                         f"（{e['detail'].get('reason') or '未注明'}）")
    else:
        lines.append('- 无')
    c = data['conservation']
    lines += ['', '## 资金守恒', '',
              f"- 有效到账 {c['receipts_active_cents']} = 已核销 {c['allocated_cents']} "
              f"+ 待认款 {c['suspense_cents']}",
              f"- 付款指令合计 {c['paid_cents'] + c['outstanding_cents']}（已付 {c['paid_cents']}，"
              f"挂账 {c['outstanding_cents']}）；有效核销 {c['allocated_cents']} "
              f"+ 待追回 {c['recovery_due_cents']}"]
    for name, ok in c['checks'].items():
        if name != 'ok':
            lines.append(f"- {'✅' if ok else '❌'} `{name}`")
    lines.append(f"- 总体：{'✅ 守恒' if c['checks']['ok'] else '❌ 不守恒'}")
    return '\n'.join(lines) + '\n'
