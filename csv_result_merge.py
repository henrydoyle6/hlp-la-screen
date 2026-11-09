"""Offline L2 CSV3 and validated L2/L4 result assembly; no raw-data or AWS reads.

Uses the unchanged protected simulate_close function. L2 trace entries are price
levels (oid=null); executed_order_count is blank unless n proves the count.
The original native-L4 run_hlp.py path and checkpoint format are unchanged.
"""
from contextlib import closing
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import csv_pipeline as pipeline
import hlp_la_screen as engine
import official_l2
import state_store as store

VERSION = 'csv-result-merge-v1'
CSV1_FIELDS = pipeline.CSV1_FIELDS
CSV2_FIELDS = pipeline.CSV2_FIELDS + ['snapshot_lag_ms']
CSV3_FIELDS = pipeline.CSV3_FIELDS
CHINESE = [
    '模式版本','运行编号','记录类型','接管组编号','处理状态','原因代码','原因说明','错误文件',
    '错误文件内路径','错误行号','错误区块号','问题明细JSON','接管事件编号','接管时间UTC','区块号',
    '币种','接管方向','接管价格','接管数量','来源文件','来源文件内路径','来源行号','来源事件序号',
    '数据开始时间UTC','数据结束时间UTC（不含）','正式开始时间UTC（含）','正式结束时间UTC（含）',
    '是否正式范围接管','连续数据段编号','接管组开始时间UTC','接管组结束时间UTC','接管组首区块号',
    '接管组末区块号','接管笔数','正式范围接管笔数','缓冲区接管笔数','持续时间毫秒','接管总数量',
    '接管总金额','加权接管均价','是否部分接管组','部分接管组原因','前侧分段依据','后侧分段依据','接管组输入哈希',
]
NUMERIC_RESULTS = ['filled_qty','unfilled_qty','fill_ratio','close_notional','exit_vwap',
                   'entry_cost_of_filled_qty','executed_order_count','executed_price_level_count',
                   'last_execution_price','la','depth_insufficient','fill_trace_json']


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def read_csv_bytes(raw, expected):
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    fields = reader.fieldnames
    require(fields in expected, 'Unexpected CSV header')
    rows = list(reader)
    require(all(None not in r and all(v is not None for v in r.values()) for r in rows), 'Malformed CSV row')
    if fields == CHINESE:
        rows = [dict(zip(CSV1_FIELDS, (r[k] for k in CHINESE))) for r in rows]
    return rows


def read_csv1(path):
    return read_csv_bytes(Path(path).read_bytes(), [CSV1_FIELDS, CHINESE])


def read_csv2(path):
    rows = read_csv_bytes(Path(path).read_bytes(), [pipeline.CSV2_FIELDS, CSV2_FIELDS])
    return [{**r, 'snapshot_lag_ms': r.get('snapshot_lag_ms', '')} for r in rows]


def prior_results(path):
    with zipfile.ZipFile(path) as archive:
        require(sum(i.file_size for i in archive.infolist()) <= 512*1024**2, 'Previous archive exceeds size bound')
        require(archive.testzip() is None, 'Previous archive failed CRC verification')
        result = []
        for n, fields in [(1, CSV1_FIELDS), (2, pipeline.CSV2_FIELDS), (3, CSV3_FIELDS)]:
            members = [i for i in archive.infolist() if i.filename.rsplit('/', 1)[-1] == f'csv{n}.csv']
            require(len(members) == 1 and members[0].file_size <= 256*1024**2,
                    'Missing, duplicate, or oversized previous CSV')
            result.append(read_csv_bytes(archive.read(members[0]), [fields]))
        return result


