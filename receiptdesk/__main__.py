import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate
from .report import export
from . import field_visit

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db)
    return export(db,root/'report.json')


def field_demo(workdir=None, objective='min_total_travel'):
    """Register the field fixture, plan, walk the planned route, report."""
    root=Path(workdir or tempfile.mkdtemp(prefix='receiptdesk-field-'))
    root.mkdir(parents=True,exist_ok=True)
    db=root/'field.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'field_config.json')
    import_file(db,base/'field_events.jsonl')
    evaluate(db)
    field_visit.register(db,'route-1',base/'field_case.json')
    planned=field_visit.plan(db,'route-1',objective)
    for courier in planned['couriers']:
        for leg in courier['legs']:
            field_visit.move(db,'route-1',leg['destination'],
                             f"demo-{courier['courier_id']}-{leg['sequence']}",
                             courier_id=courier['courier_id'],path=leg['path'])
            if leg['recipient']:
                field_visit.inspect(db,'route-1',leg['recipient'],
                                    f"demo-visit-{leg['recipient']}",
                                    courier_id=courier['courier_id'])
    return field_visit.write_report(db,root/'field_report.json','route-1')


def main():
    p=argparse.ArgumentParser(description='票据投递回执台 — 离线证据与业务决策')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)

    fd=sub.add_parser('field-demo',help='登记现场核验任务、精确派工并走完路线')
    fd.add_argument('--workdir')
    fd.add_argument('--objective',default='min_total_travel',choices=field_visit.OBJECTIVES)

    fr=sub.add_parser('field-register',help='登记受件地点、有向路网与班次')
    fr.add_argument('--db',required=True);fr.add_argument('--case',required=True)
    fr.add_argument('--fixture',required=True)

    fp=sub.add_parser('field-plan',help='按目标生成可复核的精确派工计划')
    fp.add_argument('--db',required=True);fp.add_argument('--case',required=True)
    fp.add_argument('--objective',default='min_total_travel',choices=field_visit.OBJECTIVES)
    fp.add_argument('--out')

    fm=sub.add_parser('field-move',help='沿开放有向道路移动核验员')
    fm.add_argument('--db',required=True);fm.add_argument('--case',required=True)
    fm.add_argument('--courier',default=field_visit.DEFAULT_COURIER)
    fm.add_argument('--to',required=True);fm.add_argument('--via',nargs='*')
    fm.add_argument('--op',required=True)

    fi=sub.add_parser('field-inspect',help='到达受件点后记录现场观察')
    fi.add_argument('--db',required=True);fi.add_argument('--case',required=True)
    fi.add_argument('--courier',default=field_visit.DEFAULT_COURIER)
    fi.add_argument('--recipient',required=True);fi.add_argument('--op',required=True)

    fs=sub.add_parser('field-state',help='读取核验员实况与剩余派工')
    fs.add_argument('--db',required=True);fs.add_argument('--case',required=True)

    fc=sub.add_parser('field-close',help='设置时段封路；可选立即改派剩余路段')
    fc.add_argument('--db',required=True);fc.add_argument('--case',required=True)
    fc.add_argument('--from',dest='origin',required=True)
    fc.add_argument('--to',dest='destination',required=True)
    fc.add_argument('--start',type=int,required=True);fc.add_argument('--end',type=int,required=True)
    fc.add_argument('--closure-id')
    fc.add_argument('--replan',action='store_true')
    fc.add_argument('--objective',default='min_total_travel',choices=field_visit.OBJECTIVES)

    fo=sub.add_parser('field-reopen',help='解除一条时段封路')
    fo.add_argument('--db',required=True);fo.add_argument('--case',required=True)
    fo.add_argument('--closure-id',required=True)

    fv=sub.add_parser('field-recover',help='重启后按已执行动作恢复现场位置')
    fv.add_argument('--db',required=True);fv.add_argument('--case',required=True)

    fy=sub.add_parser('field-sync',help='核对任务资格快照')
    fy.add_argument('--db',required=True);fy.add_argument('--case',required=True)
    fy.add_argument('--recheck',action='store_true',help='失效后重新核对并恢复可派工目标')

    fw=sub.add_parser('field-report',help='输出 JSON + Markdown 现场核验报告')
    fw.add_argument('--db',required=True);fw.add_argument('--case',required=True)
    fw.add_argument('--out',required=True)

    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='field-demo': print(field_demo(args.workdir,args.objective))
    elif args.cmd=='field-register':
        print(json.dumps(field_visit.register(args.db,args.case,args.fixture),
                         ensure_ascii=False,indent=2))
    elif args.cmd=='field-plan':
        out=field_visit.plan(args.db,args.case,args.objective)
        if args.out:
            Path(args.out).parent.mkdir(parents=True,exist_ok=True)
            Path(args.out).write_text(json.dumps(out,ensure_ascii=False,indent=2),
                                      encoding='utf-8')
        print(json.dumps(out,ensure_ascii=False,indent=2))
    elif args.cmd=='field-move':
        print(json.dumps(field_visit.move(args.db,args.case,args.to,args.op,
                                          courier_id=args.courier,path=args.via),
                         ensure_ascii=False,indent=2))
    elif args.cmd=='field-inspect':
        print(json.dumps(field_visit.inspect(args.db,args.case,args.recipient,args.op,
                                             courier_id=args.courier),
                         ensure_ascii=False,indent=2))
    elif args.cmd=='field-state':
        print(json.dumps(field_visit.state(args.db,args.case),ensure_ascii=False,indent=2))
    elif args.cmd=='field-close':
        print(json.dumps(field_visit.close_road(
            args.db,args.case,args.origin,args.destination,args.start,args.end,
            closure_id=args.closure_id,replan=args.replan,
            objective=args.objective),ensure_ascii=False,indent=2))
    elif args.cmd=='field-reopen':
        print(json.dumps(field_visit.reopen_road(args.db,args.case,args.closure_id),
                         ensure_ascii=False,indent=2))
    elif args.cmd=='field-recover':
        print(json.dumps(field_visit.recover(args.db,args.case),ensure_ascii=False,indent=2))
    elif args.cmd=='field-sync':
        out=field_visit.sync_eligibility(args.db,args.case)
        if args.recheck: out={'sync':out,'recheck':field_visit.recheck(args.db,args.case)}
        print(json.dumps(out,ensure_ascii=False,indent=2))
    elif args.cmd=='field-report':
        print(field_visit.write_report(args.db,args.out,args.case))
if __name__=='__main__':main()
