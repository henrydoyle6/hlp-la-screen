"""Offline EP000409: first complete exchange-time batch strictly after Tn.

CoinAPI alone constructs the candidate. Native CSV2 and a local boundary diff
are separate validation answers. No network, credentials or future seed rescue.
"""
import csv
import gzip
import hashlib
import itertools
import json
import sys
import zipfile
from collections import Counter
from decimal import Decimal, localcontext
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
ROOT=None
sys.path.insert(0,str(REPO))
import official_l2 as layout
import csv_pipeline as pipeline
import hlp_la_screen as engine


def save(name,value):
    (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def active(book):
    return {str(oid):o for oid,o in book.items() if Decimal(o['qty'])>0}


def comparison(left,right):
    left,right=active(left),active(right)
    details={'only_candidate':[dict(oid=k,**left[k]) for k in sorted(set(left)-set(right))],
        'only_reference':[dict(oid=k,**right[k]) for k in sorted(set(right)-set(left))],
        'changed':[{'oid':k,'candidate':left[k],'reference':right[k]} for k in sorted(set(left)&set(right)) if left[k]!=right[k]]}
    return {'difference_count':sum(map(len,details.values())),'details':details}


def canonical(book,coin):
    result=engine.OrderBook(coin)
    for oid,order in book.items():
        result._insert_snapshot_order(dict(coin=coin,oid=oid,**order))
    return result


def read_boundary_metadata(raw_directory,answer_ns,coin='WIF'):
    """Bounded local boundary lookup; file hour can follow embedded UTC hour.

    This returns metadata only. A native boundary must never be applied blindly:
    its events may already be included in the provider's preceding segment.
    """
    stamp=layout.time_text_ns(answer_ns);hour=int(stamp[11:13]);day=stamp[:10].replace('-','')
    for candidate_hour in (hour,hour+1):
        if candidate_hour>23:continue
        path=Path(raw_directory)/(str(candidate_hour)+'.zip')
        if not path.is_file():continue
        member='hourly/'+day+'/'+str(candidate_hour)
        with zipfile.ZipFile(path) as archive,archive.open(member) as stream:
            for line_number,line in enumerate(stream,1):
                if line_number>50:break
                block=json.loads(line)
                value=layout.parse_time_ns(block['block_time'].removesuffix('Z')+'Z')
                if ((value+50)//100)*100==answer_ns:
                    return dict(source=str(path),member=member,line=line_number,line_sha256=hashlib.sha256(line).hexdigest(),
                        block=block['block_number'],block_time=block['block_time'],
                        coin_events=sum(e.get('coin')==coin for e in block['events']),
                        metadata_only=True,native_bridge_applied=False)
    return None


def main():
    task=json.loads((ROOT/'task.json').read_text(encoding='utf-8'))
    download=json.loads((ROOT/'download_record.json').read_text(encoding='utf-8'))
    with (ROOT/'csv1_sample.csv').open(encoding='utf-8-sig',newline='') as stream:
        fills=list(csv.DictReader(stream))
    assert len(fills)==4 and all(r['episode_id']=='EP000409' and r['episode_input_hash']==task['episode_input_hash'] for r in fills)
    source=ROOT/'raw'/Path(download['object']['Key']).name
    assert hashlib.sha256(source.read_bytes()).hexdigest()==download['sha256']
    midnight=layout.parse_time_ns(task['tn'][:10]+'T00:00:00Z')
    cutoff=layout.parse_time_ns(task['tn'])
    def stamp(clock):
        hours,minutes,seconds=clock.removesuffix('Z').split(':')
        whole,_,fraction=seconds.partition('.')
        if len(fraction)>9 or not (0<=int(hours)<24 and 0<=int(minutes)<60 and 0<=int(whole)<60):
            raise ValueError('Invalid provider clock')
        return midnight+(int(hours)*3600+int(minutes)*60+int(whole))*10**9+int(fraction.ljust(9,'0'))
    # Inspect all timestamps before choosing a state. A provider snapshot can
    # overlap the preceding increment segment; source row order alone is not
    # a checkpoint-time boundary. No sorting or rewriting of increments.
    inventory=[];backsteps=[];frontier=None;frontier_count=0;prior=None;open_snapshot=None
    with gzip.open(source,'rt',encoding='utf-8-sig',newline='') as stream:
        for index,raw in enumerate(csv.DictReader(stream,delimiter=';'),1):
            t=stamp(raw['time_exchange'])
            if prior is not None and t<prior:
                backsteps.append(dict(row=index,previous=layout.time_text_ns(prior),current=layout.time_text_ns(t),type=raw['update_type']))
            if t>cutoff:
                if frontier is None or t<frontier:frontier=t;frontier_count=1
                elif t==frontier:frontier_count+=1
            if raw['update_type']=='SNAPSHOT':
                if open_snapshot is None or t!=open_snapshot['time_ns']:
                    open_snapshot=dict(time_ns=t,first_row=index,last_row=index)
                    inventory.append(open_snapshot)
                open_snapshot['last_row']=index
            else:open_snapshot=None
            prior=t
    chosen_seed=max((s for s in inventory if s['time_ns']<cutoff),key=lambda s:s['time_ns'])
    chosen_answer=min((s for s in inventory if s['first_row']>chosen_seed['last_row'] and s['time_ns']>frontier),key=lambda s:s['time_ns'])
    profile=Counter();previous_exchange=previous_receipt=None
    def observed_rows():
        nonlocal previous_exchange,previous_receipt
        with gzip.open(source,'rt',encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream,delimiter=';')
            if reader.fieldnames!=['time_exchange','time_coinapi','update_type','is_buy','entry_px','entry_sx','order_id']:
                raise ValueError('Only observed legacy seven-column schema validated; never ignore an unexpected height field')
            for index,raw in enumerate(reader,1):
                if None in raw or any(v is None for v in raw.values()):raise ValueError('Malformed source CSV')
                if raw['is_buy'] not in {'0','1'} or not raw['order_id'].isdigit():raise ValueError('Invalid OID or side')
                price,qty=Decimal(raw['entry_px']),Decimal(raw['entry_sx'])
                if not price.is_finite() or not qty.is_finite() or price<=0 or qty<0:raise ValueError('Invalid price/quantity')
                t,r=stamp(raw['time_exchange']),stamp(raw['time_coinapi'])
                if previous_exchange is not None and t<previous_exchange:profile['exchange_backsteps']+=1
                if previous_receipt is not None and r<previous_receipt:profile['receipt_backsteps']+=1
                previous_exchange,previous_receipt=t,r
                kind=raw['update_type']
                if kind not in {'SNAPSHOT','SET','DELETE'}:raise ValueError('Unknown update type')
                profile[kind]+=1;profile['source_rows']+=1
                yield dict(index=index,time_ns=t,receipt_ns=r,type=kind,oid=raw['order_id'],side='bid' if raw['is_buy']=='1' else 'ask',price=layout.dec_text(price),qty=layout.dec_text(qty))
    def snapshot(batch):
        result={}
        for r in batch:
            if r['oid'] in result:raise ValueError('Duplicate OID in complete snapshot')
            result[r['oid']]={k:r[k] for k in ('side','price','qty')}
        return result
    def apply(book,batch,stats):
        for r in batch:
            if r['type']=='SET':
                book[r['oid']]={k:r[k] for k in ('side','price','qty')}
                if Decimal(r['qty'])==0:stats['zero_size_sets_retained_until_delete']+=1
            elif r['type']=='DELETE':
                if r['oid'] not in book:stats['delete_missing_oid']+=1
                else:del book[r['oid']]
            else:raise ValueError('Snapshot inside incremental batch')
    seed=None;book=None;candidate=None;answer=None;checkpoint=None;pre_answer=None;checkpoint_book=None
    checkpoint_stats=Counter();overlap_rows=0
    replay=Counter();candidate_replay=None;candidate_meta=None;snapshots=[];prior_time=None
    for timestamp,group in itertools.groupby(observed_rows(),key=lambda r:r['time_ns']):
        batch=list(group)
        if len(batch)>100000:raise ValueError('Unexpectedly large timestamp batch')
        is_snapshot=all(r['type']=='SNAPSHOT' for r in batch)
        if any(r['type']=='SNAPSHOT' for r in batch) and not is_snapshot:raise ValueError('Mixed snapshot/diff timestamp requires separate validation')
        meta=dict(time=layout.time_text_ns(timestamp),time_ns=timestamp,first_row=batch[0]['index'],last_row=batch[-1]['index'],rows=len(batch))
        if is_snapshot:
            snapshots.append({k:v for k,v in meta.items() if k!='time_ns'})
            if timestamp<cutoff:
                seed=meta;book=snapshot(batch);replay=Counter();replay['seed_rows']=len(batch)
                checkpoint_book=book.copy();checkpoint_stats=Counter()
            elif candidate is not None and answer is None:
                assert timestamp==chosen_answer['time_ns'] and batch[0]['index']==chosen_answer['first_row']
                answer=meta;pre_answer=checkpoint_book.copy();checkpoint=comparison(checkpoint_book,snapshot(batch));answer_book=snapshot(batch)
        elif book is not None and answer is None:
            apply(book,batch,replay);replay['applied_update_rows']+=len(batch)
            if timestamp<=chosen_answer['time_ns']:apply(checkpoint_book,batch,checkpoint_stats)
            else:overlap_rows+=len(batch)
            if timestamp>cutoff and candidate is None:
                candidate=book.copy();candidate_replay=dict(replay)
                candidate_meta=dict(meta,previous_batch_time=layout.time_text_ns(prior_time) if prior_time else None,last_receipt_time=layout.time_text_ns(max(r['receipt_ns'] for r in batch)),batch_update_types=dict(Counter(r['type'] for r in batch)))
        prior_time=timestamp
    if candidate is None or seed is None or answer is None:raise ValueError('Missing pre-seed, first post-Tn batch or later answer; no substitution')
    assert seed['first_row']==chosen_seed['first_row']
    assert candidate_meta['time_ns']==frontier and candidate_meta['rows']==frontier_count
    if any(seed['first_row']<=x['row']<=candidate_meta['last_row'] for x in backsteps):raise ValueError('Timestamp backstep inside candidate replay interval')
    if candidate_replay.get('delete_missing_oid'):raise ValueError('Missing DELETE order in candidate interval')
    assert seed['time_ns']<cutoff<candidate_meta['time_ns']<answer['time_ns']
    assert layout.parse_time_ns(candidate_meta['previous_batch_time'])<=cutoff
    state=canonical(candidate,task['coin'])
    side='ask' if task['direction']=='short' else 'bid'
    metrics=pipeline.book_metrics(state,side)
    orders=json.loads(metrics['orders_json'])
    bids,asks=state.side_orders('bid'),state.side_orders('ask')
    crossed=bool(bids and asks and bids[0].price>=asks[0].price)
    if crossed:
        save('crossed_book_diagnostics.json',{'best_bid':str(bids[0].price),'best_ask':str(asks[0].price),
            'bid_head':[dict(oid=str(o.oid),price=str(o.price),qty=str(o.qty)) for o in bids[:10]],
            'ask_head':[dict(oid=str(o.oid),price=str(o.price),qty=str(o.qty)) for o in asks[:10]]})
    # Resolve hour spill using content. Metadata cannot establish that boundary
    # events are missing, so do not patch them into the validation state.
    boundary={'required':bool(checkpoint['difference_count']),'used_in_candidate':False}
    if checkpoint['difference_count']:
        metadata=read_boundary_metadata(Path('E:/node_raw_book_diffs_by_block/20260506'),answer['time_ns'],task['coin'])
        if metadata:boundary.update(metadata)
    baseline_path=REPO/'results/csv123_20250322_20260518/csv2_20250322_20260518.csv'
    with baseline_path.open(encoding='utf-8-sig',newline='') as stream:
        baseline=next(r for r in csv.DictReader(stream) if r.get('episode_id')==task['episode_id'])
    for field in ('coin','direction','tn','target_block','total_qty','entry_notional','entry_vwap'):
        if str(baseline[field])!=str(task[field]):raise ValueError('Native comparison business input mismatch: '+field)
    baseline_orders=json.loads(baseline['orders_json'])
    selected={o['oid']:{'side':side,'price':o['price'],'qty':o['qty']} for o in orders}
    reference={o['oid']:{'side':side,'price':o['price'],'qty':o['qty']} for o in baseline_orders}
    native=comparison(selected,reference)
    with localcontext() as ctx:
        ctx.prec=engine.CALC_PRECISION
        assert sum(Decimal(o['qty']) for o in orders)==Decimal(metrics['total_depth'])
        assert sum(Decimal(o['price'])*Decimal(o['qty']) for o in orders)==Decimal(metrics['side_notional'])
    assert len({o['oid'] for o in orders})==len(orders) and all(Decimal(o['qty'])>0 for o in orders)
    assert metrics['book_hash']==layout.digest(orders)
    lag=layout.dec_text(Decimal(candidate_meta['time_ns']-cutoff)/1000000)
    issues=[{'code':'POST_TN_TIME_APPROXIMATION','message':'Earliest complete observed timestamp batch strictly after Tn; no provider height or certified block end.'},{'code':'LEGACY_SEVEN_COLUMN_SCHEMA','message':'No user or trigger flags; no HLP liquidity exclusion applied.'}]
    if checkpoint['difference_count']:issues.append({'code':'PROVIDER_SNAPSHOT_BOUNDARY','message':'Raw later checkpoint difference retained separately; native boundary used for validation only.'})
    row={k:'' for k in layout.CSV2_FIELDS}
    for k in ('episode_id','episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap','partial_episode','partial_reason','split_before','split_after'):row[k]=fills[0][k]
    row.update(schema_version='hlp-csv-v3',run_id='coinapi_ep000409_post_tn_20261113',record_type='episode',status='book_ready',reason_code='POST_TN_TIME_APPROXIMATE_L4',reason_message='First full observed exchange timestamp batch strictly after Tn; no block-end claim.',issues_json=layout.text(issues),tn=task['tn'],target_block=str(task['target_block']),book_state_point='post_tn_time_approximate_l4',book_side=side,snapshot_file='https://s3.flatfiles.coinapi.io/coinapi/'+download['object']['Key'],snapshot_time=candidate_meta['time'],snapshot_source_id=layout.digest({'object':download['object']['Key'],'sha256':download['sha256'],'selected_time':candidate_meta['time']}),snapshot_selection_status='first_complete_observed_batch_strictly_after_tn',replay_coverage_mode='provider_exchange_time_first_after_tn',diff_sources_json=layout.text([{'object':download['object']['Key'],'sha256':download['sha256'],'seed':seed,'selected_batch':candidate_meta,'state_lag_ms':lag,'stats':candidate_replay}]),snapshot_lag_ms=lag)
    row.update(metrics)
    row={k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in row.items()}
    candidate_row=dict(row,status='diagnostic_only',reason_code='UNACCEPTED_COINAPI_CANDIDATE',reason_message='Observed state retained only for exact comparison; not executable liquidity.')
    candidate_output=ROOT/'candidate_EP000409_coinapi_l4_post_tn_DIAGNOSTIC_ONLY.csv'
    with candidate_output.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=layout.CSV2_FIELDS);writer.writeheader();writer.writerow(candidate_row)
    checkpoint_valid=checkpoint['difference_count']==0 or boundary.get('comparison_after_boundary',{}).get('difference_count')==0
    invalid=crossed or not checkpoint_valid
    if invalid:
        issues.append({'code':'COINAPI_SOURCE_STATE_INCONSISTENT','message':'Crossed candidate and/or unreconciled later checkpoint. No seed replacement, order deletion or future-state rescue.'})
        row.update(status='skipped',reason_code='COINAPI_SOURCE_STATE_INCONSISTENT',reason_message='Source state failed validation; observed metrics/orders are in a separate diagnostic candidate, not this CSV2.',issues_json=layout.text(issues))
        for key in metrics:row[key]=''
    output=ROOT/'csv2_EP000409_coinapi_l4_post_tn.csv'
    with output.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=layout.CSV2_FIELDS);writer.writeheader();writer.writerow(row)
    with output.open(encoding='utf-8',newline='') as stream:
        reread=list(csv.DictReader(stream));assert len(reread)==1 and reread[0]==row and len(row)==47
    result=dict(episode_id=task['episode_id'],coin=task['coin'],direction=task['direction'],tn=task['tn'],target_block_reference_only=task['target_block'],source_has_block_labels=False,book_state_point=row['book_state_point'],snapshot_lag_ms=lag,selection_rule=task['time_policy'],seed=seed,selected_batch=candidate_meta,candidate_replay=candidate_replay,profile=dict(profile),provider_snapshots=snapshots,ordering_diagnostics={'backsteps':backsteps,'global_first_post_tn_time':layout.time_text_ns(frontier),'global_selected_timestamp_rows':frontier_count,'candidate_interval_backsteps':0},later_checkpoint={'answer':answer,'raw_comparison':checkpoint,'boundary_validation':boundary,'coverage':'Source increments in (S,A], using exchange timestamps; A is an answer only','overlap_tail_updates_after_answer_excluded_from_validation':overlap_rows,'replay_stats':dict(checkpoint_stats)},native_comparison={'path':str(baseline_path),'file_sha256':hashlib.sha256(baseline_path.read_bytes()).hexdigest(),'book_hash':baseline['book_hash'],'episode_input_hash':baseline['episode_input_hash'],'input_hash_note':'Historical reference predates provenance repair; all business inputs checked equal; answer only.',**native},metrics={k:v for k,v in metrics.items() if k!='orders_json'},crossed_book=crossed,csv2_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),remaining_episodes_processed=0,csv3_generated=False,issues=issues)
    result['validation_status']='FAILED_COINAPI_SOURCE_STATE_INCONSISTENCY' if invalid else 'CANDIDATE_BUILT_AND_COMPARED_NO_AUTOMATIC_USER_ACCEPTANCE'
    result['candidate_file']=candidate_output.name
    result['candidate_sha256']=hashlib.sha256(candidate_output.read_bytes()).hexdigest()
    result['successful_episode_count']=0 if invalid else 1
    result['failed_episode_count']=1 if invalid else 0
    usage=ROOT/'usage_receipt.json'
    if usage.is_file():result['account_usage']=json.loads(usage.read_text(encoding='utf-8'))
    save('assessment.json',result)
    save('comparison.json',result['native_comparison'])
    fields=['source','episode_id','tn','book_state_point','snapshot_time','snapshot_lag_ms','order_count','price_level_count','total_depth','side_notional','best_price','worst_price','book_hash']
    with (ROOT/'comparison.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        writer.writerow(dict(candidate_row,source='CoinAPI_post_Tn_unaccepted_candidate'));writer.writerow(dict(baseline,source='Existing_native_block_end'))
    print(json.dumps({k:result[k] for k in ('episode_id','tn','book_state_point','snapshot_lag_ms','seed','selected_batch','candidate_replay','profile','metrics','crossed_book','csv2_sha256')},ensure_ascii=True,indent=2))
    print(json.dumps({'native_difference_count':native['difference_count'],'native_difference_types':{k:len(v) for k,v in native['details'].items()},'later_checkpoint_difference_count':checkpoint['difference_count'],'boundary_validation':boundary},ensure_ascii=True,indent=2))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workdir',type=Path,help='Explicit local pilot directory containing task, CSV1, download receipt and raw object')
    ROOT=parser.parse_args().workdir.resolve()
    main()
