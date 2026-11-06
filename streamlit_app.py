"""Stage 5: private-GitHub-backed OSM candidate screening, review only."""
import gzip, io, json, math, time
from collections import Counter
from datetime import datetime, timezone
import folium
from folium.plugins import MeasureControl
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium
from openpyxl.styles import PatternFill, Font
from shapely.geometry import shape, Point, mapping
from shapely.ops import transform
from shapely.strtree import STRtree
from pyproj import Transformer

st.set_page_config(page_title='ABI OOH · Stage 5',page_icon='🗺️',layout='wide')
st.markdown('''<style>.block-container{padding-top:1.6rem} .stButton button[kind="primary"]{background:#1abca9;color:#07161d;border:0} .small-note{color:#91a8b6;font-size:.86rem}</style>''',unsafe_allow_html=True)
st.title('ABI OOH · LDA Compliance Hub')
st.caption('Stage 5 · NYC beta · shared daily OSM snapshots · review-only screening')
st.warning('**Not a compliance determination.** Every placement requires team review. Candidate polygons are not verified building footprints; missing OSM features can produce false negatives.')

repo='seanpnels/abi-lda-data-poc'
try:
    token=st.secrets.get('GITHUB_TOKEN','')
    repo=st.secrets.get('GITHUB_REPO',repo)
except Exception: token=''
if not token:
    st.error('Missing GITHUB_TOKEN in Streamlit Secrets. See README_STAGE5.md.');st.stop()
