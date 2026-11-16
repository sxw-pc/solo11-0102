import json
from pathlib import Path
from .store import connect

def export(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','subject','source','event','issue','decision','evidence','run')}
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    md=target.with_suffix('.md')
    md.write_text('# 决策报告\n\n'+'\n'.join(f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision'])+'\n',encoding='utf-8')
    db.close();return target
