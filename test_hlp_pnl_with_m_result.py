import contextlib
import io
import unittest
from pathlib import Path
from pnl_with_m import verify


class PublishedPnlWithM(unittest.TestCase):
    def test_every_published_event_and_episode(self):
        root = Path(__file__).resolve().parent
        with contextlib.redirect_stdout(io.StringIO()):
            verify(root / 'results/hlp_pnl_with_m_20261116',
                   root / 'results/csv1_full_20250322_20261031/csv1_20250322_20261031_um_zh.csv',
                   root / 'results/hlp_fifo_pnl_20261115/takeover_pnl.csv', write_report=False)


if __name__ == '__main__': unittest.main()
