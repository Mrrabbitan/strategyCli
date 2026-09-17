"""Evaluate verified input to stdout without network or orders."""
import argparse
import json
from pathlib import Path
from engine import evaluate, freeze_position

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--as-of')
    p.add_argument('--phase',choices=('auto','preview','live','review','next-open'),default='auto')
    a=p.parse_args()
    data=json.loads(a.input.read_text(encoding='utf-8'))
    frozen=[freeze_position(x) for x in data.get('positions',[])]
    print(json.dumps(evaluate(data,as_of=a.as_of,phase=a.phase,positions=frozen),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
