#!/usr/bin/env python3
"""Private, opt-in local transcription of small Moodle-linked public Yandex videos.

No video/audio is written to disk: public Yandex bytes -> ffmpeg stdin ->
16kHz PCM stdout -> locally cached Whisper base -> Windows DPAPI transcript.
No Moodle tokens or lecture audio sent to an external transcription service.
Never starts Moodle attempts, clicks a video, records a microphone, or uploads.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import numpy as np

import moodle_client as m
import moodle_yadisk as yd

CACHE=m.HERE/".moodle_videos.dpapi"
MODEL_CACHE=Path.home()/".cache"/"whisper"/"base.pt"
MAX_SOURCE_SIZE=112*1024*1024
MAX_AUDIO_SECONDS=1500
MAX_PCM_BYTES=MAX_AUDIO_SECONDS*32000+100000
VERSION=1

class VideoError(Exception):pass

def available():
    # Recheck the exact public link's current presence on student-visible Moodle page.
    live=yd.listed_sources()
    # Metadata-only Yandex file entries are retained separately from downloaded documents.
    return {k:r for k,r in yd.read_cache()["files"].items()
            if r.get("source") in live and
            str(r.get("mime","")).lower().startswith("video/")}

def download_video_bounded(record):
    # YDisk's document downloader intentionally permits only <=48 MiB.
    # Video-only path permits <=112 MiB with the same trusted CDN allowlist.
    from urllib.parse import urlsplit
    from urllib.request import Request
    source=yd.public_url(record.get("source"))
    size=record.get("size")
    if not isinstance(size,int) or not 0<size<=MAX_SOURCE_SIZE:
        raise VideoError("Video too large for bounded downloader")
    options={"public_key":source}
    if record.get("relative_path"):
        options["path"]=record["relative_path"]
    try:
        link=yd.request_json("download",options).get("href")
        p=urlsplit(link or "")
        if p.scheme!="https" or p.hostname not in (
                "downloader.disk.yandex.ru","downloader.disk.yandex.net","mds.storage.yandex.net"
            ) or p.username or p.password or p.port not in (None,443):
            raise VideoError("Yandex file server not trusted")
        with yd.DOWNLOADER.open(Request(link,headers={"User-Agent":"MISIS-Moodle-LocalVideo/1.0"}),timeout=90) as response:
            if response.status!=200:
                raise VideoError("Unexpected download status")
            size_header=response.headers.get("Content-Length")
            if size_header and int(size_header)>MAX_SOURCE_SIZE:
                raise VideoError("Video HTTP size too large")
            binary=response.read(MAX_SOURCE_SIZE+1)
    except (OSError,ValueError,yd.YDiskError) as exc:
        raise VideoError("Bounded video download failed: "+type(exc).__name__) from None
    if len(binary)!=size:
        raise VideoError("Downloaded bytes differ from public file metadata")
    return binary

def local_audio(record,seconds=MAX_AUDIO_SECONDS):
    if record.get("mime","").lower() not in ("video/mp4","video/mp2t","video/mpeg","video/quicktime"):
        raise VideoError("Unsupported video audio format")
    if not isinstance(record.get("size"),int) or not 0<record["size"]<=MAX_SOURCE_SIZE:
        raise VideoError("Video too large for local in-memory transcription")
    if not 30<=seconds<=MAX_AUDIO_SECONDS:
        raise VideoError("Invalid audio duration limit")
    try:
        binary=download_video_bounded(record)
        cmd=["ffmpeg","-hide_banner","-loglevel","error","-nostdin",
             "-i","pipe:0","-vn","-t",str(seconds),
             "-ac","1","-ar","16000","-f","s16le","pipe:1"]
        p=subprocess.run(cmd,input=binary,capture_output=True,timeout=110,
                         creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except (OSError,subprocess.TimeoutExpired,yd.YDiskError) as exc:
        raise VideoError("Could not privately decode audio: "+type(exc).__name__) from None
    if p.returncode or not p.stdout or len(p.stdout)>MAX_PCM_BYTES:
        raise VideoError("ffmpeg could not decode bounded audio")
    if len(p.stdout)%2:
        raise VideoError("Invalid PCM bytes")
    waveform=np.frombuffer(p.stdout,dtype="<i2").astype(np.float32)/32768.0
    return waveform

def transcribe(audio):
    if not MODEL_CACHE.is_file():
        raise VideoError("Whisper model not cached locally; refuse automatic network download")
    try:
        import torch,whisper
        torch.set_num_threads(min(6,os.cpu_count() or 4))
        engine=whisper.load_model("base",device="cpu",download_root=str(MODEL_CACHE.parent))
        result=engine.transcribe(audio,language="ru",fp16=False,temperature=0,
                                 verbose=None,condition_on_previous_text=False)
    except (RuntimeError,ImportError,OSError,ValueError) as exc:
        raise VideoError("Local transcription failed: "+type(exc).__name__) from None
    chunks=[]
    for x in result.get("segments",[])[:3000]:
        value=str(x.get("text","")).strip()
        if not value:continue
        chunks.append({"start_sec":round(float(x.get("start",0)),1),
                       "end_sec":round(float(x.get("end",0)),1),
                       "text":value[:1300]})
    return chunks

def read_cache():
    if not CACHE.exists():return {"version":VERSION,"videos":{}}
    try:
        data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if data.get("version")!=VERSION or not isinstance(data.get("videos"),dict):
            raise VideoError("Schema mismatch")
        return data
    except (OSError,ValueError,UnicodeError):
        raise VideoError("Encrypted video index unreadable") from None

def save_cache(doc):
    blob=m.dpapi(json.dumps(doc,ensure_ascii=False).encode("utf-8"),True)
    fd,tmp=tempfile.mkstemp(dir=m.HERE,prefix=".video_tmp_")
    try:
        with os.fdopen(fd,"wb") as f:f.write(blob)
        os.replace(tmp,CACHE)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def index(limit=2,clip_minutes=25,budget_mb=100):
    if not 1<=limit<=4 or not 1<=clip_minutes<=25 or not 1<=budget_mb<=200:
        raise VideoError("Invalid safe indexing limits")
    source=available()
    cache=read_cache()
    candidates=sorted((k,v) for k,v in source.items()
                      if k not in cache["videos"] or
                      (cache["videos"][k].get("status")=="metadata_only"
                       and isinstance(v.get("size"),int) and 0<v["size"]<=MAX_SOURCE_SIZE))
    candidates.sort(key=lambda p:p[1].get("size") or 999999999999)
    indexed=0;budget=budget_mb*1048576;spent=0;errors=Counter();skipped=Counter()
    for key,item in candidates:
        # Keep metadata for every existing video, without downloading large videos.
        meta={k:item.get(k) for k in ("source","course","course_id","cmid","module","name","size","mime")}
        size=item.get("size")
        if not isinstance(size,int) or size>MAX_SOURCE_SIZE or size<1:
            cache["videos"][key]={**meta,"status":"metadata_only","segments":[],"indexed_at":int(time.time())}
            skipped["size_limit"]+=1;continue
        if indexed>=limit:break
        if spent+size>budget:skipped["budget"]+=1;continue
        try:
            pcm=local_audio(item,seconds=clip_minutes*60)
            duration=round(len(pcm)/16000,2)
            segments=transcribe(pcm)
            record={**meta,"status":"transcribed","segments":segments,
                    "audio_seconds":duration,
                    "complete":duration<clip_minutes*60-1,
                    "model":"Whisper base (local)","indexed_at":int(time.time())}
        except VideoError as exc:
            record={**meta,"status":"failed","reason":str(exc)[:110],
                    "segments":[],"indexed_at":int(time.time())}
            errors[type(exc).__name__]+=1
        cache["videos"][key]=record
        indexed+=1;spent+=size
        save_cache(cache)
    save_cache(cache)
    all_records=cache["videos"]
    return {"public_video_files":len(source),"processed":indexed,
            "transcribed":sum(v.get("status")=="transcribed" for v in all_records.values()),
            "complete_videos":sum(v.get("complete",False) for v in all_records.values()),
            "metadata_only":sum(v.get("status")=="metadata_only" for v in all_records.values()),
            "audio_minutes":round(sum(v.get("audio_seconds",0) for v in all_records.values())/60,2),
            "segments":sum(len(v.get("segments",[])) for v in all_records.values()),
            "download_budget_mb_used":round(spent/1048576,1),
            "errors":dict(errors),"skipped":dict(skipped)}

def search(query,limit=20):
    terms=[x.casefold() for x in query.split() if x]
    if not terms:raise ValueError("Empty video search")
    source=available()
    results=[]
    for key,item in read_cache()["videos"].items():
        if key not in source:continue
        for segment in item.get("segments",[]):
            transcript=segment.get("text","")
            corpus=" ".join((item.get("course",""),item.get("name",""),transcript)).casefold()
            if not all(term in corpus for term in terms):continue
            results.append({"course":item.get("course"),"cmid":item.get("cmid"),
                            "video":item.get("name"),"source":item.get("source"),
                            "start_sec":segment.get("start_sec"),
                            "excerpt":transcript[:400],
                            "transcribed_complete":item.get("complete",False),
                            "transcription":"automatic_local_whisper"})
            if len(results)>=limit:return results
    return results

def status():
    cache=read_cache()["videos"]
    return {"video_files":len(cache),
            "states":dict(Counter(x.get("status") for x in cache.values())),
            "audio_minutes":round(sum(x.get("audio_seconds",0) for x in cache.values())/60,2),
            "segments":sum(len(x.get("segments",[])) for x in cache.values())}

def run(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["index","status","search"])
    p.add_argument("--limit",type=int,default=2)
    p.add_argument("--clip-minutes",type=int,default=25)
    p.add_argument("--budget-mb",type=int,default=100)
    p.add_argument("--query",default="")
    a=p.parse_args(argv)
    res=index(a.limit,a.clip_minutes,a.budget_mb) if a.command=="index" else (
        search(a.query,a.limit) if a.command=="search" else status())
    print(json.dumps(res,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    try:sys.exit(run())
    except (VideoError,m.MoodleError,yd.YDiskError,OSError,ValueError) as exc:
        print("Video: "+str(exc)[:180],file=sys.stderr)
        sys.exit(2)
