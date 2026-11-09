#!/usr/bin/env python3
"""Isolated Cyrillic OCR worker; accepts in-memory PNG via stdin and returns JSON.

Requires only the separately installed .ocrvenv (RapidOCR PP-OCRv5 Cyrillic).
No Moodle credentials, no external uploads, no image files on disk.
"""
import base64
from contextlib import redirect_stdout
import io
import json
import logging
import sys
import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, OCRVersion, ModelType

MAX_IMAGES = 8
MAX_BYTES = 8*1024*1024


def main():
    src = json.load(sys.stdin)
    raw_images = src.get("images", [])
    if not isinstance(raw_images,list) or len(raw_images)>MAX_IMAGES:
        raise ValueError("Too many images")
    logging.disable(logging.INFO)
    with redirect_stdout(io.StringIO()):
        engine = RapidOCR(params={"Rec.lang_type": LangRec.CYRILLIC,
                                  "Rec.ocr_version": OCRVersion.PPOCRV5,
                                  "Rec.model_type": ModelType.MOBILE})
    results = []
    for encoded in raw_images:
        if not isinstance(encoded,str) or len(encoded)>MAX_BYTES*2:
            raise ValueError("Image input too large")
        data=base64.b64decode(encoded, validate=True)
        if len(data)>MAX_BYTES:
            raise ValueError("Decoded image too large")
        image=Image.open(io.BytesIO(data)).convert("RGB")
        if image.width*image.height>5_000_000:
            raise ValueError("Too many pixels")
        with redirect_stdout(io.StringIO()):
            out=engine(np.array(image))
        texts=list(out.txts or []) if out is not None else []
        scores=list(out.scores or []) if out is not None else []
        results.append({"text":" ".join(texts)[:30000],
                        "mean_confidence":round(sum(float(x) for x in scores)/len(scores),3)
                        if len(scores)>0 else 0.0})
    print(json.dumps({"engine":"PP-OCRv5 cyrillic","pages":results},ensure_ascii=True))


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(json.dumps({"error":type(exc).__name__}),file=sys.stderr)
        sys.exit(2)
