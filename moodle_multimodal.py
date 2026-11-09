#!/usr/bin/env python3
"""Local, bounded multimodal extraction. No network, paid API or credential access."""
import hashlib,io,json,re,subprocess,zipfile
from xml.etree import ElementTree as ET

MAX=32*1024*1024
NS={'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
'p':'http://schemas.openxmlformats.org/presentationml/2006/main',
'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
'c':'http://schemas.openxmlformats.org/drawingml/2006/chart'}
class MultimodalError(Exception):pass

def safe_xml(blob):
 if len(blob)>8*1024*1024 or b'<!DOCTYPE' in blob.upper() or b'<!ENTITY' in blob.upper():
  raise MultimodalError("Unsafe XML")
 try:return ET.fromstring(blob)
 except ET.ParseError:raise MultimodalError("Malformed XML") from None

def text_of(root):
 return ' '.join(e.text.strip() for e in root.findall('.//a:t',NS) if e.text and e.text.strip())[:30000]

def image(blob,ocr=False):
 from PIL import Image,ImageOps
 if len(blob)>8*1024*1024:raise MultimodalError("Image over 8 MiB")
 try:
  with Image.open(io.BytesIO(blob)) as im:
   im.verify()
  with Image.open(io.BytesIO(blob)) as im:
   if im.width*im.height>5_000_000:raise MultimodalError("Too many pixels")
   dims=[im.width,im.height]
   if ocr:
    stream=io.BytesIO()
    ImageOps.exif_transpose(im).convert('RGB').save(stream,format='PNG')
 except MultimodalError:raise
 except Exception:raise MultimodalError("Invalid image") from None
 rec={'format':'image','dimensions':dims,'ocr_status':'not_requested',
      'visual_meaning':'not_inferred','chart_values':'not_inferred'}
 if ocr:
  try:
   import moodle_ocr
   info=moodle_ocr.recognize_pngs([stream.getvalue()])[0]
   rec.update(ocr_status='completed',ocr_text=info.get('text','')[:12000],
              ocr_confidence=info.get('mean_confidence'))
  except Exception:rec['ocr_status']='unavailable'
 return rec

def pdf(blob,max_pages=30,ocr=False):
 if not blob.startswith(b'%PDF') or len(blob)>MAX:raise MultimodalError("Invalid PDF")
 import fitz
 try:doc=fitz.open(stream=blob,filetype='pdf')
 except Exception:raise MultimodalError("Unreadable PDF") from None
 with doc:
  if doc.needs_pass:raise MultimodalError("Encrypted PDF")
  n=len(doc)
  records=[]
  ocr_pages=0
  for i in range(min(max_pages,n)):
   p=doc[i]
   txt=p.get_text('text')[:30000]
   row={'unit':'page','number':i+1,'text':txt,'text_source':'pdf_text_layer',
        'tables':[],'images':[],'drawings':0}
   try:
    for tab in list(p.find_tables())[:8]:
     row['tables'].append({'rows':[[str(v or '')[:500] for v in rr[:16]]
                                  for rr in tab.extract()[:70]],'source':'pdf_geometry'})
   except Exception:row['tables_status']='unavailable'
   try:
    row['images']=[{'width':x.get('width'),'height':x.get('height'),
                     'meaning':'requires_visual_interpretation'}
                   for x in p.get_image_info()[:25]]
   except Exception:row['images_status']='unavailable'
   try:row['drawings']=len(p.get_drawings())
   except Exception:row['drawings_status']='unavailable'
   if ocr and ocr_pages<4 and (len(txt.strip())<80 or len(row['images'])>0):
    ocr_pages+=1
    try:
     import moodle_ocr
     row['ocr_text']=moodle_ocr.pdf_ocr(blob,page_numbers=[i])[:16000]
     row['ocr_status']='completed'
    except Exception:row['ocr_status']='unavailable'
   elif ocr and len(row['images'])>0:
    row['ocr_status']='budget_exhausted'
   records.append(row)
 return {'format':'pdf','total_pages':n,'pages':records,
         'partial':n>max_pages,'visual_meaning':'not_inferred'}

