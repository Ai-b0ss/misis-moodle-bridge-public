import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import moodle_multimodal_index as mi
import moodle_video_frames_index as vi
from test_multimodal_v12 import fixture_pptx

class RealIndexIntegrationTests(unittest.TestCase):
 def test_structured_chart_table_acl_and_protected_cache(self):
  with tempfile.TemporaryDirectory() as t:
   cache=Path(t)/'protected.dpapi'
   record={'cmid':44,'mime':'application/vnd.openxmlformats-officedocument.presentationml.presentation',
           'size':123,'url':'https://newlms.misis.ru/webservice/pluginfile.php/test',
           'course_id':9,'course':'Synthetic','title':'Lecture','name':'fixture.pptx'}
   def fresh():return ('dummy',{'fingerprint':record})
   with patch.object(mi,'CACHE',cache),patch.object(mi,'_inventory',side_effect=fresh),\
        patch.object(mi.mat,'download',return_value=fixture_pptx()):
    result=mi.index(44)
    self.assertEqual(result['state'],'indexed')
    self.assertEqual(result['charts'],1)
    self.assertEqual(result['tables'],1)
    self.assertNotIn(b'AL',cache.read_bytes())
    charts=mi.search('42')
    self.assertTrue(any(x['evidence_type']=='chart' and x['number']==1 for x in charts))
    tables=mi.search('2026')
    self.assertTrue(any(x['evidence_type']=='table' for x in tables))
    with patch.object(mi,'_inventory',return_value=('dummy',{})):
     self.assertEqual(mi.search('42'),[])
 def test_reject_invisible_source_without_download(self):
  with patch.object(mi,'_inventory',return_value=('dummy',{})),\
       patch.object(mi.mat,'download',side_effect=AssertionError('No download')):
   self.assertEqual(mi.index(44)['state'],'no_supported_visible_file')

class FrameIndexIntegrationTests(unittest.TestCase):
 def test_frame_search_tracks_current_authorization(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'frames.dpapi'
   src={'cmid':99,'size':200,'course':'Test','name':'Video','source':'https://disk.yandex.ru/i/TEST'}
   with patch.object(vi,'CACHE',p),\
        patch.object(vi.vid,'available',return_value={'k':src}),\
        patch.object(vi.vid,'download_video_bounded',return_value=b'fake-video'),\
        patch.object(vi.mm,'video',return_value={'frames':[{'seconds_estimate':20,
             'image_analysis':{'ocr_text':'Опыты с графиками'}}],'sampled_frames':1}):
    r=vi.index(99,ocr=True)
    self.assertEqual(r['sampled_frames'],1)
    self.assertNotIn('графиками'.encode('utf-8'),p.read_bytes())
    self.assertEqual(len(vi.search('графиками')),1)
    with patch.object(vi.vid,'available',return_value={}):
     self.assertEqual(vi.search('графиками'),[])

if __name__=='__main__':unittest.main()
