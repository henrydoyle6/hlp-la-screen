import unittest
from decimal import getcontext
from fractions import Fraction
from csv1_event_um import calculate_event

def row(**changes):
    value={'记录类型':'fill','接管方向':'long','接管数量':'2','接管总数量':'1000','accountValue':'100',
        '被接管用户平均开仓价':'100','清算标记价':'110','平均开仓价未补全原因':'','清算标记价未补全原因':''}
    value.update(changes);return value

class EventUMTests(unittest.TestCase):
    def test_long_gain_uses_this_fill_quantity(self):
        self.assertEqual(calculate_event(row()),['2','20','80',''])
    def test_long_loss_keeps_negative_u(self):
        self.assertEqual(calculate_event(row(清算标记价='90')),['2','-20','120',''])
    def test_short_gain_uses_negative_q(self):
        self.assertEqual(calculate_event(row(接管方向='short',清算标记价='90')),['-2','20','80',''])
    def test_short_loss_keeps_negative_u(self):
        self.assertEqual(calculate_event(row(接管方向='short')),['-2','-20','120',''])
    def test_negative_account_and_negative_m_are_not_clamped(self):
        self.assertEqual(calculate_event(row(accountValue='-5')),['2','20','-25',''])
    def test_zero_account_is_not_missing(self):
        self.assertEqual(calculate_event(row(accountValue='0')),['2','20','-20',''])
    def test_zero_u_and_zero_m_are_valid(self):
        self.assertEqual(calculate_event(row(被接管用户平均开仓价='0',清算标记价='0',accountValue='0')),['2','0','0',''])
    def test_missing_mean_does_not_use_pnl_proxy(self):
        result=calculate_event(row(被接管用户平均开仓价='',平均开仓价_平仓损益隐含值='100',平均开仓价未补全原因='缺开仓起点'))
        self.assertEqual(result[:3],['2','','']);self.assertIn('缺开仓起点',result[3])
    def test_missing_raw_mark_does_not_use_ledger_proxy(self):
        result=calculate_event(row(清算标记价='',清算标记价_单仓位账本隐含值='110',清算标记价未补全原因='原档没有markPx'))
        self.assertEqual(result[:3],['2','','']);self.assertIn('原档没有markPx',result[3])
    def test_both_missing_causes_are_recorded(self):
        result=calculate_event(row(被接管用户平均开仓价='',清算标记价=''))
        self.assertIn('平均开仓价',result[3]);self.assertIn('原始清算标记价',result[3])
    def test_missing_account_preserves_computable_u(self):
        result=calculate_event(row(accountValue=''));self.assertEqual(result[:3],['2','20','']);self.assertIn('accountValue',result[3])
    def test_user_selected_mean_is_used_despite_existing_pnl_note(self):
        self.assertEqual(calculate_event(row(平均开仓价核验状态='用户指定采用第三方历史均价（损益核验有差异）'))[:3],['2','20','80'])
    def test_tiny_negative_m_is_preserved(self):
        self.assertEqual(calculate_event(row(接管数量='1',被接管用户平均开仓价='0',清算标记价='0.00000000000002',accountValue='0.00000000000001'))[2],'-0.00000000000001')
    def test_full_decimal_precision_matches_independent_fraction_oracle(self):
        value=row(接管数量='67858660.19999999',被接管用户平均开仓价='0.12345678901234567890123456789012345678901234567890123456789',清算标记价='0.12345678901234567890123456789012345678901234567890123456790',accountValue='1')
        before=getcontext().prec;actual=calculate_event(value)
        expected=(Fraction(value['清算标记价'])-Fraction(value['被接管用户平均开仓价']))*Fraction(value['接管数量'])
        self.assertEqual(Fraction(actual[1]),expected);self.assertEqual(Fraction(actual[2]),1-expected)
        self.assertEqual(getcontext().prec,before)
    def test_diagnostics_are_not_business_events(self):
        result=calculate_event({'记录类型':'issue'});self.assertEqual(result[:3],['','','']);self.assertIn('非接管',result[3])
    def test_invalid_direction_or_nonpositive_quantity_is_rejected(self):
        for changes in [{'接管方向':'unknown'},{'接管数量':'0'},{'接管数量':'-2'},{'接管数量':'NaN'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):calculate_event(row(**changes))
if __name__=='__main__':unittest.main()
