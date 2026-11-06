"""ABI OOH Stage 6 nationwide beta: DMA-driven, state-sharded, review-only OSM screening.
No inventory is written to GitHub. Every outcome requires human review.
"""
import gzip, hashlib, html, io, json, math, time, re, unicodedata
from collections import Counter
from datetime import datetime, timezone
import folium
from folium.plugins import MeasureControl
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from openpyxl.styles import PatternFill, Font
from shapely.geometry import Point, box, shape
from shapely.ops import transform
from shapely.strtree import STRtree
from pyproj import CRS, Transformer

st.set_page_config(page_title='ABI OOH · Nationwide Beta',page_icon='🗺️',layout='wide')
st.title('ABI OOH · LDA Compliance Hub')
st.caption('Stage 6 · 209 DMA architecture · state-sharded OSM datasets · review-only')
st.warning('**Every placement requires human review.** OSM coverage is incomplete; candidate polygons are not verified building footprints. Green is NOT a compliance determination.')
try:
    TOKEN=st.secrets.get('GITHUB_TOKEN','')
    REPO=st.secrets.get('GITHUB_REPO','seanpnels/abi-lda-data-poc')
except Exception:TOKEN='';REPO='seanpnels/abi-lda-data-poc'
if not TOKEN:st.error('Set GITHUB_TOKEN in Streamlit Secrets.');st.stop()
HEAD={'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

@st.cache_data(ttl=900,show_spinner=False)
def releases(repo,token):
    h={**HEAD,'Authorization':f'Bearer {token}'}
    out=[]
    for page in range(1,11):
        r=requests.get(f'https://api.github.com/repos/{repo}/releases',params={'per_page':100,'page':page},headers=h,timeout=35)
        r.raise_for_status(); batch=r.json();out.extend(batch)
        if len(batch)<100:break
    return out

@st.cache_data(ttl=3600,show_spinner=False)
def asset_bytes(repo,asset_id,token):
    h={**HEAD,'Authorization':f'Bearer {token}','Accept':'application/octet-stream'}
    r=requests.get(f'https://api.github.com/repos/{repo}/releases/assets/{asset_id}',headers=h,timeout=(20,180))
    r.raise_for_status()
    if 'json' in r.headers.get('Content-Type',''):
        raise RuntimeError('GitHub returned JSON instead of release asset; check token permissions')
    return r.content

def latest_asset(all_releases,prefix,filename):
    for rel in all_releases:
        if rel.get('draft') or rel.get('prerelease') or not rel.get('tag_name','').startswith(prefix):continue
        assets={a['name']:a['id'] for a in rel.get('assets',[])}
        if filename in assets and 'manifest.json' in assets:
            return {'tag':rel['tag_name'],'file_id':assets[filename],'manifest_id':assets['manifest.json']}
    return None

def latest_index(all_releases):
    for rel in all_releases:
        if rel.get('draft') or not rel.get('tag_name','').startswith('stage6-index-'):continue
        for a in rel.get('assets',[]):
            if a['name']=='dma-state-index.json':return a['id']
    return None

def normalized_market(value):
    text=unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()
    text=re.sub(r'\bd\.\s*c\.?', 'dc', text)
    text=re.sub(r'\bsaint\b','st',text)
    text=re.sub(r'\bft\.?\b','fort',text)
    text=re.sub(r'\b(designated market area|dma|market)\b',' ',text)
    return re.sub(r'[^a-z0-9]+',' ',text).strip()


def market_column_names(frame):
    priority=['dmacode','dmaid','dmanumber','dma','dmaname','designatedmarketarea',
              'designatedmarketareaname','market','marketname','mediamarket','marketdma']
    ranking={name:i for i,name in enumerate(priority)}
    columns=[(ranking[key],i,c) for i,c in enumerate(frame.columns)
             if (key:=re.sub(r'[^a-z0-9]','',str(c).lower())) in ranking]
    return [c for _,_,c in sorted(columns)]


def read_inventory_grids(files):
    frames=[];all_columns=set()
    for upload in files:
        blob=io.BytesIO(upload.getvalue())
        frame=(pd.read_excel(blob,dtype=object) if upload.name.lower().endswith('.xlsx')
               else pd.read_csv(blob,dtype=object,encoding='utf-8-sig'))
        if frame.empty:raise ValueError(f'{upload.name}: empty inventory')
        def find(names):return next((c for c in frame.columns if str(c).strip().lower() in names),None)
        lat=find({'latitude','lat'});lon=find({'longitude','lon','lng','long'})
        if lat is None or lon is None:raise ValueError(f'{upload.name}: Latitude and Longitude columns required')
        frames.append((upload.name,frame,lat,lon,market_column_names(frame)))
        all_columns.update(frame.columns)
    def unique(base):
        name=base;n=2
        while name in all_columns:name=f'{base}_{n}';n+=1
        all_columns.add(name);return name
    meta={key:unique(base) for key,base in [('file','source_grid'),('row','source_excel_row'),
          ('lat','screening_latitude'),('lon','screening_longitude'),
          ('market','source_market'),('market_column','source_market_column')]}
    combined=[];summaries=[]
    for filename,frame,lat,lon,market_columns in frames:
        frame=frame.copy()
        frame[meta['file']]=filename;frame[meta['row']]=range(2,len(frame)+2)
        frame[meta['lat']]=frame[lat];frame[meta['lon']]=frame[lon]
        values=[];origins=[]
        for _,row in frame.iterrows():
            candidates=[c for c in market_columns if pd.notna(row[c]) and str(row[c]).strip()]
            column=candidates[0] if candidates else None
            values.append(str(row[column]).strip() if column is not None else '')
            origins.append(str(column) if column is not None else '')
        frame[meta['market']]=values;frame[meta['market_column']]=origins
        combined.append(frame)
        summaries.append({'Grid':filename,'Placements':len(frame),'Market columns':', '.join(map(str,market_columns)) or 'None found'})
    return pd.concat(combined,ignore_index=True,sort=False),meta,summaries


def suggest_market_dmas(values,names):
    aliases={}
    def add(alias,code):
        key=normalized_market(alias)
        if key:aliases.setdefault(key,set()).add(code)
    for code,name in names.items():
        add(name,code)
        # Common grids abbreviate the official DMA to its first city.
        primary=re.split(r'[-–—]',name.split(',')[0])[0].strip()
        add(primary,code)
        # Preserve state-qualified city names used in shorter market labels.
        state_match=re.search(r',\s*([A-Z]{2})(?=[^A-Za-z]|$)',name)
        if state_match:add(primary+' '+state_match.group(1),code)
    matched=set();review=[]
    for value,count in values.fillna('').astype(str).value_counts(dropna=False).items():
        raw=value.strip();key=normalized_market(raw)
        numeric=re.fullmatch(r'(?:dma\s*)?(\d{3})(?:\.0)?',raw,re.I)
        if numeric and numeric.group(1) in names:
            candidates={numeric.group(1)}
        else:
            code_suffix=re.search(r'\((\d{3})\)\s*$',raw)
            if code_suffix and code_suffix.group(1) in names:
                candidates={code_suffix.group(1)}
            else:candidates=aliases.get(key,set())
        if len(candidates)==1:
            code=next(iter(candidates));matched.add(code)
            review.append({'Grid market':raw or '(blank)','Placements':int(count),'Detected DMA':names[code],'Match':'Matched'})
        elif candidates:
            review.append({'Grid market':raw,'Placements':int(count),'Detected DMA':'; '.join(names[k] for k in sorted(candidates)),'Match':'Ambiguous — select manually'})
        else:
            review.append({'Grid market':raw or '(blank)','Placements':int(count),'Detected DMA':'','Match':'No match — select manually'})
    return matched,review


# Upload first; all grids are combined without dropping repeated placements.
files=st.file_uploader('1. Upload placement grids (CSV or XLSX)',type=['csv','xlsx'],accept_multiple_files=True)
if not files:
    st.info('Upload one or more grids to begin.');st.stop()
try:
    df,inventory_meta,grid_summaries=read_inventory_grids(files)
    lat_col=inventory_meta['lat'];lon_col=inventory_meta['lon']
except Exception as exc:st.error(f'Inventory error: {exc}');st.stop()
grid_digest=hashlib.sha256()
for upload in files:
    grid_digest.update(upload.name.encode('utf-8'));grid_digest.update(hashlib.sha256(upload.getvalue()).digest())
inventory_key=grid_digest.hexdigest()
st.caption(f'{len(files)} grid(s) loaded: {len(df):,} placements total. Source filenames are included in exports.')
with st.expander('Uploaded grids'):
    st.dataframe(pd.DataFrame(grid_summaries),hide_index=True)

@st.cache_resource(show_spinner=False, max_entries=1)
def geographic_index(repo,asset_id,token):
    # Parse the large national index once per published version, not per rerun.
    data=json.loads(asset_bytes(repo,asset_id,token))
    dmas=data['dma']['features'];states=data['states']['features']
    if len(dmas)!=209 or len(states)!=51:raise ValueError('Incomplete DMA/state index')
    return ({str(f['properties']['id']):f for f in dmas},
            {f['properties']['slug']:shape(f['geometry']) for f in states},
            {f['properties']['slug']:f['properties']['name'] for f in states})

@st.cache_resource(show_spinner=False, max_entries=209)
def selected_dma_geometry(repo,asset_id,token,code):
    features,_,_=geographic_index(repo,asset_id,token)
    return shape(features[code]['geometry'])

try:
    rels=releases(REPO,TOKEN)
    idx_id=latest_index(rels)
    if idx_id is None:
        st.error('Stage 6 DMA index not published yet.');st.stop()
    dma_features,state_geoms,state_names=geographic_index(REPO,idx_id,TOKEN)
except Exception as exc:st.error(f'Cannot load Stage 6 geographic index: {exc}');st.stop()
dma_names={code:f['properties']['name'] for code,f in dma_features.items()}
labels={f'{dma_names[k]} ({k})':k for k in sorted(dma_names,key=lambda k:dma_names[k])}

suggested_codes,market_review=suggest_market_dmas(df[inventory_meta['market']],dma_names)
suggested_labels=[label for label,code in labels.items() if code in suggested_codes]
settings_context=(inventory_key,idx_id)
if st.session_state.get('stage6_inventory_context')!=settings_context:
    # New grids or a newly published boundary index must invalidate old settings/results.
    st.session_state.pop('stage6_settings',None)
    st.session_state['stage6_dma_selection']=suggested_labels
    st.session_state['stage6_distance']=500
    st.session_state['stage6_inventory_context']=settings_context
st.caption(f'Automatically selected {len(suggested_labels)} DMA(s) from grid market columns. Confirm or edit below.')
unmatched=[r for r in market_review if r['Match']!='Matched']
if unmatched:
    count=sum(r['Placements'] for r in unmatched)
    st.warning(f'{count:,} placement(s) have missing, ambiguous or unrecognized market names. Review the matches and select any additional DMAs manually.')
with st.expander('Review market detection',expanded=bool(unmatched)):
    st.dataframe(pd.DataFrame(market_review),hide_index=True)
    st.caption('Matching uses market names/codes, not geocoded addresses. Placement coordinates are still checked against your confirmed DMA selection.')

# Batch market edits: selecting or searching does not rerun the Python app.
with st.form('campaign_settings'):
    selected_labels=st.multiselect('2. Confirm campaign DMAs',list(labels),key='stage6_dma_selection')
    restriction=st.radio('Restricted distance',options=[500,250],horizontal=True,
                         format_func=lambda x:f'{x} feet',key='stage6_distance')
    apply_settings=st.form_submit_button('Apply campaign settings',type='primary')
if apply_settings:
    st.session_state['stage6_settings']=(selected_labels,restriction)
if 'stage6_settings' not in st.session_state:
    st.info('Confirm your markets and distance, then click Apply campaign settings.');st.stop()
selected_labels,restriction=st.session_state['stage6_settings']
if not selected_labels:
    st.info('Select at least one DMA and apply campaign settings.');st.stop()
st.caption('Applied markets: '+', '.join(selected_labels))
st.caption(f'Applied distance: {restriction} feet. Review band: {restriction}–{restriction+50} ft.')
dma_geoms={labels[x]:selected_dma_geometry(REPO,idx_id,TOKEN,labels[x]) for x in selected_labels}

AMENITIES={'school':'school','kindergarten':'daycare','childcare':'daycare','place_of_worship':'worship','college':'university','university':'university'}
BUILDINGS={'school':'school','kindergarten':'daycare','church':'worship','chapel':'worship','mosque':'worship','synagogue':'worship','temple':'worship','college':'university','university':'university'}
def classify(t):
    cats=set()
    if t.get('amenity') in AMENITIES:cats.add(AMENITIES[t['amenity']])
    if t.get('building') in BUILDINGS:cats.add(BUILDINGS[t['building']])
    if t.get('social_facility') in ('drug_addiction','alcohol_addiction'):cats.add('substance_use_candidate')
    return sorted(cats)

def local_projection(lon,lat):
    crs=CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs')
    return Transformer.from_crs('EPSG:4326',crs,always_xy=True).transform

# The 1km state coverage search is intentionally greater than the 550ft max buffer.
# State geometries are simplified for selection only; uncertainty at boundaries is flagged.
def needed_states(lon,lat):
    p=Point(lon,lat); near=[]
    for slug,g in state_geoms.items():
        if not box(lon-.025,lat-.025,lon+.025,lat+.025).intersects(g):continue
        project=local_projection(lon,lat)
        if transform(project,g).distance(Point(0,0))<=1000:near.append(slug)
    return near

def dma_match(lon,lat,chosen):
    p=Point(lon,lat)
    return any(dma_geoms[k].covers(p) for k in chosen)

@st.cache_resource(show_spinner=False)
def load_filtered_state(repo,asset_id,manifest_id,token,bbox_tuple):
    """Only retain features near uploaded placements; do not index entire large states."""
    manifest=json.loads(asset_bytes(repo,manifest_id,token))
    blob=asset_bytes(repo,asset_id,token)
    if hashlib.sha256(blob).hexdigest()!=manifest.get('sha256'):
        raise RuntimeError('Dataset SHA256 does not match manifest; rejecting partial/mismatched snapshot')
    area=box(*bbox_tuple); features=[];errors=0;total=0
    with gzip.open(io.BytesIO(blob),'rt',encoding='utf-8') as f:
        for line in f:
            line=line.strip().lstrip('\x1e')
            if not line:continue
            try:
                obj=json.loads(line);props=obj.get('properties') or {};tags=props.get('tags') or props
                cats=classify(tags)
                if not cats or not obj.get('geometry'):continue
                total+=1
                geom=shape(obj['geometry'])
                if geom.is_empty or not geom.intersects(area):continue
                if not geom.is_valid:geom=geom.buffer(0)
                if geom.is_empty:continue
                quality='polygon_unverified' if geom.geom_type in ('Polygon','MultiPolygon') else 'point_or_line_unverified'
                features.append({'geom':geom,'name':str(tags.get('name','')),'cats':cats,
                    'osm_id':str(props.get('@id',obj.get('id',''))),'quality':quality})
            except Exception:errors+=1
    if errors or total<100:raise RuntimeError(f'Invalid dataset: {total} candidates, {errors} parse errors')
    return features,STRtree([f['geom'] for f in features]) if features else None,manifest

def facility_pin_location(geometry):
    """Use the mapped point, or an interior point of the mapped facility area."""
    if geometry.is_empty:
        return None, None
    point=geometry if geometry.geom_type=='Point' else geometry.representative_point()
    return float(point.y), float(point.x)


def triggering_facility(near, status):
    if not near:
        return None
    # A nearer unverified point must not replace the polygon that caused RED.
    if status.startswith('RED'):
        return next((item for item in near if item[1]['quality']=='polygon_unverified'),None)
    return near[0]


def excel_bytes(result):
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        result.to_excel(writer,index=False,sheet_name='Screening')
        ws=writer.sheets['Screening'];idx=result.columns.get_loc('screening_status')+1
        colors={'RED':'FCE8E8','YELLOW':'FFF0C2','GREEN':'E2F3E8','GRAY':'E6EAF0'}
        for row in range(2,len(result)+2):
            key=str(ws.cell(row,idx).value).split(' — ')[0]
            for cell in ws[row]:cell.fill=PatternFill('solid',fgColor=colors.get(key,'FFFFFF'))
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='23384B')
    return out.getvalue()

