#!/usr/bin/env python3
"""Bounded frame OCR index for Yandex videos linked from visible Moodle pages."""
import argparse,json,os,tempfile
import moodle_client as m
import moodle_videos as vid
import moodle_multimodal as mm

CACHE=m.HERE/".moodle_video_frames.dpapi"
class FramesError(Exception):pass

def _read():
 if not CACHE.exists():return {'version':1,'videos':{}}
 try:
  data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode('utf-8'))
  if data.get('version')!=1 or not isinstance(data.get('videos'),dict):
   raise ValueError('schema')
  return data
 except (ValueError,OSError,UnicodeError):
  raise FramesError('Protected frame index unreadable') from None

def _save(data):
 raw=m.dpapi(json.dumps(data,ensure_ascii=False).encode('utf-8'),True)
 fd,tmp=tempfile.mkstemp(prefix=".frames_tmp_",dir=m.HERE)
 try:
  with os.fdopen(fd,'wb') as f:f.write(raw)
  os.replace(tmp,CACHE)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def index(cmid,interval_sec=20,max_frames=5,ocr=False):
 if not isinstance(cmid,int) or cmid<=0:raise FramesError('Invalid Moodle module')
 videos=vid.available()
 choices=[(key,item) for key,item in videos.items()
          if item.get('cmid')==cmid
          and isinstance(item.get('size'),int) and 0<item['size']<=vid.MAX_SOURCE_SIZE]
 if not choices:return {'state':'no_visible_video_below_size_limit','cmid':cmid}
 key,source=min(choices,key=lambda item:item[1]['size'])
 raw=vid.download_video_bounded(source)
 result=mm.video(raw,interval_sec=interval_sec,max_frames=max_frames,ocr=ocr)
 safe={'cmid':cmid,'course':source.get('course',''),
       'name':source.get('name',''),'frames':result['frames'],
       'interval_sec':interval_sec,'sampled_frames':result['sampled_frames']}
 cache=_read()
 cache['videos'][key]=safe
 if len(cache['videos'])>20:
  cache['videos']={k:cache['videos'][k] for k in list(cache['videos'])[-20:]}
 _save(cache)
 return {'state':'indexed','cmid':cmid,'sampled_frames':len(safe['frames']),
         'frame_ocr_attempted':ocr,'source_size_bytes':source['size'],
         'meaning':'not_inferred'}

def status():
 data=_read()['videos']
 return {'videos':len(data),'frames':sum(len(x.get('frames',[])) for x in data.values()),
         'encrypted':CACHE.exists()}

def search(query,limit=20):
 if not query.strip() or not 1<=limit<=100:raise FramesError('Invalid query')
 visible=vid.available()
 out=[]
 terms=[x.casefold() for x in query.split()]
 for key,rec in _read()['videos'].items():
  if key not in visible:continue
  for frame in rec['frames']:
   text=(frame.get('image_analysis') or {}).get('ocr_text','')
   if not text or not all(t in text.casefold() for t in terms):continue
   out.append({'type':'video_frame_ocr','course':rec['course'],
               'cmid':rec['cmid'],'video':rec['name'],
               'seconds_approximate':frame.get('seconds_estimate'),
               'excerpt':text[:350],'meaning':'OCR_only_not_visual_interpretation'})
   if len(out)>=limit:return out
 return out

def run(argv=None):
 p=argparse.ArgumentParser()
 p.add_argument('command',choices=('index','status','search'))
 p.add_argument('--cmid',type=int)
 p.add_argument('--interval',type=int,default=20)
 p.add_argument('--max-frames',type=int,default=5)
 p.add_argument('--ocr',action='store_true')
 p.add_argument('--query',default='')
 p.add_argument('--limit',type=int,default=20)
 a=p.parse_args(argv)
 if a.command=='index':
  if a.cmid is None:p.error('--cmid required')
  result=index(a.cmid,a.interval,a.max_frames,a.ocr)
 elif a.command=='search':result=search(a.query,a.limit)
 else:result=status()
 print(json.dumps(result,ensure_ascii=True,indent=2))
 return 0

if __name__=='__main__':
 try:raise SystemExit(run())
 except (FramesError,mm.MultimodalError,vid.VideoError,m.MoodleError,OSError) as exc:
  print('Frame index: '+str(exc)[:200],file=__import__('sys').stderr)
  raise SystemExit(2)
