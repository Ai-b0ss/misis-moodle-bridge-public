#!/usr/bin/env python3
"""Encrypted local content search for student-visible Moodle PDFs, DOCX and PPTX.

Uses Moodle's documented pluginfile endpoint with tokens sent exclusively to
newlms.misis.ru. No redirects. Does not save file binaries or credential URLs.
Does not touch quizzes, attempts, submissions, completion or read receipts.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import unescape
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request
from xml.etree import ElementTree
import zipfile

import moodle_client as m

CACHE = m.HERE / ".moodle_materials.dpapi"
FORMAT_VERSION = 1
MIME_EXT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
}
MAX_BYTES = 32 * 1024 * 1024
MAX_XML_MEMBER = 8 * 1024 * 1024
MAX_XML_SUM = 48 * 1024 * 1024
MAX_TEXT = 120_000
MAX_PAGES = 250
MAX_CACHE_AGE = 7 * 24 * 3600
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


class MaterialError(Exception):
    pass


def canonical_file_url(url):
    """Reject untrusted host, embedded creds, invalid paths and unsafe schemes."""
    if not isinstance(url, str):
        raise MaterialError("missing url")
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname != "newlms.misis.ru" or
            p.username is not None or p.password is not None or
            p.port not in (None, 443) or
            not p.path.startswith("/webservice/pluginfile.php/") or
            p.fragment):
        raise MaterialError("untrusted file address")
    q = parse_qsl(p.query, keep_blank_values=True)
    if any(k.lower() in ("token", "wstoken", "password", "key") for k, v in q):
        raise MaterialError("credential-bearing source URL")
    if any(k.lower() not in ("forcedownload", "preview") for k, v in q):
        raise MaterialError("unexpected file URL parameter")
    return urlunsplit(("https", "newlms.misis.ru", p.path, urlencode(q), ""))


def download(url, token, expected_size, max_bytes=MAX_BYTES, opener=None):
    if not isinstance(expected_size, int) or not 0 < expected_size <= max_bytes:
        raise MaterialError("file exceeds configured size or unknown size")
    clean = canonical_file_url(url)
    p = urlsplit(clean)
    params = parse_qsl(p.query)
    params.append(("token", token))
    signed = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))
    # m.SAFE_OPENER does not follow any HTTP redirects (including same-host).
    opener = opener or m.SAFE_OPENER
    try:
        req = Request(signed, headers={"Accept": "application/octet-stream",
                                       "User-Agent": "MISIS-Moodle-Indexer/1.0"})
        with opener.open(req, timeout=20) as response:
            if response.status != 200:
                raise MaterialError("unexpected HTTP status")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise MaterialError("server response too large")
            data = response.read(max_bytes + 1)
    except MaterialError:
        raise
    except Exception as exc:
        # No URLs/Request objects in exception messages, ever.
        raise MaterialError("network or permission error: " + type(exc).__name__) from None
    if len(data) > max_bytes:
        raise MaterialError("download too large")
    if len(data) != expected_size:
        raise MaterialError("file length mismatches Moodle metadata")
    return data


def text_from_zip(data, ext):
    if not data.startswith(b"PK\x03\x04"):
        raise MaterialError("wrong OOXML signature")
    out = []
    total_xml = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.infolist()
            if len(names) > 2000:
                raise MaterialError("too many OOXML parts")
            candidates = []
            for info in names:
                path = info.filename
                if ext == "docx":
                    match = path == "word/document.xml" or bool(re.fullmatch(
                        r"word/(?:header|footer)\d+\.xml", path))
                else:
                    match = bool(re.fullmatch(r"ppt/slides/slide\d+\.xml", path))
                if not match:
                    continue
                if info.file_size > MAX_XML_MEMBER:
                    raise MaterialError("XML member too large")
                total_xml += info.file_size
                if total_xml > MAX_XML_SUM:
                    raise MaterialError("decompressed XML exceeds limit")
                candidates.append((path, info))
            if ext == "pptx":
                candidates.sort(key=lambda x: int(re.search(r"slide(\d+)\.xml", x[0]).group(1)))
            for path, info in candidates:
                with z.open(info, "r") as fd:
                    xml = fd.read(MAX_XML_MEMBER + 1)
                if len(xml) > MAX_XML_MEMBER or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise MaterialError("unsafe XML content")
                root = ElementTree.fromstring(xml)
                tag = "w:t" if ext == "docx" else "a:t"
                # Limit total stored text; continue no further parsing when full.
                piece = " ".join((x.text or "") for x in root.findall(".//" + tag, NS))
                if piece:
                    if ext == "pptx":
                        out.append("Слайд " + re.search(r"slide(\d+)\.xml", path).group(1) + ": " + piece)
                    else:
                        out.append(piece)
                if sum(map(len, out)) >= MAX_TEXT:
                    break
    except (zipfile.BadZipFile, ElementTree.ParseError, OSError, ValueError) as exc:
        raise MaterialError("damaged document: " + type(exc).__name__) from None
    return " \n".join(out)[:MAX_TEXT].strip()


def text_from_pdf(data):
    if not data.startswith(b"%PDF"):
        raise MaterialError("wrong PDF signature")
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            if doc.needs_pass:
                raise MaterialError("encrypted PDF")
            parts = []
            total = 0
            image_only_pages=[]
            for i in range(min(len(doc), MAX_PAGES)):
                page=doc[i]
                value=page.get_text()
                parts.append("Страница " + str(i + 1) + ": " + value)
                # Mixed PDFs can contain scanned pages even when other pages
                # have searchable text; remember at most eight image-heavy ones.
                if len(value.strip())<45 and len(image_only_pages)<8 and page.get_images(full=True):
                    image_only_pages.append(i)
                total += len(value)
                if total >= MAX_TEXT:
                    break
            result = "\n".join(parts)[:MAX_TEXT].strip()
            import moodle_ocr
            if image_only_pages or not moodle_ocr.has_searchable_pdf_text(result):
                try:
                    if image_only_pages:
                        recognized = moodle_ocr.pdf_ocr(data,page_numbers=image_only_pages)
                    else:
                        recognized = moodle_ocr.pdf_ocr(data,max_pages=3)
                    if recognized:
                        return (result+"\n"+recognized)[:MAX_TEXT]
                except (moodle_ocr.OcrUnavailable, OSError, ValueError):
                    pass  # preserve original readable text if optional OCR fails
            return result
        finally:
            doc.close()
    except ImportError:
        try:
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(data), strict=False)
            if pdf.is_encrypted:
                raise MaterialError("encrypted PDF")
            parts = []
            total = 0
            for i, page in enumerate(pdf.pages):
                if i >= MAX_PAGES or total >= MAX_TEXT:
                    break
                value = page.extract_text() or ""
                parts.append("Страница " + str(i + 1) + ": " + value)
                total += len(value)
            return "\n".join(parts)[:MAX_TEXT].strip()
        except ImportError:
            raise MaterialError("PDF reader not installed") from None
    except MaterialError:
        raise
    except Exception as exc:
        raise MaterialError("PDF parsing failed: " + type(exc).__name__) from None


def text_from_ppt(data):
    import base64, subprocess
    worker=m.HERE / "moodle_ppt_worker.py"
    exe=m.HERE / ".ocrvenv" / "Scripts" / "python.exe"
    if not worker.exists() or not exe.exists() or len(data)>MAX_BYTES:
        raise MaterialError("legacy PPT parser unavailable")
    payload=json.dumps({"base64":base64.b64encode(data).decode("ascii")})
    try:
        p=subprocess.run([str(exe),str(worker)],input=payload,capture_output=True,
                         text=True,encoding="utf-8",timeout=45,
                         creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except (subprocess.TimeoutExpired, OSError):
        raise MaterialError("legacy PPT worker failed") from None
    if p.returncode:
        raise MaterialError("legacy PPT was not readable")
    try:
        response=json.loads(p.stdout)
        return response.get("text","")[:MAX_TEXT]
    except (ValueError,TypeError):
        raise MaterialError("legacy PPT parser returned invalid output") from None


def extract_text(data, mime):
    ext = MIME_EXT.get(mime)
    if ext == "pdf":
        return text_from_pdf(data)
    if ext in ("docx", "pptx"):
        return text_from_zip(data, ext)
    if ext == "ppt":
        return text_from_ppt(data)
    raise MaterialError("unsupported format")


def read_cache():
    if not CACHE.exists():
        return {"version": FORMAT_VERSION, "files": {}}
    try:
        body = json.loads(m.dpapi(CACHE.read_bytes(), False).decode("utf-8"))
        if body.get("version") != FORMAT_VERSION or not isinstance(body.get("files"), dict):
            raise MaterialError("index version mismatch")
        return body
    except (ValueError, UnicodeError, KeyError, OSError) as exc:
        raise MaterialError("encrypted index cannot be read") from None


def write_cache(cache):
    # DPAPI encryption plus atomic replacement. No unencrypted intermediary file.
    blob = m.dpapi(json.dumps(cache, ensure_ascii=False).encode("utf-8"), True)
    temp = CACHE.with_suffix(".tmp")
    try:
        with open(temp, "wb") as f:
            f.write(blob)
        temp.replace(CACHE)
    finally:
        temp.unlink(missing_ok=True)


def make_key(url, size):
    clean = canonical_file_url(url)
    return hashlib.sha256((clean + "|" + str(size)).encode("utf-8")).hexdigest()


def inventory(token, site):
    courses = m.get_courses(token, site["userid"])
    files = {}
    for c in courses:
        sections = m.call(token, "core_course_get_contents", {"courseid": c["id"]})
        for sec in sections:
            for mod in sec.get("modules", []):
                if not isinstance(mod, dict) or mod.get("uservisible") is False:
                    continue
                for content in mod.get("contents", []) or []:
                    if not isinstance(content, dict):
                        continue
                    mime = content.get("mimetype")
                    url = content.get("fileurl")
                    size = content.get("filesize")
                    if mime not in MIME_EXT or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
                        continue
                    try:
                        key = make_key(url, size)
                    except MaterialError:
                        continue
                    if key not in files:
                        files[key] = {"url": url, "size": size, "mime": mime,
                                      "course": c["name"], "course_id": c["id"],
                                      "title": mod.get("name", ""), "cmid": mod.get("id"),
                                      "name": content.get("filename", "")}
    return files


def index(limit=15, max_megabytes=75, refresh_scans=False):
    token, site = m.site_info(m.read_credential())
    known = read_cache()
    found = inventory(token, site)
    budget = min(max_megabytes, 400) * 1024 * 1024
    indexed = 0
    transferred = 0
    failures = Counter()
    pending = sorted(
        ((key, f) for key, f in found.items()
         if key not in known["files"] or known["files"][key].get("state") == "failed"
         or (time.time() - known["files"][key].get("indexed_at", 0)) >= MAX_CACHE_AGE
         or (refresh_scans and f.get("mime")=="application/pdf")),
        key=lambda entry: entry[1]["size"],
    )
    for key, f in pending:
        if indexed >= limit or transferred + f["size"] > budget:
            break
        try:
            data = download(f["url"], token, f["size"])
            value = extract_text(data, f["mime"])
            record = {k: f[k] for k in ("course", "course_id", "title", "cmid", "name", "mime", "size")}
            record.update({"state": "indexed" if value else "no_extractable_text",
                           "text": value, "indexed_at": int(time.time())})
        except MaterialError as exc:
            reason = str(exc)
            record = {k: f[k] for k in ("course", "course_id", "title", "cmid", "name", "mime", "size")}
            record.update({"state": "failed", "reason": reason, "indexed_at": int(time.time())})
            failures[reason] += 1
        known["files"][key] = record
        indexed += 1
        transferred += f["size"]
        if indexed % 5 == 0:
            write_cache(known)
    write_cache(known)
    return {"available_supported_files": len(found), "processed_this_run": indexed,
            "bytes_listed_this_run": transferred, "index_size": len(known["files"]),
            "states": dict(Counter(x.get("state") for x in known["files"].values())),
            "failures_this_run": dict(failures), "remaining": len(
                [key for key in found if key not in known["files"] or known["files"][key].get("state") == "failed"])}


def find(query, limit=20, validate_access=True):
    terms = [x.casefold() for x in query.split() if x]
    if not terms:
        raise ValueError("search query is empty")
    data = read_cache()
    # Fail closed: remove cached files that are no longer listed as student-visible.
    if validate_access:
        token, site = m.site_info(m.read_credential())
        allowed = set(inventory(token, site))
    else:
        allowed = set(data["files"])
    rows = []
    for key, record in data["files"].items():
        if key not in allowed:
            continue
        corpus = " ".join(str(record.get(k) or "") for k in ("title", "name", "course", "text"))
        if not all(t in corpus.casefold() for t in terms):
            continue
        out = {k: record.get(k) for k in ("course", "course_id", "cmid", "title", "name", "mime", "size", "state")}
        text = record.get("text") or ""
        hits = [text.casefold().find(t) for t in terms]
        hits = [h for h in hits if h >= 0]
        if hits:
            start = max(0, min(hits) - 55)
            out["excerpt"] = re.sub(r"\s+", " ", text[start:start + 260])
        rows.append(out)
        if len(rows) >= limit:
            break
    return rows


def status():
    data = read_cache()
    return {"total": len(data["files"]), "states": dict(
        Counter(x.get("state") for x in data["files"].values())),
        "indexed_text_characters": sum(len(x.get("text", "")) for x in data["files"].values())}


def run(argv=None):
    p = argparse.ArgumentParser(description="Secure local Moodle material search")
    p.add_argument("action", choices=("index", "find", "status"))
    p.add_argument("--query", default="")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--budget-mb", type=int, default=75)
    p.add_argument("--refresh-scans", action="store_true",
                   help="Re-read existing PDFs using per-page mixed-scan OCR")
    a = p.parse_args(argv)
    if not 1 <= a.limit <= 300 or not 1 <= a.budget_mb <= 400:
        p.error("limit must be 1..300 and budget 1..400")
    if a.action == "index":
        output = index(a.limit, a.budget_mb, refresh_scans=a.refresh_scans)
    elif a.action == "find":
        output = {"matches": find(a.query, a.limit)}
    else:
        output = status()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (MaterialError, m.MoodleError, OSError) as exc:
        print("Material index: " + str(exc), file=sys.stderr)
        sys.exit(2)
