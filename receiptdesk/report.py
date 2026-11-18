import json
from pathlib import Path
from .store import connect, masked_subjects, active_source_blobs
from . import retention


def _redact_addresses(text, addresses):
    out = text
    for addr in sorted(addresses, key=len, reverse=True):
        out = out.replace(addr, '[REDACTED]')
    return out


def export(db_path,out):
    db=connect(db_path)
    hidden=masked_subjects(db)
    # Addresses of masked subjects (from the still-held blob) must not leak
    # through free-text columns such as issue details.
    hidden_addresses=set()
    if hidden:
        ph=','.join('?'*len(hidden))
        for r in db.execute(
                f'SELECT payload FROM event WHERE subject IN ({ph})', tuple(hidden)):
            try:
                addr=json.loads(r['payload']).get('address')
                if addr: hidden_addresses.add(addr)
            except (ValueError, TypeError):
                pass
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')
                 if not (table in ('decision','evidence') and r['subject'] in hidden)]
          for table in ('batch','subject','issue','decision','evidence','run')}
    # Event rows of a pending-erasure subject are filtered at the read boundary.
    data['event']=[dict(r) for r in db.execute('SELECT * FROM event ORDER BY time,id')
                   if r['subject'] not in hidden]
    if hidden_addresses:
        for table in ('issue','decision'):
            for row in data[table]:
                row['detail' if table=='issue' else 'details']=_redact_addresses(
                    row['detail' if table=='issue' else 'details'], hidden_addresses)
    # Raw blobs are never exported, but their hashes make the active set verifiable.
    data['source']=[{'id':b['id'],'path':b['path'],'sha256':b['sha256'],
                     'blob_bytes':len(b['raw'])} for b in active_source_blobs(db)]
    data['masked_subjects']=sorted(hidden)
    data['retention_request']=[dict(r) for r in db.execute(
        'SELECT id,contract,request_type,source_id,source_sha256,subject,'
        'credential_version,hold_until,status,reason,created_at,updated_at,details '
        'FROM retention_request ORDER BY created_at,id')]
    data['source_lineage']=retention.lineage(db_path)
    data['source_hash_tombstone']=[dict(r) for r in db.execute(
        'SELECT sha256,source_id,request_id,reason,replaced_by_sha256,at '
        'FROM source_hash_tombstone ORDER BY at')]
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    db.close()
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    md=target.with_suffix('.md')
    lines=['# 决策报告','']
    lines += [f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
    if data['retention_request']:
        lines += ['', '## 保留/擦除台账']
        lines += [f"- {r['id']} [{r['contract']}] {r['subject']}: {r['status']} — {r['reason']}"
                  for r in data['retention_request']]
    if data['masked_subjects']:
        lines += ['', f"屏蔽中的主体: {', '.join(data['masked_subjects'])}"]
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return target
