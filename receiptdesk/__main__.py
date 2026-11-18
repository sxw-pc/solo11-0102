import argparse
import json
import tempfile
from pathlib import Path

from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import (disclosure_ledger, export, publish_disclosure,
                     recover_disclosures)
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


def disclosure_demo(workdir=None):
    """Publish the external-safe fixture summary, then show the ledger."""
    root = Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-disclosure-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'channel_disclosure_config.json')
    import_file(db, base / 'channel_disclosure_events.jsonl')
    request = json.loads((base / 'channel_disclosure_requests.json').read_text(encoding='utf-8'))
    result = publish_disclosure(db, request, 'minimum_safe_coarsening')
    return result, disclosure_ledger(db, request['audience'])


def main():
    p = argparse.ArgumentParser(description='票据投递回执台 — 离线证据与业务决策')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--workdir')
    a = sub.add_parser('ingest')
    a.add_argument('--db', required=True)
    a.add_argument('--config', required=True)
    a.add_argument('events')
    e = sub.add_parser('inspect')
    e.add_argument('--db', required=True)
    r = sub.add_parser('report')
    r.add_argument('--db', required=True)
    r.add_argument('--out', required=True)

    # External path: no evidence, decisions or source paths are reachable here.
    x = sub.add_parser('disclose', help='按策略发布外部渠道汇总或记录拒绝')
    x.add_argument('--db', required=True)
    x.add_argument('--request', required=True)
    x.add_argument('--policy', choices=('deny_unsafe', 'minimum_safe_coarsening'))
    x.add_argument('--out-dir')
    l = sub.add_parser('disclosure-ledger', help='查看某一受众的发布台账')
    l.add_argument('--db', required=True)
    l.add_argument('--audience', required=True)
    g = sub.add_parser('recover-disclosures', help='修复中断的文件/台账发布')
    g.add_argument('--db', required=True)
    g.add_argument('--out-dir')
    dd = sub.add_parser('disclosure-demo')
    dd.add_argument('--workdir')

    args = p.parse_args()
    try:
        if args.cmd == 'demo':
            print(demo(args.workdir))
        elif args.cmd == 'ingest':
            freeze_config(args.db, args.config)
            print(import_file(args.db, args.events))
        elif args.cmd == 'inspect':
            print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
        elif args.cmd == 'report':
            print(export(args.db, args.out))
        elif args.cmd == 'disclose':
            print(json.dumps(publish_disclosure(args.db, args.request, args.policy, args.out_dir),
                             ensure_ascii=False, indent=2))
        elif args.cmd == 'disclosure-ledger':
            print(json.dumps(disclosure_ledger(args.db, args.audience),
                             ensure_ascii=False, indent=2))
        elif args.cmd == 'recover-disclosures':
            print(json.dumps(recover_disclosures(args.db, args.out_dir),
                             ensure_ascii=False, indent=2))
        elif args.cmd == 'disclosure-demo':
            result, ledger = disclosure_demo(args.workdir)
            print(json.dumps({'result': result, 'ledger': ledger},
                             ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError) as exc:
        # Contract violations are expected operator errors; report the code
        # instead of an internal traceback.
        raise SystemExit(f'error: {exc}')


if __name__ == '__main__':
    main()
