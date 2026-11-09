#!/usr/bin/env python3
"""Read-only MISIS Moodle API facade. Uses the existing Windows DPAPI session.

No enrolments, submissions, quiz starts, view-tracking, or other writes.
Standard library only; deliberately does not log tokens or full site_info.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import re
import sys
import time

import moodle_client as m

READ_METHODS = frozenset({
    "core_course_get_contents", "core_completion_get_activities_completion_status",
    "core_calendar_get_calendar_upcoming_view",
    "mod_quiz_get_quizzes_by_courses", "mod_assign_get_submission_status",
    "mod_page_get_pages_by_courses", "mod_lesson_get_lessons_by_courses",
    "gradereport_user_get_grade_items", "mod_forum_get_forums_by_courses",
    "mod_resource_get_resources_by_courses", "mod_folder_get_folders_by_courses",
    "mod_book_get_books_by_courses", "core_course_get_course_module",
    "mod_lesson_get_pages", "mod_lesson_get_lesson_access_information",
    "mod_lesson_get_lesson", "mod_lesson_get_user_timers",
})
MAX_WORKERS = 4
MAX_SEARCH_TEXT = 8000


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(html):
    parser = TextOnly()
    parser.feed(str(html or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def clock(timestamp, offset=3):
    if not isinstance(timestamp, (float, int)) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=offset))).isoformat(timespec="minutes")


def batch_course_ids(courses):
    return {f"courseids[{i}]": c["id"] for i, c in enumerate(courses)}


def state_of(response):
    if not isinstance(response, dict):
        return {"status": "unknown", "graded": False}
    attempt = response.get("lastattempt") or {}
    submission = (attempt.get("submission") or {}) if isinstance(attempt, dict) else {}
    feedback = response.get("feedback") or {}
    status = submission.get("status", "none") if isinstance(submission, dict) else "unknown"
    grade = feedback.get("grade") if isinstance(feedback, dict) else None
    grading_status = attempt.get("gradingstatus") if isinstance(attempt, dict) else None
    graded = grading_status == "graded" or (
        grading_status not in ("graded", "notgraded") and grade is not None
    )
    return {
        "status": str(status or "none"),
        "graded": graded,
        "editable": attempt.get("canedit") if isinstance(attempt, dict) else None,
    }


class MoodleReader:
    def __init__(self, token=None, site=None, retries=1):
        if token is None:
            token, site = m.site_info(m.read_credential())
        self._token = token
        self._site = site or {}
        self.retries = max(0, min(int(retries), 2))
        self.issues = []

    def _issue(self, method, message, course_id=None):
        self.issues.append({"method": method, "course_id": course_id,
                            "reason": str(message)[:150]})

    def read(self, method, params=None, course_id=None):
        if method not in READ_METHODS:
            raise ValueError(f"Write or unverified Moodle method prohibited: {method}")
        for attempt in range(self.retries + 1):
            try:
                result = m.call(self._token, method, params or {})
                if isinstance(result, dict):
                    for w in result.get("warnings", []):
                        if isinstance(w, dict):
                            self._issue(method, w.get("message", "warning"), course_id)
                return result
            except m.MoodleError as exc:
                # Retry only transient network errors, never privilege or parameter errors.
                message = str(exc)
                transient = "Не удалось связаться" in message or any(
                    f"HTTP {status}" in message for status in (429, 500, 502, 503, 504)
                )
                if transient and attempt < self.retries:
                    time.sleep(1.0 if "HTTP 429" in message else 0.2 * (attempt + 1))
                    continue
                self._issue(method, exc, course_id)
                return None

    def collect(self, statuses=True, pages=False, grades=False):
        courses = m.get_courses(self._token, self._site["userid"])
        assignments, assignment_warnings = m.get_assignments(self._token, courses)
        for warning in assignment_warnings:
            self._issue("mod_assign_get_assignments", warning)
        by_cmid = {}
        records = []
        for course in courses:
            cid = course["id"]
            sections = self.read("core_course_get_contents", {"courseid": cid}, cid)
            if not isinstance(sections, list):
                continue
            for section in sections:
                for mod in section.get("modules", []):
                    if not isinstance(mod, dict):
                        continue
                    cmid = mod.get("id")
                    if not isinstance(cmid, int):
                        continue
                    item = {
                        "course_id": cid, "course": course["name"], "cmid": cmid,
                        "instance": mod.get("instance"), "type": mod.get("modname", "unknown"),
                        "title": mod.get("name", ""), "url": mod.get("url"),
                        "section": section.get("name", ""), "visible": mod.get("visible"),
                        "completion": None,
                    }
                    # Moodle may expose due dates directly in module metadata
                    # even if the Assign API omits an inaccessible assignment.
                    for date in mod.get("dates") or []:
                        if isinstance(date, dict) and date.get("dataid") in ("duedate", "timeclose"):
                            if isinstance(date.get("timestamp"), (int, float)):
                                item.setdefault("due_at", date["timestamp"])
                    if isinstance(mod.get("contents"), list):
                        item["attachments"] = [
                            {"filename": f.get("filename"), "mimetype": f.get("mimetype"),
                             "fileurl": f.get("fileurl")}
                            for f in mod["contents"] if isinstance(f, dict)
                        ]
                    records.append(item)
                    by_cmid[(cid, cmid)] = item
            completion = self.read(
                "core_completion_get_activities_completion_status",
                {"courseid": cid, "userid": self._site["userid"]}, cid
            )
            if isinstance(completion, dict):
                for row in completion.get("statuses", []):
                    target = by_cmid.get((cid, row.get("cmid")))
                    if target:
                        target["completion"] = row.get("state")

        # Enrich assignment records, including deadlines and real submission state.
        by_assign_id = {}
        for a in assignments:
            item = by_cmid.get((a["course_id"], self._extract_cmid(a.get("url"))))
            if item is None:
                item = {
                    "course_id": a["course_id"], "course": a["course"],
                    "cmid": self._extract_cmid(a.get("url")), "type": "assign",
                    "title": a["name"], "url": a.get("url"), "section": None,
                    "completion": None,
                }
                records.append(item)
            item["assign_id"] = a.get("id")
            item["due_at"] = a.get("due_at") or item.get("due_at")
            item["cutoff_at"] = a.get("cutoff_at")
            if isinstance(a.get("id"), int):
                by_assign_id[a["id"]] = item

        checked = 0
        if statuses and by_assign_id:
            def get_status(assign_id):
                response = self.read("mod_assign_get_submission_status", {"assignid": assign_id})
                return assign_id, state_of(response) if response is not None else None
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for assign_id, result in pool.map(get_status, by_assign_id):
                    if result is not None:
                        by_assign_id[assign_id]["submission"] = result
                        checked += 1

        quiz_data = self.read("mod_quiz_get_quizzes_by_courses", batch_course_ids(courses))
        quizzes = quiz_data.get("quizzes", []) if isinstance(quiz_data, dict) else []
        quiz_map = {(q.get("course"), q.get("coursemodule")): q
                    for q in quizzes if isinstance(q, dict)}
        for item in records:
            if item["type"] == "quiz":
                q = quiz_map.get((item["course_id"], item.get("cmid")))
                if q is not None:
                    item["due_at"] = q.get("timeclose") or item.get("due_at")
                    item["open_at"] = q.get("timeopen") or None

        calendar_data = self.read("core_calendar_get_calendar_upcoming_view")
        events = []
        if isinstance(calendar_data, dict):
            for e in calendar_data.get("events", []):
                if isinstance(e, dict):
                    events.append({"name": e.get("name"), "start_at": e.get("timestart"),
                                   "url": e.get("url"), "course_id": e.get("course", {}).get("id")
                                   if isinstance(e.get("course"), dict) else None})

        if pages:
            for course in courses:
                response = self.read(
                    "mod_page_get_pages_by_courses", {"courseids[0]": course["id"]}, course["id"]
                )
                if not isinstance(response, dict):
                    continue
                for pg in response.get("pages", []):
                    if not isinstance(pg, dict):
                        continue
                    item = by_cmid.get((course["id"], pg.get("coursemodule")))
                    if item:
                        item["search_text"] = plain_text(pg.get("content", ""))[:MAX_SEARCH_TEXT]

        grade_summary = {}
        if grades:
            for course in courses:
                response = self.read(
                    "gradereport_user_get_grade_items",
                    {"courseid": course["id"], "userid": self._site["userid"]}, course["id"]
                )
                if not isinstance(response, dict):
                    continue
                rows = [r for u in response.get("usergrades", [])
                        if isinstance(u, dict) for r in u.get("gradeitems", [])
                        if isinstance(r, dict)]
                grade_summary[str(course["id"])] = {
                    "course": course["name"], "items": len(rows),
                    "graded_items": sum(r.get("gradeformatted") not in ("", "-", "—", None) for r in rows),
                    "feedback_items": sum(bool(r.get("feedback")) for r in rows),
                    "grade_entries": [
                        {"title": plain_text(r.get("itemname") or r.get("itemmodule") or ""),
                         "grade": plain_text(r.get("gradeformatted") or ""),
                         "feedback": plain_text(r.get("feedback") or "")[:500],
                         "hidden": bool(r.get("hidden"))}
                        for r in rows
                        if r.get("gradeformatted") not in ("", "-", "—", None)
                        or r.get("feedback")
                    ][:100],
                }

        issues = Counter((i["method"], i["reason"]) for i in self.issues)
        return {
            "source": m.BASE, "retrieved_at": int(time.time()),
            "courses": courses, "activities": records, "calendar": events,
            "grades": grade_summary,
            "summary": {
                "courses": len(courses), "activities": len(records),
                "activity_types": dict(Counter(x["type"] for x in records)),
                "assignments": len(assignments), "submissions_checked": checked,
                "submitted": sum(x.get("submission", {}).get("status") == "submitted" for x in records),
                "drafts": sum(x.get("submission", {}).get("status") == "draft" for x in records),
                "unverified_assignments": len(assignments) - checked,
                "quizzes": len(quizzes), "calendar_events": len(events),
                "issues_total": len(self.issues),
            },
            "issues": [{"method": a, "reason": b, "count": n}
                       for (a, b), n in issues.most_common()],
        }

    @staticmethod
    def _extract_cmid(url):
        if not url:
            return None
        matched = re.search(r"[?&]id=(\d+)", url)
        return int(matched.group(1)) if matched else None


def find_activities(snapshot, query):
    terms = [x.casefold() for x in query.split() if x]
    if not terms:
        raise ValueError("Search query must not be empty")
    matches = []
    for a in snapshot["activities"]:
        search_text = str(a.get("search_text", ""))
        corpus = " ".join((str(a.get("title", "")), str(a.get("course", "")),
                           str(a.get("section", "")), search_text)).casefold()
        if not all(term in corpus for term in terms):
            continue
        row = {k: v for k, v in a.items() if k != "search_text"}
        positions = [search_text.casefold().find(term) for term in terms]
        positions = [p for p in positions if p >= 0]
        if positions:
            start = max(0, min(positions) - 65)
            row["text_excerpt"] = search_text[start:start + 220]
        matches.append(row)
    return matches


def deadlines(snapshot, now=None, limit=20, offset=3):
    now = time.time() if now is None else now
    rows = []
    for a in snapshot["activities"]:
        due = a.get("due_at")
        if isinstance(due, (int, float)) and due > 0 and due >= now:
            rows.append({"source": a["type"], "course": a["course"], "title": a["title"],
                         "due_at": due, "due": clock(due, offset), "url": a.get("url"),
                         "submission": a.get("submission", {}).get("status", "unknown")})
    for e in snapshot.get("calendar", []):
        due = e.get("start_at")
        if isinstance(due, (int, float)) and due >= now:
            rows.append({"source": "calendar", "course": None, "title": e.get("name"),
                         "due_at": due, "due": clock(due, offset), "url": e.get("url")})
    return sorted(rows, key=lambda x: x["due_at"])[:limit]


def run(argv=None):
    parser = argparse.ArgumentParser(description="Unified, strictly read-only Moodle MISIS API")
    parser.add_argument("command", choices=["overview", "deadlines", "search", "grades", "audit"])
    parser.add_argument("--query", default="")
    parser.add_argument("--with-pages", action="store_true", help="Index accessible page HTML for search")
    parser.add_argument("--with-files", action="store_true", help="Search encrypted local PDF/DOCX/PPTX index")
    parser.add_argument("--fast", action="store_true", help="Skip per-assignment submission checks")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--tz-offset", type=int, default=3, help="Hours from UTC, default Moscow +3")
    args = parser.parse_args(argv)
    if not -12 <= args.tz_offset <= 14:
        parser.error("Timezone offset must be between -12 and +14")
    if args.command == "search" and not args.query.strip():
        parser.error("search requires --query")
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be 1..500")

    reader = MoodleReader()
    result = reader.collect(
        statuses=not args.fast and args.command != "search",
        pages=args.with_pages and args.command == "search",
        grades=args.command == "grades",
    )
    if args.command == "overview":
        output = {"summary": result["summary"],
                  "next_deadlines": deadlines(result, limit=args.limit, offset=args.tz_offset),
                  "issues": result["issues"]}
    elif args.command == "deadlines":
        output = {"deadlines": deadlines(result, limit=args.limit, offset=args.tz_offset),
                  "summary": result["summary"]}
    elif args.command == "search":
        matches = find_activities(result, args.query)
        output = {"matches": matches[:args.limit], "total_matches": len(matches),
                  "searched_page_content": args.with_pages, "issues": result["issues"]}
        if args.with_files:
            import moodle_materials
            try:
                output["file_matches"] = moodle_materials.find(args.query, args.limit)
                output["file_index_status"] = moodle_materials.status()
            except (moodle_materials.MaterialError, m.MoodleError, OSError) as exc:
                output["file_matches"] = []
                output["file_index_error"] = str(exc)[:180]
    elif args.command == "grades":
        output = {"grades": result["grades"], "issues": result["issues"]}
    else:
        output = result
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (m.MoodleError, OSError) as e:
        print("Moodle error: " + str(e), file=sys.stderr)
        sys.exit(2)