def _chart(root):
 kinds=[]; series=[]
 for typ in ('barChart','lineChart','pieChart','scatterChart','areaChart','doughnutChart','radarChart'):
  for c in root.findall('.//c:'+typ,NS):
   kinds.append(typ)
   for s in c.findall('c:ser',NS):
    title=s.find('c:tx/c:v',NS)
    if title is None:title=s.find('c:tx/c:strRef/c:strCache/c:pt/c:v',NS)
    result={'name':title.text if title is not None else '',
            'categories':[],'values':[],'source':'ooxml_chart_cache'}
    for col,tag in (('categories','cat'),('categories','xVal'),('values','val'),('values','yVal')):
     parent=s.find('c:'+tag,NS)
     if parent is None:continue
     coords=[]
     for pt in parent.findall('.//c:pt',NS):
      v=pt.find('c:v',NS)
      if v is None:continue
      try:coords.append((int(pt.get('idx','0')),v.text or ''))
      except ValueError:pass
     if col=='values' or not result[col]:
      result[col]=[v for _,v in sorted(coords)][:300]
    series.append(result)
 return {'types':kinds,'series':series,'data_source':'editable_chart_cache',
         'data_available':any(s['values'] for s in series)}

def pptx(blob,max_slides=120,ocr=False):
 if not blob.startswith(b'PK\x03\x04') or len(blob)>MAX:raise MultimodalError("Invalid PPTX")
 try:
  with zipfile.ZipFile(io.BytesIO(blob)) as z:
   infos=z.infolist()
   if len(infos)>3000 or sum(f.file_size for f in infos)>70*1024*1024:
    raise MultimodalError("ZIP expansion limit")
   allowed={f.filename for f in infos if f.file_size<8*1024*1024}
   paths=sorted([p for p in allowed if re.fullmatch(r'ppt/slides/slide\d+\.xml',p)],
                key=lambda p:int(re.search(r'slide(\d+)',p).group(1)))
   out=[]
   picture_ocr_used=0
   for path in paths[:max_slides]:
    num=int(re.search(r'slide(\d+)',path).group(1))
    root=safe_xml(z.read(path))
    record={'unit':'slide','number':num,'text':text_of(root),
            'tables':[],'charts':[],'images':[],'visual_semantics':'not_inferred'}
    for tbl in root.findall('.//a:tbl',NS)[:8]:
     rows=[]
     for tr in tbl.findall('a:tr',NS)[:70]:
      rows.append([text_of(cell)[:600] for cell in tr.findall('a:tc',NS)[:16]])
     record['tables'].append({'rows':rows,'source':'ooxml_table'})
    relations={}
    relpath='ppt/slides/_rels/'+path.rsplit('/',1)[-1]+'.rels'
    if relpath in allowed:
     for rel in safe_xml(z.read(relpath)):
      if rel.get('TargetMode')=='External':continue
      target=rel.get('Target','')
      if target.startswith('../'):target='ppt/'+target[3:]
      elif target.startswith('/'):target=target[1:]
      else:target='ppt/slides/'+target
      if re.fullmatch(r'ppt/(?:charts/chart\d+\.xml|media/image\d+\.[a-zA-Z0-9]+)',target):
       relations[rel.get('Id')]=target
    for element in root.findall('.//c:chart',NS):
     target=relations.get(element.get('{'+NS['r']+'}id'))
     if target in allowed:
      c=_chart(safe_xml(z.read(target)))
      if c['types']:record['charts'].append(c)
    for picture in root.findall('.//p:pic',NS)[:12]:
     blip=picture.find('.//a:blip',NS)
     target=relations.get(blip.get('{'+NS['r']+'}embed')) if blip is not None else None
     visual={'source':'ooxml_picture','meaning':'requires_visual_interpretation'}
     if target in allowed:
      part=z.read(target)
      visual['sha256_prefix']=hashlib.sha256(part).hexdigest()[:12]
      if ocr and picture_ocr_used<4:
       picture_ocr_used+=1
       try:visual['image_analysis']=image(part,ocr=True)
       except MultimodalError:visual['image_analysis']={'ocr_status':'unsupported'}
      elif ocr:visual['image_analysis']={'ocr_status':'budget_exhausted'}
     record['images'].append(visual)
    out.append(record)
 except zipfile.BadZipFile:raise MultimodalError("Bad ZIP") from None
 return {'format':'pptx','total_slides':len(paths),'slides':out,
         'partial':len(paths)>max_slides}

