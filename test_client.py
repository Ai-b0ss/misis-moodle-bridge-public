import unittest
from unittest.mock import patch
import moodle_client as m

class ReadOnlyTests(unittest.TestCase):
    def test_invalid_login(self):
        with patch.object(m,"post",return_value={"errorcode":"invalidlogin"}):
            with self.assertRaisesRegex(m.MoodleError,"Логин/пароль"):
                m.auth("bad","bad")
    def test_disabled_service(self):
        with patch.object(m,"post",return_value={"errorcode":"servicenotavailable"}):
            with self.assertRaisesRegex(m.MoodleError,"отключён"):
                m.auth("no","no")
    def test_success_login(self):
        with patch.object(m,"post",return_value={"token":"testing_token"}):
            self.assertEqual(m.auth("test","test"),"testing_token")
    def test_no_token_leaked_to_data(self):
        with patch.object(m,"read_credential",return_value={"username":"x","password":"secret","token":"private"}), patch.object(m,"site_info",return_value=("private",{"userid":7,"fullname":"student"})), patch.object(m,"get_courses",return_value=[]):
            report=m.get_report()
            self.assertNotIn("secret",repr(report))
            self.assertNotIn("private",repr(report))
    def test_assignments_missing_privileges(self):
        with patch.object(m,"call",side_effect=m.MoodleError("no access")):
            result,warnings=m.get_assignments("nope",[{"id":1,"name":"Test"}])
            self.assertEqual(result,[])
            self.assertEqual(warnings,["no access"])
    def test_null_submission_safe(self):
        tasks=[{"id":5}]
        with patch.object(m,"call",return_value={"lastattempt":{"submission":None},"feedback":None}):
            self.assertEqual(m.check_submission("test",tasks),[])
            self.assertEqual(tasks[0]["submission_status"],"unknown")
    def test_dpapi(self):
        self.assertEqual(m.dpapi(m.dpapi(b"a test password only",True),False),b"a test password only")

    def test_hidden_password_input(self):
        from unittest.mock import MagicMock
        stdin = MagicMock()
        stdin.isatty.return_value = True
        password = "unit-test"
        with patch.object(m.sys, "stdin", stdin), patch.object(m.sys, "argv", ["moodle_client.py", "setup"]), patch("builtins.input", return_value="student") as inp, patch.object(m.getpass, "getpass", return_value=password) as hidden, patch.object(m, "auth", return_value="test-token") as auth, patch.object(m, "call", return_value={"userid": 99, "fullname": "student"}), patch.object(m, "save_credential") as saved:
            self.assertEqual(m.main(), 0)
            self.assertEqual(inp.call_count, 1)
            hidden.assert_called_once()
            auth.assert_called_once_with("student", password)
            saved.assert_called_once_with({"username": "student", "token": "test-token"})

    def test_redirects_never_followed(self):
        from urllib.request import Request
        req = Request("https://newlms.misis.ru/webservice/rest/server.php", data=b"secret")
        self.assertIsNone(m.NoRedirect().redirect_request(req, None, 302, "redirect", {},
                      "https://somewhere-else.example/collect"))

    def test_all_assignments_checked_by_default(self):
        tasks = [{"id": i} for i in range(60)]
        reply = {"lastattempt": {"submission": {"status": "submitted"}}, "feedback": None}
        with patch.object(m, "call", return_value=reply) as call:
            self.assertEqual(m.check_submission("token", tasks), [])
            self.assertEqual(call.call_count, 60)
            self.assertEqual(sum(a["submission_status"] == "submitted" for a in tasks), 60)

if __name__=="__main__":
    unittest.main(verbosity=2)
