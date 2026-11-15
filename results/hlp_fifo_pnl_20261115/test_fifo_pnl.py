import unittest
from decimal import Decimal as D
from fifo_pnl import Fifo


class FifoTests(unittest.TestCase):
    def trade(self, book, uid, size, side, px, **kw):
        return book.trade(uid, book.position, D(size), side, D(px), **kw)

    def test_reference_overlap(self):
        b = Fifo()
        for uid, sz, px in [('a', '10', '100'), ('b', '20', '110'), ('c', '30', '120')]:
            self.trade(b, uid, sz, 'B', px)
        m = self.trade(b, 'x', '15', 'A', '130')[3] + self.trade(b, 'y', '45', 'A', '140')[3]
        self.assertEqual([(x.uid, q, p) for x, q, p in m], [('a', D(10), D(300)), ('b', D(5), D(100)), ('b', D(15), D(450)), ('c', D(30), D(600))])
        self.assertEqual(b.position, 0)

    def test_unknown_initial_cost(self):
        b = Fifo(); b.initial(D(50))
        for uid, sz, px in [('a', '10', '100'), ('b', '20', '110'), ('c', '30', '120')]:
            self.trade(b, uid, sz, 'B', px)
        x = self.trade(b, 'x', '15', 'A', '130')[3]
        y = self.trade(b, 'y', '45', 'A', '140')[3]
        self.assertIsNone(x[0][2]); self.assertIsNone(y[0][2])
        self.assertEqual(y[1][2], D(400))
        self.assertEqual([(x.uid, x.remaining) for x in b.lots], [('b', D(20)), ('c', D(30))])

    def test_reverse_old_short(self):
        b = Fifo(); self.trade(b, 'a', '20', 'A', '120', after='old')
        c, o, e, m = self.trade(b, 'b', '50', 'B', '100', before='old', after='new')
        self.assertEqual((c, o, e, m[0][2]), (D(20), D(30), D(30), D(400)))
        self.assertEqual(self.trade(b, 'c', '30', 'A', '110', before='new')[3][0][2], D(300))

    def test_offset_only(self):
        b = Fifo(); self.trade(b, 'a', '10', 'B', '100')
        c, o, e, m = self.trade(b, 'b', '4', 'A', '90')
        self.assertEqual((c, o, e, m[0][0].uid, m[0][2]), (D(4), D(0), D(6), 'a', D(-40)))

    def test_short_and_ordinary_opening(self):
        b = Fifo(); self.trade(b, 'ordinary', '2', 'A', '100', origin='普通成交')
        self.trade(b, 'takeover', '3', 'A', '110')
        m = self.trade(b, 'adl', '5', 'B', '90', origin='ADL')[3]
        self.assertEqual([x[2] for x in m], [D(20), D(60)])

    def test_gap_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'POSITION_GAP'):
            Fifo().trade('x', D(1), D(1), 'B', D(1))

    def test_decimal_exact(self):
        b = Fifo(); self.trade(b, 'a', '.1', 'B', '.123456789')
        m = self.trade(b, 'b', '.1', 'A', '.223456789')[3]
        self.assertEqual(m[0][2], D('.01')); self.assertEqual(b.position, 0)

    def test_account_isolation(self):
        a, b = Fifo(), Fifo()
        self.trade(a, 'a', '2', 'B', '100'); self.trade(b, 'b', '2', 'A', '110')
        self.assertEqual(self.trade(a, 'c', '2', 'A', '101')[3][0][2], D(2))
        self.assertEqual(b.position, D(-2))


if __name__ == '__main__':
    unittest.main()
