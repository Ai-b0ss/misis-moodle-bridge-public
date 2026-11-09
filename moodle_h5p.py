#!/usr/bin/env python3
"""Read-only indexing of student-visible H5P packages.

Only fetches packages attached to visible Moodle H5P modules. ZIP parsing is
in-memory and only content/content.json and h5p.json are inspected. Never
executes embedded JS, H5P scoring, upload or attempt methods. Text index is
Windows DPAPI-encrypted; no H5P binaries or OAuth tokens are persisted.
"""
from collections import Counter
from html.parser import HTMLParser
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import zipfile

import moodle_client as m
import moodle_materials as mat

CACHE=m.HERE / ".moodle_h5p.dpapi"
MAX_PACKAGE=32*1024*1024
MAX_ENTRY=8*1024*1024
MAX_ITEMS=3000
MAX_CONTENT=170000
VERSION=1

class H5PError(Exception):pass

class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.values=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ("script","style") and self.hidden:self.hidden-=1
    def handle_data(self,value):
        if not self.hidden and value.strip():self.values.append(value.strip())

def html_text(value):
    if not value or not isinstance(value,str):return ""
    parser=PlainText()
    parser.feed(value)
    return re.sub(r"\s+"," "," ".join(parser.values)).strip()

def walk_text(obj,prefix="",depth=0):
    if depth>28:return
    if isinstance(obj,str):
        value=html_text(obj)
        if value and not re.fullmatch(r"(?:https?://|data:|file:).+",value):
            yield prefix,value[:9000]
    elif isinstance(obj,list):
        for i,item in enumerate(obj[:2000]):yield from walk_text(item,f"{prefix}[{i}]",depth+1)
    elif isinstance(obj,dict):
        for k,v in list(obj.items())[:2000]:
            if k in ("path","mime","copyright","license","source","attribution","font","fontSize","fontColor","style","cssClass"):
                continue
            yield from walk_text(v,(prefix+"."+k).lstrip("."),depth+1)

def extract(data):
    if len(data)>MAX_PACKAGE or not data.startswith(b"PK\x03\x04"):
        raise H5PError("Bad H5P package or oversized")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>MAX_ITEMS:raise H5PError("H5P package has too many parts")
            wanted=("content/content.json","h5p.json")
            structs={}
            for name in wanted:
                try:meta=z.getinfo(name)
                except KeyError:continue
                if meta.file_size>MAX_ENTRY:raise H5PError("H5P JSON too big")
                with z.open(meta) as f:payload=f.read(MAX_ENTRY+1)
                if len(payload)>MAX_ENTRY:raise H5PError("H5P JSON too large")
                structs[name]=json.loads(payload)
            if "content/content.json" not in structs:
                raise H5PError("H5P content JSON missing")
    except (zipfile.BadZipFile, ValueError, UnicodeDecodeError, OSError) as e:
        raise H5PError("Invalid H5P archive") from None
    text=[]
    seen=set()
    for path,value in walk_text(structs["content/content.json"]):
        if value not in seen:
            seen.add(value)
            text.append(value)
        if sum(len(x) for x in text)>=MAX_CONTENT:break
    meta=structs.get("h5p.json") or {}
    if not isinstance(meta,dict):meta={}
    return {"text":"\n".join(text)[:MAX_CONTENT],
            "main_library":str(meta.get("mainLibrary") or "")[:140],
            "title":str(meta.get("title") or "")[:220],
            "text_fragments":len(text)}

def modules_visible(token,courses):
    out={}
    for c in courses:
        content=m.call(token,"core_course_get_contents",{"courseid":c["id"]})
        for sec in content:
            for mod in sec.get("modules",[]):
                if mod.get("modname")=="h5pactivity" and mod.get("uservisible") is not False:
                    out[(c["id"],mod["id"])]={"course":c["name"],"title":mod.get("name","")}
    return out

