#!/usr/bin/env python3
"""Unified authenticated, read-only Moodle content index with safe fallbacks.

Combines authorized Moodle REST, active Lesson pages, and structured
descriptions for Pages, Labels, URLs, Glossaries and H5P.
Local data is DPAPI-encrypted. Does NOT start attempts, read restricted modules,
click quizzes, export passwords, bypass Moodle ACLs, or upload to third parties.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit,parse_qsl

import moodle_client as m
import moodle_lessons as lessons

CACHE = m.HERE / ".moodle_hybrid.dpapi"
VERSION = 1
MAX_TEXT = 70000
MODULE_FIELDS = {
    "page": ("mod_page_get_pages_by_courses", "pages"),
    "label": ("mod_label_get_labels_by_courses", "labels"),
    "url": ("mod_url_get_urls_by_courses", "urls"),
    "glossary": ("mod_glossary_get_glossaries_by_courses", "glossaries"),
    "h5pactivity": ("mod_h5pactivity_get_h5pactivities_by_courses", "h5pactivities"),
}


class HybridError(Exception):
    pass


def safe_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return None
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        return None
    if p.port not in (None,443):
        return None
    if any(k.lower() in ("token","wstoken","sesskey","password","key")
           for k,v in parse_qsl(p.query)):
        return None
    return url


def read_cache():
    if not CACHE.exists():
        raise HybridError("Hybrid index absent; run index first")
    try:
        data=json.loads(m.dpapi(CACHE.read_bytes(),False).decode("utf-8"))
        if data.get("version") != VERSION or not isinstance(data.get("items"), list):
            raise HybridError("Index schema mismatch")
        return data
    except (ValueError,UnicodeError,OSError) as e:
        raise HybridError("Hybrid index cannot be decrypted") from None


def save_cache(data):
    import os,tempfile
    blob=m.dpapi(json.dumps(data,ensure_ascii=False).encode("utf-8"),True)
    fd,tmp=tempfile.mkstemp(prefix=".hybrid_tmp_",dir=m.HERE)
    try:
        with os.fdopen(fd,"wb") as stream: stream.write(blob)
        os.replace(tmp,CACHE)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def modules_by_course(token,courses):
    visible={}
    for c in courses:
        sections=m.call(token,"core_course_get_contents",{"courseid":c["id"]})
        for sec in sections:
            for mod in sec.get("modules",[]):
                if not isinstance(mod,dict) or mod.get("uservisible") is False:
                    continue
                if not isinstance(mod.get("id"),int):
                    continue
                visible[(c["id"],mod["id"])]={"title":mod.get("name",""),
                    "type":mod.get("modname"),"url":safe_url(mod.get("url")),
                    "section":sec.get("name","")}
    return visible


def batch_course_ids(courses):
    return {"courseids["+str(i)+"]":c["id"] for i,c in enumerate(courses)}


def build(max_lesson_pages=150):
    token,site=m.site_info(m.read_credential())
    courses=m.get_courses(token,site["userid"])
    visible=modules_by_course(token,courses)
    names={c["id"]:c["name"] for c in courses}
    ids=batch_course_ids(courses)
    records=[]
    issues=[]
    def put(cid,cmid,typ,text="",title=None,links=None,detail=None,quality="full_text"):
        meta=visible.get((cid,cmid))
        if not meta or meta["type"]!=typ:
            return
        record={"course_id":cid,"cmid":cmid,"course":names[cid],
                "type":typ,"title":title or meta["title"],
                "text":re.sub(r"\s+"," ",text or "")[:MAX_TEXT],
                "url":meta["url"],"section":meta["section"],
                "quality":quality}
        if links:
            record["links"]=[x for x in links if safe_url(x.get("url"))][:60]
        if detail:record["detail"]=detail
        records.append(record)
    for typ,(method,field) in MODULE_FIELDS.items():
        try:
            data=m.call(token,method,ids)
        except m.MoodleError as e:
            issues.append({"method":method,"reason":str(e)[:120]})
            continue
        for obj in data.get(field,[]):
            if not isinstance(obj,dict):continue
            cid=obj.get("course")
            cmid=obj.get("coursemodule")
            if (cid,cmid) not in visible:continue
            title=obj.get("name")
            parser=lessons.parse_html(obj.get("content") if typ=="page" else obj.get("intro"))
            external=[]
            for item in parser.get("links",[]):
                if safe_url(item.get("url")):
                    external.append(item)
            if typ=="url":
                u=safe_url(obj.get("externalurl"))
                if u:external.append({"host":urlsplit(u).hostname,"type":"external","url":u})
            put(cid,cmid,typ,parser["text"],title=title,links=external,
                quality="full_text" if parser["text"] else "metadata_only")
            if typ=="glossary":
                try:
                    r=m.call(token,"mod_glossary_get_entries_by_letter",
                             {"id":obj["id"],"letter":"ALL","from":0,"limit":100})
                    for e in r.get("entries",[]):
                        p=lessons.parse_html(e.get("definition") or "")
                        put(cid,cmid,typ,
                            (e.get("concept") or "")+" "+p["text"],
                            title=e.get("concept") or title,
                            links=p["links"],detail={"glossary_entry":e.get("id")},
                            quality="full_text")
                except m.MoodleError as e:
                    issues.append({"method":"mod_glossary_get_entries_by_letter",
                                   "reason":str(e)[:120]})
    # Extract content from existing active attempts. Absolutely no start/finish.
    inspector=lessons.Inspector(token=token,site=site)
    total_attempt_needed=0
    read_lessons=0
    for course in courses:
        try:
            all_lessons=m.call(token,"mod_lesson_get_lessons_by_courses",
                               {"courseids[0]":course["id"]})
        except m.MoodleError:
            continue
        for entry in all_lessons.get("lessons",[]):
            cmid=entry.get("coursemodule")
            if visible.get((course["id"],cmid),{}).get("type")!="lesson":continue
            known={"course_id":course["id"],"course":course["name"],"cmid":cmid,
                   "lesson_id":entry["id"],"title":visible[(course["id"],cmid)]["title"]}
            try:
                item=inspector.inspect(cmid,known=known)
            except (m.MoodleError, lessons.LessonError) as exc:
                issues.append({"method":"lesson_inspect","reason":str(exc)[:120]})
                continue
            for pg in item.get("pages",[]):
                state=pg.get("state")
                if state=="requires_attempt":total_attempt_needed+=1
                if state=="readable":read_lessons+=1
                # Do not cache restricted page content; only actively readable.
                detail={"lessonid":entry["id"],"pageid":pg.get("page_id"),
                        "page_state":state}
                text=pg.get("text","") if state=="readable" else ""
                put(course["id"],cmid,"lesson",text,
                    title=(known["title"]+" / "+str(pg.get("title",""))),
                    links=pg.get("links") if state=="readable" else None,
                    detail=detail,quality="full_text" if text else "metadata_only")
            if not item.get("pages"):
                put(course["id"],cmid,"lesson",title=known["title"],quality="metadata_only")
    doc={"version":VERSION,"created_at":int(time.time()),"items":records,
         "status":{"courses":len(courses),
                   "readable_modules":len(visible),
                   "indexed_items":len(records),
                   "by_type":dict(Counter(r["type"] for r in records)),
                   "lesson_pages_read":read_lessons,
                   "lesson_pages_require_attempt":total_attempt_needed,
                   "embedded_links":sum(len(r.get("links",[])) for r in records),
                   "issues":issues[:50]}}
    save_cache(doc)
    return doc["status"]


def search(query,limit=20):
    if not query.strip():
        raise ValueError("Empty search")
    doc=read_cache()
    token,site=m.site_info(m.read_credential())
    courses=m.get_courses(token,site["userid"])
    visible=modules_by_course(token,courses)
    terms=[x.casefold() for x in query.split()]
    hits=[]
    for r in doc["items"]:
        if (r["course_id"],r["cmid"]) not in visible:
            continue
        h=visible[(r["course_id"],r["cmid"])]
        if h["type"]!=r["type"]:
            continue
        blob=" ".join([r.get("title",""),r.get("course",""),
                        r.get("section",""),r.get("text","")]).casefold()
        if not all(term in blob for term in terms):
            continue
        if r["type"]=="lesson" and r.get("text"):
            # Re-read this specific active page; cached lesson content must not
            # survive an expired attempt or newly imposed access restriction.
            d=r.get("detail",{})
            if not d.get("lessonid") or not d.get("pageid"):
                continue
            try:
                page=m.call(token,"mod_lesson_get_page_data",
                            {"lessonid":d["lessonid"],"pageid":d["pageid"]})
                html=(page.get("page") or {}).get("contents")
                if not isinstance(html,str):
                    continue
                text=lessons.parse_html(html)["text"]
            except m.MoodleError:
                continue
        else:
            text=r.get("text","")
        offsets=[text.casefold().find(t) for t in terms]
        offsets=[o for o in offsets if o>=0]
        start=max(0,min(offsets)-60) if offsets else 0
        row={"title":r["title"],"course":r["course"],"type":r["type"],
             "cmid":r["cmid"],"url":r["url"],
             "quality":r["quality"],"excerpt":text[start:start+300],
             "links":r.get("links",[])[:8]}
        hits.append(row)
        if len(hits)>=limit:break
    return {"matches":hits,"matched":len(hits),"indexed_at":doc.get("created_at"),
            "index_items":len(doc["items"]),
            "note":"Also search local PDF/DOCX/PPTX with search --with-files."}


def run(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["index","status","search"])
    parser.add_argument("--query",default="")
    parser.add_argument("--limit",type=int,default=20)
    parser.add_argument("--no-files",action="store_true",help="Skip encrypted attachments index")
    parser.add_argument("--no-slides",action="store_true",help="Skip published Google Slides index")
    parser.add_argument("--no-drive",action="store_true",help="Skip public Drive PDF previews")
    parser.add_argument("--no-h5p",action="store_true",help="Skip locally indexed H5P activities")
    parser.add_argument("--no-yadisk",action="store_true",help="Skip Yandex Disk public resources")
    parser.add_argument("--no-videos",action="store_true",help="Skip locally transcribed videos")
    parser.add_argument("--no-multimodal",action="store_true",
                        help="Exclude the optional encrypted multimodal chart/table index")
    parser.add_argument("--no-browser-cache",action="store_true",
                        help="Skip protected browser text previously indexed via explicit URL")
    args=parser.parse_args(argv)
    if not 1<=args.limit<=100:
        parser.error("--limit must be 1..100")
    if args.action=="index":res=build()
    elif args.action=="status":
        d=read_cache();res=d["status"]
    else:
        res=search(args.query,args.limit)
        if not args.no_files:
            try:
                import moodle_materials
                res["attachment_matches"]=moodle_materials.find(args.query,args.limit)
                res["attachments_indexed"]=moodle_materials.status().get("total",0)
            except (m.MoodleError, moodle_materials.MaterialError, OSError, ValueError) as exc:
                res["attachment_search_error"]=str(exc)[:160]
        if not args.no_slides:
            try:
                import moodle_slides
                res["slide_matches"]=moodle_slides.find(args.query,args.limit)
                res["slides_status"]=moodle_slides.status()
            except (m.MoodleError, OSError, ValueError) as exc:
                res["slide_search_error"]=str(exc)[:160]
        if not args.no_drive:
            try:
                import moodle_drive
                res["drive_matches"]=moodle_drive.find(args.query,args.limit)
                res["drive_status"]=moodle_drive.status()
            except (m.MoodleError, moodle_drive.DriveError, OSError, ValueError) as exc:
                res["drive_search_error"]=str(exc)[:160]
        if not args.no_h5p:
            try:
                import moodle_h5p
                res["h5p_matches"]=moodle_h5p.find(args.query,args.limit)
                res["h5p_status"]=moodle_h5p.status()
            except (m.MoodleError, moodle_h5p.H5PError, OSError, ValueError) as exc:
                res["h5p_search_error"]=str(exc)[:160]
        if not args.no_yadisk:
            try:
                import moodle_yadisk
                res["yadisk_matches"]=moodle_yadisk.search(args.query,args.limit)
                res["yadisk_status"]=moodle_yadisk.status()
            except (m.MoodleError,moodle_yadisk.YDiskError,OSError,ValueError) as exc:
                res["yadisk_search_error"]=str(exc)[:160]
        if not args.no_videos:
            try:
                import moodle_videos
                res["video_matches"]=moodle_videos.search(args.query,args.limit)
                res["video_status"]=moodle_videos.status()
            except (m.MoodleError,moodle_videos.VideoError,OSError,ValueError) as exc:
                res["video_search_error"]=str(exc)[:160]
        if not args.no_browser_cache:
            try:
                import moodle_browser_index
                # Cache-only search; never launch Chrome automatically, never
                # start Lesson/Quiz attempts. Refresh Moodle module visibility.
                if moodle_browser_index.status()["cached_pages"]:
                    res["browser_matches"]=moodle_browser_index.search(args.query,args.limit)
                else:
                    res["browser_matches"]=[]
                res["browser_cache_status"]=moodle_browser_index.status()
            except (m.MoodleError,OSError,ValueError) as exc:
                res["browser_cache_error"]=str(exc)[:160]
        if not args.no_multimodal:
            try:
                import moodle_multimodal_index as mi
                state=mi.status()
                res["multimodal_status"]=state
                res["multimodal_matches"]=mi.search(args.query,args.limit) if state["files_indexed"] else []
            except (m.MoodleError,mi.IndexErrorSafe,OSError,ValueError) as exc:
                res["multimodal_error"]=str(exc)[:160]
        if not args.no_multimodal:
            try:
                import moodle_video_frames_index as fi
                video_state=fi.status()
                res["video_frames_status"]=video_state
                res["video_frame_matches"]=fi.search(args.query,args.limit) if video_state["videos"] else []
            except (m.MoodleError,OSError,ValueError) as exc:
                res["video_frames_error"]=str(exc)[:160]
    print(json.dumps(res,ensure_ascii=True,indent=2))
    return 0


if __name__=="__main__":
    try:sys.exit(run())
    except (HybridError,m.MoodleError,lessons.LessonError,OSError) as exc:
        print("Hybrid: "+str(exc)[:180],file=sys.stderr)
        sys.exit(2)
