import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import alias_map,resolve
from .timeline import advance

VERSION='2'
TERMINALS=('bounced','receipt','revoke')
DISQUALIFIERS=('illegal_transition','channel_mismatch','missing_queue','unknown_original')

def _order(a,b): return (a>b)-(a<b)

def _audit(event):
    v=json.loads(event['payload'])
    return {'event':event['id'],'kind':event['kind'],'time':v.get('time'),'time_utc':event['time'],
            'device':v.get('device'),'seq':v.get('seq'),'relay':v.get('relay'),'rseq':v.get('rseq'),
            'message_id':v.get('message_id'),'attempt':v.get('attempt'),'resends':v.get('resends'),
            'source_id':event['source_id'],'line_no':event['line_no']}

def _att(event):
    a=json.loads(event['payload']).get('attempt')
    return (a is None,a or 0)

def _device_drift(events,devices):
    # 同一设备随序号推进时钟不得回退；回退则该设备时间不可用于因果裁决
    for device in devices:
        rows=sorted((json.loads(e['payload']).get('seq'),e['time']) for e in events
                    if json.loads(e['payload']).get('device')==device and json.loads(e['payload']).get('seq') is not None)
        if any(rows[i+1][1]<rows[i][1] for i in range(len(rows)-1)): return device
    return None

def _seq_closed(payloads):
    # 接收端序号契约的前置条件：同一 relay、序号单调连续、尝试链闭合并与序号同向
    if any(p.get('relay') is None or p.get('rseq') is None for p in payloads): return False,'sequence_gap'
    if len({p['relay'] for p in payloads})!=1: return False,'sequence_gap'
    rseqs=sorted(p['rseq'] for p in payloads)
    if rseqs!=list(range(rseqs[0],rseqs[0]+len(rseqs))): return False,'sequence_gap'
    attempts=[p.get('attempt') for p in payloads]
    if any(a is None for a in attempts) or sorted(attempts)!=list(range(1,len(attempts)+1)): return False,'attempt_gap'
    if [p['attempt'] for p in sorted(payloads,key=lambda p:p['rseq'])]!=sorted(attempts): return False,'sequence_gap'
    return True,None

def _adjudicate(contract,receipt,revoke,payloads,events,problems,record):
    # 终态对裁决：只有可证明的因果顺序才产生 winner，否则保持 None（证据不足）
    rv,kv=json.loads(receipt['payload']),json.loads(revoke['payload'])
    record['pair']=[_audit(receipt),_audit(revoke)]
    record['time_order']=_order(receipt['time'],revoke['time'])
    if rv.get('rseq') is not None and kv.get('rseq') is not None:
        record['seq_order']=_order(rv['rseq'],kv['rseq'])
    if record['time_order'] and record['seq_order'] and record['time_order']!=record['seq_order']:
        problems.append('causal_contradiction')
    if _device_drift(events,{rv.get('device'),kv.get('device')}-{None}):
        problems.append('clock_drift');record['reason']='clock_drift';return None
    if contract=='event_time':
        if not record['time_order']:
            problems.append('clock_conflict');record['reason']='clock_conflict';return None
        record['reason']='event_time'
        return receipt if record['time_order']<0 else revoke
    closed,reason=_seq_closed(payloads)
    if not closed:
        problems.append(reason);record['reason']=reason;return None
    if not record['seq_order']:
        problems.append('sequence_gap');record['reason']='sequence_gap';return None
    record['reason']='relay_seq'
    return receipt if record['seq_order']<0 else revoke

