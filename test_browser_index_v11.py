import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import moodle_browser_index as bi

PASSIVE_URL="https://newlms.misis.ru/mod/page/view.php?id=123456"
MODULES={(33,123456):{"title":"Russian lecture","type":"page",
                        "url":PASSIVE_URL,"section":"A"}}

class BrowserEnrichmentTests(unittest.TestCase):
    def test_url_must_be_single_visible_passive_module(self):
        self.assertEqual(("mod" if False else "/mod/page/view.php",123456),bi._key(PASSIVE_URL))
        for url in (
            "https://newlms.misis.ru/mod/quiz/view.php?id=123456",
            "https://newlms.misis.ru/mod/lesson/view.php?id=123456",
            "https://newlms.misis.ru/mod/page/view.php?id=123456&forceview=1",
            "https://other.example/mod/page/view.php?id=123456",
            "https://newlms.misis.ru/mod/page/view.php?id=0",
            "https://newlms.misis.ru/mod/page/view.php?id=123456&sesskey=abc",
        ):
            with self.assertRaises(Exception,msg=url):
                bi._key(url)

    def test_verified_course_module_and_type(self):
        r=bi.verified(PASSIVE_URL,MODULES)
        self.assertEqual(r["course_id"],33)
        self.assertEqual(r["cmid"],123456)
        with self.assertRaises(bi.EnrichmentError):
            bi.verified(PASSIVE_URL,{})
        changed={(33,123456):{**MODULES[(33,123456)],"type":"assign"}}
        with self.assertRaises(bi.EnrichmentError):
            bi.verified(PASSIVE_URL,changed)

    def test_encrypted_cache_and_index_search_refresh_acl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"browser_cache.dpapi"
            with patch.object(bi,"CACHE",path),\
                 patch.object(bi,"_modules",return_value=(MODULES,[{"id":33,"name":"History"}])),\
                 patch.object(bi.browser,"inspect",return_value={
                     "state":"readable","text":"Russian lecture material, algorithms and logic."}):
                item=bi.index(PASSIVE_URL)
                self.assertEqual(item["state"],"indexed")
                self.assertNotIn(b"Russian lecture material",path.read_bytes())
                got=bi.search("algorithms",10)
                self.assertEqual(len(got),1)
                self.assertEqual(got[0]["cmid"],123456)
                self.assertEqual(bi.status()["cached_pages"],1)
                with patch.object(bi,"_modules",return_value=({},[])):
                    self.assertEqual(bi.search("algorithms",10),[])

    def test_missing_protected_session_does_not_launch_chrome(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            missing=Path(tmp)/"browser_session_missing.dpapi"
            with patch.object(bi.browser,"STATE",missing),\
                 patch.object(bi.browser,"open_session",
                              side_effect=AssertionError("Chrome must remain closed")):
                status=bi.browser.check_session()
                self.assertFalse(status["authorized"])
                self.assertEqual(status["reason"],"protected_session_missing")
                x=bi.browser.inspect(PASSIVE_URL)
                self.assertEqual(x["state"],"needs_browser_login")

    def test_does_not_cache_redirect_to_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"browser_cache.dpapi"
            with patch.object(bi,"CACHE",path),\
                 patch.object(bi,"_modules",return_value=(MODULES,[])),\
                 patch.object(bi.browser,"inspect",return_value={
                     "state":"needs_browser_login"}):
                self.assertFalse(bi.index(PASSIVE_URL)["cached"])
                self.assertFalse(path.exists())

if __name__=="__main__":
    unittest.main(verbosity=2)
