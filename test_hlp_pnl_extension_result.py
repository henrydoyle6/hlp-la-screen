"""Read-only validation of the published Nov 10 extension; no raw/API access."""
import csv
import hashlib
import json
import unittest
from collections import defaultdict
from fractions import Fraction as F
from pathlib import Path

ROOT=Path(__file__).resolve().parent
NEW=ROOT/'results/hlp_pnl_to_20261110_20261126'
OLD=ROOT/'results/hlp_pnl_with_account_value_20261118'

def read(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        return reader.fieldnames,list(reader)

class PublishedExtension(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence=json.loads((NEW/'validation.json').read_text(encoding='utf-8'))
        cls.h,cls.rows=read(next(NEW.glob('csv1_*.csv')))
        cls.eh,cls.episodes=read(next(NEW.glob('452*.csv')))
        cls.events={r['接管事件编号']:r for r in cls.rows if r['记录类型']=='fill'}
        _,old=read(next(OLD.glob('csv1_*.csv')))
        cls.old={r['接管事件编号']:r for r in old if r['记录类型']=='fill'}

    def test_population_source_hashes_and_no_m(self):
        self.assertEqual(len(self.rows),22320)
        self.assertEqual(len(self.events),22315)
        self.assertEqual(len(self.episodes),452)
        self.assertEqual(len(self.events.keys()-self.old.keys()),271)
        self.assertFalse(any('M' in h for h in self.h+self.eh))
        for name,meta in self.evidence['output_files'].items():
            data=(NEW/name).read_bytes()
            self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
            self.assertEqual(hashlib.sha256(data).hexdigest(),meta['sha256'])

    def test_old_financial_values_and_episode_ids_preserved(self):
        changed={'运行编号','数据结束时间UTC（不含）','正式结束时间UTC（含）','后侧分段依据','是否部分接管组','部分接管组原因','接管组输入哈希'}
        for uid,old in self.old.items():
            self.assertEqual({k:v for k,v in old.items() if k not in changed},
                             {k:v for k,v in self.events[uid].items() if k not in changed})
        _,prior=read(next(OLD.glob('443*.csv')))
        self.assertEqual(self.episodes[:443],prior)

    def test_exact_event_and_episode_formula_and_flags(self):
        groups=defaultdict(list)
        for row in self.events.values():
            result=F(row['FIFO仓位盈亏_USDC'])+F(row['accountValue'])
            self.assertEqual(F(row['HLP盈亏_加AccountValue_USDC']),result)
            self.assertEqual(row['盈亏方向'],'盈利' if result>0 else '亏损' if result<0 else '持平')
            missing=[k for k in ['被接管用户平均开仓价','清算标记价'] if row[k]=='']
            self.assertEqual(row['历史价格数据缺失'].startswith('是'),bool(missing))
            for name in missing:self.assertIn(name,row['缺失价格字段'])
            if missing:self.assertEqual(row['U'],'')
            else:
                q=F(row['接管数量'])*(1 if row['接管方向']=='long' else -1)
                self.assertEqual(F(row['U']),(F(row['清算标记价'])-F(row['被接管用户平均开仓价']))*q)
            groups[row['接管组编号']].append(row)
        for row in self.episodes:
            items=groups[row['接管组编号']]
            self.assertEqual(int(row['接管笔数']),len(items))
            self.assertEqual(F(row['HLP事件盈亏_加AccountValue_USDC']),sum((F(x['HLP盈亏_加AccountValue_USDC']) for x in items),F(0)))
            self.assertEqual(int(row['历史缺价接管笔数']),sum(x['历史价格数据缺失'].startswith('是') for x in items))

if __name__=='__main__':unittest.main()
