"""Research-only average-cost reconstruction and separate victim-PnL diagnostics.

No historical native ledger entry price is asserted by either calculation.
"""
from collections import defaultdict
from decimal import Decimal as D, localcontext, ROUND_HALF_EVEN
from functools import wraps
import os,ntpath

def source_key(value):
    return ntpath.normcase(ntpath.normpath(value)) if len(value)>1 and value[1]==':' else os.path.normpath(value)

def research_precision(function):
    @wraps(function)
    def scoped(*args,**kwargs):
        with localcontext() as context:
            context.prec=60
            context.rounding=ROUND_HALF_EVEN
            return function(*args,**kwargs)
    return scoped

def dec(x):
    n=D(str(x))
    if not n.is_finite():raise ValueError('non_finite_decimal')
    return n
def decimal_text(x):
    if x is None:return ''
    s=format(x,'f').rstrip('0').rstrip('.') if '.' in format(x,'f') else format(x,'f')
    return s if s and s!='-0' else '0'
@research_precision
def implied(f):
    """No use of the receiving HLP's PnL. Only pure victim closing fills."""
    if f.get('closedPnl') is None:raise ValueError('missing_victim_closedPnl')
    if f.get('fee') is None:raise ValueError('missing_victim_fee')
    sp=dec(f['startPosition']);q=dec(f['sz']);px=dec(f['px']);pnl=dec(f['closedPnl'])
    if not q>0 or sp==0 or abs(sp)!=q:raise ValueError('not_full_position_close')
    if f['side']!=('A' if sp>0 else 'B'):raise ValueError('not_victim_closing_side')
    if dec(f.get('fee','0'))!=0:raise ValueError('nonzero_fee_requires_separate_convention_check')
    entry=px-pnl/((D(1) if sp>0 else D(-1))*q)
    if entry<=0:raise ValueError('nonpositive_pnl_implied_entry')
    return entry

@research_precision
def order_same_time(records,prior_q):
    if len(records)<2:return records,False
    # A native block and event index establishes stream order. Otherwise
    # require a unique exact startPosition -> endPosition chain.
    if all(r['f'].get('block_number') is not None and r['f'].get('event_index') is not None for r in records):
        return sorted(records,key=lambda r:(r['f']['block_number'],r['f']['event_index'])),False
    if all(r['f'].get('native_order') is not None for r in records):
        files={source_key(r['f']['native_order'][0]) for r in records}
        if len(files)==1:
            return sorted(records,key=lambda r:tuple(r['f']['native_order'][1:])),False
    remaining=list(records);ordered=[];q=prior_q
    if q is None:
        ends={dec(r['f']['startPosition'])+(dec(r['f']['sz']) if r['f']['side']=='B' else -dec(r['f']['sz'])) for r in remaining}
        beginnings=[r for r in remaining if dec(r['f']['startPosition']) not in ends]
        if len(beginnings)!=1:return records,True
        q=dec(beginnings[0]['f']['startPosition'])
    while remaining:
        options=[r for r in remaining if dec(r['f']['startPosition'])==q]
        if len(options)!=1:return records,True
        r=options[0];remaining.remove(r);ordered.append(r)
        q+=dec(r['f']['sz']) if r['f']['side']=='B' else -dec(r['f']['sz'])
    return ordered,False

@research_precision
def replay(records):
    results={};q=None;entry=None;anchor=None;chain=0;failure='no_zero_or_reversal_anchor';previous=[]
    groups=defaultdict(list)
    for r in records:groups[r['f']['time']].append(r)
    for t,group in sorted(groups.items()):
        ordered,ambiguous=order_same_time(group,q)
        if ambiguous:
            for r in group:results[r['id']]={'entry':None,'reason':'ambiguous_same_millisecond_order','chain_length':0}
            q=entry=anchor=None;chain=0;failure='ambiguous_same_millisecond_order';continue
        for r in ordered:
            f=r['f'];sp=dec(f['startPosition']);size=dec(f['sz']);px=dec(f['px'])
            delta=size if f['side']=='B' else -size;after=sp+delta
            if q is not None and q!=sp:
                entry=None;anchor=None;chain=0;failure='position_chain_discontinuity'
            if r['conflict']:
                entry=None;anchor=None;chain=0;failure='conflicting_source_fill_fields'
            valid=entry is not None and q==sp and not r['conflict']
            results[r['id']]={'entry':decimal_text(entry) if valid else None,
                'reason':'' if valid else failure,'anchor':anchor,'chain_length':chain,
                'before_position':decimal_text(sp),'source':r['source'],'source_line':r['source_line']}
            if r['conflict']:
                entry=anchor=None;chain=0;failure='conflicting_source_fill_fields'
            elif sp==0 or sp*after<0:
                entry=px if after else None;anchor={'identity':r['id'],'time':t,'source':r['source'],
                     'source_line':r['source_line'],'kind':'zero_position' if sp==0 else 'reversal_remainder'}
                chain=1;failure=''
            elif after==0:
                entry=None;anchor=None;chain=0;failure='no_zero_or_reversal_anchor'
            elif entry is not None and not r['conflict']:
                if sp*delta>0:entry=(abs(sp)*entry+size*px)/abs(after)
                chain+=1
            q=after
    return results