def available(token,site):
    courses=m.get_courses(token,site["userid"])
    visible=modules_visible(token,courses)
    params={"courseids["+str(i)+"]":c["id"] for i,c in enumerate(courses)}
    items=m.call(token,"mod_h5pactivity_get_h5pactivities_by_courses",params).get("h5pactivities",[])
    return [(x,visible[(x.get("course"),x.get("coursemodule"))])
            for x in items if (x.get("course"),x.get("coursemodule")) in visible]

def read_cache():
    if not CACHE.exists():return {"version":VERSION,"activities":{}}
    try:
        data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if data.get("version")!=VERSION or not isinstance(data.get("activities"),dict):
            raise H5PError("Index schema mismatch")
        return data
    except (OSError,ValueError,UnicodeError):
        raise H5PError("Encrypted H5P index cannot be read") from None

def save_cache(data):
    binary=m.dpapi(json.dumps(data,ensure_ascii=False).encode("utf-8"),True)
    fd,temp=tempfile.mkstemp(prefix=".h5p_tmp_",dir=m.HERE)
    try:
        with os.fdopen(fd,"wb") as f:f.write(binary)
        os.replace(temp,CACHE)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def index():
    token,site=m.site_info(m.read_credential())
    records=available(token,site)
    cache=read_cache()
    errors=[]
    for h,meta in records:
        key=str(h["id"])
        packages=h.get("package") or []
        if not packages:errors.append("missing_package");continue
        file=packages[0]
        try:
            size=file.get("filesize")
            if not isinstance(size,int) or not 0<size<=MAX_PACKAGE:
                raise H5PError("Bad package size")
            blob=mat.download(file.get("fileurl"),token,size)
            parsed=extract(blob)
            cache["activities"][key]={"course_id":h["course"],"cmid":h["coursemodule"],
                "course":meta["course"],"title":meta["title"],**parsed,
                "indexed_at":int(time.time()),"package_size":size}
        except (mat.MaterialError,H5PError) as exc:
            errors.append(type(exc).__name__+":"+str(exc)[:60])
    save_cache(cache)
    return {"available":len(records),"indexed":len(cache["activities"]),
            "text_chars":sum(len(x.get("text","")) for x in cache["activities"].values()),
            "failures":dict(Counter(errors))}

def find(query,limit=20):
    tokens=[x.casefold() for x in query.split() if x]
    if not tokens:raise ValueError("Missing query")
    token,site=m.site_info(m.read_credential())
    visible={(h["course"],h["coursemodule"]):h["id"] for h,meta in available(token,site)}
    result=[]
    for k,item in read_cache()["activities"].items():
        if str(visible.get((item["course_id"],item["cmid"])))!=k:continue
        hay=" ".join((item.get("title",""),item.get("course",""),item.get("text",""))).casefold()
        if not all(x in hay for x in tokens):continue
        text=item.get("text","")
        positions=[text.casefold().find(t) for t in tokens]
        positions=[x for x in positions if x>=0]
        start=max(0,min(positions)-65) if positions else 0
        result.append({"course":item["course"],"cmid":item["cmid"],"title":item["title"],
                       "library":item["main_library"],"excerpt":text[start:start+380],
                       "text_chars":len(text)})
        if len(result)>=limit:break
    return result

def status():
    data=read_cache()["activities"]
    return {"indexed_activities":len(data),"chars":sum(len(x.get("text","")) for x in data.values())}

def run(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=("index","search","status"))
    p.add_argument("--query",default="")
    p.add_argument("--limit",type=int,default=20)
    a=p.parse_args(argv)
    if not 1<=a.limit<=100:p.error("invalid limit")
    res=index() if a.command=="index" else (find(a.query,a.limit) if a.command=="search" else status())
    print(json.dumps(res,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    try:sys.exit(run())
    except (H5PError,m.MoodleError,OSError,ValueError) as exc:
        print("H5P: "+str(exc)[:150],file=sys.stderr)
        sys.exit(2)