HEADERS={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

def api_get(url, accept=None):
    h=dict(HEADERS)
    if accept:h['Accept']=accept
    r=requests.get(url,headers=h,timeout=(15,90),allow_redirects=True)
    r.raise_for_status()
    return r

@st.cache_data(ttl=600,show_spinner=False)
def newest_snapshot(repo,token):
    headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    r=requests.get(f'https://api.github.com/repos/{repo}/releases?per_page=30',headers=headers,timeout=30)
    r.raise_for_status()
    releases=[x for x in r.json() if x.get('tag_name','').startswith('nyc-osm-') and not x.get('draft')]
    for rel in releases:
        assets={a['name']:a for a in rel.get('assets',[])}
        if {'nyc-facilities.geojsonseq.gz','manifest.json'}.issubset(assets):
            return {'tag':rel['tag_name'],'data_asset':assets['nyc-facilities.geojsonseq.gz']['id'],'manifest_asset':assets['manifest.json']['id'],'published_at':rel.get('published_at')}
    raise RuntimeError('No Stage 5 dataset release found. Run the new Stage 5 GitHub Action first.')

@st.cache_data(ttl=3600,show_spinner='Loading validated OSM snapshot…')
def download_asset(repo,asset_id,token):
    h={'Authorization':f'Bearer {token}','Accept':'application/octet-stream','X-GitHub-Api-Version':'2022-11-28'}
    r=requests.get(f'https://api.github.com/repos/{repo}/releases/assets/{asset_id}',headers=h,timeout=(20,180),allow_redirects=True)
    r.raise_for_status()
    if r.content[:1]==b'{' and 'json' in r.headers.get('Content-Type',''):
        raise RuntimeError('GitHub returned asset metadata instead of file content; verify token and release access.')
    return r.content

PROJ=Transformer.from_crs('EPSG:4326','EPSG:2263',always_xy=True).transform
REVERSE=Transformer.from_crs('EPSG:2263','EPSG:4326',always_xy=True).transform
AMENITIES={'school':'school','kindergarten':'daycare','childcare':'daycare','place_of_worship':'worship','college':'university','university':'university'}
BUILDINGS={'school':'school','kindergarten':'daycare','church':'worship','chapel':'worship','mosque':'worship','synagogue':'worship','temple':'worship','college':'university','university':'university'}
def classify(t):
    cats=set()
    if t.get('amenity') in AMENITIES:cats.add(AMENITIES[t['amenity']])
    if t.get('building') in BUILDINGS:cats.add(BUILDINGS[t['building']])
    if t.get('social_facility') in ('drug_addiction','alcohol_addiction'):cats.add('substance_use_candidate')
    return sorted(cats)

@st.cache_resource(show_spinner='Indexing geographic candidates…')
def load_geometries(repo,asset_id,token):
    blob=download_asset(repo,asset_id,token)
    feats=[]; errors=0
    with gzip.open(io.BytesIO(blob),'rt',encoding='utf8') as f:
        for line in f:
            line=line.strip().lstrip('\x1e')
            if not line:continue
            try:
                obj=json.loads(line); props=obj.get('properties') or {};tags=props.get('tags') or props
                cats=classify(tags)
                if not cats or not obj.get('geometry'):continue
                geom=transform(PROJ,shape(obj['geometry']))
                if not geom.is_valid:geom=geom.buffer(0)
                if geom.is_empty:continue
                quality='polygon_unverified' if geom.geom_type in ('Polygon','MultiPolygon') else 'point_or_line_unverified'
                feats.append({'geom':geom,'name':tags.get('name',''),'cats':cats,'osm_id':str(props.get('@id',obj.get('id',''))),'quality':quality})
            except Exception:errors+=1
    if errors or len(feats)<10000:raise RuntimeError(f'Dataset failed validation: {len(feats)} candidates, {errors} parsing errors')
    return feats,STRtree([f['geom'] for f in feats])

def find_columns(df):
    def pick(candidates):
        for c in df.columns:
            if str(c).strip().lower() in candidates:return c
        return None
    return pick({'latitude','lat','y'}),pick({'longitude','lon','lng','long','x'})

def screen(df,lat_col,lon_col,features,index,restriction):
    results=[]; radius=restriction+50
    for _,r in df.iterrows():
        rec={'screening_status':'GRAY — Unable to screen','distance_ft':None,'nearby_facility':'','facility_category':'','osm_id':'','geometry_quality':'','nearby_candidates':0,'screening_notes':'Human review required'}
        try:
            lat=float(r[lat_col]);lon=float(r[lon_col])
            if not (40.4<=lat<=41.1 and -74.4<=lon<=-73.5):raise ValueError('Outside Stage 5 NYC beta coverage')
            p=transform(PROJ,Point(lon,lat));matches=[]
            for i in index.query(p.buffer(radius)):
                f=features[int(i)];d=p.distance(f['geom'])
                if d<=radius:matches.append((d,int(i)))
            matches.sort(key=lambda x:x[0]);rec['nearby_candidates']=len(matches)
            poly=[(d,i) for d,i in matches if features[i]['quality']=='polygon_unverified']
            nonpoly=[(d,i) for d,i in matches if features[i]['quality']!='polygon_unverified']
            if poly and poly[0][0]<=restriction: status='RED — Potential conflict'
            elif poly and poly[0][0]<=radius:status='YELLOW — Review required'
            elif nonpoly:status='YELLOW — Unverified geometry'
            else:status='GREEN — No detected conflict'
            if matches:
                d,i=matches[0];f=features[i]
                rec.update({'distance_ft':round(d,1),'nearby_facility':f['name'],'facility_category':'; '.join(f['cats']),'osm_id':f['osm_id'],'geometry_quality':f['quality']})
            rec['screening_status']=status
            rec['screening_notes']='Candidate geometry, not verified building footprint; team review required.'
        except Exception as exc:rec['screening_notes']=str(exc)+'; team review required.'
        results.append(rec)
    return pd.concat([df.reset_index(drop=True),pd.DataFrame(results)],axis=1)

def to_excel(df):
    bio=io.BytesIO()
    with pd.ExcelWriter(bio,engine='openpyxl') as writer:
        df.to_excel(writer,sheet_name='Screening',index=False)
        ws=writer.sheets['Screening'];status_col=df.columns.get_loc('screening_status')+1
        colors={'RED':'FCE8E8','YELLOW':'FFF0C2','GREEN':'E2F3E8','GRAY':'E6EAF0'}
        for row in range(2,len(df)+2):
            status=str(ws.cell(row,status_col).value)
            fill=PatternFill('solid',fgColor=next((v for k,v in colors.items() if status.startswith(k)),'FFFFFF'))
            for cell in ws[row]:cell.fill=fill
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for c in ws[1]:c.font=Font(bold=True,color='FFFFFF');c.fill=PatternFill('solid',fgColor='23384B')
    return bio.getvalue()

try:
    snap=newest_snapshot(repo,token)
    manifest=json.loads(download_asset(repo,snap['manifest_asset'],token))
    feats,index=load_geometries(repo,snap['data_asset'],token)
except Exception as exc:
    st.error(f'Unable to load shared geographic snapshot: {exc}');st.stop()

snapshot_date=manifest.get('osm_timestamp') or manifest.get('osm_replication_timestamp') or manifest.get('generated_utc') or snap['published_at']
st.info(f'OSM source: {snapshot_date} · Snapshot: {snap["tag"]} · {len(feats):,} candidate geometries · NYC-only beta')
try:
    dt=datetime.fromisoformat(str(manifest.get('generated_utc','')).replace('Z','+00:00'))
    if (datetime.now(timezone.utc)-dt).total_seconds()>48*3600:
        st.error('The shared OSM dataset is more than 48 hours old. Screening is paused until a successful refresh.');st.stop()
except Exception:pass

left,right=st.columns([1,1])
with left:
    restriction=st.radio('Restricted distance',options=[250,500],horizontal=True,format_func=lambda x:f'{x} feet')
with right:
    st.metric('Manual review band',f'{restriction}–{restriction+50} ft')
file=st.file_uploader('Upload placement inventory (CSV or XLSX)',type=['csv','xlsx'])
if file is None:
    st.caption('Upload a file to begin. Inventory is processed in this app session and is not committed to GitHub by this workflow.')
    st.stop()
try:
    df=pd.read_excel(file,dtype=object) if file.name.lower().endswith('xlsx') else pd.read_csv(file,dtype=object,encoding='utf-8-sig')
    if df.empty:raise ValueError('The file has no rows')
    lat_col,lon_col=find_columns(df)
    if not lat_col or not lon_col:raise ValueError('Required columns: Latitude and Longitude (or Lat/Lon).')
except Exception as exc:st.error(f'Unable to read inventory: {exc}');st.stop()
st.caption(f'{len(df):,} placements loaded. Coordinates: {lat_col}, {lon_col}.')
if st.button('Run geographic screening',type='primary'):
    with st.spinner('Screening against shared OSM candidates…'):
        start=time.perf_counter();st.session_state['stage5_results']=screen(df,lat_col,lon_col,feats,index,restriction)
        st.session_state['stage5_elapsed']=time.perf_counter()-start
        st.session_state['stage5_params']=(file.name,restriction,snap['tag'])
if 'stage5_results' not in st.session_state:st.stop()
if st.session_state.get('stage5_params')!=(file.name,restriction,snap['tag']):
    st.warning('Inventory or restriction changed. Run screening again.');st.stop()
res=st.session_state['stage5_results']
counts=Counter(x.split(' — ')[0] for x in res['screening_status'])
a,b,c,d,e=st.columns(5)
for col,label in zip((a,b,c,d),('RED','YELLOW','GREEN','GRAY')):col.metric(label,counts.get(label,0))
e.metric('Screening time',f'{st.session_state["stage5_elapsed"]:.2f}s')
st.dataframe(res,use_container_width=True,hide_index=True)
col1,col2=st.columns(2)
with col1:st.download_button('Download color-coded Excel',data=to_excel(res),file_name='ABI_Stage5_screening.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
with col2:st.download_button('Download My Maps CSV',data=res.to_csv(index=False).encode('utf-8-sig'),file_name='ABI_Stage5_MyMaps.csv',mime='text/csv')

st.subheader('Review map')
map_rows=[]
for _,r in res.iterrows():
    try:
        lat=float(r[lat_col]);lon=float(r[lon_col]);
        if 40.4<=lat<=41.1 and -74.4<=lon<=-73.5:map_rows.append((lat,lon,r))
    except Exception:pass
if map_rows:
    center=[sum(x[0] for x in map_rows)/len(map_rows),sum(x[1] for x in map_rows)/len(map_rows)]
    m=folium.Map(location=center,zoom_start=12,tiles='CartoDB dark_matter',control_scale=True)
    MeasureControl(position='topright',primary_length_unit='feet',secondary_length_unit='miles').add_to(m)
    colors={'RED':'red','YELLOW':'orange','GREEN':'green','GRAY':'gray'}
    for lat,lon,r in map_rows[:1000]:
        status=r['screening_status'];color=colors.get(status.split(' — ')[0],'gray')
        popup=f"<b>{status}</b><br>Distance: {r['distance_ft']} ft<br>Candidate: {str(r['nearby_facility'])[:120]}<br>Category: {str(r['facility_category'])[:120]}"
        folium.CircleMarker([lat,lon],radius=6,color=color,fill=True,fill_opacity=.85,popup=folium.Popup(popup,max_width=320)).add_to(m)
    st_folium(m,height=500,width=None,returned_objects=[])
    if len(map_rows)>1000:st.caption('Map limited to first 1,000 placements; export includes all rows.')
st.caption('Known limitation: OSM polygons can represent grounds/campuses, and point-only facilities may lack footprints. Red/yellow/green are triage labels, never final approvals.')
