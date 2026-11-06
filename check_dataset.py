import gzip,json,sys,collections
from pathlib import Path
p=Path(sys.argv[1]); count=0; classes=collections.Counter()
with gzip.open(p,'rt',encoding='utf8') as f:
 for line in f:
  line=line.strip().lstrip('\x1e')
  if not line: continue
  o=json.loads(line); props=o.get('properties') or {}; t=props.get('tags') or props
  if t.get('amenity') in ('school','kindergarten','childcare','place_of_worship','college','university') or t.get('building') in ('school','kindergarten','church','chapel','mosque','synagogue','temple','college','university') or t.get('social_facility') in ('drug_addiction','alcohol_addiction'):
   count+=1;classes[str(t.get('amenity') or t.get('building') or t.get('social_facility'))]+=1
if count<10000:raise RuntimeError(f'Unexpectedly few candidate features: {count}')
print('Validated candidate features:',count)
print('Categories:',dict(classes))