def evaluate(db_path):
    db=connect(db_path);cfg=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    contract=cfg.get('adjudication','event_time')
    roster={r['id']:r for r in cfg['recipients']};aliases=alias_map(cfg['recipients'])
    deadline=timestamp(cfg['deadline'])
    by=defaultdict(list)
    for row in db.execute('SELECT * FROM event ORDER BY time,id'):
        e=dict(row);v=json.loads(e['payload']);addr=v.get('address',e['subject']);subject=resolve(e['subject'],addr,aliases);by[subject].append(e)
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict')")
        for recipient,spec in roster.items():
            events=by.pop(recipient,[]);problems=[];messages={};terminal_map={};adjudications=[]
            delivered=late=bounced_seen=revoked_before=False
            recipient_contested=message_contested=False
            groups=defaultdict(list);seen=set()
            for e in sorted(events,key=lambda e:(e['time'],e['id'])):
                v=json.loads(e['payload'])
                if v.get('channel')!=spec['channel']:problems.append('channel_mismatch')
                mid=v.get('message_id')
                if not mid:problems.append('missing_message_id');continue
                if e['kind']=='queued' and v.get('resends') and v['resends'] not in seen:problems.append('unknown_original')
                seen.add(mid);groups[mid].append(e)
                if e['kind']=='receipt' and timestamp(e['time'])>deadline:late=True;problems.append('late_delivery')
            devices={json.loads(e['payload']).get('device') for e in events}-{None}
            if _device_drift(events,devices):problems.append('clock_drift');recipient_contested=True
            resent_by={}
            for mid in sorted(groups,key=lambda m:min((e['time'],e['id']) for e in groups[m])):
                evs=sorted(groups[mid],key=lambda e:(e['time'],_att(e),e['id']))
                payloads=[json.loads(e['payload']) for e in evs]
                attempts=[p.get('attempt') for p in payloads];contested=False
                if all(a is not None for a in attempts):
                    if len(set(attempts))!=len(attempts):problems.append('attempt_conflict');contested=True
                    elif sorted(attempts)!=list(range(1,len(attempts)+1)):problems.append('attempt_gap');contested=True
                state=None;terms=[];broken=False;terminal_seen=False
                for e,p in zip(evs,payloads):
                    kind=e['kind']
                    if kind in TERMINALS:
                        if state is None:problems.append('missing_queue');broken=True
                        else:
                            _,err=advance(state,kind)
                            if err:problems.append(err);broken=True
                        terms.append(e);terminal_seen=True
                    else:
                        if terminal_seen:problems.append('illegal_transition');broken=True
                        nxt,err=advance(state,kind)
                        if err:problems.append(err);broken=True
                        else:state=nxt
                        if kind=='queued' and p.get('resends'):resent_by[p['resends']]=(mid,e['time'])
                if terms:terminal_map[mid]=terms
                outcome=None
                if broken or contested or not terms:pass
                elif len(terms)==1:outcome=terms[0]
                elif len(terms)==2 and {t['kind'] for t in terms}=={'receipt','revoke'}:
                    rcp=next(t for t in terms if t['kind']=='receipt');rvk=next(t for t in terms if t['kind']=='revoke')
                    record={'message_id':mid,'contract':contract,'pair':[],'time_order':None,'seq_order':None,'winner':None,'reason':None}
                    winner=_adjudicate(contract,rcp,rvk,payloads,events,problems,record)
                    if winner is None:contested=True
                    else:
                        record['winner']=winner['kind'];outcome=winner
                        if winner['kind']=='revoke':revoked_before=True;problems.append('revoked_before_delivery')
                        else:problems.append('revoked_after_delivery')
                    adjudications.append(record)
                else:problems.append('duplicate_terminal');contested=True
                if outcome and outcome['kind']=='receipt':delivered=True
                if any(t['kind']=='bounced' for t in terms):bounced_seen=True
                if outcome:messages[mid]=outcome['kind']
                elif contested:messages[mid]='contested'
                elif state:messages[mid]=state
                if contested:message_contested=True
            for original,(resender,boundary) in resent_by.items():
                if any(e['kind']=='revoke' and e['time']>boundary for e in groups.get(original,[])):
                    problems.append('revoke_crosses_resend');recipient_contested=True
            if bounced_seen:problems.append('hard_bounce_history')
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            disqualified=any(p in problems for p in DISQUALIFIERS)
            if delivered and not late and not disqualified and not recipient_contested:status='effective'
            elif recipient_contested or message_contested:status='insufficient'
            elif late or bounced_seen or revoked_before:status='undelivered'
            else:status='insufficient'
            detail=json.dumps({'issues':sorted(set(problems)),'messages':messages,
                'terminals':{mid:[_audit(t) for t in terms] for mid,terms in terminal_map.items()},
                'adjudications':adjudications,'contract':contract,
                'evidence':[e['id'] for e in events]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(recipient,status,detail,VERSION,run_id))
            for e in events:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(recipient,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,recipient))
            results[recipient]=status
        for subject in by:db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_recipient',subject))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
