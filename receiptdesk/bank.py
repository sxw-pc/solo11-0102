"""Local synthetic bank endpoint: irrevocable transfers with idempotent nonces.

The bank keeps its own SQLite database, deliberately separate from the
settlement ledger: once a transfer is confirmed, rolling back the settlement
database can never pull the cash back. Clients must therefore use stable
payment nonces, recover lost acknowledgements through ``status`` and record
explicit reversal/compensation entries instead of relying on rollback.

Test hooks: ``refuse_payees`` rejects transfers to the listed payees (no bank
state changes, so a later retry may succeed) and ``drop_acks`` processes the
transfer but raises :class:`AckLost` instead of returning the record.
"""
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer(
  nonce TEXT PRIMARY KEY,
  payee TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  bank_ref TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account(payee TEXT PRIMARY KEY,balance_cents INTEGER NOT NULL);
"""


class AckLost(Exception):
    """The transfer was processed but the acknowledgement was lost in transit."""


class TransferRejected(Exception):
    """The bank refused the transfer (for example a frozen payee account)."""


class NonceConflict(Exception):
    """The nonce was already used with different transfer details."""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Bank:
    """Synthetic bank endpoint backed by its own SQLite database."""

    def __init__(self, path=':memory:'):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self.refuse_payees = set()
        self.drop_acks = False

    def close(self):
        self._db.close()

    def submit(self, nonce, payee, amount_cents, digest):
        """Submit an irrevocable transfer; idempotent on ``nonce``."""
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError('bank_amount_invalid')
        with self._lock:
            old = self._db.execute('SELECT * FROM transfer WHERE nonce=?', (nonce,)).fetchone()
            if old is not None:
                if (old['payee'], old['amount_cents'], old['digest']) != (payee, amount_cents, digest):
                    raise NonceConflict(nonce)
                record = dict(old)
            else:
                if payee in self.refuse_payees:
                    raise TransferRejected(payee)
                ref = 'BK-' + hashlib.sha256(nonce.encode()).hexdigest()[:16]
                with self._db:
                    self._db.execute(
                        'INSERT INTO transfer VALUES (?,?,?,?,?,?,?)',
                        (nonce, payee, amount_cents, digest, 'confirmed', ref, _now()))
                    self._db.execute(
                        'INSERT INTO account VALUES (?,?) '
                        'ON CONFLICT(payee) DO UPDATE SET balance_cents=balance_cents+?',
                        (payee, amount_cents, amount_cents))
                record = {'nonce': nonce, 'payee': payee, 'amount_cents': amount_cents,
                          'digest': digest, 'status': 'confirmed', 'bank_ref': ref}
            if self.drop_acks:
                raise AckLost(nonce)
            return record

    def status(self, nonce):
        """Final bank-side state of a transfer, or None if never processed."""
        with self._lock:
            row = self._db.execute('SELECT * FROM transfer WHERE nonce=?', (nonce,)).fetchone()
            return dict(row) if row is not None else None

    def balance(self, payee):
        with self._lock:
            row = self._db.execute('SELECT balance_cents FROM account WHERE payee=?', (payee,)).fetchone()
            return row['balance_cents'] if row is not None else 0

    def total_issued(self):
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS t FROM transfer WHERE status='confirmed'"
            ).fetchone()
            return row['t']

    def transfers(self):
        with self._lock:
            return [dict(r) for r in self._db.execute('SELECT * FROM transfer ORDER BY nonce')]
