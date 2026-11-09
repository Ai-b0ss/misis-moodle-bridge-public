#!/usr/bin/env python3
"""Index public Google Slides embeds linked by student-visible Moodle pages.

Only publicly viewable presentation /pubembed and /embed links; no OAuth, Moodle tokens, cookies,
Google Drive account access, or file uploads. Reads the viewer's accessibility
text for each slide. Stores an encrypted DPAPI text index, no screenshots.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
import moodle_client as m
import moodle_hybrid as hybrid

CACHE = m.HERE / ".moodle_slides.dpapi"
MAX_SLIDES = 160
SLIDE_RE = re.compile(r"(?:Слайд|Slide)\s+(\d+)\s+(?:из|of)\s+(\d+)", re.I)


class SlidesError(Exception):
    pass


def valid_published_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise SlidesError("Invalid URL")
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname != "docs.google.com" or
        p.username or p.password or p.port not in (None,443) or p.fragment):
        raise SlidesError("Only published docs.google.com links supported")
    if not re.fullmatch(r"/presentation/d/(?:e/)?[a-zA-Z0-9_-]+/(?:pubembed|embed)", p.path):
        raise SlidesError("Only published Slides presentation embeds supported")
    if any(x in p.query.lower() for x in ("token=", "key=", "auth=", "sesskey=")):
        raise SlidesError("Credentials in presentation URL")
    return url


def public_sources():
    snap = hybrid.read_cache()
    token, site = m.site_info(m.read_credential())
    courses = m.get_courses(token, site["userid"])
    visible = hybrid.modules_by_course(token, courses)
    import moodle_embed_visibility
    live=moodle_embed_visibility.current_links(token,site)
    unique = {}
    for item in snap["items"]:
        if (item["course_id"], item["cmid"]) not in visible:
            continue
        for link in item.get("links", []):
            u = link.get("url")
            try: valid_published_url(u)
            except SlidesError: continue
            if u not in live.get((item["course_id"],item["cmid"]),set()):
                continue
            unique.setdefault(u, {"url":u,"course":item["course"],
                                  "course_id":item["course_id"],"cmid":item["cmid"],
                                  "module_title":item["title"]})
    return unique


def inspect_deck(page, url, slide_cap=MAX_SLIDES):
    """Capture public accessibility text; seek both ends when navigation stalls."""
    valid_published_url(url)
    page.goto(url,wait_until="domcontentloaded",timeout=30000)
    page.locator(".punch-viewer-svgpage-svgcontainer").wait_for(timeout=23000)
    title=page.title()[:180]
    seen={}
    total=0

    def capture():
        nonlocal total
        labels=page.locator('[role="img"][aria-label]').evaluate_all(
            "nodes => nodes.map(e => e.getAttribute('aria-label')).filter(Boolean)")
        if not labels:return None
        found=SLIDE_RE.search(labels[0])
        if not found:return None
        n,count=map(int,found.groups())
        if n<1 or count<1 or count>1000 or n>count:return None
        total=max(total,count)
        words=[];distinct=set()
        for line in labels[1:]:
            value=re.sub(r"\\s+"," ",line).strip()
            if value and value not in distinct and not value.startswith("https://"):
                distinct.add(value);words.append(value)
        record={"number":n,"text":"\\n".join(words)[:10000]}
        if n not in seen or len(record["text"])>len(seen[n]["text"]):
            seen[n]=record
        return n

    def move(direction):
        before=capture()
        if before is None:return None
        if direction=="forward":
            keys=["ArrowRight","Space","PageDown"]
            arrow_label='button[title^="Далее"],button[title^="Next"]'
        else:
            keys=["ArrowLeft","PageUp","Shift+Space"]
            arrow_label='button[title^="Назад"],button[title^="Previous"]'
        # Alternate controls cope with embedded interactive elements that
        # consume ArrowRight or ArrowLeft without changing the slide.
        for key in keys:
            page.keyboard.press(key)
            try:
                page.wait_for_function(
                    "([before]) => {const x=document.querySelector('[role=img][aria-label]');"
                    "const m=x && x.getAttribute('aria-label').match(/(Слайд|Slide)\\s+(\\d+)/i);"
                    "return m && Number(m[2]) !== before;}",
                    arg=[before],timeout=1300)
            except Exception:pass
            after=capture()
            if after is not None and after!=before:
                return after
        if page.locator(arrow_label).count():
            try:
                page.locator(arrow_label).first.click(timeout=1300)
                page.wait_for_timeout(400)
            except Exception:pass
        return capture()

    first=capture()
    if first is None:raise SlidesError("Slide counter unavailable")
    # Forward scan from slide 1.
    current=first
    for _ in range(min(slide_cap,MAX_SLIDES)):
        if current>=total or len(seen)>=total:break
        next_num=move("forward")
        if next_num is None or next_num==current:break
        current=next_num
    # Many viewers allow End even when ordinary right navigation is stuck.
    if len(seen)<total:
        page.keyboard.press("End")
        page.wait_for_timeout(750)
        last=capture()
        if last is not None:
            current=last
            for _ in range(min(slide_cap,MAX_SLIDES)):
                if current<=1 or len(seen)>=total:break
                previous_num=move("backward")
                if previous_num is None or previous_num==current:break
                current=previous_num
    # Published Google Slides presentation mode officially supports
    # "slide number + Enter". Seek holes directly: interactive animations,
    # embedded media and focus often block Left/Right navigation.
    if len(seen)<total and total<=160:
        for requested in [n for n in range(1,total+1) if n not in seen][:slide_cap]:
            for attempt in range(2):
                try:
                    page.keyboard.type(str(requested),delay=18)
                    page.keyboard.press("Enter")
                    page.wait_for_function(
                        "(wanted) => {const e=document.querySelector('[role=img][aria-label]');"
                        "const match=e && e.getAttribute('aria-label').match(/(?:Слайд|Slide)\\s+(\\d+)/i);"
                        "return match && Number(match[1]) === wanted;}",
                        arg=requested,timeout=1850)
                except Exception:
                    # A viewer exception must not erase text from the remaining
                    # correctly navigated slides.
                    try:
                        if capture()==requested:break
                    except Exception:pass
                    continue
                try:
                    if capture()==requested:break
                except Exception:
                    break
    # Keep a sparse or incomplete scan honest rather than inventing pages.
    return {"title":title,"slides_total":total,"slides_read":len(seen),
            "complete":len(seen)==total and all(i in seen for i in range(1,total+1)),
            "slides":[seen[k] for k in sorted(seen)]}

def read_cache():
    if not CACHE.exists():return {"version":1,"decks":{}}
    try:
        data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if data.get("version")!=1 or not isinstance(data.get("decks"),dict):
            raise SlidesError("Index schema mismatch")
        return data
    except (ValueError,UnicodeError,OSError):
        raise SlidesError("Encrypted Slides index unreadable") from None


def save_cache(obj):
    import os,tempfile
    blob=m.dpapi(json.dumps(obj,ensure_ascii=False).encode("utf-8"),True)
    fd,tmp=tempfile.mkstemp(prefix=".slides_tmp_",dir=m.HERE)
    try:
        with os.fdopen(fd,"wb") as f:f.write(blob)
        os.replace(tmp,CACHE)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def index(limit=6, slide_cap=160):
    sources=public_sources()
    cache=read_cache()
    # Cover unseen lectures first; only then retry a partially extracted deck.
    pending=[(url,meta) for url,meta in sources.items() if url not in cache["decks"]]
    pending += [(url,meta) for url,meta in sources.items()
                if url in cache["decks"] and not cache["decks"][url].get("complete")
                and cache["decks"][url].get("error") not in ("TimeoutError",)]
    count=0; errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel="chrome",headless=True,
                                   args=["--disable-extensions"])
        try:
            page=browser.new_page(viewport={"width":1250,"height":850})
            for url,meta in pending[:limit]:
                try:
                    result=inspect_deck(page,url,slide_cap)
                    # Store only authorized source metadata plus public presentation text.
                    old=cache["decks"].get(url,{})
                    # Merge separately visited slides by number. A stalled forward
                    # scan can be completed by reading backwards from End.
                    numbers={z["number"]:z for z in old.get("slides",[]) if isinstance(z.get("number"),int)}
                    for z in result.get("slides",[]):
                        if len(z.get("text",""))>=len(numbers.get(z["number"],{}).get("text","")):
                            numbers[z["number"]]=z
                    total=result.get("slides_total") or old.get("slides_total",0)
                    merged={**meta,**result,"slides_total":total,
                            "slides":[numbers[k] for k in sorted(numbers)],
                            "slides_read":len(numbers),
                            "complete":len(numbers)==total and
                                all(i in numbers for i in range(1,total+1)),
                            "indexed_at":int(time.time())}
                    cache["decks"][url]=merged
                except Exception as exc:
                    # Record unsupported viewer once; explicit future retry can be added.
                    # Never output presentation ID or sensitive link in error reports.
                    kind=type(exc).__name__
                    errors.append(kind)
                    old=cache["decks"].get(url,{})
                    if old.get("slides_read",0)>0:
                        # Never replace a partial index with an empty one.
                        old.pop("error",None)
                        old["last_error"]=kind
                        old["last_checked_at"]=int(time.time())
                        cache["decks"][url]=old
                    else:
                        cache["decks"][url]={**meta,"slides":[],"slides_total":0,
                            "slides_read":0,"complete":False,"error":kind,
                            "indexed_at":int(time.time())}
                count+=1
                save_cache(cache)
        finally:browser.close()
    return {"source_decks":len(sources),"processed":count,
            "indexed_decks":len(cache["decks"]),
            "complete":sum(x.get("complete",False) for x in cache["decks"].values()),
            "slides_read":sum(x.get("slides_read",0) for x in cache["decks"].values()),
            "errors":dict(Counter(errors)),
            "remaining":sum(u not in cache["decks"] or not cache["decks"][u].get("complete")
                            for u in sources)}


def find(query,limit=20):
    terms=[x.casefold() for x in query.split() if x]
    if not terms:raise ValueError("Empty slide search")
    cache=read_cache()
    visible=public_sources()
    matches=[]
    for url,record in cache["decks"].items():
        if url not in visible:continue
        for slide in record.get("slides",[]):
            corpus=" ".join((record.get("module_title",""),record.get("course",""),
                             slide.get("text",""))).casefold()
            if not all(t in corpus for t in terms):continue
            text=slide.get("text","")
            places=[text.casefold().find(t) for t in terms]
            places=[x for x in places if x>=0]
            start=max(0,min(places)-60) if places else 0
            matches.append({"course":record["course"],
                            "module":record["module_title"],"cmid":record["cmid"],
                            "slide_number":slide.get("number"),
                            "slides_total":record.get("slides_total"),
                            "excerpt":text[start:start+370],
                            "url":url,"complete":record.get("complete",False)})
            if len(matches)>=limit:return matches
    return matches


def status():
    cache=read_cache()
    return {"indexed_decks":len(cache["decks"]),
            "complete":sum(x.get("complete",False) for x in cache["decks"].values()),
            "slides_read":sum(x.get("slides_read",0) for x in cache["decks"].values())}


def run(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=("index","find","status"))
    p.add_argument("--query",default="")
    p.add_argument("--limit",type=int,default=6)
    p.add_argument("--slide-cap",type=int,default=160)
    a=p.parse_args(argv)
    if not 1<=a.limit<=200 or not 1<=a.slide_cap<=160:
        p.error("Out of bounds")
    result=index(a.limit,a.slide_cap) if a.command=="index" else (
        find(a.query,a.limit) if a.command=="find" else status())
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    try:sys.exit(run())
    except (SlidesError,m.MoodleError,OSError,ValueError) as exc:
        print("Slides: "+str(exc)[:160],file=sys.stderr)
        sys.exit(2)
