import contextlib
import io
import unittest
from pathlib import Path
from pnl_with_account_value import verify


class PublishedAccountValuePnl(unittest.TestCase):
    def test_every_event_episode_and_five_price_gaps(self):
        root = Path(__file__).resolve().parent
        prior = root / 'results/hlp_pnl_with_m_20261116'
        with contextlib.redirect_stdout(io.StringIO()):
            verify(root / 'results/hlp_pnl_with_account_value_20261118',
                   prior / 'csv1_22044笔_含U_M及HLP盈亏.csv',
                   prior / '443个episode_含M事件盈亏.csv',
                   root / 'results/hlp_fifo_pnl_20261115/takeover_pnl.csv', write_report=False)


if __name__ == '__main__': unittest.main()
