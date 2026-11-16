import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import alias_map,resolve
from .timeline import advance

VERSION='1'
def evaluate(db_path):
    db=connect(db_path);cfg=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    roster={r['id']:r for r in cfg['recipients']};aliases=alias_map(cfg['recipients'])
    by=defaultdict(list)
    for row in db.execute('SELECT * FROM event ORDER BY time,id'):
        e=dict(row);v=json.loads(e['payload']);addr=v.get('address',e['subject']);subject=resolve(e['subject'],addr,aliases);by[subject].append(e)
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict')")
        for recipient,spec in roster.items():
            events=by.pop(recipient,[]);problems=[];messages={};delivered=False;late=False
            for e in events:
                v=json.loads(e['payload']);mid=v.get('message_id');kind=e['kind']
                if v.get('channel')!=spec['channel']:problems.append('channel_mismatch')
                if not mid:problems.append('missing_message_id');continue
                next_state,error=advance(messages.get(mid),kind)
                if error:problems.append(error)
                else:messages[mid]=next_state
                if kind=='receipt':
                    delivered=True
                    if timestamp(e['time'])>timestamp(cfg['deadline']):late=True
                if kind=='queued' and v.get('resends') and v['resends'] not in messages:problems.append('unknown_original')
            if any(s=='bounced' for s in messages.values()):problems.append('hard_bounce_history')
            if late:problems.append('late_delivery')
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            status='effective' if delivered and not late and not any(p in problems for p in ('illegal_transition','channel_mismatch','missing_queue','unknown_original')) else ('undelivered' if late or 'hard_bounce_history' in problems else 'insufficient')
            detail=json.dumps({'issues':sorted(set(problems)),'messages':messages,'evidence':[e['id'] for e in events]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(recipient,status,detail,VERSION,run_id))
            for e in events:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(recipient,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,recipient))
            results[recipient]=status
        for subject in by:db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_recipient',subject))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
