#!/usr/bin/env python3
"""Compare two mainline JSON files using per-kind Jaccard agreement."""
import argparse
import json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('reference')
    p.add_argument('actual')
    args = p.parse_args()
    def read(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data['signals'] if isinstance(data, dict) else data
    reference, actual = read(args.reference), read(args.actual)
    passed = True
    for kind in ('PREP', 'A', 'B', 'FAST'):
        a = {(r['date'], r['board']) for r in reference if r['kind'] == kind}
        b = {(r['date'], r['board']) for r in actual if r['kind'] == kind}
        rate = len(a & b) / len(a | b) if a | b else 1.0
        print('{}: reference={} actual={} intersection={} agreement={:.2%}'.format(
            kind, len(a), len(b), len(a & b), rate))
        passed &= rate >= .9
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
