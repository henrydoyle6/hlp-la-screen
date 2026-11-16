"""Portable offline comparison against the immutable released CSV1 baseline."""
import csv,hashlib,json,math
from collections import Counter
from decimal import Decimal as D,localcontext
from pathlib import Path
BASELINE_SHA='dd929446da5c778142ad41c5e26560bb2c51276f5dbe30f542d40cb302a84e10'
def matrix(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as h:return list(csv.reader(h))
def check(output,baseline,audit):
    raw=Path(output).read_bytes();assert raw.startswith(b'\xef\xbb\xbf') and b'\n' not in raw.replace(b'\r\n',b'')
    assert hashlib.sha256(Path(baseline).read_bytes()).hexdigest()==BASELINE_SHA
    old=matrix(baseline);new=matrix(output);assert len(old)==len(new)==22049 and old[0]==new[0] and len(new[0])==58
    assert all(len(r)==58 for r in new);header=new[0];idx={v:k for k,v in enumerate(header)}
    evidence=json.loads(Path(audit).read_text(encoding='utf-8'));details={r['event_id']:r for r in evidence['details']}
    marks={r['event_id']:r for r in evidence['missing_mark_details']};assert len(details)==166 and len(marks)==3
    assert set(details)=={r[idx['接管事件编号']] for r in old[1:] if r[idx['记录类型']]=='fill' and not r[idx['被接管用户平均开仓价']]}
    assert set(marks)=={r[idx['接管事件编号']] for r in old[1:] if r[idx['记录类型']]=='fill' and not r[idx['清算标记价']]}
    counts=Counter();ids=set();changed=0
    for before,after in zip(old[1:],new[1:]):
        assert before[:46]==after[:46]
        event=before[idx['接管事件编号']];allowed=set()
        if event in details:allowed.update(idx[c] for c in ['被接管用户平均开仓价','平均开仓价核验状态','平均开仓价来源','平均开仓价未补全原因'])
        if event in marks:allowed.add(idx['清算标记价未补全原因'])
        for col,(a,b) in enumerate(zip(before,after)):
            if a!=b:assert col in allowed,(event,header[col]);changed+=1
        if after[idx['记录类型']]!='fill':assert before==after;counts['diagnostic_rows']+=1;continue
        counts['fills']+=1;assert event and event not in ids;ids.add(event)
        entry=after[idx['被接管用户平均开仓价']];mark=after[idx['清算标记价']]
        assert after[idx['accountValue']]!='';D(after[idx['accountValue']])
        if entry:assert D(entry).is_finite() and D(entry)>0;counts['average_entry_filled']+=1
        if mark:assert D(mark).is_finite() and D(mark)>0;counts['mark_filled']+=1
        if event not in details:continue
        e=details[event];assert entry==(e['average_entry'] or '')
        assert e['user']==after[idx['被接管用户地址']] and e['coin']==after[idx['币种']]
        assert after[idx['平均开仓价未补全原因']]==e['remaining_reason_zh']
        assert D(e['max_ignored_tail'])<=D(e['quantity_policy']['cap'])
        if e['ignored_tail_count']:counts['events_with_tolerated_tails']+=1
        if entry:
            counts['newly_filled_entry']+=1
            assert not e['remaining_reason_zh'] and e['source_zh']
            if e['recovery_method']=='local_history':
                counts['newly_reconstructed']+=1
                assert entry==e['local_average_entry']
                assert e['anchor']['time']<=e['time_ms'] and e['source_time_coverage']['covered'] is True
                assert not e['source_time_coverage']['gaps']
                if e['ignored_tail_count']:counts['recovered_with_tolerated_tails']+=1
                if not e['entry_without_quantity_tolerance']:counts['recovery_requires_quantity_tolerance']+=1
            else:
                assert e['recovery_method']=='third_party_pre_fill_state' and not e['local_average_entry']
                counts['newly_from_archive_state']+=1
                external=e['archive_verification'];p=external['proof']
                assert external['entry']==entry and external['reason']=='verified_archive_pre_fill_state'
                assert p['candidate_entry']==entry and p['target_identity_and_financial_fields_verified']
                assert p['no_future_entry_state'] and p['target_millisecond_complete'] and p['provider_quality']=='complete'
                assert p['requested_asof_ms']==p['returned_asof_ms']==e['time_ms']
                assert p['finalized_through_ms']>e['time_ms'] and p['built_through_ms']>e['time_ms']
                assert p['provider_source']=='reconstructed' and p['entry_field'] in ['asof.entry_price','same_millisecond.entry_price_after']
                assert len(p['provider_state_response_sha256'])==64 and p['provider_state_response_sha256']==external['state_receipt']['sha256']
                assert external['state_receipt']['http_status']==200 and external['changes_receipts']
                assert all(x['http_status']==200 and len(x['sha256'])==64 for x in external['changes_receipts'])
                with localcontext() as context:
                    context.prec=60
                    native=D(p['pnl_native_usdc']);price=D(p['native_close_price']);quantity=D(p['native_closed_size']);cost=D(entry)
                    predicted=(price-cost)*quantity*(D(1) if D(p['native_start_position'])>0 else D(-1))
                    def ulp(value):return D.from_float(math.ulp(float(value)))*8
                    bound=ulp(native)+ulp(predicted)+abs(quantity)*(ulp(price)+ulp(cost))+abs(price-cost)*ulp(quantity)
                    assert predicted==D(p['pnl_predicted_usdc']) and abs(predicted-native)==D(p['pnl_abs_difference_usdc'])
                    assert bound==D(p['pnl_validation_bound_usdc']) and abs(predicted-native)<=bound
                from victim_quantity_precision import QuantityTailPolicy
                policy=QuantityTailPolicy(e['quantity_policy']['szDecimals'],D(e['quantity_policy']['min_observed_fill_size']))
                assert policy.equal(abs(D(p['native_start_position'])),quantity) and D(p['native_fee'])==0
                for tail in p['quantity_tail_adjustments']:
                    assert abs(D(tail['difference']))<=D(tail['tolerance'])<=D(e['quantity_policy']['cap'])
        else:
            assert e['recovery_method']=='unfilled'
            counts['remaining_entry']+=1;counts['remaining_'+e['reason']]+=1;assert e['remaining_reason_zh']
            a=e['archive_verification'];assert a['entry'] is None and a['reason']==e['reason']
            if e['reason']=='archive_entry_disagrees_with_native_close_pnl':
                p=a['proof'];assert D(p['pnl_abs_difference_usdc'])>D(p['pnl_validation_bound_usdc'])
            else:assert e['reason']=='archive_entry_unavailable' and a['provider_quality']=='partial'
        if not e['local_average_entry']:counts['local_unresolved_'+e['local_reason']]+=1
        if e['local_reason']=='position_chain_discontinuity':assert abs(D(e['blocker']['difference']))>D(e['blocker']['tolerance'])
    for event,m in marks.items():
        assert m['fresh_official_query']['http_status']==200 and m['fresh_official_query']['records']==0
        assert m['receiving_official_query']['http_status']==200 and m['receiving_official_query']['records']==0
        row=next(r for r in new[1:] if r[idx['接管事件编号']]==event)
        assert not row[idx['清算标记价']] and D(row[idx['清算标记价_单仓位账本隐含值']])>0
        assert row[idx['清算标记价未补全原因']]==m['reason_zh']
        assert m['archive_liquidations_query']['http_status']==400 and m['archive_liquidations_query']['error_code']=='range_before_coverage'
        assert m['archive_asof_query']['http_status']==200 and not m['archive_asof_query']['positions']
    assert counts['fills']==22044 and counts['diagnostic_rows']==4 and counts['mark_filled']==22041
    assert counts['newly_reconstructed']+counts['newly_from_archive_state']+counts['remaining_entry']==166
    assert counts['newly_filled_entry']==counts['newly_reconstructed']+counts['newly_from_archive_state']
    assert counts['average_entry_filled']==21878+counts['newly_filled_entry']
    for key in ['newly_reconstructed','newly_from_archive_state','newly_filled_entry','remaining_entry','events_with_tolerated_tails','recovered_with_tolerated_tails','recovery_requires_quantity_tolerance']:
        counts.setdefault(key,0)
    return {'status':'PASS','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'columns':58,'data_rows':22048,
        'baseline_sha256':BASELINE_SHA,'counts':dict(counts),'changed_cells':changed,'original_46_columns_preserved':True,
        'all_other_baseline_cells_preserved':True,'remaining_raw_marks':3}
