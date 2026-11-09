#!/usr/bin/env python3
"""Pure binary parser for legacy OLE PowerPoint PPT text atoms.

Runs within isolated .ocrvenv using olefile. No PowerPoint/Office COM,
no macros, no external resource loading or uploaded documents.
"""
import base64
import io
import json
import re
import struct
import sys
import olefile

MAX_INPUT = 32 * 1024 * 1024
MAX_STREAM = 90 * 1024 * 1024


def atoms(stream):
    fragments=[]
    count=0
    def walk(lo,hi,depth=0):
        nonlocal count
        if depth>20:return
        while lo+8<=hi and count<200000:
            count+=1
            verinst,typ,size=struct.unpack_from("<HHI",stream,lo)
            begin=lo+8
            end=begin+size
            if end>hi or size>MAX_STREAM:
                return
            if verinst & 15 == 15:
                walk(begin,end,depth+1)
            elif typ in (4000,4008):
                if typ==4000 and size%2==0:
                    value=stream[begin:end].decode("utf-16le","replace")
                elif typ==4008:
                    value=stream[begin:end].decode("latin1","replace")
                else:value=""
                value=re.sub(r"[\x00-\x08\x0b-\x1f]+"," ",value)
                value=re.sub(r"\s+"," ",value).strip()
                if len(value)>2:fragments.append(value[:15000])
            lo=end
    walk(0,len(stream))
    unique=[];seen=set()
    for text in fragments:
        if text not in seen:
            seen.add(text);unique.append(text)
    return "\n".join(unique)[:120000]


def extract(blob):
    if len(blob)>MAX_INPUT or not olefile.isOleFile(io.BytesIO(blob)):
        raise ValueError("not a valid legacy OLE PPT")
    with olefile.OleFileIO(io.BytesIO(blob),raise_defects=olefile.DEFECT_FATAL) as doc:
        if not doc.exists("PowerPoint Document"):
            raise ValueError("PowerPoint Document stream absent")
        if doc.get_size("PowerPoint Document")>MAX_STREAM:
            raise ValueError("PPT stream too large")
        contents=doc.openstream("PowerPoint Document").read(MAX_STREAM+1)
    return atoms(contents)


def main():
    request=json.load(sys.stdin)
    encoded=request.get("base64","")
    if not isinstance(encoded,str) or len(encoded)>MAX_INPUT*2:
        raise ValueError("input oversize")
    blob=base64.b64decode(encoded,validate=True)
    text=extract(blob)
    print(json.dumps({"text":text,"characters":len(text),"parser":"ppt_ole_text_atoms"},ensure_ascii=True))


if __name__=="__main__":
    try:main()
    except Exception as e:
        print(json.dumps({"error":type(e).__name__}),file=sys.stderr)
        sys.exit(2)
