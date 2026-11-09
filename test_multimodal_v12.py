import io,subprocess,unittest,zipfile
from PIL import Image,ImageDraw
import fitz
import moodle_multimodal as mm

def fixture_pptx():
 ns={'p':'http://schemas.openxmlformats.org/presentationml/2006/main',
     'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
     'c':'http://schemas.openxmlformats.org/drawingml/2006/chart',
     'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
 slide='''<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:c="{c}" xmlns:r="{r}">
 <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>АЛГЕБРА И ГРАФИК</a:t></a:r></a:p></p:txBody></p:sp>
 <a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Год</a:t></a:r></a:p></a:txBody></a:tc>
 <a:tc><a:txBody><a:p><a:r><a:t>2026</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>
 <c:chart r:id="rId5"/></p:spTree></p:cSld></p:sld>'''.format(**ns)
 chart='''<c:chartSpace xmlns:c="{c}"><c:chart><c:plotArea><c:barChart>
 <c:ser><c:tx><c:v>Выручка</c:v></c:tx><c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Q1</c:v></c:pt></c:strCache></c:strRef></c:cat>
 <c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>42</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser>
 </c:barChart></c:plotArea></c:chart></c:chartSpace>'''.format(**ns)
 rel='''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId5" Target="../charts/chart1.xml" Type="chart"/></Relationships>'''
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w') as z:
  z.writestr('ppt/slides/slide1.xml',slide)
  z.writestr('ppt/charts/chart1.xml',chart)
  z.writestr('ppt/slides/_rels/slide1.xml.rels',rel)
 return b.getvalue()

class UniversalMultimodalTests(unittest.TestCase):
 def test_pptx_chart_table_and_text(self):
  x=mm.analyze(fixture_pptx(),'pptx')
  self.assertEqual(x['total_slides'],1)
  self.assertIn('АЛГЕБРА',x['slides'][0]['text'])
  self.assertEqual(x['slides'][0]['tables'][0]['rows'][0],['Год','2026'])
  c=x['slides'][0]['charts'][0]
  self.assertTrue(c['data_available'])
  self.assertEqual(c['series'][0]['values'],['42'])
  self.assertIn('Q1',mm.flatten(x))
 def test_pdf_vector_and_text(self):
  d=fitz.open();p=d.new_page()
  p.insert_text((80,80),"Annual results 2026")
  p.draw_rect(fitz.Rect(80,110,210,180),color=(0,0,0))
  data=d.tobytes();d.close()
  r=mm.analyze(data,'pdf')
  self.assertEqual(r['total_pages'],1)
  self.assertIn('Annual results',r['pages'][0]['text'])
  self.assertGreaterEqual(r['pages'][0]['drawings'],1)
 def test_raster_image_metadata_no_semantic_hallucination(self):
  im=Image.new('RGB',(240,100),'white')
  d=ImageDraw.Draw(im);d.text((4,4),'graph 20',fill='black')
  out=io.BytesIO();im.save(out,'PNG')
  r=mm.analyze(out.getvalue(),'png')
  self.assertEqual(r['dimensions'],[240,100])
  self.assertEqual(r['visual_meaning'],'not_inferred')
  self.assertNotIn('chart_value',r)
 def test_invalid_mime_and_zipbomb(self):
  with self.assertRaises(mm.MultimodalError):mm.analyze(b'x','png')
  with self.assertRaises(mm.MultimodalError):mm.analyze(b'x','pptx')
  with self.assertRaises(mm.MultimodalError):mm.analyze(b'x','unknown')
  ioout=io.BytesIO()
  with zipfile.ZipFile(ioout,'w',zipfile.ZIP_DEFLATED) as z:
   z.writestr('ppt/slides/slide1.xml',b'a'*(71*1024*1024))
  with self.assertRaises(mm.MultimodalError):mm.analyze(ioout.getvalue(),'pptx')
 def test_video_samples(self):
  cmd=['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
       'testsrc=size=160x120:rate=1','-t','4','-c:v','mpeg4',
       '-movflags','frag_keyframe+empty_moov','-f','mp4','pipe:1']
  p=subprocess.run(cmd,capture_output=True,timeout=20)
  if p.returncode:self.skipTest('ffmpeg test source unavailable')
  r=mm.analyze(p.stdout,'mp4',interval_sec=2,max_frames=3)
  self.assertEqual(r['format'],'video')
  self.assertGreater(r['sampled_frames'],0)
  self.assertLessEqual(r['sampled_frames'],3)

if __name__=='__main__':unittest.main()
