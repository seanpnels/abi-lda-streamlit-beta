"""Fail closed when OSM candidate export is empty, invalid, or implausibly small."""
import gzip, json, sys, hashlib
from collections import Counter
from pathlib import Path
path=Path(sys.argv[1]); count=0; categories=Counter(); errors=0
with gzip.open(path,'rt',encoding='utf-8') as f:
    for line in f:
        line=line.strip().lstrip('\x1e')
        if not line:continue
        try:
            o=json.loads(line); p=o.get('properties') or {};t=p.get('tags') or p
            if not o.get('geometry'):continue
            if any(t.get(k) for k in ('amenity','building','social_facility')):
                count+=1;categories[str(t.get('amenity') or t.get('building') or t.get('social_facility'))]+=1
        except Exception:errors+=1
minimum=int(sys.argv[2]) if len(sys.argv)>2 else 100
if errors or count<minimum:raise RuntimeError(f'Validation failed: {count} candidates, {errors} errors; minimum {minimum}')
print(json.dumps({'count':count,'categories':categories,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},indent=2))
