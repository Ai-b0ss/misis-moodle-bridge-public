"""Offline regression tests for accessible Google Drive preview text extraction."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import moodle_drive as drive


URL="https://drive.google.com/file/d/ABCDEFGHIJKLMN012345/preview"


class DriveTests(unittest.TestCase):
    def test_only_public_preview(self):
        self.assertEqual(drive.valid_preview(URL),URL)
        for u in (
            "https://drive.google.com.evil.org/file/d/x/preview",
            "http://drive.google.com/file/d/x/preview",
            "https://drive.google.com/file/d/123/edit",
            "https://drive.google.com/file/d/123/preview?token=secret",
            "https://me@drive.google.com/file/d/123/preview",
            "https://drive.google.com:555/file/d/123/preview"
        ):
            with self.subTest(url=u),self.assertRaises(drive.DriveError):
                drive.valid_preview(u)

    def test_russian_page_segmentation(self):
        text=("Страница\nиз\n3\nСтраница 1 из 3\n"
              "Лекция о математике\nТеорема\nСтраница 2 из 3\n"
              "Доказательство\nСтраница 3 из 3\nКонец")
        out=drive.parse_pages(text)
        self.assertEqual(sorted(out),[1,2,3])
        self.assertIn("Теорема",out[1]["text"])
        self.assertIn("Доказательство",out[2]["text"])

    def test_encrypted_local_storage(self):
        obj={"version":1,"files":{URL:{"pages":[{"number":1,"text":"Теорема 2026"}]}}}
        with tempfile.TemporaryDirectory() as p,patch.object(drive,"CACHE",Path(p)/"drive.dpapi"):
            drive.save_cache(obj)
            self.assertNotIn("Теорема".encode("utf-8"),drive.CACHE.read_bytes())
            self.assertEqual(drive.read_cache(),obj)

    def test_revoke_visibility_means_no_results(self):
        cache={"version":1,"files":{URL:{"course":"A","cmid":99,
                "module_title":"Math","pages_total":1,"complete":True,
                "pages":[{"number":1,"text":"Вычислительная математика"}]}}}
        with patch.object(drive,"read_cache",return_value=cache),\
             patch.object(drive,"sources",return_value={}):
            self.assertEqual(drive.find("математика"),[])

    def test_search_contains_source_page(self):
        cache={"version":1,"files":{URL:{"course":"A","cmid":99,
                "module_title":"Math","pages_total":1,"complete":True,
                "pages":[{"number":1,"text":"Вычислительная математика"}]}}}
        with patch.object(drive,"read_cache",return_value=cache),\
             patch.object(drive,"sources",return_value={URL:{"cmid":99}}):
            match=drive.find("математика")
        self.assertEqual(len(match),1)
        self.assertEqual(match[0]["page_number"],1)


if __name__=="__main__":
    unittest.main(verbosity=2)
