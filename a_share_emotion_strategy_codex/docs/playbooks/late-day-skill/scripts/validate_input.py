"""Input audit, not profitability testing; code 2 denotes missing evidence."""
import json
from pathlib import Path
import sys
from engine import evaluate, freeze_position

def main():
    try:
        data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        for position in data.get('positions',[]): freeze_position(position)
        r=evaluate(data,phase='review')
        unknown=[{'code':s['code'],'missing':[c for c in s['checks'] if c['passed'] is None]} for s in r['rows'] if s['evidence_missing']]
        print(json.dumps({'version':r['rule_version'],'missing':r['missing'],'stocks':unknown},ensure_ascii=False,indent=2))
        return 2 if unknown or r['missing'] else 0
    except (OSError,ValueError,KeyError,TypeError,IndexError) as exc:
        print(json.dumps({'invalid':str(exc)},ensure_ascii=False))
        return 2

if __name__=='__main__': sys.exit(main())
