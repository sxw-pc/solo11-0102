import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .packet import CONTRACTS,allocate
from .rules import evaluate
from .report import export
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
    k=sub.add_parser('packet');k.add_argument('--db',required=True);k.add_argument('--signing',required=True)
    k.add_argument('--files');k.add_argument('--contract',choices=CONTRACTS,default=CONTRACTS[0])
    k.add_argument('--config');k.add_argument('--out')
    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='packet':
        if args.config: freeze_config(args.db,args.config)
        result=allocate(args.db,args.signing,files_dir=args.files,contract=args.contract)
        text=json.dumps(result,ensure_ascii=False,indent=2)
        if args.out: Path(args.out).write_text(text,encoding='utf-8')
        print(text)
if __name__=='__main__':main()
