#!/usr/bin/env python3
"""Offline Base timestamp repair; preserve every event field except ts.

--apply repartitions the ledger after verifying count and non-timestamp fields.
No network, cursor changes, deletions, or notification delivery.
"""
import argparse, datetime as dt, hashlib, json, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DENIM_BLOCK=53556127
DENIM_TS=1793901601

def timestamp(block):
    return DENIM_TS+(block-DENIM_BLOCK)*2 if block<DENIM_BLOCK else DENIM_TS+(block-DENIM_BLOCK)//5

def digest(row):
    return int.from_bytes(hashlib.sha256(json.dumps({k:v for k,v in row.items() if k!='ts'},sort_keys=True,separators=(',',':')).encode()).digest(),'big')

def repair(root,apply=False):
    data=root/'data';paths=sorted(data.glob('*.jsonl'));count=changed=moved=before=0
    with tempfile.TemporaryDirectory(prefix='denim-ledger-') as tmp:
        stage=Path(tmp);handles={}
        try:
            for path in paths:
                for line in path.read_text().splitlines():
                    if not line.strip():continue
                    row=json.loads(line)
                    if type(row.get('block')) is not int or type(row.get('ts')) is not int:raise ValueError('invalid timestamp input')
                    count+=1;before+=digest(row);correct=timestamp(row['block']);changed+=row['ts']!=correct;row['ts']=correct
                    name=dt.datetime.fromtimestamp(correct,dt.UTC).strftime('%Y-%m-%d.jsonl');moved+=name!=path.name
                    if name not in handles:handles[name]=(stage/name).open('w')
                    handles[name].write(json.dumps(row,separators=(',',':'))+'\n')
        finally:
            for handle in handles.values():handle.close()
        after=total=0
        for path in stage.glob('*.jsonl'):
            rows=[json.loads(line) for line in path.read_text().splitlines()];rows.sort(key=lambda r:(r['block'],r['li'],r['tx']))
            path.write_text(''.join(json.dumps(row,separators=(',',':'))+'\n' for row in rows))
            for row in rows:total+=1;after+=digest(row)
        if count!=total or before!=after:raise ValueError('event preservation failed')
        if apply:
            for name in sorted({p.name for p in paths}|set(handles)):
                source=stage/name;target=data/name;content=source.read_bytes() if source.exists() else b''
                if not target.exists() or target.read_bytes()!=content:
                    pending=target.with_suffix('.repair');pending.write_bytes(content);pending.replace(target)
        return {'rows':count,'timestamps_corrected':changed,'rows_repartitioned':moved,'event_preservation':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(json.dumps(repair(ROOT,args.apply)))
