#!/usr/bin/env python3
"""Restricted, user-authorized Moodle web text enrichment.

Only indexes individually requested student-visible passive course modules.
Never automatically starts Lesson/Quiz, follows arbitrary links, or exports
browser cookies. Data stays encrypted under Windows CurrentUser DPAPI.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import moodle_browser as browser
import moodle_client as m
import moodle_hybrid as hybrid

CACHE = m.HERE / ".moodle_browser_content.dpapi"
SUPPORTED = {
    "/mod/page/view.php": "page",
    "/mod/book/view.php": "book",
    "/mod/glossary/view.php": "glossary",
    "/mod/folder/view.php": "folder",
}
MAX_ITEMS = 100
MAX_TEXT = 50000


class EnrichmentError(Exception):
    pass


def _key(url):
    browser.validate_url(url)
    u = urlsplit(url)
    if u.path not in SUPPORTED:
        raise EnrichmentError("Only passive Moodle study resources are supported")
    query = parse_qs(u.query)
    if set(query) != {"id"} or len(query["id"]) != 1:
        raise EnrichmentError("Only a single course module id is accepted")
    raw = query["id"][0]
    if not raw.isdecimal() or not 0 < int(raw) < 2147483647:
        raise EnrichmentError("Invalid course module id")
    return (u.path, int(raw))


def _modules():
    token, site = m.site_info(m.read_credential())
    courses = m.get_courses(token, site["userid"])
    return hybrid.modules_by_course(token, courses), courses


def verified(url, modules):
    path, cmid = _key(url)
    # Check exact course-module ID, path and module type, fresh from Moodle.
    for (course_id, current_cmid), meta in modules.items():
        if cmid != current_cmid or SUPPORTED[path] != meta.get("type"):
            continue
        source = meta.get("url")
        if not source:
            continue
        try:
            if _key(source) == (path,cmid) and urlsplit(source).hostname == "newlms.misis.ru":
                return {"cmid":cmid, "course_id":course_id, "type":meta["type"],
                        "title":meta["title"],"url":source}
        except (browser.BrowserError, EnrichmentError):
            continue
    raise EnrichmentError("URL is not a currently visible student study resource")


def read_cache():
    if not CACHE.exists():
        return {"version":1,"items":[]}
    try:
        data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("items"), list):
            raise ValueError("Cache format")
        return data
    except (ValueError, UnicodeError, OSError):
        raise EnrichmentError("Encrypted browser content cache unreadable") from None


def save_cache(doc):
    encoded=m.dpapi(json.dumps(doc,ensure_ascii=False).encode("utf-8"),True)
    fd,tmp=tempfile.mkstemp(dir=m.HERE,prefix=".browser_content_tmp_")
    try:
        with os.fdopen(fd,"wb") as f: f.write(encoded)
        os.replace(tmp,CACHE)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def index(url):
    modules, courses=_modules()
    info=verified(url,modules)
    page=browser.inspect(info["url"])
    if page.get("state")!="readable":
        return {"state":page.get("state","unavailable"),
                "type":info["type"],"cmid":info["cmid"],"cached":False}
    body=page.get("text","")
    if not isinstance(body,str) or not body.strip():
        return {"state":"empty","cached":False,"cmid":info["cmid"]}
    names={c["id"]:c["name"] for c in courses}
    row={**info,"course":names.get(info["course_id"],""),
         "text":body[:MAX_TEXT],
         "retrieved_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}
    doc=read_cache()
    doc["items"]=[x for x in doc["items"] if
                 (x.get("course_id"),x.get("cmid"))!=(info["course_id"],info["cmid"])]
    doc["items"].append(row)
    doc["items"]=doc["items"][-MAX_ITEMS:]
    save_cache(doc)
    return {"state":"indexed","type":info["type"],"cmid":info["cmid"],
            "characters":len(row["text"]),"cached":True}


def search(query,limit=20):
    if not query.strip() or not 1<=limit<=100:
        raise EnrichmentError("Query and limit are required")
    doc=read_cache()
    if not doc["items"]:
        return []
    modules,_=_modules()
    terms=query.casefold().split()
    results=[]
    for row in doc["items"]:
        try:
            info=verified(row["url"],modules)
        except (EnrichmentError,browser.BrowserError):
            continue
        if (info["course_id"],info["cmid"]) != (row["course_id"],row["cmid"]):
            continue
        text=row.get("text","")
        corpus=" ".join([text,info["title"],row.get("course","")]).casefold()
        if not all(term in corpus for term in terms):
            continue
        indexes=[text.casefold().find(t) for t in terms]
        start=max(0,min((i for i in indexes if i>=0),default=0)-55)
        results.append({"type":info["type"],"title":info["title"],
                        "course":row.get("course",""),"url":info["url"],
                        "cmid":info["cmid"],"excerpt":text[start:start+300],
                        "retrieved_at":row.get("retrieved_at")})
        if len(results)>=limit: break
    return results


def status():
    return {"encrypted_cache_exists":CACHE.exists(),
            "cached_pages":len(read_cache()["items"])}


def run(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["index","status","search"])
    parser.add_argument("--url",default="")
    parser.add_argument("--query",default="")
    parser.add_argument("--limit",type=int,default=20)
    a=parser.parse_args(argv)
    if a.action=="index":
        if not a.url:
            parser.error("index requires --url")
        result=index(a.url)
    elif a.action=="status": result=status()
    else: result=search(a.query,a.limit)
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0


if __name__=="__main__":
    try:sys.exit(run())
    except (EnrichmentError,browser.BrowserError,m.MoodleError,OSError) as exc:
        print("Browser enrichment: "+str(exc)[:180],file=sys.stderr)
        sys.exit(2)
