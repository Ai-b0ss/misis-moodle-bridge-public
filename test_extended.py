"""Offline regression tests for Moodle's unified read-only facade."""
import unittest
from unittest.mock import patch
import moodle_client as m
import moodle_extended as e


COURSES = [{"id": 10, "name": "Course A", "url": "https://newlms.misis.ru/course/view.php?id=10"}]
ASSIGNMENTS = [
    {"id": 60, "name": "Homework", "course_id": 10, "course": "Course A",
     "due_at": 2000000000, "cutoff_at": None,
     "url": "https://newlms.misis.ru/mod/assign/view.php?id=20"},
]
SECTIONS = [{"name": "Unit 1", "modules": [
    {"id": 20, "instance": 60, "modname": "assign", "name": "Homework",
     "url": "https://newlms.misis.ru/mod/assign/view.php?id=20"},
    {"id": 21, "instance": 61, "modname": "page", "name": "Lecture",
     "url": "https://newlms.misis.ru/mod/page/view.php?id=21"},
    {"id": 22, "instance": 62, "modname": "quiz", "name": "Quiz",
     "url": "https://newlms.misis.ru/mod/quiz/view.php?id=22"},
]}]


def fake_api(token, method, params):
    if method == "core_course_get_contents":
        return SECTIONS
    if method == "core_completion_get_activities_completion_status":
        return {"statuses": [{"cmid": 20, "state": 0}, {"cmid": 21, "state": 1}]}
    if method == "mod_assign_get_submission_status":
        return {"lastattempt": {"submission": {"status": "submitted"},
                                "gradingstatus": "graded", "canedit": False},
                "feedback": {"grade": 0}}
    if method == "mod_quiz_get_quizzes_by_courses":
        return {"quizzes": [{"course": 10, "coursemodule": 22, "timeclose": 2000000300}]}
    if method == "core_calendar_get_calendar_upcoming_view":
        return {"events": [{"name": "Meeting", "timestart": 2000000200, "course": {"id": 10}}]}
    if method == "mod_page_get_pages_by_courses":
        return {"pages": [{"coursemodule": 21, "content":
                           "<style>ignore</style><p>Graph theory and graphs</p>"}]}
    if method == "gradereport_user_get_grade_items":
        return {"usergrades": [{"gradeitems": [
            {"gradeformatted": "0", "feedback": "Check"},
            {"gradeformatted": "-", "feedback": ""},
        ]}]}
    raise AssertionError(f"Unexpected API method: {method}")


class FacadeTests(unittest.TestCase):
    def setUp(self):
        self.reader = e.MoodleReader(token="fake", site={"userid": 99}, retries=0)

    def collect(self, statuses=True, pages=False, grades=False, api=fake_api):
        with patch.object(m, "get_courses", return_value=COURSES), \
             patch.object(m, "get_assignments", return_value=(ASSIGNMENTS, [])), \
             patch.object(m, "call", side_effect=api):
            return self.reader.collect(statuses=statuses, pages=pages, grades=grades)

    def test_prevents_write_even_if_called_explicitly(self):
        with self.assertRaises(ValueError):
            self.reader.read("mod_quiz_start_attempt", {"quizid": 123})
        with self.assertRaises(ValueError):
            self.reader.read("mod_lesson_launch_attempt", {"lessonid": 123})
        with self.assertRaises(ValueError):
            self.reader.read("mod_assign_save_submission", {"assignmentid": 60})

    def test_joins_modules_and_assignments(self):
        data = self.collect()
        self.assertEqual(data["summary"]["activities"], 3)
        self.assertEqual(data["summary"]["assignments"], 1)
        self.assertEqual(data["summary"]["submissions_checked"], 1)
        self.assertEqual(data["summary"]["submitted"], 1)
        assignment = next(a for a in data["activities"] if a["type"] == "assign")
        self.assertEqual(assignment["submission"]["status"], "submitted")
        self.assertTrue(assignment["submission"]["graded"])
        self.assertEqual(assignment["completion"], 0)

    def test_fast_mode_does_not_infer_submission_status(self):
        data = self.collect(statuses=False)
        self.assertEqual(data["summary"]["submissions_checked"], 0)
        self.assertEqual(data["summary"]["unverified_assignments"], 1)
        self.assertEqual(data["summary"]["submitted"], 0)

    def test_search_escaped_html_content(self):
        data = self.collect(pages=True)
        self.assertEqual(len(e.find_activities(data, "GRAPH Theory")), 1)
        self.assertEqual(len(e.find_activities(data, "ignore")), 0)

    def test_course_name_search(self):
        data = self.collect()
        self.assertEqual(len(e.find_activities(data, "course a")), 3)

    def test_deadlines_sort_and_timezone(self):
        data = self.collect()
        rows = e.deadlines(data, now=1999999999, limit=10, offset=3)
        self.assertEqual([x["source"] for x in rows], ["assign", "calendar", "quiz"])
        self.assertTrue(rows[0]["due"].endswith("+03:00"))

    def test_grade_zero_counts_as_graded(self):
        data = self.collect(grades=True)
        self.assertEqual(data["grades"]["10"]["graded_items"], 1)
        self.assertEqual(data["grades"]["10"]["grade_entries"][0]["grade"], "0")

    def test_missing_module_list_does_not_crash(self):
        def fake(token, method, params):
            if method == "core_course_get_contents":
                return None
            return fake_api(token, method, params)
        data = self.collect(api=fake)
        self.assertEqual(data["summary"]["assignments"], 1)
        self.assertEqual(data["summary"]["activities"], 1)

    def test_api_warning_aggregated(self):
        def warn(token, method, params):
            if method == "core_completion_get_activities_completion_status":
                return {"statuses": [], "warnings": [
                    {"message": "No access rights in module context"}] * 3}
            return fake_api(token, method, params)
        data = self.collect(api=warn)
        self.assertEqual(data["summary"]["issues_total"], 3)
        self.assertEqual(data["issues"][0]["count"], 3)

    def test_error_recorded_without_fabricating_status(self):
        def fail(token, method, params):
            if method == "mod_assign_get_submission_status":
                raise m.MoodleError("permission denied")
            return fake_api(token, method, params)
        data = self.collect(api=fail)
        self.assertEqual(data["summary"]["unverified_assignments"], 1)
        self.assertEqual(data["summary"]["submissions_checked"], 0)
        self.assertNotIn("submission", next(a for a in data["activities"] if a["type"] == "assign"))

    def test_time_and_search_edges(self):
        self.assertIsNone(e.clock(0))
        with self.assertRaises(ValueError):
            e.find_activities({"activities": []}, "  ")
        self.assertEqual(e.plain_text("<p>x &amp; y</p>"), "x & y")

    def test_retry_network_once_but_never_permission(self):
        calls = [0]
        def flaky(token, method, params):
            calls[0] += 1
            if calls[0] == 1:
                raise m.MoodleError("Не удалось связаться с newlms.misis.ru.")
            return {"events": []}
        self.reader.retries = 1
        with patch.object(m, "call", side_effect=flaky), patch.object(e.time, "sleep"):
            self.assertEqual(self.reader.read("core_calendar_get_calendar_upcoming_view"),
                             {"events": []})
        self.assertEqual(calls[0], 2)
        calls[0] = 0
        def permission(token, method, params):
            calls[0] += 1
            raise m.MoodleError("permission denied")
        with patch.object(m, "call", side_effect=permission):
            self.assertIsNone(self.reader.read("core_calendar_get_calendar_upcoming_view"))
        self.assertEqual(calls[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
