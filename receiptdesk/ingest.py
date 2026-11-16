import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from .store import connect

def hash_bytes(data): return hashlib.sha256(data).hexdigest()
def timestamp(text):
    dt=datetime.fromisoformat(text.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timezone_required')
    return dt
def freeze_config(db_path,config):
    raw=Path(config).read_bytes();digest=hash_bytes(raw); db=connect(db_path)
    old=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
    if old and old['value']!=digest:
        db.close()
        raise ValueError('frozen_config_conflict')
    parsed=json.loads(raw)
    subjects=parsed.get('boxes',parsed.get('stations',parsed.get('recipients',[])))
    ids=[item.get('box',item.get('id')) for item in subjects]
    if not ids or any(not item for item in ids) or len(ids)!=len(set(ids)):
        raise ValueError('duplicate_or_missing_subject')
    aliases=[alias for item in subjects for alias in item.get('aliases',[])]
    if len(aliases)!=len(set(aliases)) or set(aliases)&set(ids):
        raise ValueError('ambiguous_alias')
    with db:
        old=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
        if old and old['value']!=digest: raise ValueError('frozen_config_conflict')
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_hash',?)",(digest,))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_json',?)",(raw.decode('utf-8'),))
        db.execute("INSERT OR IGNORE INTO batch VALUES ('batch-1',?)",(digest,))
        for item in subjects:
            db.execute("INSERT OR IGNORE INTO subject VALUES (?,?)",(item.get('box',item.get('id')),'batch-1'))
    db.close();return digest
def import_file(db_path,path):
    raw=Path(path).read_bytes(); digest=hash_bytes(raw);db=connect(db_path)
    if db.execute('SELECT 1 FROM source WHERE sha256=?',(digest,)).fetchone():
        db.close();return {'imported':0,'duplicate_file':True}
    count=0
    with db:
        sid=db.execute('INSERT INTO source(path,sha256,raw) VALUES (?,?,?)',(str(path),digest,raw)).lastrowid
        for line_no,line in enumerate(raw.decode('utf-8-sig').splitlines(),1):
            if not line.strip(): continue
            try:
                row=json.loads(line)
                kind=str(row['kind']); subject=str(row['subject']);ts=str(row['time'])
                normalized=timestamp(ts).astimezone(timezone.utc).isoformat()
                identity=str(row.get('identity') or f"{row['device']}:{int(row['seq'])}")
                if not kind or not subject: raise ValueError('missing_kind_or_subject')
                canonical=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))
                h=hash_bytes(canonical.encode())
                old=db.execute('SELECT sha256 FROM event WHERE identity=?',(identity,)).fetchone()
                if old:
                    if old['sha256']!=h: db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('identity_conflict',identity))
                    continue
                db.execute('INSERT INTO event(source_id,line_no,identity,subject,kind,time,payload,sha256) VALUES (?,?,?,?,?,?,?,?)',(sid,line_no,identity,subject,kind,normalized,canonical,h));count+=1
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
                db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('parse_error',json.dumps({'source_id':sid,'line_no':line_no,'error':str(exc),'raw_line':line},ensure_ascii=False)))
    db.close();return {'imported':count,'duplicate_file':False}
