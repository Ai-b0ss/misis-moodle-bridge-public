"""Safety and regression tests for MISIS hybrid-v9."""
import io
from unittest.mock import patch,MagicMock
import unittest
import moodle_slides as slides
import moodle_videos as vid

S="https://docs.google.com/presentation/d/e/2PACX-Test/pubembed"
Y="https://disk.yandex.ru/i/ABCdef123_-"

class FakeHttpResponse:
    status=200
    headers={"Content-Length":"4"}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,n):
        assert n>=4
        return b"1234"

class VideoV9(unittest.TestCase):
    def test_video_size_cap(self):
        self.assertGreater(vid.MAX_SOURCE_SIZE,89*1048576)
        with self.assertRaises(vid.VideoError):
            vid.download_video_bounded({"source":Y,"size":4*1024*1024*1024})
    def test_bad_video_download_host_rejected(self):
        with patch.object(vid.yd,"request_json",return_value={"href":"https://attacker.example/video.mp4"}):
            with self.assertRaisesRegex(vid.VideoError,"not trusted"):
                vid.download_video_bounded({"source":Y,"size":4})
    def test_good_bounded_download_is_not_saved(self):
        with patch.object(vid.yd,"request_json",return_value={
             "href":"https://downloader.disk.yandex.ru/file?id=PUBLIC"}),\
             patch.object(vid.yd.DOWNLOADER,"open",return_value=FakeHttpResponse()) as opener:
            result=vid.download_video_bounded({"source":Y,"size":4})
        self.assertEqual(result,b"1234")
        self.assertEqual(opener.call_count,1)
    def test_truncated_content_rejected(self):
        with patch.object(vid.yd,"request_json",return_value={
             "href":"https://downloader.disk.yandex.ru/file"}),\
             patch.object(vid.yd.DOWNLOADER,"open",return_value=FakeHttpResponse()):
            with self.assertRaisesRegex(vid.VideoError,"differ"):
                vid.download_video_bounded({"source":Y,"size":5})

class SlidesV9(unittest.TestCase):
    def _run_index_mock(self,cached,side_effect):
        meta={"url":S,"course_id":1,"cmid":2,"course":"Math","module_title":"Slides"}
        updated={"version":1,"decks":{S:cached}}
        with patch.object(slides,"public_sources",return_value={S:meta}),\
             patch.object(slides,"read_cache",return_value=updated),\
             patch.object(slides,"save_cache") as saved,\
             patch.object(slides,"inspect_deck",
                          side_effect=side_effect if isinstance(side_effect,Exception) else None,
                          return_value=side_effect if isinstance(side_effect,dict) else None),\
             patch.object(slides,"sync_playwright") as playwright:
            playwright.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value=MagicMock()
            response=slides.index(limit=1)
        return response,updated["decks"][S],saved
    def test_failed_retry_keeps_partial_content(self):
        old={"slides_read":2,"slides_total":10,"slides":[
            {"number":1,"text":"Previously read one"},
            {"number":2,"text":"Previously read two"}],"complete":False}
        result,record,saved=self._run_index_mock(old,RuntimeError("Viewer crashed"))
        self.assertEqual(result["errors"],{"RuntimeError":1})
        self.assertEqual(record["slides_read"],2)
        self.assertEqual(len(record["slides"]),2)
        self.assertNotIn("error",record)
        self.assertEqual(record["last_error"],"RuntimeError")
        self.assertEqual(saved.call_count,1)
    def test_generic_error_can_be_retried(self):
        old={"slides_read":0,"slides_total":0,"slides":[],"complete":False,"error":"Error"}
        fresh={"title":"Deck","slides_total":2,"slides_read":2,"complete":True,
               "slides":[{"number":1,"text":"A"},{"number":2,"text":"B"}]}
        r,record,_=self._run_index_mock(old,fresh)
        self.assertTrue(record["complete"])
        self.assertEqual(record["slides_read"],2)
        self.assertEqual(r["complete"],1)

if __name__=="__main__":
    unittest.main(verbosity=2)
