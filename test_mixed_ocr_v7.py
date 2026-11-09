import io
import unittest
from unittest.mock import patch
import fitz
from PIL import Image,ImageDraw,ImageFont
import moodle_materials as mat
import moodle_ocr as ocr

class MixedPDF(unittest.TestCase):
 def test_scanned_page_in_mixed_pdf_is_recognized(self):
  img=Image.new("RGB",(1200,220),"white")
  draw=ImageDraw.Draw(img)
  font=ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf",54)
  draw.text((25,55),"Проверка МИСИС лекция 5",fill="black",font=font)
  b=io.BytesIO();img.save(b,"PNG")
  pdf=fitz.open()
  pdf.new_page().insert_text((40,50),"Searchable text document, course content. "*3)
  pdf.new_page(width=600,height=220).insert_image(fitz.Rect(0,0,600,220),stream=b.getvalue())
  content=pdf.tobytes();pdf.close()
  val=mat.text_from_pdf(content)
  self.assertIn("Searchable text document",val)
  self.assertIn("Страница 2 [OCR]",val)
  self.assertIn("Проверка МИСИС",val)
 def test_ocr_page_range_is_bounded(self):
  d=fitz.open();d.new_page();blob=d.tobytes();d.close()
  with self.assertRaises(ocr.OcrUnavailable):
   ocr.pdf_ocr(blob,page_numbers=list(range(9)))

if __name__=="__main__":
 unittest.main(verbosity=2)
