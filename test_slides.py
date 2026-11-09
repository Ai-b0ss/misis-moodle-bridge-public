"""Offline tests of read-only published Google Slides fallback."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import moodle_slides as slides

PUBLIC="https://docs.google.com/presentation/d/e/2PACX-1vTest/pubembed?start=false"


class SlideTests(unittest.TestCase):
    def test_public_only(self):
        self.assertEqual(slides.valid_published_url(PUBLIC),PUBLIC)
        self.assertEqual(slides.valid_published_url("https://docs.google.com/presentation/d/abcDEF123/embed"),"https://docs.google.com/presentation/d/abcDEF123/embed")
        for url in (
            "https://docs.google.com.evil.org/presentation/d/e/xyz/pubembed",
            "http://docs.google.com/presentation/d/e/xyz/pubembed",
            "https://user@docs.google.com/presentation/d/e/xyz/pubembed",
            "https://docs.google.com/presentation/d/xyz/edit",
            "https://docs.google.com/presentation/d/e/xyz/pubembed?token=secret",
            "https://docs.google.com:444/presentation/d/e/xyz/pubembed",
        ):
            with self.subTest(url=url),self.assertRaises(slides.SlidesError):
                slides.valid_published_url(url)

    def test_secure_encrypted_cache(self):
        item={"version":1,"decks":{PUBLIC:{"title":"Лекция МИСИС",
              "course_id":3417,"cmid":100,"slides":[{"number":1,"text":"Знания"}]}}}
        with tempfile.TemporaryDirectory() as temp,patch.object(slides,"CACHE",Path(temp)/"slides.dpapi"):
            slides.save_cache(item)
            self.assertNotIn("Знания".encode("utf-8"),slides.CACHE.read_bytes())
            self.assertEqual(slides.read_cache(),item)

    def test_revoked_moodle_source_is_not_searched(self):
        c={"version":1,"decks":{PUBLIC:{"course":"Course","cmid":100,
           "module_title":"Лекция","slides_total":1,"slides_read":1,
           "slides":[{"number":1,"text":"Алгоритмы"}]}}}
        with patch.object(slides,"read_cache",return_value=c),\
             patch.object(slides,"public_sources",return_value={}):
            self.assertEqual(slides.find("Алгоритмы"),[])

    def test_slide_search_and_excerpt(self):
        c={"version":1,"decks":{PUBLIC:{"course":"Course","cmid":100,
           "module_title":"Лекция","slides_total":1,"slides_read":1,
           "complete":True,"slides":[{"number":1,"text":"Понятие алгоритмов"}]}}}
        with patch.object(slides,"read_cache",return_value=c),\
             patch.object(slides,"public_sources",return_value={PUBLIC:{"cmid":100}}):
            res=slides.find("алгоритмов")
        self.assertEqual(len(res),1)
        self.assertIn("алгоритмов",res[0]["excerpt"])
        self.assertEqual(res[0]["slide_number"],1)

    def test_index_sources_respect_current_visibility(self):
        c={"items":[
            {"course_id":1,"cmid":2,"course":"Математика","title":"Лекция",
             "links":[{"url":PUBLIC}]},
            {"course_id":1,"cmid":3,"course":"Hidden",
             "links":[{"url":"https://docs.google.com/presentation/d/e/2PACX-Hidden/pubembed"}]}
        ]}
        with patch.object(slides.hybrid,"read_cache",return_value=c),\
             patch.object(slides.m,"read_credential",return_value={"token":"dummy"}),\
             patch.object(slides.m,"site_info",return_value=("dummy",{"userid":9})),\
             patch.object(slides.m,"get_courses",return_value=[{"id":1,"name":"Математика"}]),\
             patch.object(slides.hybrid,"modules_by_course",return_value={(1,2):{"type":"page"}}),\
             patch("moodle_embed_visibility.current_links",return_value={(1,2):{PUBLIC}}):
            result=slides.public_sources()
        self.assertEqual(list(result),[PUBLIC])


if __name__=="__main__":
    unittest.main(verbosity=2)
