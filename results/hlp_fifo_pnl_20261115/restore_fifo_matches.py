"""Restore the byte-identical match CSV from ordered, header-bearing parts."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    base = Path(__file__).resolve().parent
    manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    with a.output.open('xb') as out:
        expected_header = None
        for i, item in enumerate(manifest['parts']):
            path = base / item['name']
            with path.open('rb') as f:
                assert hashlib.file_digest(f, 'sha256').hexdigest() == item['sha256']
                f.seek(0); header = f.readline()
                if i == 0:
                    expected_header = header; out.write(header)
                assert header == expected_header
                for line in f:
                    out.write(line)
    with a.output.open('rb') as f:
        assert hashlib.file_digest(f, 'sha256').hexdigest() == manifest['full_sha256']
    print('PASS: byte-identical FIFO match CSV restored')


if __name__ == '__main__':
    main()
