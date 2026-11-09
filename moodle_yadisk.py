#!/usr/bin/env python3
"""Public Yandex Disk educational link index, grounded in Moodle visibility.

Tokenless calls to documented public-resource Yandex API. No user Yandex login,
no Moodle credentials sent outside Moodle. Download URLs only from trusted
Yandex disk hosts. PDF/DOCX/PPTX content extracted in memory, text encrypted
by Windows DPAPI. Does not download video or files above configured limits.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit,urlencode,parse_qsl
from urllib.request import Request, build_opener, HTTPRedirectHandler
import moodle_client as m
import moodle_hybrid as hybrid
import moodle_materials as mat

ROOT = m.HERE
CACHE = ROOT/".moodle_yadisk.dpapi"
VERSION = 1
MAX_FILE = 48*1024*1024
MAX_RESPONSE = 250000
ALLOWED_MIME = {
 "application/pdf","application/vnd.openxmlformats-officedocument.presentationml.presentation",
 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
 "application/vnd.ms-powerpoint",
}

class YDiskError(Exception):pass

class YandexOnlyRedirect(HTTPRedirectHandler):
    """Allow only HTTPS Yandex delivery redirects, never arbitrary external hosts."""
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        p=urlsplit(newurl)
        if (p.scheme!="https" or p.hostname not in
             ("downloader.disk.yandex.ru","downloader.disk.yandex.net",
              "mds.storage.yandex.net") or
            p.username or p.password or p.port not in (None,443)):
            return None
        return super().redirect_request(req,fp,code,msg,headers,newurl)

DOWNLOADER=build_opener(YandexOnlyRedirect)

def public_url(url):
    if not isinstance(url,str) or len(url)>500:
        raise YDiskError("invalid share URL")
    p=urlsplit(url)
    if (p.scheme!="https" or p.hostname!="disk.yandex.ru" or p.port not in (None,443) or
        p.username or p.password or p.query or p.fragment or
        not re.fullmatch(r"/(?:i|d)/[A-Za-z0-9_-]{6,}",p.path)):
        raise YDiskError("only supported public Yandex share URL")
    return url

def safe_filename(name):
    return str(name or "")[:200]

def request_json(endpoint, params):
    if endpoint not in ("resources","download"):
        raise YDiskError("invalid endpoint")
    path=("/v1/disk/public/resources"+("/download" if endpoint=="download" else ""))
    uri="https://cloud-api.yandex.net"+path+"?"+urlencode(params)
    try:
        with m.SAFE_OPENER.open(Request(uri,headers={"Accept":"application/json","User-Agent":"MISIS-Moodle-ReadOnly/2.0"}),timeout=25) as r:
            if r.status != 200:raise YDiskError("public API returned non-200")
            b=r.read(MAX_RESPONSE+1)
        if len(b)>MAX_RESPONSE:raise YDiskError("API response too large")
        return json.loads(b)
    except (OSError,ValueError,UnicodeError) as e:
        raise YDiskError("public API network or JSON error: "+type(e).__name__) from None

def listed_sources():
    data=hybrid.read_cache()
    token,site=m.site_info(m.read_credential())
    courses=m.get_courses(token,site["userid"])
    visible=hybrid.modules_by_course(token,courses)
    # Refresh the original page HTML, not merely Moodle module visibility.
    # A lecturer may leave the page visible but remove an old public link.
    from html.parser import HTMLParser
    class CurrentLinks(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.urls=set()
        def handle_starttag(self,tag,attrs):
            if tag not in ("a","iframe","embed"):return
            for name,value in attrs:
                if name in ("href","src") and value:
                    try:self.urls.add(public_url(value))
                    except YDiskError:pass
    ids={"courseids["+str(i)+"]":c["id"] for i,c in enumerate(courses)}
    pages=m.call(token,"mod_page_get_pages_by_courses",ids)
    live={}
    for page in pages.get("pages",[]):
        k=(page.get("course"),page.get("coursemodule"))
        if k not in visible:continue
        parser=CurrentLinks()
        parser.feed(page.get("content") or "")
        live[k]=parser.urls
    out={}
    for entry in data["items"]:
        k=(entry["course_id"],entry["cmid"])
        if k not in visible:continue
        for link in entry.get("links",[]):
            try:url=public_url(link.get("url"))
            except YDiskError:continue
            if url not in live.get(k,set()):continue
            out.setdefault(url,{"course_id":entry["course_id"],"cmid":entry["cmid"],
                 "course":entry["course"],"module":entry["title"]})
    return out

def inventory():
    sources=listed_sources()
    files={}
    unavailable=[]
    for url,meta in sources.items():
        try:
            response=request_json("resources",{"public_key":url,"limit":100})
            typ=response.get("type")
            items=(response.get("_embedded",{}).get("items",[]) if typ=="dir" else
                   [response] if typ=="file" else [])
            for f in items:
                if f.get("type")!="file":continue
                name=safe_filename(f.get("name"))
                path=f.get("path") if typ=="dir" else ""
                size=f.get("size")
                mime=f.get("mime_type")
                # In a shared folder, only API-listed direct descendants are allowed.
                if path and (not isinstance(path,str) or not path.startswith("/") or
                             ".." in path or len(path)>250):continue
                filekey=hashlib.sha256((url+"|"+str(path)+"|"+str(size)).encode()).hexdigest()
                files[filekey]={"source":url,"relative_path":path,"name":name,
                     "mime":mime,"size":size,**meta}
        except YDiskError as e:
            unavailable.append(type(e).__name__)
    return files,{"source_links":len(sources),"unavailable":len(unavailable)}

def download_public(record):
    source=public_url(record["source"])
    size=record.get("size")
    if not isinstance(size,int) or not 0<size<=MAX_FILE:
        raise YDiskError("oversized or unknown size")
    args={"public_key":source}
    if record.get("relative_path"):args["path"]=record["relative_path"]
    response=request_json("download",args)
    href=response.get("href")
    if not isinstance(href,str):raise YDiskError("no download link")
    target=urlsplit(href)
    if (target.scheme!="https" or target.hostname not in
          ("downloader.disk.yandex.ru","downloader.disk.yandex.net") or
          target.username or target.password or target.port not in (None,443)):
        raise YDiskError("download host not trusted")
    try:
        with DOWNLOADER.open(Request(href,headers={"User-Agent":"MISIS-Moodle-Indexer/2.0"}),timeout=65) as r:
            if r.status!=200:raise YDiskError("download HTTP status")
            content_len=r.headers.get("Content-Length")
            if content_len and int(content_len)>MAX_FILE:
                raise YDiskError("download too large")
            data=r.read(MAX_FILE+1)
    except (OSError,ValueError) as e:
        raise YDiskError("download network error "+type(e).__name__) from None
    if len(data)>MAX_FILE:raise YDiskError("download exceeds cap")
    if len(data)!=size:raise YDiskError("file size differs from public API")
    return data

def read_cache():
    if not CACHE.exists():return {"version":VERSION,"files":{}}
    try:
        o=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if o.get("version")!=VERSION or not isinstance(o.get("files"),dict):raise YDiskError("schema")
        return o
    except (OSError,ValueError,UnicodeError):
        raise YDiskError("local encrypted index unavailable") from None

def save_cache(data):
    blob=m.dpapi(json.dumps(data,ensure_ascii=False).encode("utf-8"),True)
    fd,temp=tempfile.mkstemp(dir=ROOT,prefix=".ydisk_tmp_")
    try:
        with os.fdopen(fd,"wb") as f:f.write(blob)
        os.replace(temp,CACHE)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def index(limit=12,budget_mb=180):
    sources,stat=inventory()
    cache=read_cache()
    budget=budget_mb*1024*1024
    processed=0; transferred=0; failures=Counter(); skipped=Counter()
    eligible=sorted((k,v) for k,v in sources.items() if k not in cache["files"] or cache["files"][k].get("state")=="failed")
    eligible.sort(key=lambda pair:pair[1].get("size") or 9999999999)
    for key,record in eligible:
        size=record.get("size")
        if record.get("mime") not in ALLOWED_MIME:
            skipped["unsupported_format"]+=1
            cache["files"][key]={**record,"state":"metadata_only","text":"",
                "indexed_at":int(time.time())}
            continue
        if not isinstance(size,int) or not 0<size<=MAX_FILE:
            skipped["too_large"]+=1
            cache["files"][key]={**record,"state":"too_large","text":"",
                "indexed_at":int(time.time())}
            continue
        if transferred+size>budget:
            skipped["budget"]+=1
            continue
        if processed>=limit:break
        try:
            binary=download_public(record)
            content=mat.extract_text(binary,record["mime"])
            status="indexed" if content else "no_text"
            entry={"state":status,"text":content,"indexed_at":int(time.time())}
        except (YDiskError,mat.MaterialError) as e:
            entry={"state":"failed","reason":str(e)[:110],"indexed_at":int(time.time())}
            failures[type(e).__name__]+=1
        cache["files"][key]={**record,**entry}
        processed+=1;transferred+=size
        if processed%3==0:save_cache(cache)
    save_cache(cache)
    states=Counter(x.get("state") for x in cache["files"].values())
    return {**stat,"available_files":len(sources),"processed":processed,
            "downloaded_mb":round(transferred/1048576,1),
            "indexed_files":len(cache["files"]),
            "states":dict(states),"skipped":dict(skipped),"failures":dict(failures),
            "remaining_documents":sum(k not in cache["files"] and
                 v.get("mime") in ALLOWED_MIME and isinstance(v.get("size"),int)
                 and v["size"]<=MAX_FILE for k,v in sources.items())}

def search(query,limit=20):
    terms=[x.casefold() for x in query.split() if x]
    if not terms:raise ValueError("Empty search")
    live=listed_sources()
    out=[]
    for item in read_cache()["files"].values():
        if item["source"] not in live:continue
        text=item.get("text","")
        corpus=(item.get("course","")+" "+item.get("module","")+" "+
                item.get("name","")+" "+text).casefold()
        if not all(t in corpus for t in terms):continue
        positions=[text.casefold().find(t) for t in terms]
        positions=[x for x in positions if x>=0]
        st=max(0,min(positions)-70) if positions else 0
        out.append({"course":item["course"],"cmid":item["cmid"],
                    "module":item["module"],"file_name":item["name"],
                    "url":item["source"],"text_excerpt":text[st:st+450],
                    "state":item.get("state")})
        if len(out)>=limit:break
    return out

def status():
    cache=read_cache()
    return {"indexed_files":len(cache["files"]),
            "by_state":dict(Counter(x.get("state") for x in cache["files"].values())),
            "total_characters":sum(len(x.get("text","")) for x in cache["files"].values())}

def run(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("action",choices=("index","search","status"))
    p.add_argument("--query",default="")
    p.add_argument("--limit",type=int,default=12)
    p.add_argument("--budget-mb",type=int,default=180)
    a=p.parse_args(argv)
    if not 1<=a.limit<=100 or not 1<=a.budget_mb<=400:p.error("Out of range")
    result=index(a.limit,a.budget_mb) if a.action=="index" else (
        search(a.query,a.limit) if a.action=="search" else status())
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    try:sys.exit(run())
    except (YDiskError,m.MoodleError,OSError) as e:
        print("YDisk: "+str(e)[:170],file=sys.stderr)
        sys.exit(2)
