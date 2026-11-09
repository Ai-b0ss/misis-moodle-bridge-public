import io,json,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import moodle_h5p as h

def archive():
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w") as z:
        z.writestr("content/content.json",json.dumps({"cards":[{"text":"<b>Теорема Коши</b>"},{"text":"Предел функции"}]},ensure_ascii=False))
        z.writestr("h5p.json",json.dumps({"title":"Theory","mainLibrary":"H5P.Flashcards"}))
        z.writestr("unused.js","alert('not executed')")
    return b.getvalue()

class H5PTests(unittest.TestCase):
    def test_text_without_scripts(self):
        x=h.extract(archive())
        self.assertIn("Теорема Коши",x["text"])
        self.assertIn("Предел функции",x["text"])
        self.assertNotIn("alert",x["text"])
    def test_invalid_zip(self):
        with self.assertRaises(h.H5PError):h.extract(b"garbage")
    def test_missing_content(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,"w") as z:z.writestr("h5p.json","{}")
        with self.assertRaises(h.H5PError):h.extract(b.getvalue())
    def test_local_encryption(self):
        data={"version":1,"activities":{"1":{"text":"Лекция МИСИС"}}}
        with tempfile.TemporaryDirectory() as p,patch.object(h,"CACHE",Path(p)/"h5p.dpapi"):
            h.save_cache(data)
            self.assertNotIn("Лекция".encode(),h.CACHE.read_bytes())
            self.assertEqual(h.read_cache(),data)
    def test_not_visible(self):
        data={"version":1,"activities":{"1":{"course_id":2,"cmid":3,"course":"A","title":"X","text":"Коши","main_library":"H5P.Column"}}}
        with patch.object(h,"read_cache",return_value=data),patch.object(h.m,"read_credential",return_value={"token":"test"}),patch.object(h.m,"site_info",return_value=("test",{"userid":7})),patch.object(h,"available",return_value=[]):
            self.assertEqual(h.find("Коши"),[])
    def test_visible(self):
        data={"version":1,"activities":{"1":{"course_id":2,"cmid":3,"course":"A","title":"X","text":"Коши","main_library":"H5P.Column"}}}
        with patch.object(h,"read_cache",return_value=data),patch.object(h.m,"read_credential",return_value={"token":"test"}),patch.object(h.m,"site_info",return_value=("test",{"userid":7})),patch.object(h,"available",return_value=[({"id":1,"course":2,"coursemodule":3},{})]):
            self.assertEqual(h.find("Коши")[0]["cmid"],3)
if __name__=="__main__":
    unittest.main(verbosity=2)