def group_rows(rows):
    groups = defaultdict(list)
    events = set()
    for r in rows:
        if r['record_type'] == 'issue':
            continue
        require(r['record_type'] == 'fill' and r['event_id'] and r['episode_id'], 'Invalid CSV1 record')
        require(r['event_id'] not in events, 'Duplicate CSV1 event ID')
        events.add(r['event_id'])
        groups[r['episode_id']].append(r)
    for name, fills in groups.items():
        first, last = fills[0], fills[-1]
        require(len(fills) == int(first['event_count']), 'CSV1 episode fill count mismatch: '+name)
        require(all(all(r[k] == first[k] for k in pipeline.SUMMARY+['episode_input_hash']) for r in fills),
                'Inconsistent episode summary: '+name)
        require(pipeline.episode_hash(fills) == first['episode_input_hash'], 'Episode hash mismatch: '+name)
        require(first['episode_start'] == first['event_time'] and first['episode_end'] == last['event_time'],
                'Episode time endpoints mismatch: '+name)
        with localcontext() as ctx:
            ctx.prec = engine.CALC_PRECISION
            q = sum((Decimal(r['qty']) for r in fills), Decimal(0))
            n = sum((Decimal(r['qty'])*Decimal(r['price']) for r in fills), Decimal(0))
            require(q == Decimal(first['total_qty']) and n == Decimal(first['entry_notional'])
                    and n/q == Decimal(first['entry_vwap']), 'Episode arithmetic mismatch: '+name)
    return dict(groups)


def business_key(row):
    return row['coin'], row['direction'], row.get('tn', row.get('episode_end'))


def episode(fills):
    first, last = fills[0], fills[-1]
    return {**{k:first[k] for k in pipeline.SUMMARY}, 'episode_id':first['episode_id'],
            'episode_input_hash':first['episode_input_hash'], 'tn':last['event_time'],
            'target_block':last['block_number']}


def parsed(row):
    return {k: pipeline.value_text(v) for k,v in row.items()}


@dataclass
class PublishedL2Book:
    levels: list
    side: str

    def side_orders(self, side):
        require(side == self.side, 'Wrong L2 simulation side')
        return [SimpleNamespace(oid=None, price=Decimal(x['price']), qty=Decimal(x['qty'])) for x in self.levels]


def l2_metrics(row):
    levels = json.loads(row['orders_json'])
    require(isinstance(levels, list), 'L2 levels must be a list')
    for level in levels:
        require(isinstance(level, dict) and set(level) in ({'price','qty'}, {'price','qty','n'}), 'Invalid L2 level fields')
        require(all(isinstance(level[k],str) and Decimal(level[k]).is_finite() and Decimal(level[k])>0
                    for k in ('price','qty')), 'Invalid L2 price or quantity')
        require('n' not in level or type(level['n']) is int and level['n']>0, 'Invalid L2 order count')
    prices = [Decimal(x['price']) for x in levels]
    require(len(prices) == len(set(prices)) and prices == sorted(prices, reverse=row['book_side']=='bid'),
            'L2 price levels are not unique and ordered')
    empty = (levels, []) if row['direction']=='long' else ([], levels)
    metrics = official_l2.book_metrics(SimpleNamespace(levels=empty), row['direction'])
    for key in metrics:
        require(pipeline.value_text(metrics[key]) == row[key], 'L2 CSV2 metric mismatch: '+key)
    return levels


def l2_executed_count(levels, trace):
    """A partial multi-order level cannot reveal the executed order count."""
    total = 0
    for level, trade in zip(levels, trace):
        n = level.get('n')
        if n is None or Decimal(trade['filled_qty']) < Decimal(level['qty']) and n != 1:
            return ''
        total += n
    return total


