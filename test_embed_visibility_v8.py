import unittest
from unittest.mock import patch
import moodle_embed_visibility as v
import moodle_slides as s
import moodle_drive as d

SLIDE="https://docs.google.com/presentation/d/e/2PACX-Example/pubembed"
DRIVE="https://drive.google.com/file/d/ABCDEFGHIJKLMN012345/preview"

class EmbeddedAccess(unittest.TestCase):
 def test_markup_extracts_links_without_script_or_text_links(self):
  html="<iframe src='"+SLIDE+"'></iframe><script>var x='"+DRIVE+"'</script><a href='"+DRIVE+"'>Preview</a>"
  x=v.parse_urls(html)
  self.assertEqual(x,{SLIDE,DRIVE})
 def test_slides_revoked_after_module_still_visible(self):
  cached={"items":[{"course_id":1,"cmid":2,"type":"page",
            "course":"Course","title":"Title","links":[{"url":SLIDE}]}]}
  with patch.object(s.hybrid,"read_cache",return_value=cached),\
       patch.object(s.m,"read_credential",return_value={"token":"fake"}),\
       patch.object(s.m,"site_info",return_value=("fake",{"userid":4})),\
       patch.object(s.m,"get_courses",return_value=[{"id":1,"name":"Course"}]),\
       patch.object(s.hybrid,"modules_by_course",return_value={(1,2):{"type":"page"}}),\
       patch.object(v,"current_links",return_value={(1,2):set()}):
   self.assertEqual(s.public_sources(),{})
 def test_drive_revoked_after_module_still_visible(self):
  cached={"items":[{"course_id":1,"cmid":2,"type":"page",
           "course":"Course","title":"Title","links":[{"url":DRIVE}]}]}
  with patch.object(d.h,"read_cache",return_value=cached),\
       patch.object(d.m,"read_credential",return_value={"token":"fake"}),\
       patch.object(d.m,"site_info",return_value=("fake",{"userid":4})),\
       patch.object(d.m,"get_courses",return_value=[{"id":1,"name":"Course"}]),\
       patch.object(d.h,"modules_by_course",return_value={(1,2):{"type":"page"}}),\
       patch.object(v,"current_links",return_value={(1,2):set()}):
   self.assertEqual(d.sources(),{})
if __name__=="__main__":unittest.main(verbosity=2)
