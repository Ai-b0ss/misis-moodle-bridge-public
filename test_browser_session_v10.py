import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import moodle_browser as b

class MockBrowser:
    def __init__(self,cookies):
        self._cookies=cookies
        self.restored=None
    def cookies(self,urls=None):
        return self._cookies
    def add_cookies(self,cookies):
        self.restored=cookies

class SessionSecurityTests(unittest.TestCase):
    def test_session_dpapi_roundtrip_without_plaintext(self):
        fake_session="SYNTHETIC-TEST-SESSION-DO-NOT-USE"
        cookie={"name":"MoodleSession","value":fake_session,
                "domain":"newlms.misis.ru","path":"/","expires":-1,"httpOnly":True,"secure":True,"sameSite":"Lax"}
        another={"name":"external","value":"SHOULD-NOT-BE-SAVED","domain":"example.org","path":"/","expires":-1,
                 "httpOnly":True,"secure":True,"sameSite":"Lax"}
        with tempfile.TemporaryDirectory(dir=str(b.PROFILE.parent)) as td:
            state=Path(td)/"session.dpapi"
            with patch.object(b,"STATE",state):
                b._save_session(MockBrowser([cookie,another]))
                self.assertTrue(state.exists())
                raw=state.read_bytes()
                self.assertNotIn(fake_session.encode(),raw)
                self.assertNotIn(b"SHOULD-NOT-BE-SAVED",raw)
                other=MockBrowser([])
                b._restore_session(other)
                self.assertEqual([cookie],other.restored)
    def test_bad_domain_rejected(self):
        import moodle_client as m
        with tempfile.TemporaryDirectory(dir=str(b.PROFILE.parent)) as td:
            state=Path(td)/"session.dpapi"
            state.write_bytes(m.dpapi(b'[{"domain":"example.org","name":"x","value":"y"}]',True))
            with patch.object(b,"STATE",state):
                with self.assertRaisesRegex(b.BrowserError,"Protected Moodle session unreadable"):
                    b._restore_session(MockBrowser([]))

if __name__=="__main__":
    unittest.main(verbosity=2)
