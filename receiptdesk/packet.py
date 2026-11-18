"""离线附件包配发：完整包原子性、签章员授权与额度账、双合同优先级。

配发只读冻结名单（subject）与签章配置，不触碰 source/event/decision。
一次配发的包、文件版本/哈希、签章员、额度流水与未配发原因在同一事务落库；
相同输入（签章配置 + 附件实际内容 + 合同）重跑命中 packet_run.input_hash，直接
返回已落库结果，不二次配发、不二次扣减额度。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from .identity import alias_map
from .ingest import hash_bytes, timestamp
from .store import connect

CONTRACT_OLDEST = 'oldest_complete_request'
CONTRACT_MAX = 'max_completed_recipients'
CONTRACTS = (CONTRACT_OLDEST, CONTRACT_MAX)

# 精确搜索的请求数上限；超出时退化为按等待时间的贪心最大集，避免指数爆炸。
_SEARCH_LIMIT = 24


def _load_signing(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), hash_bytes(raw)


def _form_states(spec, files_dir):
    """逐表单核对清单版本与实际文件：重复版本、缺文件、改字节都标记为不可用。"""
    forms = spec.get('forms', [])
    ids = [f.get('id') for f in forms]
    versions = [f.get('version') for f in forms]
    dup_ids = {x for x in ids if ids.count(x) > 1}
    dup_versions = {x for x in versions if versions.count(x) > 1}
    states = {}
    for form in forms:
        fid, version = form.get('id'), form.get('version')
        state = {'version': version, 'sha256': form.get('sha256'), 'digest': None, 'error': None}
        if fid in dup_ids or version in dup_versions:
            state['error'] = 'duplicate_form_version'
        elif not version or Path(version).name != version:
            state['error'] = 'invalid_version'
        else:
            path = Path(files_dir) / version
            if not path.is_file():
                state['error'] = 'missing_attachment'
            else:
                digest = hash_bytes(path.read_bytes())
                state['digest'] = digest
                if digest != form.get('sha256'):
                    state['error'] = 'attachment_hash_mismatch'
        states[fid] = state
    return states


def _parse_requests(spec, subjects, aliases, form_states):
    """规范化请求：别名归并到冻结名单，未知对象、重复请求、坏表单直接拒。"""
    eligible, rejected, seen = [], [], set()
    for req in spec.get('requests', []):
        if not isinstance(req, dict):
            req = {}
        raw_recipient = str(req.get('recipient') or '')
        recipient = aliases.get(raw_recipient, raw_recipient)
        entry = {'recipient': recipient, 'requested_at': None,
                 'forms': list(req.get('required_forms') or [])}
        try:
            entry['requested_at'] = timestamp(
                str(req.get('requested_at'))).astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            rejected.append((entry, 'invalid_request'))
            continue
        if recipient not in subjects:
            rejected.append((entry, 'unknown_recipient'))
            continue
        if recipient in seen:
            rejected.append((entry, 'duplicate_request'))
            continue
        seen.add(recipient)
        forms = entry['forms']
        if not forms or len(set(forms)) != len(forms):
            rejected.append((entry, 'invalid_request'))
            continue
        if any(f not in form_states for f in forms):
            rejected.append((entry, 'unknown_form'))
            continue
        broken = next((form_states[f]['error'] for f in forms if form_states[f]['error']), None)
        if broken:
            rejected.append((entry, broken))
            continue
        entry['forms'] = sorted(set(forms))
        eligible.append(entry)
    return eligible, rejected


def _slots(spec, consumed):
    """把签章员剩余额度展开成可分配槽位；已消费额度从流水账扣减。"""
    slots = []
    for signer in sorted(spec.get('signers', []), key=lambda s: str(s.get('id'))):
        left = int(signer.get('remaining_seals', 0)) - consumed.get(signer.get('id'), 0)
        for _ in range(max(0, left)):
            slots.append({'signer': signer.get('id'),
                          'forms': frozenset(signer.get('authorized_forms', []))})
    return slots


def _assign(demands, slots):
    """联立求解表单需求与签章员授权的二部匹配。

    demands: [(label, form_id)]；slots: [{'signer','forms'}]。
    返回 {demand_index: slot_index}，不可行返回 None。
    需求按候选槽位从少到多、槽位按授权面从窄到宽排序，保证结果确定且
    优先占用最受限的额度，为后续请求保留灵活度。
    """
    order = sorted(range(len(slots)),
                   key=lambda j: (len(slots[j]['forms']), str(slots[j]['signer']), j))
    candidates = [[j for j in order if form in slots[j]['forms']] for _, form in demands]
    sequence = sorted(range(len(demands)),
                      key=lambda i: (len(candidates[i]), str(demands[i][0]), demands[i][1]))
    holder = {}
    result = {}

    def augment(i, seen):
        # 空槽优先于占用槽：只在无空槽时才走增广路重排，保证配发结果稳定直观
        for j in sorted(candidates[i], key=lambda j: j in holder):
            if j in seen:
                continue
            seen.add(j)
            if j not in holder or augment(holder[j], seen):
                holder[j] = i
                result[i] = j
                return True
        return False

    for i in sequence:
        if i not in result and not augment(i, set()):
            return None
    return result


def _plan_oldest(eligible, slots):
    """先服务最久等待的可完整发包请求，再在剩余额度内尽量多完成。"""
    remaining = list(slots)
    plans, rejected = {}, []
    for entry in sorted(eligible, key=lambda e: (e['requested_at'], e['recipient'])):
        demands = [(entry['recipient'], f) for f in entry['forms']]
        found = _assign(demands, remaining)
        if found is None:
            rejected.append((entry, 'insufficient_seals'))
            continue
        plans[entry['recipient']] = {demands[i][1]: remaining[j]['signer']
                                     for i, j in found.items()}
        used = set(found.values())
        remaining = [s for j, s in enumerate(remaining) if j not in used]
    return plans, rejected


def _plan_max(eligible, slots):
    """先最大化完整发包的不同收件人数，同分时优先等待更久的请求。"""
    order = sorted(eligible, key=lambda e: (e['requested_at'], e['recipient']))
    n = len(order)
    if n > _SEARCH_LIMIT:  # 兜底：超规模输入退化为按等待时间的贪心
        return _plan_oldest(eligible, slots)

    def feasible(indices):
        demands = [(order[i]['recipient'], f) for i in indices for f in order[i]['forms']]
        return _assign(demands, slots) is not None

    best = []

    def dfs(i, chosen):
        nonlocal best
        if len(chosen) + (n - i) <= len(best):
            return
        if i == n:
            best = list(chosen)
            return
        if feasible(chosen + [i]):  # 先走“纳入”分支：同分时等待最久者优先
            dfs(i + 1, chosen + [i])
        dfs(i + 1, chosen)

    dfs(0, [])
    chosen = set(best)
    demands = [(order[i]['recipient'], f) for i in best for f in order[i]['forms']]
    found = _assign(demands, slots) or {}
    plans = {}
    for i, j in found.items():
        recipient, form = demands[i]
        plans.setdefault(recipient, {})[form] = slots[j]['signer']
    rejected = [(order[i], 'insufficient_seals') for i in range(n) if i not in chosen]
    return plans, rejected


def _manifest_from_run(db, run_id, spec, already_applied):
    run = db.execute('SELECT * FROM packet_run WHERE id=?', (run_id,)).fetchone()
    packets = []
    for p in db.execute(
            "SELECT * FROM packet WHERE run_id=? AND status='issued'"
            ' ORDER BY requested_at,recipient', (run_id,)):
        items = [{'form': i['form_id'], 'version': i['version'], 'sha256': i['sha256'],
                  'signer': i['signer_id']}
                 for i in db.execute(
                     'SELECT * FROM packet_item WHERE packet_id=? ORDER BY form_id', (p['id'],))]
        packets.append({'recipient': p['recipient'], 'requested_at': p['requested_at'],
                        'items': items})
    rejected = [{'recipient': r['recipient'], 'requested_at': r['requested_at'],
                 'reason': r['reason']}
                for r in db.execute(
                    "SELECT * FROM packet WHERE run_id=? AND status='rejected'"
                    ' ORDER BY recipient,rowid', (run_id,))]
    consumed = {r['signer_id']: r['c'] for r in db.execute(
        'SELECT signer_id,COUNT(*) c FROM seal_ledger GROUP BY signer_id')}
    seals = {s.get('id'): {'remaining': int(s.get('remaining_seals', 0)) - consumed.get(s.get('id'), 0),
                           'consumed': consumed.get(s.get('id'), 0)}
             for s in spec.get('signers', [])}
    return {'run_id': run_id, 'contract': run['contract'], 'config_hash': run['config_hash'],
            'input_hash': run['input_hash'], 'already_applied': already_applied,
            'packets': packets, 'rejected': rejected, 'seals': seals}


def allocate(db_path, signing_path, files_dir=None, contract=CONTRACT_OLDEST):
    """按合同对冻结名单中的收件人做离线附件包配发，返回获配清单。"""
    if contract not in CONTRACTS:
        raise ValueError('unknown_contract')
    signing_path = Path(signing_path)
    spec, config_hash = _load_signing(signing_path)
    allowed = spec.get('contracts')
    if allowed and contract not in allowed:
        raise ValueError('contract_not_allowed')
    files_dir = Path(files_dir) if files_dir else signing_path.parent / 'packet_files'

    db = connect(db_path)
    row = db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
    if not row:
        db.close()
        raise ValueError('config_not_frozen')
    cfg = json.loads(row['value'])
    if spec.get('batch_id') and not db.execute(
            'SELECT 1 FROM batch WHERE id=?', (spec['batch_id'],)).fetchone():
        db.close()
        raise ValueError('unknown_batch')
    subjects = {r['id'] for r in db.execute('SELECT id FROM subject')}
    aliases = alias_map(cfg.get('recipients', []))

    form_states = _form_states(spec, files_dir)
    fingerprint = hash_bytes(json.dumps(
        {'config': config_hash, 'contract': contract,
         'files': {fid: (s['digest'] or s['error']) for fid, s in sorted(form_states.items())}},
        sort_keys=True).encode())
    old = db.execute('SELECT id FROM packet_run WHERE input_hash=?', (fingerprint,)).fetchone()
    if old:  # 相同输入重跑：直接返回已落库结果，不二次配发或扣减
        result = _manifest_from_run(db, old['id'], spec, already_applied=True)
        db.close()
        return result

    consumed = {r['signer_id']: r['c'] for r in db.execute(
        'SELECT signer_id,COUNT(*) c FROM seal_ledger GROUP BY signer_id')}
    eligible, rejected = _parse_requests(spec, subjects, aliases, form_states)
    issued = {r['recipient'] for r in db.execute(
        "SELECT recipient FROM packet WHERE status='issued'")}
    pool = []
    for entry in eligible:  # 已获配过的收件人不再二次配发
        (rejected.append((entry, 'already_issued')) if entry['recipient'] in issued
         else pool.append(entry))

    authorized = {}
    for signer in spec.get('signers', []):
        for form in signer.get('authorized_forms', []):
            authorized.setdefault(form, set()).add(signer.get('id'))
    full_slots = _slots(spec, {})
    remaining_slots = _slots(spec, consumed)
    candidates = []
    for entry in pool:
        if any(f not in authorized for f in entry['forms']):
            rejected.append((entry, 'unauthorized_form'))
        elif _assign([(entry['recipient'], f) for f in entry['forms']], full_slots) is None:
            rejected.append((entry, 'insufficient_seals'))
        else:
            candidates.append(entry)

    planner = _plan_oldest if contract == CONTRACT_OLDEST else _plan_max
    plans, plan_rejected = planner(candidates, remaining_slots)
    rejected += plan_rejected

    now = datetime.now(timezone.utc).isoformat()
    by_recipient = {e['recipient']: e for e in candidates}
    with db:  # 配发决定、文件版本/哈希、签章员、额度流水与未配发原因同事务落库
        run_id = db.execute(
            'INSERT INTO packet_run(input_hash,contract,config_hash,created_at,status)'
            ' VALUES (?,?,?,?,?)', (fingerprint, contract, config_hash, now, 'done')).lastrowid
        for recipient in sorted(plans, key=lambda r: (by_recipient[r]['requested_at'], r)):
            entry = by_recipient[recipient]
            pid = db.execute(
                "INSERT INTO packet(run_id,recipient,requested_at,status,reason)"
                " VALUES (?,?,?,'issued',NULL)",
                (run_id, recipient, entry['requested_at'])).lastrowid
            for form in entry['forms']:
                state = form_states[form]
                db.execute('INSERT INTO packet_item VALUES (?,?,?,?,?)',
                           (pid, form, state['version'], state['sha256'],
                            plans[recipient][form]))
                db.execute('INSERT INTO seal_ledger(run_id,packet_id,signer_id,form_id)'
                           ' VALUES (?,?,?,?)',
                           (run_id, pid, plans[recipient][form], form))
        for entry, reason in sorted(rejected,
                                    key=lambda er: (er[0]['requested_at'] or '',
                                                    er[0]['recipient'])):
            db.execute("INSERT INTO packet(run_id,recipient,requested_at,status,reason)"
                       " VALUES (?,?,?,'rejected',?)",
                       (run_id, entry['recipient'], entry['requested_at'], reason))
    result = _manifest_from_run(db, run_id, spec, already_applied=False)
    db.close()
    return result
