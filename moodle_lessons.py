#!/usr/bin/env python3
"""Authorized Moodle Lesson inspector.

Read operations never start/finish attempts. Optional practice start is a
separate, explicit command restricted to verified practice lessons and a
matching course-module ID; never run in a broad indexing workflow.
"""
import argparse
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit
import json
import re
import sys

import moodle_client as m

READS = frozenset({
    "mod_lesson_get_lessons_by_courses",
    "mod_lesson_get_lesson",
    "mod_lesson_get_pages",
    "mod_lesson_get_page_data",
    "mod_lesson_get_lesson_access_information",
    "mod_lesson_get_user_timers",
})


class LessonError(Exception):
    pass


class ParseHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._hidden += 1
        if tag in ("iframe", "a", "embed", "object"):
            data = dict(attrs)
            url = data.get("src") or data.get("href") or data.get("data")
            if not url:
                return
            p = urlsplit(url)
            if p.scheme == "https" and p.hostname and not p.username and not p.password:
                if not any(k in ("token", "wstoken", "sesskey") for k in
                           re.findall(r"([a-zA-Z_]+)=", p.query)):
                    self.links.append({"type": tag, "host": p.hostname, "url": url})

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden and data.strip():
            self.text.append(data.strip())


def parse_html(value):
    parser = ParseHTML()
    parser.feed(value or "")
    return {
        "text": re.sub(r"\s+", " ", " ".join(parser.text))[:50000],
        "links": parser.links[:80],
    }


class Inspector:
    def __init__(self, token=None, site=None):
        if token is None:
            token, site = m.site_info(m.read_credential())
        self.token = token
        self.site = site or {}

    def call(self, method, params):
        if method not in READS:
            raise LessonError("Write endpoint cannot be called in read mode")
        return m.call(self.token, method, params)

    def lesson_list(self):
        courses = m.get_courses(self.token, self.site["userid"])
        lessons = []
        for c in courses:
            sections = m.call(self.token, "core_course_get_contents", {"courseid": c["id"]})
            allowed = {}
            for sec in sections:
                for mod in sec.get("modules", []):
                    if mod.get("modname") == "lesson" and mod.get("uservisible") is not False:
                        allowed[mod["id"]] = mod
            if not allowed:
                continue
            r = self.call("mod_lesson_get_lessons_by_courses", {"courseids[0]": c["id"]})
            for item in r.get("lessons", []):
                cmid = item.get("coursemodule")
                if cmid in allowed:
                    lessons.append({"course_id": c["id"], "course": c["name"],
                                    "cmid": cmid, "lesson_id": item["id"],
                                    "title": allowed[cmid].get("name", item.get("name",""))})
        return lessons

    def resolve(self, cmid):
        for l in self.lesson_list():
            if l["cmid"] == cmid:
                return l
        raise LessonError("Lesson not in student-visible enrolled modules")

    def inspect(self, cmid, include_pages=True, known=None):
        lesson = dict(known) if known is not None else self.resolve(cmid)
        if lesson["cmid"] != cmid:
            raise LessonError("Resolved lesson ID mismatch")
        lid = lesson["lesson_id"]
        item = self.call("mod_lesson_get_lesson", {"lessonid": lid})
        meta = item.get("lesson") or {}
        access = self.call("mod_lesson_get_lesson_access_information", {"lessonid": lid})
        # Using a read-only service method does not start/continue attempts.
        practice = bool(meta.get("practice"))
        lesson["practice"] = practice
        lesson["grade"] = meta.get("grade")
        lesson["maxattempts"] = meta.get("maxattempts")
        lesson["time_limit"] = meta.get("timelimit")
        lesson["preventaccessreasons"] = access.get("preventaccessreasons", [])
        if not include_pages:
            return lesson
        try:
            pages = self.call("mod_lesson_get_pages", {"lessonid": lid})
        except m.MoodleError as exc:
            lesson["pages"] = []
            lesson["pages_error"] = str(exc)
            return lesson
        result = []
        for row in pages.get("pages", []):
            p = row.get("page",row)
            if not isinstance(p, dict) or not isinstance(p.get("id"), int):
                continue
            entry = {"page_id": p["id"], "title": p.get("title", "")}
            try:
                data = self.call("mod_lesson_get_page_data", {"lessonid": lid, "pageid": p["id"]})
                content = (data.get("page") or {}).get("contents")
                if isinstance(content, str):
                    entry.update({"state": "readable", **parse_html(content)})
                else:
                    entry["state"] = "unknown"
            except m.MoodleError as exc:
                # "cannotfindtimer" means no active attempt; not access permission denial.
                msg = str(exc)
                entry["state"] = "requires_attempt" if "cannotfindtimer" in msg else "unavailable"
                if entry["state"] == "unavailable":
                    entry["error"] = msg[:170]
            result.append(entry)
        lesson["pages"] = result
        lesson["summary"] = dict(Counter(e["state"] for e in result))
        return lesson

    def start_practice(self, cmid, confirmed_cmid):
        # NEVER called automatically by list/search/inspect.
        if cmid != confirmed_cmid:
            raise LessonError("Explicit confirmation of exact lesson cmid is required")
        lesson = self.inspect(cmid, include_pages=False)
        if not lesson.get("practice"):
            raise LessonError("This is not a practice lesson; refusing to start attempt")
        if lesson.get("preventaccessreasons"):
            raise LessonError("Lesson access restrictions exist; refusing to start")
        if lesson.get("grade") not in (0, 0.0, None):
            raise LessonError("Lesson has a grade weight; refusing to start")
        if lesson.get("maxattempts") not in (0, None):
            raise LessonError("Attempt limit is set; refusing to start")
        # This is the single isolated, explicitly user-authorized write.
        return m.call(self.token, "mod_lesson_launch_attempt",
                      {"lessonid": lesson["lesson_id"]})


def run(args=None):
    p = argparse.ArgumentParser(description="Inspect only your accessible Moodle lessons")
    p.add_argument("action", choices=["list","inspect","start-practice"])
    p.add_argument("--cmid", type=int)
    p.add_argument("--confirm-cmid", type=int)
    p.add_argument("--no-pages", action="store_true")
    a = p.parse_args(args)
    if a.action != "list" and (not a.cmid or a.cmid <= 0):
        p.error("You must provide --cmid")
    reader = Inspector()
    if a.action == "list":
        result = reader.lesson_list()
    elif a.action == "inspect":
        result = reader.inspect(a.cmid, include_pages=not a.no_pages)
    else:
        # Explicit named consent must precede this call; never used in automated indexing.
        result = reader.start_practice(a.cmid, a.confirm_cmid)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (m.MoodleError, LessonError, OSError) as exc:
        print("Lesson: " + str(exc)[:200], file=sys.stderr)
        sys.exit(2)
