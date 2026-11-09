"""Offline simulation-only entry point wrapping the unmodified matching engine."""
from __future__ import annotations
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
FROZEN = HERE.parent / '冻结原始代码' / '原始源码与测试证据' / '算法一V0.1'
sys.path.insert(0, str(FROZEN))
from chaintrace_matcher import Engine, Config, sha256  # original, read only

class InputError(ValueError):
    pass

def preflight(data):
    if not isinstance(data, dict) or data.get('is_simulated') is not True:
        raise InputError('仅接受明确标记为模拟数据的JSON对象。')
    for key in ('records', 'events', 'claims'):
        if not isinstance(data.get(key), list):
            raise InputError(f'{key}必须为列表。')
    records = {}
    for r in data['records']:
        if not isinstance(r, dict) or any(k not in r for k in ('id', 'case_id', 'payload', 'raw_sha256', 'is_simulated')):
            raise InputError('来源记录缺少必要字段。')
        if not isinstance(r['id'], str) or not r['id'] or not isinstance(r['case_id'], str) or not r['case_id']:
            raise InputError('来源记录标识及案件标识必须是非空字符串。')
        if r['id'] in records:
            raise InputError(f"来源记录重复：{r['id']}")
        if r['is_simulated'] is not True:
            raise InputError(f"来源不是模拟记录：{r['id']}")
        if sha256(r['payload']) != r['raw_sha256']:
            raise InputError(f"来源内容与摘要不一致：{r['id']}")
        records[r['id']] = r
    for key in ('events', 'claims'):
        for item in data[key]:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or not isinstance(item.get('case_id'), str) or not item['case_id']:
                raise InputError(f'{key}的标识或案件标识无效。')
            ids = item.get('source_ids')
            if not isinstance(ids, list) or not ids or not all(isinstance(x,str) and x for x in ids):
                raise InputError(f"{item['id']}缺少有效来源记录。")
            for rid in ids:
                if rid not in records:
                    raise InputError(f"{item['id']}引用不存在的来源：{rid}")
                if records[rid]['case_id'] != item['case_id']:
                    raise InputError(f"{item['id']}引用跨案件来源：{rid}")
    try:
        return Engine(data, Config())
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f'输入字段不符合原型要求：{exc}') from exc

def run_data(data):
    engine = preflight(data)
    bundle = engine.run_bundle()
    seen = {x['explanation']['claim_business_key'] for x in bundle['candidate_matches'] if x['match_type']=='EVENT_TO_CLAIM'}
    no_output = [{'case_id': c['case_id'], 'claim_id': c['id']} for c in data['claims'] if c['id'] not in seen]
    return {'is_simulated':True, 'input_sha256':sha256(data), 'input_counts':{'records':len(data['records']), 'events':len(data['events']), 'claims':len(data['claims'])},
            'parameters':Config().snapshot(), 'analysis_runs':bundle['analysis_runs'],
            'event_claim_results':[r for r in bundle['candidate_matches'] if r['match_type']=='EVENT_TO_CLAIM'],
            'event_reconciliation':[r for r in bundle['candidate_matches'] if r['match_type']=='EVENT_TO_EVENT'],
            'claims_without_output':no_output, 'metrics':bundle['metrics']}

def read_input(path):
    try:
        with Path(path).open(encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        raise InputError(f'无法读取JSON：{exc}') from exc

def write_failure(out_dir, exc):
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    record={'time_utc':datetime.now(timezone.utc).isoformat(),'status':'rejected','error':str(exc),'is_simulated':True}
    (path/'运行失败.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def evidence_bundle(result, data):
    # The snapshot is validated before any export file is created.
    preflight(data)
    if sha256(data) != result.get('input_sha256'):
        raise InputError('输入快照与核验结果不一致，无法导出。')
    def index(items):
        result_index = {}
        for item in items:
            key = (item['case_id'], item['id'])
            if key in result_index:
                raise InputError(f"同一案件中标识重复：{item['id']}")
            result_index[key] = item
        return result_index
    claims, events, records = (index(data[k]) for k in ('claims', 'events', 'records'))
    def locate(items, case_id, item_id):
        try:
            item = items[(case_id, item_id)]
            sources = [records[(case_id, rid)] for rid in item['source_ids']]
        except KeyError as exc:
            raise InputError(f'结果无法定位输入或来源：{exc}') from exc
        return item, sources
    rows = []
    for row in result['event_claim_results']:
        e = row['explanation']; case = row['case_id']
        claim, cs = locate(claims, case, e['claim_business_key'])
        event, es = locate(events, case, e['event_business_key'])
        rows.append({'result':row, 'claim':claim, 'event':event, 'claim_sources':cs, 'event_sources':es})
    reconciliations = []
    for row in result['event_reconciliation']:
        e = row['explanation']; case = row['case_id']
        transfer, ts = locate(events, case, e['transfer_id'])
        deposit, ds = locate(events, case, e['deposit_id'])
        reconciliations.append({'result':row, 'transfer':transfer, 'deposit':deposit, 'transfer_sources':ts, 'deposit_sources':ds})
    unmatched = []
    for row in result['claims_without_output']:
        claim, sources = locate(claims, row['case_id'], row['claim_id'])
        unmatched.append({'claim':claim, 'claim_sources':sources})
    return {'is_simulated':True, 'input_sha256':result['input_sha256'],
            'scope':'来源记录关联到输入对象；未建立字段至原始材料页段的映射。哈希用于检查记录内容一致性，不能证明原始材料真实性。复核状态不代表已完成人工复核。',
            'event_claim_evidence':rows, 'event_reconciliation_evidence':reconciliations, 'claims_without_output_evidence':unmatched}

def export(result, out_dir, data):
    evidence = evidence_bundle(result, data)
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'核验完整结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    (dest/'核验来源与输入.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    with (dest/'核验结果摘要.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['案件标识','资金主张标识','资金事件标识','结果类型','复核状态','排序分','部分或完全支持的维度','所有可比较维度及分值','缺失项','冲突项'])
        for row in result['event_claim_results']:
            e=row['explanation']
            features=e.get('feature_scores',{})
            supports=[k for k,v in features.items() if Decimal(str(v))>0]
            w.writerow([row['case_id'],e['claim_business_key'],e['event_business_key'],e['match_class'],row['review_status'],row['score'],
                        '; '.join(supports),json.dumps(features,ensure_ascii=False),'; '.join(row['missing_items']),'; '.join(row['conflict_items'])])
    log={'time_utc':datetime.now(timezone.utc).isoformat(),'status':'completed','input_sha256':result['input_sha256'],
         'input_counts':result['input_counts'],'parameters':result['parameters'],
         'event_claim_rows':len(result['event_claim_results']),'event_reconciliation_rows':len(result['event_reconciliation']),
         'claims_without_output':len(result['claims_without_output']),'is_simulated':True,
         'output_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [dest/'核验完整结果.json',dest/'核验结果摘要.csv',dest/'核验来源与输入.json']}}
    (dest/'运行记录.json').write_text(json.dumps(log,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return log

def run_file(input_path, out_dir):
    try:
        data = read_input(input_path)
        result = run_data(data)
        return result, export(result, out_dir, data)
    except (InputError, ValueError, TypeError, KeyError, OSError) as exc:
        write_failure(out_dir,exc)
        raise
