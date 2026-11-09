import io
import unittest
from unittest.mock import patch
import fitz
from PIL import Image,ImageDraw,ImageFont
import moodle_materials as mat
import moodle_ocr as ocr


class OCRTests(unittest.TestCase):
    def test_russian_scanned_pdf(self):
        image=Image.new("RGB",(1200,220),"white")
        draw=ImageDraw.Draw(image)
        font=ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf",56)
        draw.text((25,60),"Проверка МИСИС лекция 5",font=font,fill="black")
        png=io.BytesIO()
        image.save(png,"PNG")
        pdf=fitz.open()
        page=pdf.new_page(width=600,height=220)
        page.insert_image(fitz.Rect(0,0,600,220),stream=png.getvalue())
        content=pdf.tobytes()
        pdf.close()
        text=mat.text_from_pdf(content)
        self.assertIn("Проверка МИСИС",text)
        self.assertIn("[OCR]",text)

    def test_text_pdf_does_not_call_ocr(self):
        pdf=fitz.open()
        p=pdf.new_page()
        p.insert_text((40,40),"OpenAI Moodle reliable processing text "*3)
        data=pdf.tobytes();pdf.close()
        with patch.object(ocr,"pdf_ocr",side_effect=AssertionError("OCR not needed")):
            value=mat.text_from_pdf(data)
        self.assertIn("Moodle",value)

    def test_invalid_pdf(self):
        with self.assertRaises(mat.MaterialError):
            mat.text_from_pdf(b"not a pdf")


if __name__=="__main__":
    unittest.main(verbosity=2)
