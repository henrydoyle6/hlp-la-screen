"""Synthetic boundary tests for the standalone published FIFO research script."""
import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent / 'results/hlp_fifo_pnl_20261115'


def load_tests(loader, tests, pattern):
    for name in ('fifo_pnl', 'test_fifo_pnl'):
        spec = importlib.util.spec_from_file_location(name, BASE / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return loader.loadTestsFromModule(sys.modules['test_fifo_pnl'])


if __name__ == '__main__':
    unittest.main()
