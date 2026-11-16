"""Verify a third-party pre-fill average entry against native execution evidence."""
import math
from datetime import datetime,timezone
from decimal import Decimal as D
from victim_entry_math import dec,decimal_text,research_precision

def epoch(value):
    if value is None or value=='':return 0
    if isinstance(value,int):return value
    delta=datetime.fromisoformat(value.replace('Z','+00:00'))-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1000+delta.microseconds//1000

@research_precision
def verify_pre_fill_state(state,changes,native,policy):
    result={'entry':None,'reason':None,'method':'third_party_pre_fill_state'}
    def fail(reason,**details):return {**result,'reason':reason,**details}
    meta=state.get('meta') or {};stamp=native['time']
    if state.get('http_status')!=200:return fail('archive_state_http_error')
    if state.get('requested_asof_ms')!=stamp or epoch(meta.get('as_of',0))!=stamp or meta.get('source')!='reconstructed':return fail('archive_state_time_or_source_mismatch')
    if epoch(meta.get('finalized_through',0))<=stamp or epoch(meta.get('built_through',0))<=stamp:return fail('archive_history_not_finalized')
    if meta.get('quality') in ['degraded','incomplete','preliminary','unreconciled']:return fail('archive_degraded_state')
    positions=[p for p in state.get('positions',[]) if p.get('coin',p.get('symbol'))==native['coin']]
    if len(positions)!=1:return fail('archive_position_not_available',account_seen=state.get('account_seen'))
    position=positions[0];q=dec(position['size'])
    entry=dec(position['entry_price']) if position.get('quality')=='complete' and position.get('entry_price') is not None else None
    if entry is not None and entry<=0:return fail('archive_nonpositive_entry')
    if not changes.get('complete'):return fail('archive_target_millisecond_not_complete')
    rows=changes.get('rows') or [];keys=[]
    for row in rows:
        if epoch(row['timestamp'])!=stamp or row.get('coin',row.get('symbol'))!=native['coin']:return fail('archive_change_time_or_market_mismatch')
        if not row.get('finalized'):return fail('archive_unfinalized_change')
        if row.get('seq') is None:return fail('archive_same_millisecond_order_missing')
        keys.append(row['seq'])
    if len(set(keys))!=len(keys):return fail('archive_same_millisecond_order_not_unique')
    rows=sorted(rows,key=lambda x:x['seq']);target=None
    for i,row in enumerate(rows):
        if str(row.get('trade_id'))==str(native['tid']) and str(row.get('order_id'))==str(native.get('oid')) and row['side']==native['side']:
            if target is not None:return fail('archive_native_target_not_unique')
            target=i
    if target is None:return fail('archive_native_target_not_found')
    target_row=rows[target]
    if dec(target_row['price'])!=dec(native['px']) or not policy.equal(target_row['size'],native['sz']) or not policy.equal(target_row['start_position'],native['startPosition']):return fail('archive_target_financial_fields_mismatch')
    if target_row.get('continuity')=='quarantined':return fail('archive_target_quarantined')
    if not policy.equal(target_row['end_position'],0):return fail('archive_target_not_full_close')
    tails=[];entry_source='asof.entry_price';reset=False
    for row in rows[:target]:
        if row.get('continuity')=='quarantined':continue
        before=dec(row['start_position']);after=dec(row['end_position']);size=dec(row['size'])
        if not policy.equal(q,before):return fail('archive_same_millisecond_quantity_gap',expected=decimal_text(q),reported=decimal_text(before))
        if q!=before:tails.append({'difference':decimal_text(before-q),'tolerance':decimal_text(policy.tolerance(q,before))})
        expected=before+(size if row['side']=='B' else -size)
        if not policy.equal(expected,after):return fail('archive_change_arithmetic_mismatch')
        is_reset=policy.equal(before,0) or before*after<0
        if is_reset:reset=True
        q=after
        if policy.equal(after,0):entry=None
        elif (entry is not None or reset) and row.get('entry_price_after') is not None:
            entry=dec(row['entry_price_after']);entry_source='same_millisecond.entry_price_after'
        else:entry=None
    if not policy.equal(q,native['startPosition']):return fail('archive_pre_target_quantity_mismatch',state_size=decimal_text(q),native_start=decimal_text(dec(native['startPosition'])))
    if q!=dec(native['startPosition']):tails.append({'difference':decimal_text(dec(native['startPosition'])-q),'tolerance':decimal_text(policy.tolerance(q,native['startPosition']))})
    if entry is None:return fail('archive_entry_unavailable',provider_quality=position.get('quality'),provider_opened_at=position.get('opened_at'))
    if dec(native.get('fee',0))!=0 or not policy.equal(abs(dec(native['startPosition'])),native['sz']):return fail('native_close_not_suitable_for_entry_validation')
    if native.get('closedPnl') is None:return fail('native_close_pnl_missing')
    observed=dec(native['closedPnl']);predicted=(dec(native['px'])-entry)*dec(native['sz'])*(D(1) if dec(native['startPosition'])>0 else D(-1))
    # Propagate binary64 serialization uncertainty through q * (px - entry).
    # This is only a PnL cross-check; source prices and identity joins stay exact.
    def uncertainty(value):return D.from_float(math.ulp(float(value)))*8
    px=dec(native['px']);quantity=dec(native['sz'])
    bound=(uncertainty(observed)+uncertainty(predicted)
        +abs(quantity)*(uncertainty(px)+uncertainty(entry))
        +abs(px-entry)*uncertainty(quantity))
    difference=abs(predicted-observed)
    proof={'requested_asof_ms':state.get('requested_asof_ms'),'returned_asof_ms':epoch(meta['as_of']),
        'provider_source':meta['source'],'provider_quality':position.get('quality'),'provider_meta_quality':meta.get('quality'),
        'finalized_through_ms':epoch(meta['finalized_through']),'built_through_ms':epoch(meta['built_through']),
        'state_size':position['size'],'native_start_position':native['startPosition'],'target_trade_id':str(native['tid']),
        'target_order_id':str(native.get('oid')),'target_seq':target_row['seq'],'prior_same_millisecond_legs':target,
        'entry_field':entry_source,'candidate_entry':decimal_text(entry),'pnl_predicted_usdc':decimal_text(predicted),
        'native_close_price':native['px'],'native_closed_size':native['sz'],'native_side':native['side'],'native_fee':native.get('fee',0),
        'target_millisecond_complete':True,'same_millisecond_rows':len(rows),
        'pnl_native_usdc':decimal_text(observed),'pnl_abs_difference_usdc':decimal_text(difference),'pnl_validation_bound_usdc':decimal_text(bound),
        'pnl_bound_method':'Eight binary64 ULPs per input and output, propagated through q * (px - entry); no price tolerance in identity matching',
        'quantity_tail_adjustments':tails,'target_identity_and_financial_fields_verified':True,'no_future_entry_state':True,
        'provider_state_response_sha256':state.get('sha256')}
    if difference>bound:return fail('archive_entry_disagrees_with_native_close_pnl',proof=proof)
    return {**result,'entry':decimal_text(entry),'reason':'verified_archive_pre_fill_state','proof':proof}
