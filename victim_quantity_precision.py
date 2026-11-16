"""Quantity-tail tolerant research replay; source quantities/prices are untouched."""
import bisect,math
from dataclasses import dataclass
from decimal import Decimal as D
from collections import defaultdict
from victim_entry_math import dec,decimal_text,research_precision,source_key

@dataclass(frozen=True)
class QuantityTailPolicy:
    sz_decimals:int
    min_observed_fill_size:D|None=None
    absolute_floor:D=D('0.0000001')
    ulps:int=8

    @property
    def cap(self):
        unit=D(1).scaleb(-self.sz_decimals)
        if self.min_observed_fill_size is not None and self.min_observed_fill_size>0:
            unit=min(unit,self.min_observed_fill_size)
        return unit/D(100)

    def tolerance(self,a,b):
        scale=max(abs(dec(a)),abs(dec(b)))
        binary=D.from_float(math.ulp(float(scale)))*self.ulps if scale else D(0)
        return min(self.cap,max(self.absolute_floor,binary))

    def equal(self,a,b):
        a,b=dec(a),dec(b)
        return a==b or abs(a-b)<=self.tolerance(a,b)

    def position(self,value,other):
        value=dec(value)
        return D(0) if abs(value)<=self.tolerance(value,other) else value

def native_order(records):
    if all(r['f'].get('block_number') is not None and r['f'].get('event_index') is not None for r in records):
        keys=[(r['f']['block_number'],r['f']['event_index']) for r in records]
        if len(set(keys))==len(keys):return sorted(records,key=lambda r:(r['f']['block_number'],r['f']['event_index']))
    if all(r['f'].get('native_order') is not None for r in records):
        if len({source_key(r['f']['native_order'][0]) for r in records})==1:
            keys=[tuple(r['f']['native_order'][1:]) for r in records]
            if len(set(keys))==len(keys):return sorted(records,key=lambda r:tuple(r['f']['native_order'][1:]))
    return None

@research_precision
def order_same_time(records,prior_q,policy,diagnostic=None):
    if len(records)<2:return records,False
    known=native_order(records)
    if known is not None:return known,False
    starts=sorted((dec(r['f']['startPosition']),i) for i,r in enumerate(records))
    keys=[a for a,i in starts];remaining=set(range(len(records)));ordered=[]
    def nearby(values,value):
        return range(bisect.bisect_left(values,value-policy.cap),bisect.bisect_right(values,value+policy.cap))
    q=prior_q
    if q is None:
        ends=sorted(dec(r['f']['startPosition'])+(dec(r['f']['sz']) if r['f']['side']=='B' else -dec(r['f']['sz'])) for r in records)
        beginnings=[i for sp,i in starts if not any(policy.equal(sp,ends[j]) for j in nearby(ends,sp))]
        if len(beginnings)!=1:
            if diagnostic is not None:diagnostic.update(stage='starting_position',candidate_count=len(beginnings))
            return records,True
        q=dec(records[beginnings[0]]['f']['startPosition'])
    while remaining:
        options=[starts[j][1] for j in nearby(keys,q) if starts[j][1] in remaining and policy.equal(keys[j],q)]
        if len(options)!=1:
            if diagnostic is not None:diagnostic.update(stage='position_link',candidate_count=len(options),step=len(ordered),expected_position=decimal_text(q))
            return records,True
        index=options[0];remaining.remove(index);r=records[index];ordered.append(r)
        f=r['f'];sp=policy.position(f['startPosition'],f['sz'])
        after=sp+(dec(f['sz']) if f['side']=='B' else -dec(f['sz']))
        q=policy.position(after,max(abs(sp),dec(f['sz'])))
    return ordered,False

@research_precision
def replay(records,policy,target_ids=None):
    """Only target snapshots are materialized, avoiding quadratic audit copies."""
    results={};q=entry=anchor=None;chain=0;failure='no_zero_or_reversal_anchor';detail=None;previous=None
    adjustments=[];groups=defaultdict(list)
    for r in records:groups[r['f']['time']].append(r)
    def note(kind,stamp,left,right,record):
        if left!=right:adjustments.append({'kind':kind,'time_ms':stamp,'left':decimal_text(left),'right':decimal_text(right),
            'difference':decimal_text(right-left),'tolerance':decimal_text(policy.tolerance(left,right)),
            'identity':record['id'],'source':record['source'],'source_line':record['source_line']})
    def snapshot(record,stamp,sp):
        if target_ids is not None and record['id'] not in target_ids:return
        valid=entry is not None and q is not None and policy.equal(q,sp) and not record['conflict']
        results[record['id']]={'entry':decimal_text(entry) if valid else None,'reason':'' if valid else failure,
            'anchor':anchor,'chain_length':chain,'blocker':detail,
            'quantity_tail_adjustments':list(adjustments),'quantity_cap':decimal_text(policy.cap)}
    for stamp,group in sorted(groups.items()):
        ordering_detail={};ordered,ambiguous=order_same_time(group,q,policy,ordering_detail)
        if ambiguous:
            failure='ambiguous_same_millisecond_order';detail={'time_ms':stamp,'records':len(group),'prior_position':decimal_text(q),**ordering_detail}
            entry=anchor=None;chain=0
            for r in group:
                if target_ids is None or r['id'] in target_ids:
                    results[r['id']]={'entry':None,'reason':failure,'anchor':None,'chain_length':0,'blocker':detail,
                        'quantity_tail_adjustments':list(adjustments),'quantity_cap':decimal_text(policy.cap)}
            q=None;previous=None;continue
        for r in ordered:
            f=r['f'];raw_sp=dec(f['startPosition']);size=dec(f['sz']);px=dec(f['px'])
            sp=policy.position(raw_sp,size);delta=size if f['side']=='B' else -size
            raw_after=sp+delta;after=policy.position(raw_after,max(abs(sp),size))
            if q is not None:
                if not policy.equal(q,sp):
                    entry=anchor=None;chain=0;failure='position_chain_discontinuity'
                    detail={'time_ms':stamp,'expected_position':decimal_text(q),'reported_position':decimal_text(raw_sp),
                        'difference':decimal_text(raw_sp-q),'tolerance':decimal_text(policy.tolerance(q,sp)),
                        'source':r['source'],'source_line':r['source_line'],
                        'previous_time_ms':previous['f']['time'] if previous else None,'previous_identity':previous['id'] if previous else None}
                else:note('position_link',stamp,q,sp,r)
            note('near_zero_start',stamp,raw_sp,sp,r)
            if r['conflict']:
                entry=anchor=None;chain=0;failure='conflicting_source_fill_fields';detail={'time_ms':stamp,'identity':r['id']}
            snapshot(r,stamp,sp)
            if r['conflict']:
                entry=anchor=None
            elif sp==0 or sp*after<0:
                entry=px if after else None;anchor={'identity':r['id'],'time':stamp,'source':r['source'],
                    'source_line':r['source_line'],'kind':'zero_position' if sp==0 else 'reversal_remainder'}
                chain=1;failure='';detail=None;adjustments=[]
                note('near_zero_start',stamp,raw_sp,sp,r)
            elif after==0:
                entry=anchor=None;chain=0;failure='no_zero_or_reversal_anchor';detail=None;adjustments=[]
            elif entry is not None:
                if sp*delta>0:entry=(abs(sp)*entry+size*px)/abs(after)
                chain+=1
            note('near_zero_after',stamp,raw_after,after,r)
            q=after;previous=r
    return results
