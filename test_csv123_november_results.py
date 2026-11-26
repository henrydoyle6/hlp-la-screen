"""Portable checks of the published November extension, without source access."""
from collections import Counter
from decimal import Decimal, localcontext
import csv
import hashlib
import json
from pathlib import Path
import unittest


class NovemberResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parent/'results/csv123_20250322_20261110'
        cls.report=json.loads((cls.root/'validation.json').read_text(encoding='utf8'))
        cls.tables=[]
        csv.field_size_limit(128*1024**2)
        for name,expected in cls.report['output_sha256'].items():
            path=cls.root/name
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise AssertionError('Published CSV hash mismatch: '+name)
            with path.open(encoding='utf-8-sig',newline='') as f:
                reader=csv.DictReader(f,strict=True);cls.tables.append((reader.fieldnames,list(reader)))

    def test_original_column_counts_complete_ids_and_source_gaps(self):
        self.assertEqual([len(h) for h,_ in self.tables],[45,47,46])
        one=self.tables[0][1];two=self.tables[1][1];three=self.tables[2][1]
        fills=[r for r in one if r['记录类型']=='fill'];issues=[r for r in one if r['记录类型']=='issue']
        self.assertEqual(len(fills),22315);self.assertEqual(len(issues),5)
        self.assertEqual(len({r['接管事件编号'] for r in fills}),22315)
        self.assertEqual(sum(not r['区块号'] for r in fills),249)
        expected=[f'EP{i:06d}' for i in range(1,453)]
        self.assertEqual([r['episode_id'] for r in two],expected)
        self.assertEqual([r['episode_id'] for r in three],expected)
        self.assertEqual({r['接管组编号'] for r in fills},set(expected))
        self.assertEqual({r['数据结束时间UTC（不含）'] for r in fills},{'2026-11-11T00:00:00.000Z'})
        self.assertEqual({r['正式结束时间UTC（含）'] for r in fills},{'2026-11-10T23:00:00.000Z'})
        self.assertEqual([r['episode_id'] for r in two if r['status']=='skipped'],['EP000353','EP000354'])

    def test_all_csv3_rows_bind_actual_csv2_and_preserve_unknown_results(self):
        two=self.tables[1][1];three=self.tables[2][1]
        for source,result in zip(two,three):
            text=json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            self.assertEqual(result['csv2_row_hash'],hashlib.sha256(text.encode()).hexdigest())
            if source['status']=='skipped':
                self.assertEqual(result['la'],'');self.assertEqual(result['status'],'skipped')
            else:
                self.assertEqual(result['book_hash'],source['book_hash'])
        self.assertEqual(Counter(r['status'] for r in three),self.report['csv3_statuses'])

    def test_every_new_l4_book_and_la_with_independent_decimal_calculation(self):
        two=self.tables[1][1][443:];three=self.tables[2][1][443:]
        self.assertEqual(len(two),9)
        for source,result in zip(two,three):
            self.assertEqual(source['status'],'book_ready')
            self.assertEqual(source['book_state_point'],'block_end')
            self.assertEqual(source['last_applied_block'],source['target_block'])
            self.assertEqual(source['snapshot_lag_ms'],'')
            orders=json.loads(source['orders_json'])
            self.assertEqual(len(orders),int(source['order_count']))
            self.assertEqual(len({r['oid'] for r in orders}),len(orders))
            self.assertEqual(source['book_side'],'bid' if source['direction']=='long' else 'ask')
            with localcontext() as ctx:
                ctx.prec=60
                target=Decimal(source['total_qty']);remaining=target;filled=amount=Decimal(0)
                prices=[Decimal(r['price']) for r in orders]
                self.assertEqual(prices,sorted(prices,reverse=source['book_side']=='bid'))
                self.assertTrue(all(Decimal(r['qty'])>0 for r in orders))
                self.assertEqual(sum((Decimal(r['qty']) for r in orders),Decimal(0)),Decimal(source['total_depth']))
                for order in orders:
                    take=min(remaining,Decimal(order['qty']))
                    filled+=take;remaining-=take;amount+=take*Decimal(order['price'])
                    if not remaining:break
                cost=filled*Decimal(source['entry_vwap'])
                la=amount-cost if source['direction']=='long' else cost-amount
                for name,value in [('filled_qty',filled),('unfilled_qty',remaining),('close_notional',amount),('la',la)]:
                    self.assertEqual(Decimal(result[name]),value,(source['episode_id'],name))


if __name__=='__main__':unittest.main()
