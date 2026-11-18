import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate
from .report import export
from .paper_waste import register, state as paper_state, set_route_authorization
from .campaign import create_campaign, run_campaign, preflight, plan, CONTRACTS
from .handoff import build_report
from .store import connect

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db)
    return export(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='票据投递回执台 — 离线证据、业务决策与保密校样纸处置')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)

    pw=sub.add_parser('paper-register',help='冻结废纸案：校验 queued 来源与包质量守恒')
    pw.add_argument('--db',required=True);pw.add_argument('--case-id',required=True);pw.add_argument('fixture')

    ps=sub.add_parser('paper-state',help='查看包/箱实况与处理厂授权')
    ps.add_argument('--db',required=True);ps.add_argument('--case-id',required=True)

    pf=sub.add_parser('paper-preflight',help='重验 queued 来源摘要、冻结记录与质量守恒')
    pf.add_argument('--db',required=True);pf.add_argument('--case-id',required=True)

    pp=sub.add_parser('paper-plan',help='求全局精确装箱/路线计划（不落库，仅打印）')
    pp.add_argument('--db',required=True);pp.add_argument('--case-id',required=True)
    pp.add_argument('--contract',required=True,choices=CONTRACTS)

    pc=sub.add_parser('paper-campaign',help='创建并持久化可恢复的整批处置战役')
    pc.add_argument('--db',required=True);pc.add_argument('--case-id',required=True)
    pc.add_argument('--plan-id',required=True);pc.add_argument('--contract',required=True,choices=CONTRACTS)

    pr=sub.add_parser('paper-run',help='经真实端点执行/恢复战役，持久化每步意图与结果')
    pr.add_argument('--db',required=True);pr.add_argument('--case-id',required=True);pr.add_argument('--plan-id',required=True)

    ph=sub.add_parser('paper-handoff',help='导出 JSON 实物交接与质量守恒报告')
    ph.add_argument('--db',required=True);ph.add_argument('--plan-id',required=True);ph.add_argument('--out',required=True)

    pa=sub.add_parser('paper-authorize',help='冻结/吊销处理厂路线授权')
    pa.add_argument('--db',required=True);pa.add_argument('--case-id',required=True)
    pa.add_argument('--route',required=True,choices=('recover','thermal'))
    pa.add_argument('--enabled',required=True,choices=('true','false'))

    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='paper-register': print(json.dumps(register(args.db,args.case_id,args.fixture),ensure_ascii=False))
    elif args.cmd=='paper-state': print(json.dumps(paper_state(args.db,args.case_id),ensure_ascii=False,indent=2))
    elif args.cmd=='paper-preflight': print(json.dumps(preflight(args.db,args.case_id),ensure_ascii=False,indent=2))
    elif args.cmd=='paper-plan':
        db=connect(args.db)
        row=db.execute('SELECT payload FROM paper_case WHERE id=?',(args.case_id,)).fetchone()
        auth={r['route']:bool(r['enabled']) for r in db.execute(
            'SELECT route,enabled FROM paper_authorization WHERE case_id=?',(args.case_id,))}
        db.close()
        if not row: raise ValueError('unknown_paper_case')
        doc=json.loads(row['payload'])
        print(json.dumps(plan(doc['packs'],doc['containers'],args.contract,auth),ensure_ascii=False,indent=2))
    elif args.cmd=='paper-campaign':
        print(json.dumps(create_campaign(args.db,args.case_id,args.plan_id,args.contract),ensure_ascii=False,indent=2))
    elif args.cmd=='paper-run':
        print(json.dumps(run_campaign(args.db,args.case_id,args.plan_id),ensure_ascii=False,indent=2))
    elif args.cmd=='paper-handoff':
        print(build_report(args.db,args.plan_id,args.out))
    elif args.cmd=='paper-authorize':
        print(json.dumps(set_route_authorization(
            args.db,args.case_id,args.route,args.enabled=='true'),ensure_ascii=False))
if __name__=='__main__':main()
