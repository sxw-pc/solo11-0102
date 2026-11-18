import argparse, json, tempfile
from pathlib import Path

from .ingest import DEFAULT_CHUNK_SIZE, SOURCE_CONTRACTS, freeze_config, import_file
from .rules import evaluate
from .report import export
from .store import connect


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    return export(db, root / 'report.json')


def audit(db_path):
    """Report which source snapshot identity was imported and under which contract."""
    db = connect(db_path)
    rows = db.execute(
        'SELECT s.id AS source_id,s.path,s.sha256,LENGTH(s.raw) AS raw_bytes,'
        'a.snapshot_id,a.contract,a.attempts,COALESCE(a.resumed,0) AS resumed,'
        'a.path_changed,a.chunk_size,a.line_count,a.imported,a.parse_errors,a.created_at '
        'FROM source s LEFT JOIN import_audit a ON a.source_id=s.id ORDER BY s.id'
    )
    data = [dict(row) for row in rows]
    db.close()
    return data


def main():
    p = argparse.ArgumentParser(description='票据投递回执台 — 离线证据与业务决策')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--workdir')
    a = sub.add_parser('ingest')
    a.add_argument('--db', required=True)
    a.add_argument('--config', required=True)
    a.add_argument('--contract', choices=SOURCE_CONTRACTS, default='freeze_on_open',
                   help='source snapshot selection contract for a path replaced mid-import')
    a.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
                   help='fixed streaming read size in bytes')
    a.add_argument('events')
    au = sub.add_parser('audit')
    au.add_argument('--db', required=True)
    e = sub.add_parser('inspect')
    e.add_argument('--db', required=True)
    r = sub.add_parser('report')
    r.add_argument('--db', required=True)
    r.add_argument('--out', required=True)
    args = p.parse_args()
    if args.cmd == 'demo':
        print(demo(args.workdir))
    elif args.cmd == 'ingest':
        freeze_config(args.db, args.config)
        print(json.dumps(import_file(args.db, args.events, contract=args.contract,
                                     chunk_size=args.chunk_size), ensure_ascii=False))
    elif args.cmd == 'audit':
        print(json.dumps(audit(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'inspect':
        print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report':
        print(export(args.db, args.out))


if __name__ == '__main__':
    main()
