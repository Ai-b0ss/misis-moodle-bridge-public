"""Offline binary PowerPoint atom extraction validation."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
import moodle_materials as mat

ROOT=Path(__file__).resolve().parent
VENV=ROOT/".ocrvenv"/"Scripts"/"python.exe"


class PPTTests(unittest.TestCase):
    def test_utf16_and_ascii_records_without_office(self):
        source=("import struct,json;"
                "from moodle_ppt_worker import atoms;"
                "a='Теорема МИСИС'.encode('utf-16le');"
                "b=b'PowerPoint 2003';"
                "records=struct.pack('<HHI',0,4000,len(a))+a+"
                "struct.pack('<HHI',0,4008,len(b))+b;"
                "print(json.dumps({'result':atoms(records)},ensure_ascii=True))")
        result=subprocess.run([str(VENV),"-c",source],
                              cwd=str(ROOT),capture_output=True,
                              encoding="utf-8",text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        text=json.loads(result.stdout)["result"]
        self.assertIn("Теорема МИСИС",text)
        self.assertIn("PowerPoint 2003",text)

    def test_refuses_unknown_format(self):
        with self.assertRaises(mat.MaterialError):
            mat.extract_text(b"\x00", "video/mp4")


if __name__=="__main__":
    unittest.main(verbosity=2)
