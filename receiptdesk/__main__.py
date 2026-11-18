import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate
from .report import export
from .store import connect, hash_bytes
from . import retention

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
    h=sub.add_parser('hold',help='shared_source_hold：授权整源短期保全')
    h.add_argument('--db',required=True);h.add_argument('--request-id',required=True)
    h.add_argument('--source-sha256',required=True);h.add_argument('--subject',required=True)
    h.add_argument('--credential',required=True);h.add_argument('--reason',required=True)
    x=sub.add_parser('erase',help='subject_erasure_with_rebase / 共源保全待执行')
    x.add_argument('--db',required=True);x.add_argument('--request-id',required=True)
    x.add_argument('--source-sha256',required=True);x.add_argument('--subject',required=True)
    x.add_argument('--contract',required=True,choices=retention.CONTRACTS)
    x.add_argument('--credential-version',required=True);x.add_argument('--reason',required=True)
    rel=sub.add_parser('release-hold',help='解除整源保全并串行重处理待办擦除')
    rel.add_argument('--db',required=True);rel.add_argument('--request-id',required=True)
    rel.add_argument('--reason',required=True)
    st=sub.add_parser('retention-status',help='查看内部保留/擦除台账')
    st.add_argument('--db',required=True);st.add_argument('--request-id')
    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='hold':
        cred=Path(args.credential)
        print(json.dumps(retention.submit_shared_hold(
            args.db,request_id=args.request_id,source_sha256=args.source_sha256,
            hold_subject=args.subject,credential_path=cred,
            credential_sha256=hash_bytes(cred.read_bytes()),reason=args.reason),
            ensure_ascii=False,indent=2))
    elif args.cmd=='erase':
        print(json.dumps(retention.request_erasure(
            args.db,request_id=args.request_id,source_sha256=args.source_sha256,
            subject=args.subject,contract=args.contract,
            credential_version=args.credential_version,reason=args.reason),
            ensure_ascii=False,indent=2))
    elif args.cmd=='release-hold':
        print(json.dumps(retention.release_hold(
            args.db,request_id=args.request_id,reason=args.reason),
            ensure_ascii=False,indent=2))
    elif args.cmd=='retention-status':
        if args.request_id:
            print(json.dumps(retention.request_status(args.db,args.request_id),
                             ensure_ascii=False,indent=2))
        else:
            print(json.dumps(retention.list_requests(args.db),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
