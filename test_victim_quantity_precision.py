import unittest
from decimal import Decimal as D,getcontext
from victim_quantity_precision import QuantityTailPolicy,replay,order_same_time

def record(i,sp,size,side='B',px='10',time=None):
    return {'id':str(i),'f':{'startPosition':sp,'sz':size,'side':side,'px':px,'time':i if time is None else time},'source':'fixture','source_line':i,'conflict':False}

class PrecisionTests(unittest.TestCase):
    def test_user_large_quantity_example(self):
        self.assertTrue(QuantityTailPolicy(5,D('0.00001')).equal('67858660.19999999','67858660.20000000'))
    def test_one_real_lot_remains_a_gap(self):
        self.assertFalse(QuantityTailPolicy(5,D('0.00001')).equal('0.1','0.10001'))
    def test_observed_historical_finer_size_limits_tolerance(self):
        p=QuantityTailPolicy(2,D('0.00000001'))
        self.assertFalse(p.equal('0','0.00000001'))
    def test_weighted_cost_survives_dust_and_source_is_unchanged(self):
        rows=[record(1,'0','1',px='10'),record(2,'1.0000000001','1',px='20'),record(3,'2','2','A',px='12')]
        p=QuantityTailPolicy(4,D('1'));result=replay(rows,p,{'3'})['3']
        self.assertIsNotNone(result['entry']);self.assertTrue(result['quantity_tail_adjustments'])
        self.assertEqual(rows[1]['f']['startPosition'],'1.0000000001')
        self.assertLess(abs(D(result['entry'])-D('15')),D('0.00000001'))
    def test_true_quantity_gap_blocks_price(self):
        rows=[record(1,'0','1'),record(2,'1.05','1'),record(3,'2.05','2.05','A')]
        self.assertEqual(replay(rows,QuantityTailPolicy(4),{'3'})['3']['reason'],'position_chain_discontinuity')
    def test_zero_residual_does_not_create_a_reverse_basis(self):
        rows=[record(1,'0','1'),record(2,'1','0.99999999999','A'),record(3,'0','1',px='20'),record(4,'1','1','A')]
        self.assertEqual(replay(rows,QuantityTailPolicy(4),{'4'})['4']['entry'],'20')
    def test_ambiguous_orders_remain_ambiguous(self):
        rows=[record(1,'0','1',time=1),record(2,'0','1',time=1)]
        self.assertTrue(order_same_time(rows,D(0),QuantityTailPolicy(4))[1])
    def test_unique_tail_chain_orders_correctly(self):
        rows=[record(2,'1.0000000001','1',time=1),record(1,'0','1',time=1)]
        self.assertEqual([r['id'] for r in order_same_time(rows,D(0),QuantityTailPolicy(4))[0]],['1','2'])
    def test_precision_context_is_restored(self):
        before=getcontext().prec;replay([record(1,'0','1')],QuantityTailPolicy(4));self.assertEqual(getcontext().prec,before)

if __name__=='__main__':unittest.main()
