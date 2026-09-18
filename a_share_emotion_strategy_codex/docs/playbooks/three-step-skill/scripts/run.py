"""Evaluate a verified local evidence file; no network or trading."""
import argparse
import datetime as dt
import json
from pathlib import Path
from engine import evaluate, TZ


def read_input(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>100_000_000:
        raise ValueError('Expected bounded regular evidence JSON')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or 'module_id' in data:
        raise ValueError('Raw evidence object required, not a result snapshot')
    return data


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--as-of',help='Shanghai ISO cutoff or completed YYYY-MM-DD')
    a=p.parse_args()
    text=a.as_of
    if text and len(text)==10: text+='T15:00:00+08:00'
    now=dt.datetime.fromisoformat(text) if text else dt.datetime.now(TZ)
    if now.tzinfo is None: now=now.replace(tzinfo=TZ)
    if now>dt.datetime.now(TZ): raise ValueError('Future research cutoff')
    result=evaluate(read_input(a.input),as_of=now)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__': main()
