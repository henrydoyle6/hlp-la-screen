import unittest
from decimal import Decimal as D
from pnl_with_m import combine, group_result, sign


class PnlWithMTests(unittest.TestCase):
    def test_signed_addition(self):
        self.assertEqual(combine('-100', '60'), D('-40'))
        self.assertEqual(combine('10', '-25'), D('-15'))
        self.assertEqual(combine('-10', '25'), D('15'))

    def test_no_second_short_sign(self):
        self.assertEqual(combine('10', '5'), D('15'))

    def test_missing_is_not_zero(self):
        self.assertIsNone(combine('100', ''))
        self.assertEqual(combine('100', '0'), D('100'))

    def test_zero_and_unknown(self):
        self.assertEqual(sign(combine('-5', '5')), '持平')
        self.assertEqual(sign(None), '未确定')

    def test_offset_still_receives_supplied_m(self):
        self.assertEqual(combine('0', '30'), D('30'))

    def test_partial_group(self):
        z = group_result([{'M': '10', 'FIFO仓位盈亏_USDC': '5'}, {'M': '', 'FIFO仓位盈亏_USDC': '100'}])
        self.assertIsNone(z['total']); self.assertEqual(z['known_total'], D(15)); self.assertEqual(z['fifo'], D(105))

    def test_full_group(self):
        z = group_result([{'M': '-10', 'FIFO仓位盈亏_USDC': '5'}, {'M': '2', 'FIFO仓位盈亏_USDC': '-1'}])
        self.assertEqual(z['total'], D(-4))

    def test_precision(self):
        self.assertEqual(combine('100000000000000000000.00000000000000000001', '-100000000000000000000'), D('.00000000000000000001'))


if __name__ == '__main__': unittest.main()
