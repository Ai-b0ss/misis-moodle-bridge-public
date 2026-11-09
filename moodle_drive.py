#!/usr/bin/env python3
"""Read text in publicly viewable Google Drive PDF preview pages.

Access only previews linked from student-visible Moodle modules. No Google
login, Moodle API token to Google, direct-download bypass, or file saving.
The browser's ordinary preview scroll loads pages; their displayed text is
stored in a Windows DPAPI-encrypted local index.
"""
import argparse
from collections import Counter
import json
import re
import sys
import time
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
import moodle_client as m
import moodle_hybrid as h

CACHE=m.HERE/".moodle_drive.dpapi"
URL_RE=re.compile(r"/file/d/[A-Za-z0-9_-]+/preview")
PAGES_RE=re.compile(r"(?im)^(?:Страница\s+(\d+)\s+из\s+(\d+)|Page\s+(\d+)\s+of\s+(\d+))\s*$")
MAX_PAGES=350


class DriveError(Exception):pass


def valid_preview(url):
    if not isinstance(url,str) or len(url)>2048:
        raise DriveError("Invalid Drive preview link")
    p=urlsplit(url)
    if (p.scheme!="https" or p.hostname!="drive.google.com" or
        p.port not in (None,443) or p.username or p.password or p.fragment or p.query or
        not URL_RE.fullmatch(p.path)):
        raise DriveError("Only public Drive file previews supported")
    return url


def sources():
    cache=h.read_cache()
    token,site=m.site_info(m.read_credential())
    courses=m.get_courses(token,site["userid"])
    visible=h.modules_by_course(token,courses)
    import moodle_embed_visibility
    live=moodle_embed_visibility.current_links(token,site)
    out={}
    for item in cache["items"]:
        if (item["course_id"],item["cmid"]) not in visible:continue
        for link in item.get("links",[]):
            try:u=valid_preview(link.get("url"))
            except DriveError:continue
            if u not in live.get((item["course_id"],item["cmid"]),set()):
                continue
            out.setdefault(u,{"course":item["course"],"course_id":item["course_id"],
                              "cmid":item["cmid"],"module_title":item["title"]})
    return out


def parse_pages(body):
    matches=list(PAGES_RE.finditer(body))
    out={}
    for i,mch in enumerate(matches):
        n,total=(int(mch.group(1)),int(mch.group(2))) if mch.group(1) else (
            int(mch.group(3)),int(mch.group(4)))
        if n<1 or total<1 or total>MAX_PAGES or n>total:continue
        end=matches[i+1].start() if i+1<len(matches) else len(body)
        text=re.sub(r"\s+"," ",body[mch.end():end]).strip()
        if len(text)>len(out.get(n,{}).get("text","")):
            out[n]={"number":n,"text":text[:10000],"total":total}
    return out


def inspect(page,url,max_steps=400):
    valid_preview(url)
    response=page.goto(url,wait_until="domcontentloaded",timeout=25000)
    if response and response.status>=400:
        raise DriveError("Preview returned HTTP error")
    selector="div.ndfHFb-c4YZDc-cYSp0e-s2gQvd"
    page.locator(selector).wait_for(timeout=18000)
    page.wait_for_timeout(550)
    texts={};total=0
    last_top=-1;idle=0
    for i in range(max_steps):
        state=page.evaluate("""(selector) => {
            const scroll=document.querySelector(selector);
            return scroll ? {top:scroll.scrollTop, height:scroll.scrollHeight,
             client:scroll.clientHeight, body:document.body.innerText} : null;
        }""",selector)
        if not state:break
        new=parse_pages(state["body"])
        for n,obj in new.items():
            total=max(total,obj["total"])
            if len(obj["text"])>len(texts.get(n,{}).get("text","")):
                texts[n]=obj
        if total and len(texts)>=total:break
        if state["top"]>=state["height"]-state["client"]-20:
            idle+=1
            if idle>=4:break
        else:idle=0
        inc=max(280,int(state["client"]*0.72))
        page.evaluate("""([selector,inc]) => {
            const scroll=document.querySelector(selector);
            if(scroll)scroll.scrollTop=Math.min(scroll.scrollTop+inc,scroll.scrollHeight);
        }""",[selector,inc])
        page.wait_for_timeout(110)
    if not total:
        raise DriveError("No accessible PDF page labels found")
    # Recover pages skipped by the viewer lazy loader when the scroll step
    # crossed a short page. These seek operations only change the local viewer.
    for missing in [x for x in range(1,total+1) if x not in texts][:35]:
        if missing in texts:continue
        for delta in (-550,0,550):
            body=page.evaluate("""([selector,n,total,delta]) => {
                const sc=document.querySelector(selector);
                if (!sc)return "";
                const base=(n-1)/total*(sc.scrollHeight-sc.clientHeight);
                sc.scrollTop=Math.max(0,Math.min(sc.scrollHeight,base+delta));
                return document.body.innerText;
            }""",[selector,missing,total,delta])
            page.wait_for_timeout(220)
            body=page.evaluate("() => document.body.innerText")
            for n,obj in parse_pages(body).items():
                if len(obj["text"])>len(texts.get(n,{}).get("text","")):
                    texts[n]=obj
            if missing in texts:break
    return {"pages_total":total,"pages_found":len(texts),
            "pages_with_text":sum(bool(x["text"]) for x in texts.values()),
            "complete":len(texts)>=total,
            "pages":[texts[n] for n in sorted(texts)]}


