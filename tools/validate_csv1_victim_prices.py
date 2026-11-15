"""Offline integrity gate for the additive CSV1 victim-price research result."""
import argparse,csv,hashlib,json
from collections import Counter
from decimal import Decimal
from pathlib import Path

ADDED_COLUMNS=['被接管用户地址','被接管用户平均开仓价','平均开仓价核验状态','平均开仓价来源',
 '平均开仓价未补全原因','平均开仓价_平仓损益隐含值','损益隐含值说明','清算标记价','清算标记价来源','清算标记价未补全原因',
 '清算标记价_单仓位账本隐含值','账本隐含标记价说明']

def check(path,source=None):
    raw=path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf'),'Expected UTF-8 BOM'
    assert b'\n' not in raw.replace(b'\r\n',b''),'Expected CRLF'
    with path.open(encoding='utf-8-sig',newline='') as h:
        rows=list(csv.reader(h))
    header=rows[0]
    assert len(header)==58 and header[45]=='accountValue' and header[46:]==ADDED_COLUMNS
    assert all(len(r)==58 for r in rows)
    if source:
        with source.open(encoding='utf-8-sig',newline='') as h:original=list(csv.reader(h))
        assert len(original)==len(rows)
        assert all(a[:46]==b for a,b in zip(rows,original)),'Original cells changed'
    counts=Counter();ids=set();episodes=set()
    for values in rows[1:]:
        r=dict(zip(header,values))
        if r['记录类型']!='fill':
            counts['diagnostic_rows']+=1;assert all(v=='' for v in values[46:]);continue
        counts['fills']+=1
        event=r['接管事件编号'];assert event and event not in ids;ids.add(event)
        assert r['accountValue']!='';Decimal(r['accountValue'])
        user=r['被接管用户地址'];assert len(user)==42 and user.startswith('0x') and all(c in '0123456789abcdef' for c in user[2:])
        for col,count in [('被接管用户平均开仓价','average_entry_reconstructed'),('平均开仓价_平仓损益隐含值','pnl_implied_entry'),('清算标记价','mark_price')]:
            value=r[col]
            if value:
                n=Decimal(value);assert n.is_finite() and n>0
                counts[count]+=1
        if r['被接管用户平均开仓价']:
            assert r['平均开仓价核验状态']=='成交链重建（理论加权成本）'
            assert r['平均开仓价来源'] and not r['平均开仓价未补全原因']
        else:
            assert r['平均开仓价未补全原因'];counts['average_entry_unfilled']+=1
        if r['平均开仓价_平仓损益隐含值']:
            assert '非原生账本成本' in r['损益隐含值说明']
        if r['清算标记价']:
            assert r['清算标记价来源'] and not r['清算标记价未补全原因']
        else:
            assert r['清算标记价未补全原因'];counts['mark_unfilled']+=1
            reference=Decimal(r['清算标记价_单仓位账本隐含值']);assert reference.is_finite() and reference>0
            assert '仅参考' in r['账本隐含标记价说明'];counts['ledger_implied_mark_reference']+=1
    assert counts['fills']==22044 and counts['diagnostic_rows']==4
    assert counts['mark_price']==22041 and counts['mark_unfilled']==3
    assert counts['pnl_implied_entry']==22041
    assert counts['average_entry_reconstructed']+counts['average_entry_unfilled']==22044
    return {'status':'PASS','file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
            'columns':58,'rows':len(rows)-1,'counts':dict(counts),'source_cells_compared':source is not None,
            'comparison_scope':'Original 46 columns including accountValue, headers, row ordering and diagnostic rows' if source else 'Curated result structural integrity; provenance checked locally'}

def main():
    p=argparse.ArgumentParser();p.add_argument('csv',type=Path);p.add_argument('--source',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    report=check(a.csv,a.source);text=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if a.output:a.output.write_text(text,encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
if __name__=='__main__':main()
