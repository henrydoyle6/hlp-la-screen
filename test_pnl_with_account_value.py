import unittest
from decimal import Decimal as D
from pnl_with_account_value import add_account_value, sign, price_gap


class AccountValuePnlTests(unittest.TestCase):
    def test_signed_values(self):
        self.assertEqual(add_account_value('-100', '60'), D(-40))
        self.assertEqual(add_account_value('10', '-25'), D(-15))
        self.assertEqual(add_account_value('-10', '25'), D(15))

    def test_zero_and_missing(self):
        self.assertIsNone(add_account_value('5', ''))
        self.assertIsNone(add_account_value('', '5'))
        self.assertEqual(add_account_value('5', '0'), D(5))
        self.assertEqual(sign(add_account_value('-5', '5')), '持平')

    def test_no_short_flip_or_m(self):
        self.assertEqual(add_account_value('10', '20'), D(30))

    def test_price_gap_does_not_block_new_inputs(self):
        r = {'被接管用户平均开仓价': '', '清算标记价': '1', '平均开仓价未补全原因': '原始起点缺失', '清算标记价未补全原因': ''}
        self.assertEqual(price_gap(r), ('被接管用户平均开仓价', '缺少被接管用户平均开仓价：原始起点缺失'))
        self.assertEqual(add_account_value('-1', '2'), D(1))

    def test_precision(self):
        self.assertEqual(add_account_value('100000000000000000000.00000000000000000001', '-100000000000000000000'), D('.00000000000000000001'))


if __name__ == '__main__': unittest.main()
