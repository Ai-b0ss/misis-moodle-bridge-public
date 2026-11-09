#!/usr/bin/env python3
"""Explicit per-file Moodle multimodal indexing, encrypted and ACL-checked."""
import argparse,hashlib,json,os,tempfile
from pathlib import Path
import moodle_client as m
import moodle_materials as mat
import moodle_multimodal as multi
CACHE=m.HERE/".moodle_multimodal.dpapi"
MIMES={'application/pdf':'pdf',
       'application/vnd.openxmlformats-officedocument.presentationml.presentation':'pptx'}
class IndexErrorSafe(Exception):pass

def _inventory():
 token,site=m.site_info(m.read_credential())
 return token,mat.inventory(token,site)

def _load():
 if not CACHE.exists():return {'version':1,'files':{}}
 try:
  result=json.loads(m.dpapi(CACHE.read_bytes(),False).decode('utf-8'))
  if result.get('version')!=1 or not isinstance(result.get('files'),dict):
   raise ValueError("invalid schema")
  return result
 except (ValueError,OSError,UnicodeError):
  raise IndexErrorSafe('Encrypted multimodal cache cannot be loaded') from None

def _save(doc):
 payload=m.dpapi(json.dumps(doc,ensure_ascii=False).encode('utf-8'),True)
 fd,tmp=tempfile.mkstemp(prefix='.multimodal_tmp_',dir=m.HERE)
 try:
  with os.fdopen(fd,'wb') as stream:stream.write(payload)
  os.replace(tmp,CACHE)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def index(cmid,max_file_mb=12,ocr=False):
 if not isinstance(cmid,int) or cmid<=0:raise IndexErrorSafe("Invalid module id")
 if not 1<=max_file_mb<=32:raise IndexErrorSafe("Invalid size bound")
 token,files=_inventory()
 options=[(k,f) for k,f in files.items() if f.get('cmid')==cmid
          and f.get('mime') in MIMES and
          isinstance(f.get('size'),int) and 0<f['size']<=max_file_mb*1048576]
 if not options:
  return {'state':'no_supported_visible_file','cmid':cmid}
 key,f=sorted(options,key=lambda x:x[1]['size'])[0]
 ext=MIMES[f['mime']]
 data=mat.download(f['url'],token,f['size'])
 result=multi.analyze(data,ext,ocr=ocr)
 # Only metadata and extracted text; never persist original file or images.
 safe={'course_id':f['course_id'],'course':f['course'],'cmid':cmid,
       'title':f.get('title',''),'name':f.get('name',''),
       'format':ext,'source_key':key,'analysis':result}
 doc=_load()
 doc['files'][key]=safe
 if len(doc['files'])>40:
  doc['files']={k:doc['files'][k] for k in list(doc['files'])[-40:]}
 _save(doc)
 return {'state':'indexed','cmid':cmid,'format':ext,
         'units':len(result.get('slides',result.get('pages',[]))),
         'tables':sum(len(u.get('tables',[])) for u in result.get('slides',result.get('pages',[]))),
         'charts':sum(len(u.get('charts',[])) for u in result.get('slides',[])),
         'images':sum(len(u.get('images',[])) for u in result.get('slides',result.get('pages',[]))),
         'ocr':ocr}

def search(query,limit=20):
 if not query.strip() or not 1<=limit<=100:raise IndexErrorSafe("Invalid query or limit")
 doc=_load()
 if not doc['files']:return []
 token,live=_inventory()
 terms=[t.casefold() for t in query.split()]
 rows=[]
 for key,item in doc['files'].items():
  if key not in live or live[key].get('cmid')!=item.get('cmid'):
   continue
  output=item['analysis']
  for u in output.get('slides',output.get('pages',[])):
   fields=[('text',u.get('text',''))]
   if u.get('ocr_text'):fields.append(('ocr',u['ocr_text']))
   for table in u.get('tables',[]):
    fields.append(('table',' '.join(str(x) for row in table.get('rows',[]) for x in row)))
   for chart in u.get('charts',[]):
    for series in chart.get('series',[]):
     fields.append(('chart',' '.join([series.get('name','')]+series.get('categories',[])+series.get('values',[]))))
   for visual in u.get('images',[]):
    ocr=visual.get('image_analysis',{}).get('ocr_text')
    if ocr:fields.append(('image_ocr',ocr))
   for kind,txt in fields:
    if not txt or not all(t in txt.casefold() for t in terms):continue
    positions=[txt.casefold().find(t) for t in terms]
    start=max(0,min(positions)-45)
    rows.append({'source':'moodle_multimodal','course':item['course'],
                 'module':item['title'],'file_name':item['name'],
                 'cmid':item['cmid'],'format':item['format'],
                 'unit':u['unit'],'number':u['number'],'evidence_type':kind,
                 'excerpt':txt[start:start+350],
                 'interpretation':'raw_extracted_not_visual_semantics'})
    if len(rows)>=limit:return rows
 return rows

def status():
 doc=_load()
 return {'files_indexed':len(doc['files']),
         'units':sum(len(x['analysis'].get('slides',x['analysis'].get('pages',[]))) for x in doc['files'].values()),
         'charts':sum(len(u.get('charts',[])) for x in doc['files'].values()
                      for u in x['analysis'].get('slides',[])),
         'encrypted':CACHE.exists()}

def run(argv=None):
 p=argparse.ArgumentParser()
 p.add_argument('command',choices=('index','status','search'))
 p.add_argument('--cmid',type=int)
 p.add_argument('--max-file-mb',type=int,default=12)
 p.add_argument('--ocr',action='store_true')
 p.add_argument('--query',default='')
 p.add_argument('--limit',type=int,default=20)
 a=p.parse_args(argv)
 if a.command=='index':
  if a.cmid is None:p.error('--cmid required')
  out=index(a.cmid,a.max_file_mb,a.ocr)
 elif a.command=='search':out=search(a.query,a.limit)
 else:out=status()
 print(json.dumps(out,ensure_ascii=True,indent=2))
 return 0

if __name__=='__main__':
 try:raise SystemExit(run())
 except (IndexErrorSafe,m.MoodleError,mat.MaterialError,multi.MultimodalError,OSError) as e:
  print('Multimodal: '+str(e)[:180],file=__import__('sys').stderr)
  raise SystemExit(2)
