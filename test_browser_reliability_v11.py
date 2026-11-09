import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import moodle_browser as b

class FakePage:
    def __init__(self, landed):
        self.url="about:blank"
        self.landed=landed
    def route(self,*args): pass
    def goto(self,*args,**kwargs):
        self.url=self.landed
        return type("Response",(),{"status":200})()
    def wait_for_timeout(self,*args):pass
    def close(self):pass

class FakeContext:
    def __init__(self,url):
        self.page=FakePage(url)
    def new_page(self):return self.page
    def close(self):pass

class BrowserReliabilityTests(unittest.TestCase):
    def test_strict_allowed_path(self):
        good="https://newlms.misis.ru/mod/page/view.php?id=123"
        b.validate_url(good)
        for path in ("view.php.delete","view.php/extra","view.php.html"):
            with self.assertRaises(b.BrowserError):
                b.validate_url(good.replace("view.php",path))
        for url in ("https://newlms.misis.ru:444/mod/page/view.php?id=123",
                    "https://evil.com/mod/page/view.php?id=123"):
            with self.assertRaises(b.BrowserError):
                b.validate_url(url)

    def test_request_guard_blocks_other_ports_and_hosts(self):
        for address,continue_allowed in [
            ("https://newlms.misis.ru/mod/page/view.php?id=1",True),
            ("https://newlms.misis.ru:1234/mod/page/view.php?id=1",False),
            ("https://external.org/",False),
            ("http://newlms.misis.ru/",False)]:
            route=MagicMock()
            route.request.url=address
            b.guard_requests(route)
            if continue_allowed:
                route.continue_.assert_called_once()
                route.abort.assert_not_called()
            else:
                route.abort.assert_called_once()
                route.continue_.assert_not_called()

    def test_expired_cookie_returns_needs_relogin_without_exposing_cookie(self):
        with tempfile.TemporaryDirectory() as t:
            state=Path(t)/"fake.dpapi"
            state.write_bytes(b"nonsecret-for-mocked-open-session")
            ctx=FakeContext("https://newlms.misis.ru/login/index.php")
            pw=MagicMock()
            with patch.object(b,"STATE",state),patch.object(b,"open_session",return_value=(pw,ctx)):
                result=b.check_session()
            self.assertEqual(result["reason"],"session_expired")
            self.assertFalse(result["authorized"])
            pw.stop.assert_called_once()

    def test_resource_redirect_never_returned_as_readable(self):
        original="https://newlms.misis.ru/mod/page/view.php?id=123"
        for target in ("https://newlms.misis.ru/my/",
                       "https://newlms.misis.ru/mod/page/view.php?id=999"):
            with tempfile.TemporaryDirectory() as t:
                state=Path(t)/"fake.dpapi"
                state.write_bytes(b"mock-not-real")
                ctx=FakeContext(target)
                pw=MagicMock()
                with patch.object(b,"STATE",state),patch.object(b,"open_session",return_value=(pw,ctx)):
                    result=b.inspect(original)
                self.assertEqual(result["state"],"unexpected_navigation")
                pw.stop.assert_called_once()

if __name__=="__main__":
    unittest.main(verbosity=2)