def calculate(ep, source, run_id):
    row = {k:'' for k in CSV3_FIELDS}
    row.update(schema_version='hlp-csv-v3',run_id=run_id,record_type='episode',
               episode_id=ep['episode_id'],status='skipped',issues_json='[]',
               csv2_row_hash=store.digest(source))
    for key in ['episode_input_hash','coin','direction','episode_start','tn','target_block','event_count',
                'total_qty','entry_notional','entry_vwap','partial_episode','partial_reason','split_before','split_after']:
        row[key] = ep[key]
    for key in ['episode_id','episode_input_hash','coin','direction','tn','target_block','total_qty','entry_notional','entry_vwap']:
        require(str(ep[key]) == source[key], 'CSV1/CSV2 association mismatch: '+key)
    if source['status'] == 'skipped':
        for key in pipeline.COMMON[5:]:
            row[key] = source[key]
        return parsed(row)
    require(source['status'] == 'book_ready', 'Unknown CSV2 status')
    side = 'bid' if ep['direction']=='long' else 'ask'
    require(source['book_side'] == side, 'Wrong close side')
    is_l2 = source['book_state_point'] == 'post_takeover_l2_snapshot'
    if is_l2:
        levels = l2_metrics(source)
        book = PublishedL2Book(levels, side)
    else:
        require(source['book_state_point'] == 'block_end', 'Unsupported book state point')
        orders = json.loads(source['orders_json'])
        book = pipeline.book_from_orders(ep['coin'], orders, side)
        metrics = pipeline.book_metrics(book, side)
        for k in metrics:
            require(pipeline.value_text(metrics[k]) == source[k], 'L4 CSV2 metric mismatch: '+k)
    result = engine.simulate_close(book, ep['direction'], Decimal(ep['total_qty']), Decimal(ep['entry_vwap']))
    problems = json.loads(source['issues_json'])
    if is_l2:
        problems.append(dict(stage='csv3',code='L2_AGGREGATED_LEVEL_SIMULATION',
            message='LA uses published L2 levels only; trace entries are price levels, oid is null. '
                    'Executed order count is unavailable when a multi-order level is partially consumed.',
            book_state_point=source['book_state_point'], snapshot_time=source['snapshot_time'],
            snapshot_lag_ms=source['snapshot_lag_ms'], unobserved_deeper_liquidity_unknown=True))
    for key in ['book_hash','book_side','order_count','price_level_count','total_depth','best_price','worst_price']:
        row[key] = source[key]
    with localcontext() as ctx:
        ctx.prec = engine.CALC_PRECISION
        amount = Decimal(0)
        for trade in result['fill_trace']:
            trade['notional'] = engine.dec_text(Decimal(trade['price'])*Decimal(trade['filled_qty']))
            amount += Decimal(trade['notional'])
        f = result['filled_qty']
        row.update(status='calculated_full' if not result['unfilled_qty'] else 'calculated_partial' if f else 'empty_book',
            filled_qty=engine.dec_text(f), unfilled_qty=engine.dec_text(result['unfilled_qty']),
            fill_ratio=engine.dec_text(f/Decimal(ep['total_qty'])), close_notional=engine.dec_text(amount),
            exit_vwap=engine.dec_text(result['exit_vwap']) if result['exit_vwap'] is not None else '',
            entry_cost_of_filled_qty=engine.dec_text(Decimal(ep['entry_vwap'])*f),
            executed_order_count=l2_executed_count(levels,result['fill_trace']) if is_l2 else len(result['fill_trace']),
            executed_price_level_count=len({t['price'] for t in result['fill_trace']}),
            last_execution_price=result['fill_trace'][-1]['price'] if result['fill_trace'] else '',
            la=engine.dec_text(result['la']), depth_insufficient=result['depth_insufficient'],
            fill_trace_json=store.text(result['fill_trace']), issues_json=store.text(problems))
    return parsed(row)


class Log:
    def __init__(self,path):
        self.handle = Path(path).open('a',encoding='utf-8',newline='\n')
    def info(self,event,**values):
        self.handle.write(store.text(dict(at=datetime.now(timezone.utc).isoformat(),event=event,**values))+'\n')
        self.handle.flush()
    warning=info
    def close(self):self.handle.close()


def write_run(directory, rows, config):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    allowed={'csv1.csv','csv2.csv','csv3.csv','run.log','progress.sqlite','cache.sqlite'}
    recovery_files=allowed|{'progress.sqlite-journal','cache.sqlite-journal'}
    require(not directory.is_symlink() and all(p.name in recovery_files and p.is_file() and not p.is_symlink()
                                               for p in directory.iterdir()), 'Unexpected existing run files')
    with store.run_lock(directory/'progress.sqlite'), closing(store.connect(directory/'progress.sqlite')) as db, closing(Log(directory/'run.log')) as log:
        store.init_progress(db)
        old=store.get(db,'merge_config')
        require(old is None or old==config, 'Different inputs/version cannot resume this result run')
        with db:
            store.put(db,'merge_config',config)
        with closing(store.Cache(directory/'cache.sqlite',log)):
            for number,fields in [(1,CSV1_FIELDS),(2,CSV2_FIELDS),(3,CSV3_FIELDS)]:
                source=rows[number-1]
                with closing(store.AppendCSV(directory,number,fields,db,log)) as output:
                    require(output.rows<=len(source), 'Result cursor exceeds input rows')
                    if not output.done:
                        for r in source[output.rows:]:
                            output.write(r)
                            if len(output.pending)>=256:output.commit()
                        output.commit(complete=True)
                    require(output.rows==len(source), 'Result row count differs')
                log.info('RESULT_STAGE_COMPLETE',stage=number,rows=len(source))
        manifest={f'csv{n}.csv':sha(directory/f'csv{n}.csv') for n in (1,2,3)}
        with db:
            store.put(db,'result_sha256',manifest)
            store.put(db,'merge_complete',True)
        log.info('RESULTS_COMPLETE',files=manifest)
    require({p.name for p in directory.iterdir()}==allowed, 'Run must contain exactly six durable files')
    return manifest


