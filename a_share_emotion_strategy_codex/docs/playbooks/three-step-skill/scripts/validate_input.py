"""Validate and calculate evidence; successful validation is not qualification."""
import argparse
import json
from pathlib import Path
from run import read_input
from engine import evaluate

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path)
    report=evaluate(read_input(p.parse_args().input))
    print(json.dumps({k:report[k] for k in ('status','signal_date','coverage','funnel','missing')},ensure_ascii=False))
