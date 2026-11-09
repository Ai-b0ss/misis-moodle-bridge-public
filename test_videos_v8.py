import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
import moodle_videos as v

RECORD={"source":"https://disk.yandex.ru/i/ABCdef123","course_id":9,"cmid":10,
        "course":"Лекция МИСИС","name":"Лекция.mp4","module":"Лекция",
        "size":1000000,"mime":"video/mp4"}

class VideoTests(unittest.TestCase):
 def test_dpapi(self):
  d={"version":1,"videos":{"key":{"segments":[{"text":"Теорема Коши"}]}}}
  with tempfile.TemporaryDirectory() as folder,patch.object(v,"CACHE",Path(folder)/"video.dpapi"):
   v.save_cache(d)
   self.assertNotIn("Теорема Коши".encode("utf-8"),v.CACHE.read_bytes())
   self.assertEqual(v.read_cache(),d)
 def test_revoked_source_hidden(self):
  doc={"version":1,"videos":{"id":{**RECORD,"segments":[{"text":"История науки","start_sec":20}]}}}
  with patch.object(v,"read_cache",return_value=doc),patch.object(v,"available",return_value={}):
   self.assertEqual(v.search("науки"),[])
 def test_time_stamped_search(self):
  doc={"version":1,"videos":{"id":{**RECORD,"complete":True,
         "segments":[{"text":"История науки","start_sec":20,"end_sec":30}]}}}
  with patch.object(v,"read_cache",return_value=doc),\
       patch.object(v,"available",return_value={"id":RECORD}):
   x=v.search("науки")
  self.assertEqual(len(x),1)
  self.assertEqual(x[0]["start_sec"],20)
  self.assertEqual(x[0]["transcription"],"automatic_local_whisper")
 def test_large_video_never_downloaded(self):
  large={**RECORD,"size":3*1024*1024*1024}
  with tempfile.TemporaryDirectory() as folder,\
       patch.object(v,"CACHE",Path(folder)/"video.dpapi"),\
       patch.object(v,"available",return_value={"id":large}),\
       patch.object(v,"download_video_bounded",side_effect=AssertionError("Forbidden download")):
   stats=v.index(limit=2,clip_minutes=5,budget_mb=30)
   self.assertEqual(stats["metadata_only"],1)
 def test_audio_limits(self):
  with self.assertRaises(v.VideoError):v.local_audio({**RECORD,"size":130000000})
  with self.assertRaises(v.VideoError):v.local_audio(RECORD,seconds=5)
 def test_invalid_index_parameters(self):
  with self.assertRaises(v.VideoError):v.index(limit=10)
 def test_no_model_download(self):
  with patch.object(v,"MODEL_CACHE",Path("/missing/never-download-model.pt")):
   with self.assertRaises(v.VideoError):v.transcribe(np.zeros(16000,dtype=np.float32))

if __name__=="__main__":unittest.main(verbosity=2)
