"""Freeze notice identities, invoices and actual synthetic bank receipts; no allocation."""
import hashlib
import json
from pathlib import Path
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