chosen=[labels[x] for x in selected_labels]
points=[]; all_needed=set();valid=[]
for _,r in df.iterrows():
    try:
        lat=float(r[lat_col]);lon=float(r[lon_col]);
        if not (math.isfinite(lat) and math.isfinite(lon) and -90<=lat<=90 and -180<=lon<=180):raise ValueError('Invalid coordinate')
        if not dma_match(lon,lat,chosen):raise ValueError('Outside selected DMA boundary')
        needs=needed_states(lon,lat)
        if not needs:raise ValueError('No matching state geometry / unsupported territory')
        points.append((lat,lon,needs,''));all_needed.update(needs);valid.append((lat,lon))
    except Exception as exc:points.append((None,None,[],str(exc)))

available={};missing=[]
for slug in sorted(all_needed):
    release=latest_asset(rels,f'stage6-osm-{slug}-','facilities.geojsonseq.gz')
    if release:available[slug]=release
    else:missing.append(slug)
st.caption(f'Placements: {len(df):,} · Required state datasets: {", ".join(state_names[s] for s in sorted(all_needed)) or "none"}')
if missing:st.error('Missing state snapshots: '+', '.join(state_names[s] for s in missing)+'. Placements requiring these states will be GRAY, not GREEN.')

fingerprint=(inventory_key,tuple(sorted(chosen)),restriction,tuple(sorted((k,v['tag']) for k,v in available.items())))
if st.button('Run nationwide screening',type='primary'):
    start=time.perf_counter(); datasets={};load_errors={}
    # Bound the indexed area to actual inventory plus a conservative ~2km envelope.
    if valid:
        bounds=(min(p[1] for p in valid)-.04,min(p[0] for p in valid)-.04,
                max(p[1] for p in valid)+.04,max(p[0] for p in valid)+.04)
        for slug,rel in available.items():
            try:
                feats,tree,manifest=load_filtered_state(REPO,rel['file_id'],rel['manifest_id'],TOKEN,bounds)
                date=datetime.fromisoformat(str(manifest['generated_utc']).replace('Z','+00:00'))
                if (datetime.now(timezone.utc)-date).total_seconds()>10*86400:raise RuntimeError('Snapshot older than 10 days')
                datasets[slug]=(feats,tree,manifest)
            except Exception as exc:load_errors[slug]=str(exc)
    results=[]
    for lat,lon,needs,error in points:
        rec={'screening_status':'GRAY — Unable to screen','distance_ft':None,'nearby_facility':'',
             'facility_category':'','osm_id':'','geometry_quality':'',
             'facility_latitude':None,'facility_longitude':None,'screening_notes':'Human review required',
             'state_snapshots':''}
        if error:rec['screening_notes']=error;results.append(rec);continue
        bad=[s for s in needs if s not in datasets]
        if bad:
            rec['screening_notes']='Missing/stale/failed state coverage: '+', '.join(state_names[s] for s in bad)
            results.append(rec);continue
        project=local_projection(lon,lat); p=Point(0,0)
        radius_m=(restriction+50)*.3048;near=[]
        for slug in needs:
            feats,tree,_=datasets[slug]
            if tree is None:continue
            # Broad lon/lat envelope; exact final distance is measured in local meters.
            lat_delta=radius_m/110000*1.15
            lon_delta=radius_m/(111000*max(.1,math.cos(math.radians(lat))))*1.15
            search=box(lon-lon_delta,lat-lat_delta,lon+lon_delta,lat+lat_delta)
            for i in tree.query(search):
                f=feats[int(i)]
                try:d=p.distance(transform(project,f['geom']))
                except Exception:continue
                if d<=radius_m:near.append((d,f))
        near.sort(key=lambda item:item[0]);poly=[(d,f) for d,f in near if f['quality']=='polygon_unverified']
        nonpoly=[(d,f) for d,f in near if f['quality']!='polygon_unverified']
        if poly and poly[0][0]<=restriction*.3048:status='RED — Potential conflict'
        elif poly or nonpoly:status='YELLOW — Review required'
        else:status='GREEN — No detected conflict'
        rec['screening_status']=status
        rec['state_snapshots']='; '.join(f'{s}: {available[s]["tag"]}' for s in needs)
        rec['screening_notes']='OSM candidate screening only; human review required.'
        trigger=triggering_facility(near,status)
        if trigger:
            d,f=trigger
            facility_lat,facility_lon=facility_pin_location(f['geom'])
            rec.update({'distance_ft':round(d/0.3048,1),'nearby_facility':f['name'],
                'facility_category':'; '.join(f['cats']),'osm_id':f['osm_id'],'geometry_quality':f['quality'],
                'facility_latitude':facility_lat,'facility_longitude':facility_lon})
        results.append(rec)
    st.session_state['stage6_result']=pd.concat([df.reset_index(drop=True),pd.DataFrame(results)],axis=1)
    st.session_state['stage6_fingerprint']=fingerprint
    st.session_state['stage6_elapsed']=time.perf_counter()-start
    st.session_state['stage6_errors']=load_errors
