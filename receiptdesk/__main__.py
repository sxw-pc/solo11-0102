import argparse,json,tempfile
from pathlib import Path
from .bank import Bank
from .ingest import freeze_config,import_file
from .rules import evaluate
from .report import export
from .settlement import payout,register,report as settlement_report,reverse_receipt,settle
from .store import connect

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db)
    return export(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='票据投递回执台 — 离线证据与业务决策')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    sr=sub.add_parser('settlement-register');sr.add_argument('--db',required=True);sr.add_argument('--case',required=True);sr.add_argument('--fixture',required=True);sr.add_argument('--bank-file',required=True)
    st=sub.add_parser('settle');st.add_argument('--db',required=True);st.add_argument('--case',required=True)
    po=sub.add_parser('payout');po.add_argument('--db',required=True);po.add_argument('--case',required=True);po.add_argument('--contract',required=True);po.add_argument('--bank',required=True)
    rv=sub.add_parser('reverse');rv.add_argument('--db',required=True);rv.add_argument('--case',required=True);rv.add_argument('--bank-id',required=True);rv.add_argument('--reason',default='')
    rep=sub.add_parser('settlement-report');rep.add_argument('--db',required=True);rep.add_argument('--case',required=True);rep.add_argument('--out',required=True);rep.add_argument('--bank')
    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='settlement-register': print(json.dumps(register(args.db,args.case,args.fixture,args.bank_file),ensure_ascii=False))
    elif args.cmd=='settle': print(json.dumps(settle(args.db,args.case),ensure_ascii=False,indent=2))
    elif args.cmd=='payout':
        bank=Bank(args.bank)
        try: print(json.dumps(payout(args.db,args.case,args.contract,bank),ensure_ascii=False,indent=2))
        finally: bank.close()
    elif args.cmd=='reverse': print(json.dumps(reverse_receipt(args.db,args.case,args.bank_id,args.reason),ensure_ascii=False,indent=2))
    elif args.cmd=='settlement-report':
        bank=Bank(args.bank) if args.bank else None
        try: print(settlement_report(args.db,args.case,args.out,bank))
        finally:
            if bank: bank.close()
if __name__=='__main__':main()
