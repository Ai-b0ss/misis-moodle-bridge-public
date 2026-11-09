#!/usr/bin/env python3
"""Fresh permissions and source-link verification for externally embedded Moodle media.

Only calls Moodle student read endpoints. A cached Google Slide/Drive link is
returned in search only while the original page/lesson still exposes it under
the current user's account. No browser login, attempts, or instructor bypass.
"""
from html.parser import HTMLParser
import moodle_client as m
import moodle_hybrid as hybrid

class LinkReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls=set()
    def handle_starttag(self,tag,attrs):
        if tag not in ("a","iframe","embed","object","source"):
            return
        data=dict(attrs)
        for key in ("href","src","data"):
            value=data.get(key)
            if isinstance(value,str) and value.startswith("https://"):
                self.urls.add(value)

def parse_urls(html):
    parser=LinkReader()
    parser.feed(html or "")
    return parser.urls

def current_links(token=None,site=None):
    if token is None:
        token,site=m.site_info(m.read_credential())
    courses=m.get_courses(token,site["userid"])
    visible=hybrid.modules_by_course(token,courses)
    params={"courseids["+str(i)+"]":c["id"] for i,c in enumerate(courses)}
    page_data=m.call(token,"mod_page_get_pages_by_courses",params)
    found={}
    for page in page_data.get("pages",[]):
        identity=(page.get("course"),page.get("coursemodule"))
        if identity not in visible or visible[identity].get("type")!="page":
            continue
        found[identity]=parse_urls(page.get("content"))
    # Only already-readable Lesson pages should be considered, never start attempts.
    doc=hybrid.read_cache()
    for entry in doc.get("items",[]):
        if entry.get("type")!="lesson":
            continue
        identity=(entry.get("course_id"),entry.get("cmid"))
        if identity not in visible or visible[identity].get("type")!="lesson":
            continue
        detail=entry.get("detail") or {}
        lid,pid=detail.get("lessonid"),detail.get("pageid")
        if not isinstance(lid,int) or not isinstance(pid,int):
            continue
        try:
            response=m.call(token,"mod_lesson_get_page_data",{"lessonid":lid,"pageid":pid})
            html=(response.get("page") or {}).get("contents")
            if isinstance(html,str):
                found.setdefault(identity,set()).update(parse_urls(html))
        except m.MoodleError:
            continue
    return found
