import argparse
import json
import tempfile
from pathlib import Path

from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import export
from .store import connect
from . import issuance, print_lab


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    return export(db, root / 'report.json')


def label_demo(workdir=None, contract='retain_verified_serial'):
    """End-to-end physical label run on the frozen print fixture.

    The seed prints L100 for J1 with its ACK declared lost, exactly as the
    fixture describes, then the issuance layer reconciles the device, handles
    the L101 jam and issues the remaining labels under the chosen contract.
    """
    root = Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-labels-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'labels.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'print-config.json')
    import_file(db, base / 'print-events.jsonl')
    print_lab.register(db, 'print-01', base / 'print-case.json')
    digest = issuance.layout_digest('J1', 'R1', 'P1', 1)
    print_lab.invoke(db, 'L100', 'J1', 'seed-print-L100', 0,
                     content_sha256=digest, case_id='print-01')
    opened = issuance.open_case(db, 'print-01', contract)
    issuance.run_contract(db, opened['case_id'])
    return issuance.write_report(db, root / 'labels.json', opened['case_id'])


def main():
    p = argparse.ArgumentParser(description='票据投递回执台 — 离线证据与实体标签发行')
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

    ld = sub.add_parser('label-demo', help='运行两套处置合同之一并输出标签报告')
    ld.add_argument('--workdir')
    ld.add_argument('--contract', default='retain_verified_serial',
                    choices=sorted(issuance.CONTRACTS))

    reg = sub.add_parser('label-register')
    reg.add_argument('--db', required=True)
    reg.add_argument('--config', required=True)
    reg.add_argument('--events', required=True)
    reg.add_argument('--print-case', required=True)
    reg.add_argument('--case', default='print-01')

    op = sub.add_parser('label-open')
    op.add_argument('--db', required=True)
    op.add_argument('--print-case', default='print-01')
    op.add_argument('--contract', default='retain_verified_serial',
                    choices=sorted(issuance.CONTRACTS))

    pr = sub.add_parser('label-print')
    pr.add_argument('--db', required=True)
    pr.add_argument('--case', required=True)
    pr.add_argument('--job', required=True)
    pr.add_argument('--operation')

    vo = sub.add_parser('label-void')
    vo.add_argument('--db', required=True)
    vo.add_argument('--case', required=True)
    vo.add_argument('--job', required=True)
    vo.add_argument('--operation')
    vo.add_argument('--reason', default='superseded')

    rc = sub.add_parser('label-recover')
    rc.add_argument('--db', required=True)
    rc.add_argument('--case')

    run = sub.add_parser('label-run')
    run.add_argument('--db', required=True)
    run.add_argument('--case', required=True)

    lr = sub.add_parser('label-report')
    lr.add_argument('--db', required=True)
    lr.add_argument('--out', required=True)
    lr.add_argument('--case')

    args = p.parse_args()
    if args.cmd == 'demo':
        print(demo(args.workdir))
    elif args.cmd == 'ingest':
        freeze_config(args.db, args.config)
        print(import_file(args.db, args.events))
    elif args.cmd == 'inspect':
        print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report':
        print(export(args.db, args.out))
    elif args.cmd == 'label-demo':
        print(label_demo(args.workdir, args.contract))
    elif args.cmd == 'label-register':
        freeze_config(args.db, args.config)
        import_file(args.db, args.events)
        print(print_lab.register(args.db, args.case, args.print_case))
    elif args.cmd == 'label-open':
        print(json.dumps(issuance.open_case(args.db, args.print_case, args.contract),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'label-print':
        print(json.dumps(issuance.print_label(args.db, args.case, args.job,
                                              operation_id=args.operation),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'label-void':
        print(json.dumps(issuance.void_label(args.db, args.case, args.job,
                                             operation_id=args.operation,
                                             reason=args.reason),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'label-recover':
        print(json.dumps(issuance.recover_case(args.db, args.case),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'label-run':
        print(json.dumps(issuance.run_contract(args.db, args.case),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'label-report':
        print(issuance.write_report(args.db, args.out, args.case))


if __name__ == '__main__':
    main()
