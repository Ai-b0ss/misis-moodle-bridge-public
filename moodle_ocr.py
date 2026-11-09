#!/usr/bin/env python3
"""On-device Russian/English OCR helper for non-searchable Moodle PDFs.

PDF bytes and rendered page images never leave the Windows computer.
Uses a separate .ocrvenv worker via stdin/stdout (no plaintext disk cache).
"""
import base64
import io
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".ocrvenv" / "Scripts" / "python.exe"
WORKER = ROOT / "moodle_ocr_worker.py"


class OcrUnavailable(Exception):
    pass


def recognize_pngs(images):
    if not VENV_PY.exists() or not WORKER.exists():
        raise OcrUnavailable("Offline OCR dependencies not installed")
    if len(images)>8 or any(len(x)>8*1024*1024 for x in images):
        raise OcrUnavailable("Image batch exceeds safe limits")
    payload=json.dumps({"images":[base64.b64encode(x).decode("ascii") for x in images]})
    try:
        run=subprocess.run(
            [str(VENV_PY),str(WORKER)], input=payload,
            capture_output=True,text=True,encoding="utf-8",errors="replace",
            timeout=90, check=False,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
        )
    except (OSError,subprocess.TimeoutExpired):
        raise OcrUnavailable("OCR subprocess unavailable") from None
    if run.returncode:
        raise OcrUnavailable("OCR subprocess reported failure")
    try:
        data=json.loads(run.stdout)
    except ValueError:
        raise OcrUnavailable("OCR returned invalid JSON") from None
    if data.get("engine")!="PP-OCRv5 cyrillic" or len(data.get("pages",[]))!=len(images):
        raise OcrUnavailable("OCR returned unexpected output")
    return data["pages"]


def pdf_ocr(data, max_pages=3, page_numbers=None):
    import fitz
    if not data.startswith(b"%PDF"):
        raise OcrUnavailable("Invalid PDF signature")
    images=[]
    with fitz.open(stream=data,filetype="pdf") as document:
        if document.needs_pass:
            raise OcrUnavailable("Encrypted PDF")
        if page_numbers is None:
            numbers=list(range(min(len(document),max_pages,8)))
        else:
            if not isinstance(page_numbers,(list,tuple)) or len(page_numbers)>8 or not all(
                isinstance(i,int) and 0<=i<len(document) for i in page_numbers):
                raise OcrUnavailable("Invalid page selection")
            numbers=list(dict.fromkeys(page_numbers))
        for i in numbers:
            pix=document[i].get_pixmap(matrix=fitz.Matrix(1.55,1.55),alpha=False)
            if pix.width*pix.height>5_000_000:
                pix=document[i].get_pixmap(matrix=fitz.Matrix(1.15,1.15),alpha=False)
            images.append(pix.tobytes("png"))
    result=recognize_pngs(images)
    return "\n".join(f"Страница {i+1} [OCR]: {p['text']}"
                     for i,p in zip(numbers,result) if p.get("text")).strip()


def has_searchable_pdf_text(extracted):
    return len(re.sub(r"Страница\s+\d+:\s*", "", extracted or "").strip())>=60
