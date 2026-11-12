"""Audit a failed CoinAPI pilot and its exact comparison; no raw replay/network.

Passing this audit proves the failure is represented honestly. It does not make
the provider book valid or claim that CI repeated the historical source replay.
"""
import csv
import hashlib
import json
from decimal import Decimal, localcontext
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import official_l2 as layout


def require(value,message):
    if not value:raise ValueError(message)


def read_one(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        require(reader.fieldnames==layout.CSV2_FIELDS,'Expected original 47-column delivery layout')
        rows=list(reader)
    require(len(rows)==1,'Exactly one attempted episode')
    return rows[0]


def verify():
    evidence=json.loads((ROOT/'validation/coinapi_ep000409_20261113.json').read_text(encoding='utf-8'))
    require(evidence['result']=='FAILED_SOURCE_VALIDATION','Do not promote failed evidence')
    paths=[(evidence['csv2_file'],evidence['csv2_sha256']),(evidence['candidate_file'],evidence['candidate_sha256'])]
    for filename,expected in paths:
        require(hashlib.sha256((ROOT/filename).read_bytes()).hexdigest()==expected,'Artifact checksum')
    failed=read_one(ROOT/evidence['csv2_file'])
    candidate=read_one(ROOT/evidence['candidate_file'])
    require(failed['status']=='skipped' and failed['reason_code']=='COINAPI_SOURCE_STATE_INCONSISTENT','Failed source must remain skipped')
    require(candidate['status']=='diagnostic_only','Candidate is not usable liquidity')
    metrics=['order_count','price_level_count','total_depth','side_notional','best_price','worst_price','book_is_empty','orders_json','book_hash']
    require(all(failed[k]=='' for k in metrics),'Failed CSV2 must not expose successful-book fields')
    for row in (failed,candidate):
        require(row['episode_id']=='EP000409' and row['coin']=='WIF' and row['book_side']=='ask','Single last-before-cutoff episode')
        require(row['book_state_point']=='post_tn_time_approximate_l4','No certified block-end claim')
        require(all(row[k]=='' for k in ('snapshot_block','last_applied_block','replay_from_block_exclusive','replay_to_block_inclusive')),'No inferred block height')
        lag=Decimal(layout.parse_time_ns(row['snapshot_time'])-layout.parse_time_ns(row['tn']))/1000000
        require(lag==Decimal(row['snapshot_lag_ms'])==Decimal('0.8601') and lag>0,'First post-Tn time policy')
    full=ROOT/'results/csv1_full_20250322_20261031/csv1_20250322_20261031_zh.csv'
    require(hashlib.sha256(full.read_bytes()).hexdigest()==evidence['episode']['source_csv1_sha256'],'Authoritative CSV1')
    selected=[];last=None
    with full.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.reader(stream);require(next(reader)==layout.CHINESE_CSV1,'CSV1 layout')
        for values in reader:
            fill=dict(zip(layout.CSV1_FIELDS,values))
            if fill['record_type']!='fill':continue
            if layout.parse_time_ns(fill['episode_end'])<layout.parse_time_ns('2026-05-18T00:00:00Z'):
                if last is None or layout.parse_time_ns(fill['episode_end'])>layout.parse_time_ns(last['episode_end']):last=fill
            if fill['episode_id']=='EP000409':selected.append(fill)
    require(last['episode_id']=='EP000409' and len(selected)==4,'Latest complete episode before May 18')
    for row in (failed,candidate):
        for key in ('episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap','partial_episode','partial_reason','split_before','split_after'):
            require(row[key]==selected[0][key],'Changed CSV1 business input '+key)
        require(row['tn']==selected[0]['episode_end'] and row['target_block']==selected[0]['end_block'],'Tn and target association')
    orders=json.loads(candidate['orders_json'])
    require(all(set(o)=={'oid','price','qty'} and all(isinstance(v,str) for v in o.values()) for o in orders),'Order representation')
    require(all(Decimal(o['price'])>0 and Decimal(o['qty'])>0 for o in orders),'Positive observed orders')
    require(len({o['oid'] for o in orders})==len(orders),'Unique OID')
    require(orders==sorted(orders,key=lambda o:(Decimal(o['price']),o['oid'])),'Ask sorting')
    with localcontext() as context:
        context.prec=60
        depth=sum((Decimal(o['qty']) for o in orders),Decimal(0))
        notional=sum((Decimal(o['price'])*Decimal(o['qty']) for o in orders),Decimal(0))
    require(depth==Decimal(candidate['total_depth']) and notional==Decimal(candidate['side_notional']),'Independent Decimal totals')
    require(len(orders)==int(candidate['order_count'])==241,'Observed order count')
    require(len({Decimal(o['price']) for o in orders})==int(candidate['price_level_count'])==220,'Price levels')
    require(orders[0]['price']==candidate['best_price'] and orders[-1]['price']==candidate['worst_price'],'Price bounds')
    require(layout.digest(orders)==candidate['book_hash'],'Canonical candidate hash')
    refpath=ROOT/evidence['native_reference']['path']
    require(hashlib.sha256(refpath.read_bytes()).hexdigest()==evidence['native_reference']['file_sha256'],'Unchanged existing CSV2')
    with refpath.open(encoding='utf-8-sig',newline='') as stream:
        reference=next(r for r in csv.DictReader(stream) if r['episode_id']=='EP000409')
    require(reference['book_state_point']=='block_end' and reference['last_applied_block']==failed['target_block'],'Native reference target')
    for key in ('coin','direction','tn','target_block','total_qty','entry_notional','entry_vwap'):
        require(reference[key]==candidate[key],'Comparison business input '+key)
    left={o['oid']:o for o in orders};right={o['oid']:o for o in json.loads(reference['orders_json'])}
    require(len(right)==108 and len(set(left)-set(right))==133 and not set(right)-set(left),'Exact difference membership')
    require(all(left[k]==right[k] for k in right),'All 108 reference orders exactly match')
    comparison=json.loads((ROOT/'results/coinapi_l4_ep000409/comparison.json').read_text(encoding='utf-8'))
    extra=comparison['details']['only_candidate']
    require({o['oid'] for o in extra}==set(left)-set(right) and not comparison['details']['changed'] and not comparison['details']['only_reference'],'Persisted differences')
    for o in extra:
        require(o['side']=='ask' and o['price']==left[o['oid']]['price'] and o['qty']==left[o['oid']]['qty'],'Difference payload')
    require(Decimal(evidence['crossed_book']['best_bid'])>Decimal(candidate['best_price']),'Crossed source retained as failure')
    require(evidence['source_state_failure']['extra_orders_in_seed']==133,'Seed contamination evidence')
    for example in evidence['source_state_failure']['time_inconsistent_oid_examples']:
        require(example['oid'] in set(left)-set(right),'Time evidence concerns extra OID')
        require(layout.parse_time_ns(example['provider_snapshot_time'])<layout.parse_time_ns(failed['tn'])<layout.parse_time_ns(example['native_new_time']+'Z'),'Snapshot time inconsistency')
        require(not example['used_in_candidate'],'Native answer never used to construct candidate')
    target=evidence['target_batch_validation']
    require(target['source_event_count']==target['native_event_count']==62 and target['events_match_including_order'],'Complete target batch evidence')
    require(target['source_sequence_sha256']==target['native_sequence_sha256'] and not target['used_in_candidate'],'Independent target event sequence')
    require(evidence['later_checkpoint']['difference_count']==610 and evidence['later_checkpoint']['overlap_tail_rows_excluded']==7,'Do not erase later-checkpoint failure')
    require(evidence['scope']==dict(episodes_attempted=1,successful=0,failed=1,remaining_episodes_processed=0,csv3_generated=False),'Stop boundary')
    require(Decimal(evidence['account_usage']['cash_spent_usd'])==0 and evidence['account_usage']['aws_requests']==0,'No cash or AWS activity')
    return dict(audit='PASS',source_validation='FAILED',attempted=1,successful=0,failed=1,native_orders_matched=108,extra_orders=133,snapshot_lag_ms=failed['snapshot_lag_ms'])


if __name__=='__main__':print(json.dumps(verify(),indent=2))