if st.session_state.get('stage6_fingerprint')!=fingerprint:
    st.info('Run screening to generate results.');st.stop()
res=st.session_state['stage6_result'];counts=Counter(str(x).split(' — ')[0] for x in res['screening_status'])
cols=st.columns(5)
for col,key in zip(cols[:4],('RED','YELLOW','GREEN','GRAY')):col.metric(key,counts.get(key,0))
cols[4].metric('Processing time',f'{st.session_state["stage6_elapsed"]:.2f}s')
if st.session_state['stage6_errors']:
    st.error('State data errors: '+ '; '.join(f'{state_names[k]}: {v}' for k,v in st.session_state['stage6_errors'].items()))
priority=[x for x in ('screening_status','distance_ft','nearby_facility','facility_category','Unit #','Location Description','screening_notes') if x in res]
st.dataframe(res[priority+[x for x in res if x not in priority]],use_container_width=True,hide_index=True)
a,b=st.columns(2)
with a:st.download_button('Download color-coded Excel',excel_bytes(res),'ABI_Stage6_screening.xlsx')
with b:st.download_button('Download screening CSV',res.to_csv(index=False).encode('utf-8-sig'),'ABI_Stage6_screening.csv')
st.subheader('Review map')
st.caption('Circles mark placements. Pin-shaped markers identify the primary facility behind each red/yellow flag. Facility pins use mapped OSM locations, not verified street addresses.')
map_rows=[(lat,lon,r) for (lat,lon,_,_),(_,r) in zip(points,res.iterrows()) if lat is not None]
if map_rows:
    center=[sum(x[0] for x in map_rows)/len(map_rows),sum(x[1] for x in map_rows)/len(map_rows)]
    m=folium.Map(location=center,zoom_start=10,tiles=None,control_scale=True,height=600,width='100%')
    url=st.secrets.get('CARTO_TILE_URL','');key=st.secrets.get('CARTO_API_KEY','')
    if '{api_key}' in url:url=url.replace('{api_key}',key) if key else ''
    if url:folium.TileLayer(tiles=url,attr='CARTO / OpenStreetMap contributors',name='CARTO dark').add_to(m)
    else:folium.TileLayer('CartoDB dark_matter',name='CARTO dark (public tiles)').add_to(m)
    MeasureControl(position='topright',primary_length_unit='feet',secondary_length_unit='miles').add_to(m)
    colors={'RED':'red','YELLOW':'orange','GREEN':'green','GRAY':'gray'}
    placement_layer=folium.FeatureGroup(name='Placements').add_to(m)
    facility_layer=folium.FeatureGroup(name='Flagged facilities').add_to(m)
    for row_number,(lat,lon,r) in enumerate(map_rows[:1000],start=1):
        status=str(r['screening_status']); color=colors.get(status.split(' — ')[0],'gray')
        popup=f'<b>Placement {row_number}</b><br>'+html.escape(status)+'<br>Distance: '+html.escape(str(r['distance_ft']))+' ft<br>Facility: '+html.escape(str(r['nearby_facility'])[:150])
        folium.CircleMarker([lat,lon],radius=6,color=color,fill=True,fill_opacity=.85,popup=folium.Popup(popup,max_width=350)).add_to(placement_layer)
        facility_lat=r.get('facility_latitude');facility_lon=r.get('facility_longitude')
        if status.startswith(('RED','YELLOW')) and pd.notna(facility_lat) and pd.notna(facility_lon):
            name=str(r['nearby_facility']).strip() or 'Unnamed mapped facility'
            facility_popup=(f'<b>Flagged facility for placement {row_number}</b><br>'
                +html.escape(name)+'<br>'+html.escape(str(r['facility_category']))
                +'<br>OSM: '+html.escape(str(r['osm_id']))
                +'<br>Pin marks mapped facility location; measure manually.')
            folium.Marker([float(facility_lat),float(facility_lon)],
                icon=folium.Icon(color=color,icon='exclamation-sign',prefix='glyphicon'),
                tooltip='Flagged facility: '+html.escape(name),
                popup=folium.Popup(facility_popup,max_width=350)).add_to(facility_layer)
    folium.LayerControl(collapsed=False).add_to(m)
    # Explicit heights on both Leaflet and its iframe prevent a collapsed map.
    # Pan, zoom, popups and measuring stay in the browser without app reruns.
    components.html(m.get_root().render(),height=620,scrolling=False)
    if len(map_rows)>1000:st.caption('Map displays first 1,000 placements; exports include all.')
st.caption('Coverage: 50 US states + DC, subject to successful state builds. Territories are not yet supported. OSM incompleteness, missing footprints and facility classifications require manual review for ALL statuses.')
