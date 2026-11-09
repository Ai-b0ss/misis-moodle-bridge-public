"""Offline security, format and search tests for Moodle materials."""
import io
import unittest
import zipfile
from unittest.mock import Mock, patch
import moodle_materials as mat


class FakeResponse:
    status = 200
    def __init__(self, data):
        self.data = data
        self.headers = {"Content-Length": str(len(data))}
    def read(self, n):
        return self.data[:n]
    def __enter__(self):
        return self
    def __exit__(self, typ, value, tb):
        return False


def tiny_ooxml(ext):
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as z:
        if ext == "docx":
            z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Привет, МИСИС!</w:t></w:r></w:p></w:body></w:document>')
        else:
            z.writestr("ppt/slides/slide2.xml", '<p:spTree xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Второй</a:t></p:spTree>')
            z.writestr("ppt/slides/slide1.xml", '<p:spTree xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Первый</a:t></p:spTree>')
    return data.getvalue()


class MaterialTests(unittest.TestCase):
    def test_rejects_non_moodle_host(self):
        for u in ("https://newlms.misis.ru.evil.org/webservice/pluginfile.php/path",
                  "http://newlms.misis.ru/webservice/pluginfile.php/a",
                  "https://user@newlms.misis.ru/webservice/pluginfile.php/a",
                  "https://newlms.misis.ru:444/webservice/pluginfile.php/a",
                  "https://newlms.misis.ru/pluginfile.php/a",
                  "https://newlms.misis.ru/webservice/pluginfile.php/a?token=secret",
                  "https://newlms.misis.ru/webservice/pluginfile.php/a?redirect=evil"):
            with self.subTest(url=u), self.assertRaises(mat.MaterialError):
                mat.canonical_file_url(u)

    def test_supported_url_and_token_only_to_host(self):
        u = "https://newlms.misis.ru/webservice/pluginfile.php/3/file.pdf?forcedownload=1"
        self.assertEqual(mat.canonical_file_url(u), u)
        result = []
        def capture(req, timeout):
            result.append(req.full_url)
            return FakeResponse(b"%PDF" + b"z"*15)
        data = mat.download(u, "hidden!", 19, opener=Mock(open=capture))
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith("https://newlms.misis.ru/"))
        self.assertIn("token=hidden%21", result[0])

    def test_size_guard_does_not_perform_network(self):
        u = "https://newlms.misis.ru/webservice/pluginfile.php/3/file.pdf"
        client = Mock()
        with self.assertRaises(mat.MaterialError):
            mat.download(u, "secret", 999999999, opener=client)
        client.open.assert_not_called()

    def test_reject_mismatched_response(self):
        u = "https://newlms.misis.ru/webservice/pluginfile.php/x"
        with self.assertRaisesRegex(mat.MaterialError, "length"):
            mat.download(u, "secret", 12, opener=Mock(open=lambda req, timeout: FakeResponse(b"wrong")))

    def test_pptx_and_docx_text(self):
        self.assertEqual(mat.text_from_zip(tiny_ooxml("docx"), "docx"), "Привет, МИСИС!")
        presentation = mat.text_from_zip(tiny_ooxml("pptx"), "pptx")
        self.assertTrue(presentation.startswith("Слайд 1: Первый"))
        self.assertIn("Слайд 2: Второй", presentation)

    def test_bomb_and_entity_guard(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"<x/>"*(mat.MAX_XML_MEMBER//4+1))
        with self.assertRaises(mat.MaterialError):
            mat.text_from_zip(data.getvalue(), "docx")
        data=io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("word/document.xml", b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><a>&e;</a>')
        with self.assertRaises(mat.MaterialError):
            mat.text_from_zip(data.getvalue(), "docx")

    def test_find_result_has_no_credential_url(self):
        with patch.object(mat, "read_cache", return_value={
            "version": 1, "files":{"abc":{"title":"Теория", "course":"Предмет",
                "text":"Вопросы по алгоритмам", "mime":"application/pdf","name":"lecture.pdf"}}}):
            result=mat.find("алгоритм",validate_access=False)
            self.assertEqual(len(result), 1)
            self.assertIn("алгоритм", result[0]["excerpt"])
            self.assertNotIn("url", result[0])
            self.assertEqual(mat.find("no match",validate_access=False), [])

    def test_cache_dpapi_roundtrip(self):
        data = {"version":mat.FORMAT_VERSION, "files":{"a":{"text":"abc"}}}
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as temp, patch.object(mat, "CACHE", Path(temp)/"index.dpapi"):
            mat.write_cache(data)
            self.assertNotIn(b"abc", mat.CACHE.read_bytes())
            self.assertEqual(mat.read_cache(), data)

    def test_revoked_file_excluded(self):
        stored = {"version": 1, "files": {"stale": {"title": "Лекция", "text": "алгоритмы"}}}
        with patch.object(mat, "read_cache", return_value=stored), patch.object(mat.m, "read_credential", return_value={"token":"fake"}), patch.object(mat.m, "site_info", return_value=("fake", {"userid": 1})), patch.object(mat, "inventory", return_value={}):
            self.assertEqual(mat.find("алгоритмы"), [])

    def test_invalid_format(self):
        with self.assertRaises(mat.MaterialError):
            mat.extract_text(b"invalid", "application/pdf")
        with self.assertRaises(mat.MaterialError):
            mat.extract_text(b"invalid", "application/octet-stream")


if __name__ == "__main__":
    unittest.main(verbosity=2)