def video(blob,interval_sec=10,max_frames=8,ocr=False):
 from PIL import Image
 if not blob or len(blob)>112*1024*1024 or not 2<=interval_sec<=120 or not 1<=max_frames<=16:
  raise MultimodalError("Video size or sampling limits")
 cmd=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-i','pipe:0',
      '-vf',f'fps=1/{interval_sec},scale=640:-2','-frames:v',str(max_frames),
      '-f','image2pipe','-vcodec','mjpeg','pipe:1']
 try:
  p=subprocess.run(cmd,input=blob,capture_output=True,timeout=100,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 except (OSError,subprocess.TimeoutExpired):
  raise MultimodalError("FFmpeg unavailable or timeout") from None
 if p.returncode or len(p.stdout)>20*1024*1024:
  raise MultimodalError("FFmpeg decode failure")
 rows=[];previous=None
 for idx,part in enumerate(p.stdout.split(b'\xff\xd8')[1:max_frames+2]):
  end=part.find(b'\xff\xd9')
  if end<0:continue
  jpeg=b'\xff\xd8'+part[:end+2]
  try:
   with Image.open(io.BytesIO(jpeg)) as img:
    grid=list(img.convert('L').resize((16,16)).tobytes())
  except Exception:continue
  if previous is not None and sum(abs(a-b) for a,b in zip(grid,previous))/256<5:
   continue
  previous=grid
  row={'frame_index':idx,'seconds_estimate':idx*interval_sec,
       'time_approximate':True,'visual_meaning':'not_inferred'}
  if ocr:
   try:row['image_analysis']=image(jpeg,ocr=True)
   except MultimodalError:row['image_analysis']={'ocr_status':'unavailable'}
  rows.append(row)
 return {'format':'video','frames':rows,'sampled_frames':len(rows),
         'audio':'use_existing_whisper_transcripts','partial':True}

def analyze(blob,format,**kwargs):
 typ=format.lower().lstrip('.')
 if typ=='pdf':return pdf(blob,**kwargs)
 if typ=='pptx':return pptx(blob,**kwargs)
 if typ in ('jpeg','jpg','png','webp','bmp'):return image(blob,**kwargs)
 if typ in ('mp4','mpeg','mov','mp2t'):return video(blob,**kwargs)
 raise MultimodalError("Unsupported multimedia format")

def flatten(result):
 parts=[]
 for unit in result.get('slides',result.get('pages',[])):
  parts.extend(str(unit.get(x,'') or '') for x in ('text','ocr_text'))
  for tbl in unit.get('tables',[]):
   parts.extend(str(cell) for row in tbl['rows'] for cell in row)
  for chart in unit.get('charts',[]):
   for s in chart.get('series',[]):
    parts.append(s.get('name',''))
    parts.extend(s.get('categories',[]))
    parts.extend(s.get('values',[]))
  for im in unit.get('images',[]):
   parts.append(str(im.get('image_analysis',{}).get('ocr_text','')))
 for frame in result.get('frames',[]):
  parts.append(str(frame.get('image_analysis',{}).get('ocr_text','')))
 if result.get('format')=='image':parts.append(result.get('ocr_text',''))
 return '\n'.join(parts)[:120000]
