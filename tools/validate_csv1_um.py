"""Independent whole-CSV oracle: exact rational U/M and immutable input cells."""
import csv,hashlib
from collections import Counter
from fractions import Fraction as F
from pathlib import Path

BASELINE_SHA='73dad8f7502b018bc31810fe45fc541b3afb7e75a61797cb96f9a63a69351a70'
HEADERS=['q_有符号接管数量','U','M','M未计算原因']
def read(file):
    with Path(file).open(encoding='utf-8-sig',newline='') as source:return list(csv.reader(source))
def check(output,baseline):
    raw=Path(output).read_bytes();assert raw.startswith(b'\xef\xbb\xbf') and b'\n' not in raw.replace(b'\r\n',b'')
    assert hashlib.sha256(Path(baseline).read_bytes()).hexdigest()==BASELINE_SHA
    before=read(baseline);after=read(output)
    assert len(before)==len(after)==22049 and len(before[0])==58 and after[0]==before[0]+HEADERS
    assert all(len(row)==62 for row in after)
    idx={value:n for n,value in enumerate(after[0])};counts=Counter();missing=[];seen=set()
    def sign(value):return 'positive' if value>0 else 'negative' if value<0 else 'zero'
    for original,row in zip(before[1:],after[1:]):
        assert original==row[:58]
        q_text,u_text,m_text,reason=row[58:]
        if row[idx['记录类型']]!='fill':
            assert not q_text and not u_text and not m_text and '非接管' in reason
            counts['diagnostic_rows']+=1;continue
        event=row[idx['接管事件编号']];assert event and event not in seen;seen.add(event);counts['fills']+=1
        direction=row[idx['接管方向']];assert direction in ['long','short']
        size=F(row[idx['接管数量']]);assert size>0
        signed=size*(1 if direction=='long' else -1);assert F(q_text)==signed
        counts[direction]+=1
        if size!=F(row[idx['接管总数量']]):counts['row_quantity_differs_from_group_total']+=1
        mean=row[idx['被接管用户平均开仓价']];mark=row[idx['清算标记价']];account=row[idx['accountValue']]
        absent=[]
        if not mean:
            absent.append('被接管用户平均开仓价');counts['missing_mean']+=1
            assert row[idx['平均开仓价未补全原因']] in reason
        if not mark:
            absent.append('原始清算标记价');counts['missing_raw_mark']+=1
            assert row[idx['清算标记价未补全原因']] in reason
        if not account:absent.append('accountValue');counts['missing_account_value']+=1
        if mean and mark:
            expected_u=(F(mark)-F(mean))*signed;assert u_text and F(u_text)==expected_u
            counts['u_calculated']+=1;counts['u_'+sign(expected_u)]+=1
        else:expected_u=None;assert not u_text
        if expected_u is not None and account:
            expected_m=F(account)-expected_u;assert m_text and F(m_text)==expected_m and not reason
            counts['m_calculated']+=1;counts['m_'+sign(expected_m)]+=1
        else:
            assert not m_text and absent and all(name in reason for name in absent)
            counts['m_missing']+=1;missing.append({'event_id':event,'coin':row[idx['币种']],'time_utc':row[idx['接管时间UTC']],
                'missing_inputs':absent,'reason_zh':reason})
    for name in ['missing_mean','missing_raw_mark','missing_account_value','u_positive','u_negative','u_zero','m_positive','m_negative','m_zero']:
        counts.setdefault(name,0)
    assert counts['fills']==22044 and counts['diagnostic_rows']==4 and counts['m_calculated']==22039 and counts['m_missing']==5
    assert counts['missing_mean']==2 and counts['missing_raw_mark']==3 and counts['missing_account_value']==0
    assert counts['long']==20932 and counts['short']==1112 and counts['row_quantity_differs_from_group_total']==21915
    assert counts['u_positive']+counts['u_negative']+counts['u_zero']==counts['u_calculated']==22039
    assert counts['m_positive']+counts['m_negative']+counts['m_zero']==counts['m_calculated']
    return {'status':'PASS','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'columns':62,'data_rows':22048,
        'baseline_sha256':BASELINE_SHA,'counts':dict(counts),'original_58_columns_preserved':True,
        'all_calculated_rows_checked_with_independent_exact_fractions':True,'missing_event_details':missing}
