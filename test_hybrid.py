"""Offline safety and content tests for the hybrid Moodle fallback."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import moodle_client as m
import moodle_browser as browser
import moodle_hybrid as hybrid
import moodle_lessons as lessons

VISIBLE = {"course_id":9,"course":"My Course","cmid":23,
           "lesson_id":45,"title":"Lesson A"}

def mock_lesson(token, method, kwargs):
    if method=="mod_lesson_get_lesson":
        return {"lesson":{"practice":True,"grade":0,"maxattempts":0}}
    if method=="mod_lesson_get_lesson_access_information":
        return {"preventaccessreasons":[]}
    if method=="mod_lesson_get_pages":
        return {"pages":[{"page":{"id":20,"title":"First"}}]}
    if method=="mod_lesson_get_page_data":
        return {"page":{"contents":"<p>Формулы и теория</p><iframe src='https://docs.google.com/presentation/d/e/abcdef/pubembed'></iframe>"}}
    if method=="mod_lesson_launch_attempt":
        return {"newattempt":True}
    raise AssertionError("unexpected method: "+method)


class LessonTests(unittest.TestCase):
    def setUp(self):
        self.inspector=lessons.Inspector("test-token",{"userid":1})

    def test_active_lesson_read_has_no_writes(self):
        methods=[]
        def api(t,method,kwargs):
            methods.append(method)
            return mock_lesson(t,method,kwargs)
        with patch.object(m,"call",side_effect=api):
            result=self.inspector.inspect(23,known=VISIBLE)
        self.assertEqual(result["summary"]["readable"],1)
        self.assertIn("теория",result["pages"][0]["text"])
        self.assertEqual(result["pages"][0]["links"][0]["host"],"docs.google.com")
        self.assertNotIn("mod_lesson_launch_attempt",methods)

    def test_timer_missing_reports_requires_attempt(self):
        def api(t,method,kwargs):
            if method=="mod_lesson_get_page_data":
                raise m.MoodleError("mod_lesson_get_page_data: Ошибка Moodle (cannotfindtimer).")
            return mock_lesson(t,method,kwargs)
        with patch.object(m,"call",side_effect=api):
            result=self.inspector.inspect(23,known=VISIBLE)
        self.assertEqual(result["pages"][0]["state"],"requires_attempt")

    def test_nopractice_cannot_launch(self):
        def api(t,method,kwargs):
            if method=="mod_lesson_get_lesson":
                return {"lesson":{"practice":False,"grade":100,"maxattempts":1}}
            return mock_lesson(t,method,kwargs)
        with patch.object(self.inspector,"resolve",return_value=VISIBLE), \
             patch.object(m,"call",side_effect=api) as call:
            with self.assertRaisesRegex(lessons.LessonError,"not a practice"):
                self.inspector.start_practice(23,23)
            self.assertFalse(any(c.args[1]=="mod_lesson_launch_attempt" for c in call.call_args_list))

    def test_exact_confirmation_required(self):
        with self.assertRaisesRegex(lessons.LessonError,"confirmation"):
            self.inspector.start_practice(23,24)

    def test_lesson_safe_html(self):
        result=lessons.parse_html("<script>secret</script><p>Hello &amp; goodbye</p><iframe src='https://evil.example/?token=secret'></iframe>")
        self.assertEqual(result["text"],"Hello & goodbye")
        self.assertEqual(result["links"],[])


class BrowserTests(unittest.TestCase):
    def test_allowed(self):
        self.assertEqual(browser.validate_url("https://newlms.misis.ru/mod/page/view.php?id=123"),
                         "https://newlms.misis.ru/mod/page/view.php?id=123")

    def test_reject_external_secret_admin(self):
        for url in (
            "http://newlms.misis.ru/course/view.php?id=1",
            "https://newlms.misis.ru.evil.org/course/view.php?id=1",
            "https://evil@newlms.misis.ru/course/view.php?id=1",
            "https://newlms.misis.ru/admin/index.php",
            "https://newlms.misis.ru/mod/page/view.php?id=1&token=secret",
            "https://newlms.misis.ru:444/mod/page/view.php?id=1",
        ):
            with self.subTest(url=url), self.assertRaises(browser.BrowserError):
                browser.validate_url(url)


class HybridTests(unittest.TestCase):
    def test_no_token_in_links(self):
        self.assertIsNone(hybrid.safe_url("https://docs.google.com/a?sesskey=secret"))
        self.assertIsNone(hybrid.safe_url("http://evil.example/x"))
        self.assertEqual(hybrid.safe_url("https://docs.google.com/presentation/d/e/xyz/pubembed"),
                         "https://docs.google.com/presentation/d/e/xyz/pubembed")

    def test_local_dpapi_storage(self):
        fake={"version":hybrid.VERSION,"created_at":200,"status":{"items":1},"items":[{"text":"Привет Moodle"}]}
        with tempfile.TemporaryDirectory() as d, patch.object(hybrid,"CACHE",Path(d)/"index.dpapi"):
            hybrid.save_cache(fake)
            self.assertNotIn("Привет".encode("utf-8"),hybrid.CACHE.read_bytes())
            self.assertEqual(hybrid.read_cache(),fake)

    def test_revoked_module_excluded(self):
        data={"version":1,"created_at":1,"items":[
            {"course_id":9,"cmid":23,"course":"My Course",
            "title":"Lesson A","text":"Secret vocabulary","type":"lesson","quality":"full_text","url":None},
        ]}
        with patch.object(hybrid,"read_cache",return_value=data),\
             patch.object(m,"site_info",return_value=("test",{"userid":1})),\
             patch.object(m,"read_credential",return_value={"token":"test"}),\
             patch.object(m,"get_courses",return_value=[{"id":9,"name":"My Course"}]),\
             patch.object(hybrid,"modules_by_course",return_value={}):
            self.assertEqual(hybrid.search("Secret")["matches"],[])

    def test_no_expired_lesson_data(self):
        data={"version":1,"created_at":1,"items":[{
            "course_id":9,"cmid":23,"course":"My Course","title":"Lesson A",
            "text":"Secret vocabulary","type":"lesson","quality":"full_text","url":None,
            "detail":{"lessonid":45,"pageid":20}
        }]}
        visible={(9,23):{"type":"lesson","title":"Lesson A"}}
        with patch.object(hybrid,"read_cache",return_value=data),\
             patch.object(m,"site_info",return_value=("test",{"userid":1})),\
             patch.object(m,"read_credential",return_value={"token":"test"}),\
             patch.object(m,"get_courses",return_value=[{"id":9,"name":"My Course"}]),\
             patch.object(hybrid,"modules_by_course",return_value=visible),\
             patch.object(m,"call",side_effect=m.MoodleError("cannotfindtimer")):
            self.assertEqual(hybrid.search("Secret")["matches"],[])


if __name__=="__main__":
    unittest.main(verbosity=2)
