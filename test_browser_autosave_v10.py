import unittest
from unittest.mock import patch,MagicMock
import moodle_browser as b

class FakePage:
    def __init__(self,mode="login",login_after_waits=1):
        self.url="about:blank"
        self.mode=mode
        self.waits=0
        self.login_after_waits=login_after_waits
    def goto(self,url,**kwargs):
        if "/login/" in url:
            self.url="https://newlms.misis.ru/login/index.php"
        elif "/my/" in url:
            self.url="https://newlms.misis.ru/my/"
    def route(self,*args):
        pass
    def wait_for_timeout(self,ms):
        self.waits+=1
        if self.mode=="login" and self.waits>=self.login_after_waits:
            self.url="https://newlms.misis.ru/my/"
    def close(self):
        pass

class FakeContext:
    def __init__(self):
        self.pages=[]
        self.was_closed=False
    def new_page(self):
        p=FakePage()
        self.pages.append(p)
        return p
    def close(self):
        self.was_closed=True

class BrowserAutoSaveTest(unittest.TestCase):
    def test_dashboard_url_rejects_forged_hosts(self):
        self.assertTrue(b._is_moodle_dashboard("https://newlms.misis.ru/my/"))
        for url in ("https://evil.tld/my/","http://newlms.misis.ru/my/",
                    "https://newlms.misis.ru.evil.tld/my/",
                    "https://newlms.misis.ru/login/index.php"):
            self.assertFalse(b._is_moodle_dashboard(url),url)

    def test_user_login_autosave_requires_no_console_input(self):
        c=FakeContext()
        p=MagicMock()
        with patch.object(b,"open_session",return_value=(p,c)),\
             patch.object(b,"_save_session") as save:
            result=b.authorize_interactive()
        self.assertEqual(result,0)
        save.assert_called_once_with(c)
        self.assertTrue(c.was_closed)
        p.stop.assert_called_once()

if __name__=="__main__":
    unittest.main()