def normalize_fill_rows(fills, run_id, start, end):
    result=[]
    formal_start=engine.time_text(engine.parse_time(start)+engine.BUFFER_MS)
    formal_end=engine.time_text(engine.parse_time(end)-engine.BUFFER_MS)
    for r in fills:
        row={**r,'run_id':run_id}
        if r['record_type']=='fill':
            tm=engine.parse_time(r['event_time'])
            require(engine.parse_time(start)<=tm<engine.parse_time(end), 'Whole episode extends outside prepared dates')
            formal=engine.parse_time(formal_start)<=tm<=engine.parse_time(formal_end)
            require(r['is_formal_event']==pipeline.value_text(formal), 'Requested range changes episode formal/buffer membership')
            row.update(data_start=start,data_end_exclusive=end,formal_start_inclusive=formal_start,formal_end_inclusive=formal_end)
        result.append(row)
    return result


def event_business(rows):
    # Historical exports used a different event-ID namespace. Preserve each
    # original ID and match the full multiset of observable business attributes.
    return Counter((r['event_time'],r['block_number'],r['coin'],r['direction'],
                    Decimal(r['price']),Decimal(r['qty'])) for r in rows)


def deliver(csv1_path,l2_path,old_zip,output,start_date='2025-03-22',end_date='2026-05-18'):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    inputs={'csv1_sha256':sha(csv1_path),'l2_csv2_sha256':sha(l2_path),'previous_zip_sha256':sha(old_zip)}
    start=start_date+'T00:00:00.000Z'
    end=(datetime.fromisoformat(end_date)+timedelta(days=1)).strftime('%Y-%m-%dT00:00:00.000Z')
    full=read_csv1(csv1_path); groups=group_rows(full)
    l2rows=read_csv2(l2_path)
    old1,old2,old3=prior_results(old_zip); oldgroups=group_rows(old1)
    require(all(r['record_type']=='episode' for r in l2rows+old2+old3),'Unexpected issue rows in supplied episode CSVs')
    l2by={r['episode_id']:r for r in l2rows}
    require(len(l2by)==len(l2rows),'Duplicate L2 episode')
    laterby={r['episode_id']:r for r in old2}
    prior3={r['episode_id']:r for r in old3}
    require(set(laterby)==set(oldgroups)==set(prior3),'Previous archive has orphan CSV rows')
    eligible={eid:rr for eid,rr in groups.items() if engine.parse_time(start)<=engine.parse_time(rr[-1]['event_time'])<engine.parse_time(end)}
    lookup={business_key(episode(rr)):eid for eid,rr in eligible.items()}
    require(len(lookup)==len(eligible),'Ambiguous authoritative episode key')
    mapping={}; used=set(l2by)
    for name,rr in oldgroups.items():
        key=business_key(episode(rr))
        require(key in lookup,'Previous episode is outside authoritative selected coverage')
        new=lookup[key]
        require(new not in used,'Overlapping supplied episode sources')
        require(event_business(rr)==event_business(eligible[new]),'Previous CSV1 differs in business fills: '+name)
        for field in ['total_qty','entry_notional','entry_vwap','event_count','episode_start','episode_end']:
            require(rr[0][field]==eligible[new][0][field],'Previous episode metric mismatch: '+field)
        used.add(new);mapping[name]=new
    require(used==set(eligible),'Supplied CSVs omit authoritative selected episodes')
    require(all(name in eligible for name in l2by),'Unknown L2 episode')
    for name,r in l2by.items():
        require(r['book_state_point']=='post_takeover_l2_snapshot','Supplied early CSV2 is not published L2')
        ep=episode(eligible[name])
        require(all(str(ep[k])==r[k] for k in ['episode_id','episode_input_hash','coin','direction','tn','target_block','total_qty','entry_notional','entry_vwap']),
                'Early CSV1/CSV2 association differs')
    # Recompute every provided old result with the original L4 adapter/engine.
    for name,r in laterby.items():
        expected=calculate(episode(oldgroups[name]),r,r['run_id'])
        require(all(expected[k]==prior3[name][k] for k in NUMERIC_RESULTS+['status','book_hash']),
                'Previous CSV3 does not match the protected engine: '+name)
        require(prior3[name]['csv2_row_hash']==store.digest(r),'Previous CSV2 dependency hash mismatch')
    early_id='csv3_l2_'+store.digest([sha(csv1_path),sha(l2_path)])[:12]
    merge_id='combined_'+store.digest([sha(csv1_path),sha(l2_path),sha(old_zip),start,end])[:12]
    issues=[r for r in full if r['record_type']=='issue']
    early1=[r for eid in l2by for r in groups[eid]]+issues
    early1=normalize_fill_rows(early1,early_id,start,'2025-10-22T00:00:00.000Z')
    early2=[{**r,'run_id':early_id} for r in l2rows]
    early3=[calculate(episode(groups[r['episode_id']]),r,early_id) for r in early2]
    require(inputs=={'csv1_sha256':sha(csv1_path),'l2_csv2_sha256':sha(l2_path),'previous_zip_sha256':sha(old_zip)},
            'A supplied input changed during verification')
    early_hashes=write_run(output/'l2_csv3_run',[early1,early2,early3],dict(version=VERSION,kind='published_l2_csv3',inputs=inputs,run_id=early_id))
    merged1=[r for eid in l2by for r in groups[eid]]
    for name,rr in oldgroups.items():merged1.extend({**r,'episode_id':mapping[name]} for r in rr)
    # Preserve tied fills in their original episode order: it is part of the
    # episode_input_hash. Sort groups without reordering their individual fills.
    merged_groups=defaultdict(list)
    for r in merged1:merged_groups[r['episode_id']].append(r)
    merged1=[r for name in sorted(merged_groups,key=lambda name:(merged_groups[name][0]['event_time'],name))
             for r in merged_groups[name]]
    merged1=normalize_fill_rows(merged1+issues,merge_id,start,end)
    merged2=[{**r,'run_id':merge_id} for r in l2rows]
    merged2.extend({**r,'episode_id':mapping[r['episode_id']],'run_id':merge_id,'snapshot_lag_ms':''} for r in old2)
    merged2.sort(key=lambda r:(r['tn'],r['coin'],r['direction']))
    m2={r['episode_id']:r for r in merged2}
    merged3=[{**r,'run_id':merge_id,'csv2_row_hash':store.digest(m2[r['episode_id']])} for r in early3]
    merged3.extend({**r,'episode_id':mapping[r['episode_id']],'run_id':merge_id,
                    'csv2_row_hash':store.digest(m2[mapping[r['episode_id']]])} for r in old3)
    merged3.sort(key=lambda r:(r['tn'],r['coin'],r['direction']))
    # All source hashes remain valid because IDs/run/range are outside episode_hash.
    group_rows(merged1)
    combined_hashes=write_run(output/'combined_run',[merged1,merged2,merged3],
        dict(version=VERSION,kind='mixed_l2_l4_results',inputs=inputs,run_id=merge_id,old_episode_id_map=mapping,start=start,end=end))
    result=dict(status='PASS',version=VERSION,inputs=inputs,early_episodes=len(early2),
        old_episodes=len(old2),combined_episodes=len(merged2),fills=len(merged1)-len(issues),csv1_issues=len(issues),
        early_csv3_states=dict(Counter(r['status'] for r in early3)),
        combined_csv3_states=dict(Counter(r['status'] for r in merged3)),
        unknown_l2_executed_order_counts=sum(r['status']!='skipped' and r['executed_order_count']=='' for r in early3),
        unknown_target_blocks=sum(not r['target_block'] for r in merged2),
        old_event_id_namespace_preserved=True,old_fill_business_tuples_verified=len(old1),
        old_all_numeric_results_preserved=True,authoritative_episode_coverage_verified=True,
        old_episode_id_map=mapping,early_hashes=early_hashes,combined_hashes=combined_hashes)
    (output/'generation_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv1',required=True,type=Path)
    parser.add_argument('--l2-csv2',required=True,type=Path)
    parser.add_argument('--previous-zip',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    result=deliver(args.csv1,args.l2_csv2,args.previous_zip,args.output)
    print(json.dumps({k:v for k,v in result.items() if k!='old_episode_id_map'},indent=2))


if __name__=='__main__':main()
