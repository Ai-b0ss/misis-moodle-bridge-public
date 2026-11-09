"""Offline security and functional regression for public Yandex Disk Moodle index."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from urllib.request import Request
import moodle_yadisk as yd

GOOD="https://disk.yandex.ru/i/ABCdef123_-"

class YDiskTests(unittest.TestCase):
    def test_share_url_allowlist(self):
        self.assertEqual(yd.public_url(GOOD),GOOD)
        for url in (
            "http://disk.yandex.ru/i/ABCdef123",
            "https://disk.yandex.ru.attacker.test/i/ABCdef123",
            "https://user@disk.yandex.ru/i/ABCdef123",
            "https://disk.yandex.ru/i/ABCdef123?token=123",
            "https://disk.yandex.ru/admin/index",
            "https://disk.yandex.ru:555/i/ABCdef123",
            "https://disk.yandex.ru/i/a"
        ):
            with self.subTest(url=url),self.assertRaises(yd.YDiskError):
                yd.public_url(url)

    def test_redirect_host_guard(self):
        h=yd.YandexOnlyRedirect()
        for target in ("https://evil.org/abc","http://mds.storage.yandex.net/abc",
                       "https://mal@mds.storage.yandex.net/abc",
                       "https://mds.storage.yandex.net:999/abc"):
            with self.subTest(target=target):
                self.assertIsNone(h.redirect_request(Request("https://downloader.disk.yandex.ru/test"),None,302,"found",{},target))
        self.assertIsNotNone(h.redirect_request(Request("https://downloader.disk.yandex.ru/test"),None,302,"found",{}, "https://mds.storage.yandex.net/rdisk/test"))

    def test_local_encryption(self):
        obj={"version":1,"files":{"fake":{"text":"Лекция МИСИС"}}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(yd,"CACHE",Path(tmp)/"yd.dpapi"):
            yd.save_cache(obj)
            self.assertNotIn("Лекция".encode("utf-8"),yd.CACHE.read_bytes())
            self.assertEqual(yd.read_cache(),obj)

    def test_visibility_revocation(self):
        cache={"version":1,"files":{"a":{"source":GOOD,"course":"A","cmid":12,"module":"X",
                     "name":"X.pdf","text":"Теорема синусов"}}}
        with patch.object(yd,"read_cache",return_value=cache),\
             patch.object(yd,"listed_sources",return_value={}):
            self.assertEqual(yd.search("синусов"),[])

    def test_find_allowed_material(self):
        cache={"version":1,"files":{"a":{"source":GOOD,"course":"A","cmid":12,"module":"X",
                     "name":"X.pdf","text":"Теорема синусов"}}}
        with patch.object(yd,"read_cache",return_value=cache),\
             patch.object(yd,"listed_sources",return_value={GOOD:{"cmid":12}}):
            result=yd.search("синусов")
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["cmid"],12)
        self.assertIn("синусов",result[0]["text_excerpt"])

    def test_removed_link_not_accepted_from_stale_cache(self):
        cached={"items":[{"course_id":42,"cmid":11,"course":"A",
                "title":"Page","links":[{"url":GOOD}]}]}
        with patch.object(yd.hybrid,"read_cache",return_value=cached),\
             patch.object(yd.m,"read_credential",return_value={"token":"dummy"}),\
             patch.object(yd.m,"site_info",return_value=("dummy",{"userid":5})),\
             patch.object(yd.m,"get_courses",return_value=[{"id":42,"name":"A"}]),\
             patch.object(yd.hybrid,"modules_by_course",return_value={(42,11):{"type":"page"}}),\
             patch.object(yd.m,"call",return_value={"pages":[{
                 "course":42,"coursemodule":11,
                 "content":"<p>Учебная страница без ссылки</p>"}]}):
            self.assertEqual(yd.listed_sources(),{})

    def test_skip_video_and_large_files(self):
        docs={"x":{"source":GOOD,"size":4*1024*1024*1024,"mime":"video/mp4","name":"film.mp4"},
              "y":{"source":GOOD,"size":200000,"mime":"video/mp4","name":"small.mp4"}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(yd,"CACHE",Path(tmp)/"cache.dpapi"),\
             patch.object(yd,"inventory",return_value=(docs,{"source_links":1,"unavailable":0})),\
             patch.object(yd,"download_public",side_effect=AssertionError("videos must not download")):
            result=yd.index(limit=5,budget_mb=10)
        self.assertEqual(result["processed"],0)
        self.assertEqual(result["skipped"]["unsupported_format"],2)

if __name__=="__main__":
    unittest.main(verbosity=2)
