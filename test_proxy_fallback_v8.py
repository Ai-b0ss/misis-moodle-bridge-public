import io,json,socket,unittest
from unittest.mock import patch,MagicMock
from urllib.error import URLError,HTTPError
import moodle_client as m

class PrivateFallback(unittest.TestCase):
 def test_dead_local_proxy_and_private_dns_allowed(self):
  records=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("10.22.11.1",443))]
  with patch.object(m,"getproxies",return_value={"https":"http://127.0.0.1:7890"}),\
       patch.object(m.socket,"create_connection",side_effect=ConnectionRefusedError),\
       patch.object(m.socket,"getaddrinfo",return_value=records):
   self.assertTrue(m._can_fallback_to_private_moodle())
 def test_public_address_fails_closed(self):
  records=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",443))]
  with patch.object(m,"getproxies",return_value={"https":"http://127.0.0.1:7890"}),\
       patch.object(m.socket,"create_connection",side_effect=ConnectionRefusedError),\
       patch.object(m.socket,"getaddrinfo",return_value=records):
   self.assertFalse(m._can_fallback_to_private_moodle())
 def test_proxy_working_no_fallback(self):
  with patch.object(m,"getproxies",return_value={"https":"http://127.0.0.1:7890"}),\
       patch.object(m.socket,"create_connection",return_value=MagicMock()):
   self.assertFalse(m._can_fallback_to_private_moodle())
 def test_nonloopback_proxy_no_bypass(self):
  with patch.object(m,"getproxies",return_value={"https":"http://proxy.example.org:8080"}):
   self.assertFalse(m._can_fallback_to_private_moodle())
 def test_request_retries_only_moodle_with_private_opener(self):
  with patch.object(m.SAFE_OPENER,"open",side_effect=URLError(ConnectionRefusedError)),\
       patch.object(m,"_can_fallback_to_private_moodle",return_value=True),\
       patch.object(m.PRIVATE_MOODLE_OPENER,"open",return_value=io.BytesIO(b'{"ok":true}')) as direct:
   val=m.post("/webservice/rest/server.php",{"wsfunction":"read"})
  self.assertEqual(val,{"ok":True})
  self.assertEqual(direct.call_count,1)
  self.assertTrue(direct.call_args.args[0].full_url.startswith(m.BASE))
 def test_forbidden_http_error_never_retried(self):
  err=HTTPError(m.BASE,403,"forbidden",{},None)
  with patch.object(m.SAFE_OPENER,"open",side_effect=err),\
       patch.object(m.PRIVATE_MOODLE_OPENER,"open") as direct:
   with self.assertRaisesRegex(m.MoodleError,"HTTP 403"):
    m.post("/webservice/rest/server.php",{"wsfunction":"read"})
  direct.assert_not_called()
if __name__=="__main__":unittest.main(verbosity=2)
