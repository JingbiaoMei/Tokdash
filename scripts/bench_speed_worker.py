#!/usr/bin/env python3
"""Run the real timing worker with fixture-only reader paths."""
import argparse
import sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--src',required=True)
    p.add_argument('--parsers',required=True)
    args=p.parse_args()
    sys.path.insert(0,str(Path(args.src).resolve()))
    from bench_tokdash_server import _prune_parsers
    _prune_parsers(set(args.parsers.split(',')))
    from tokdash.speed_worker import main as run
    run()

if __name__=='__main__':main()