def read_cache():
    if not CACHE.exists():return {"version":1,"files":{}}
    try:
        out=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if out.get("version")!=1 or not isinstance(out.get("files"),dict):
            raise DriveError("Index schema mismatch")
        return out
    except (OSError,UnicodeError,ValueError):
        raise DriveError("Could not read encrypted Drive index") from None


def save_cache(data):
    import os,tempfile
    blob=m.dpapi(json.dumps(data,ensure_ascii=False).encode("utf-8"),True)
    fd,path=tempfile.mkstemp(dir=m.HERE,prefix=".drive_tmp_")
    try:
        with os.fdopen(fd,"wb") as f:f.write(blob)
        os.replace(path,CACHE)
    finally:
        if os.path.exists(path):os.unlink(path)


def index(limit=2):
    live=sources();cache=read_cache();done=0;errors=[]
    pending=[(u,v) for u,v in live.items() if u not in cache["files"] or
             not cache["files"][u].get("complete")]
    with sync_playwright() as pw:
        b=pw.chromium.launch(channel="chrome",headless=True,
                             args=["--disable-extensions"])
        try:
            page=b.new_page(viewport={"width":1100,"height":850})
            for u,meta in pending[:limit]:
                try:
                    content=inspect(page,u)
                    old=cache["files"].get(u,{})
                    if old.get("pages_found",0)<=content["pages_found"] or content["complete"]:
                        cache["files"][u]={**meta,**content,"indexed_at":int(time.time())}
                except Exception as exc:
                    errors.append(type(exc).__name__)
                done+=1
                save_cache(cache)
        finally:b.close()
    return {"sources":len(live),"processed":done,
            "indexed_files":len(cache["files"]),
            "complete_files":sum(bool(x.get("complete")) for x in cache["files"].values()),
            "pages_read":sum(x.get("pages_found",0) for x in cache["files"].values()),
            "pages_with_text":sum(x.get("pages_with_text",0) for x in cache["files"].values()),
            "errors":dict(Counter(errors)),
            "remaining":sum(u not in cache["files"] or not cache["files"][u].get("complete") for u in live)}


def find(query,limit=20):
    terms=[x.casefold() for x in query.split() if x]
    if not terms:raise ValueError("Empty search")
    live=sources()
    results=[]
    for url,f in read_cache()["files"].items():
        if url not in live:continue
        for page in f.get("pages",[]):
            text=page.get("text","")
            corpus=" ".join((f.get("course",""),f.get("module_title",""),text)).casefold()
            if not all(t in corpus for t in terms):continue
            pos=[text.casefold().find(t) for t in terms]
            pos=[p for p in pos if p>=0]
            start=max(0,min(pos)-60) if pos else 0
            results.append({"course":f["course"],"module":f["module_title"],
               "cmid":f["cmid"],"page_number":page["number"],
               "pages_total":f["pages_total"],"url":url,
               "excerpt":text[start:start+340],"complete":f.get("complete",False)})
            if len(results)>=limit:return results
    return results


def status():
    files=read_cache()["files"]
    return {"files_indexed":len(files),
            "complete":sum(bool(x.get("complete")) for x in files.values()),
            "pages_read":sum(x.get("pages_found",0) for x in files.values()),
            "pages_with_text":sum(x.get("pages_with_text",0) for x in files.values())}


def run(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("index","status","find"))
    parser.add_argument("--limit",type=int,default=2)
    parser.add_argument("--query",default="")
    args=parser.parse_args(argv)
    if not 1<=args.limit<=100:parser.error("invalid limit")
    result=index(args.limit) if args.command=="index" else (
        status() if args.command=="status" else find(args.query,args.limit))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    try:sys.exit(run())
    except (DriveError,m.MoodleError,OSError) as exc:
        print("Drive: "+str(exc)[:150],file=sys.stderr)
        sys.exit(2)
