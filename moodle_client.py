#!/usr/bin/env python3
# MISIS Moodle, read-only client; Python standard library only.
import argparse
import ctypes
import ctypes.wintypes
import json
import getpass
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, getproxies
import ipaddress
from urllib.error import URLError, HTTPError

BASE = "https://newlms.misis.ru"
HERE = Path(__file__).resolve().parent
SECRET = HERE / ".moodle_credentials.dpapi"
REPORT = HERE / "moodle_report.json"
TIMEOUT = 16

class MoodleError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    """Never follow REST redirects with bearer credentials in the POST body."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


SAFE_OPENER = build_opener(NoRedirect)
# Only for the Moodle endpoint if a dead localhost proxy prevents access.
# This does NOT change the shared opener used by external file providers.
PRIVATE_MOODLE_OPENER = build_opener(ProxyHandler({}),NoRedirect)

def _can_fallback_to_private_moodle():
    """Fail closed unless local proxy is dead and Moodle DNS is private-only."""
    try:
        proxy=getproxies().get("https")
        if not proxy:return False
        p=urlsplit(proxy if "://" in proxy else "http://"+proxy)
        if p.hostname not in ("127.0.0.1","localhost","::1"):return False
        if not isinstance(p.port,int) or p.port<=0:return False
        try:
            with socket.create_connection((p.hostname,p.port),timeout=0.35):
                return False
        except OSError:
            pass
        addresses=socket.getaddrinfo("newlms.misis.ru",443,type=socket.SOCK_STREAM)
        return bool(addresses) and all(
            (lambda ip: ip.is_private and not ip.is_loopback and not ip.is_link_local)(
                ipaddress.ip_address(info[4][0])) for info in addresses)
    except (OSError,ValueError,TypeError):
        return False

class Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

def dpapi(data, encrypt):
    if os.name != "nt":
        raise MoodleError("Секреты поддерживаются только на Windows.")
    if not data:
        raise MoodleError("Пустые данные.")
    buff = ctypes.create_string_buffer(data)
    input_blob = Blob(len(data), ctypes.cast(buff, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = dll.CryptProtectData if encrypt else dll.CryptUnprotectData
    fn.restype = ctypes.wintypes.BOOL
    if encrypt:
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(input_blob), "MISIS Moodle", None, None, None, 1, ctypes.byref(output_blob))
    else:
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob))
    if not ok:
        raise MoodleError("Не удалось открыть защищённое хранилище Windows.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        k.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))

def save_credential(d):
    enc = dpapi(json.dumps(d, ensure_ascii=False).encode("utf-8"), True)
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".moodle_tmp_")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(enc)
        os.replace(tmp, SECRET)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def read_credential():
    if not SECRET.exists():
        raise MoodleError("Вход не настроен. Открой MISIS-Moodle-Login.cmd на рабочем столе.")
    try:
        return json.loads(dpapi(SECRET.read_bytes(), False).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError) as e:
        raise MoodleError("Не удалось расшифровать локальные учётные данные.") from e

def post(path, data):
    request = Request(BASE + path, data=urlencode(data).encode("utf-8"),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json",
                               "User-Agent": "MISIS-Moodle-ReadOnly/1.0"})
    try:
        try:
            with SAFE_OPENER.open(request, timeout=TIMEOUT) as response:
                raw = response.read(6 * 1024 * 1024 + 1)
        except HTTPError:
            raise
        except URLError:
            if not _can_fallback_to_private_moodle():
                raise
            # Host is fixed BASE, HTTPS certificate validation remains enabled;
            # never use this direct opener for Yandex/Google or another site.
            with PRIVATE_MOODLE_OPENER.open(request, timeout=TIMEOUT) as response:
                raw = response.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise MoodleError("Ответ сервера превышает 6 МБ.")
        return json.loads(raw.decode("utf-8-sig"))
    except HTTPError as e:
        raise MoodleError("Сервер Moodle ответил HTTP " + str(e.code)) from e
    except (URLError, socket.timeout, TimeoutError) as e:
        raise MoodleError("Не удалось связаться с newlms.misis.ru.") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise MoodleError("API Moodle вернул ответ не в формате JSON.") from e

def error_text(response):
    code = response.get("errorcode", "")
    if code in ("invalidlogin", "invalidusername", "invalidpassword"):
        return "Логин/пароль не подходят или вход через API запрещён."
    if code in ("servicenotavailable", "enablewsdescription", "webservicesdisabled"):
        return "В Moodle МИСИС отключён сервис мобильного API."
    if code in ("invalidtoken", "accessexception", "nopermissions"):
        return "Ключ доступа недействителен либо нет прав."
    return "Ошибка Moodle" + (" (" + str(code) + ")" if code else "") + "."

def call(token, method, extra=None):
    params = {"wstoken": token, "wsfunction": method, "moodlewsrestformat": "json"}
    if extra:
        params.update(extra)
    result = post("/webservice/rest/server.php", params)
    if isinstance(result, dict) and ("exception" in result or "errorcode" in result):
        raise MoodleError(method + ": " + error_text(result))
    return result

def auth(username, password):
    result = post("/login/token.php", {"username": username, "password": password,
                                       "service": "moodle_mobile_app"})
    if not isinstance(result, dict) or not result.get("token"):
        raise MoodleError(error_text(result) if isinstance(result, dict) else "Неизвестный ответ Moodle.")
    return result["token"]

def site_info(credentials):
    token = credentials.get("token", "")
    try:
        info = call(token, "core_webservice_get_site_info")
    except MoodleError as e:
        if "Ключ доступа недействителен" not in str(e) or not credentials.get("password"):
            raise
        token = auth(credentials["username"], credentials["password"])
        info = call(token, "core_webservice_get_site_info")
        credentials["token"] = token
        save_credential(credentials)
    if not isinstance(info, dict) or not isinstance(info.get("userid"), int):
        raise MoodleError("API не сообщил идентификатор пользователя.")
    return token, info

def get_courses(token, user_id):
    data = call(token, "core_enrol_get_users_courses", {"userid": user_id})
    if not isinstance(data, list):
        raise MoodleError("Список курсов имеет неожиданный формат.")
    return [{"id": x["id"], "name": x.get("fullname", x.get("shortname", "")),
             "url": BASE + "/course/view.php?id=" + str(x["id"])}
            for x in data if isinstance(x, dict) and isinstance(x.get("id"), int)]

def get_assignments(token, courses):
    if not courses:
        return [], []
    try:
        response = call(token, "mod_assign_get_assignments",
                        {"courseids[" + str(i) + "]": c["id"] for i, c in enumerate(courses)})
    except MoodleError as e:
        return [], [str(e)]
    if not isinstance(response, dict):
        return [], ["Неожиданный формат ответа по заданиям."]
    names = {c["id"]: c["name"] for c in courses}
    tasks = []
    for course in response.get("courses", []):
        if not isinstance(course, dict):
            continue
        for a in course.get("assignments", []):
            if not isinstance(a, dict):
                continue
            tasks.append({"id": a.get("id"), "course": names.get(course.get("id"), ""),
                          "course_id": course.get("id"), "name": a.get("name"),
                          "due_at": a.get("duedate") or None,
                          "cutoff_at": a.get("cutoffdate") or None,
                          "url": BASE + "/mod/assign/view.php?id=" + str(a["cmid"])
                          if a.get("cmid") else None})
    tasks.sort(key=lambda x: (x["due_at"] is None, x["due_at"] or 0))
    warnings = [str(w.get("message", "Недоступен курс")) for w in response.get("warnings", [])
                if isinstance(w, dict)]
    return tasks, warnings

def check_submission(token, tasks, max_count=None):
    errors = []
    for task in tasks[:max_count]:
        if not isinstance(task["id"], int):
            continue
        try:
            x = call(token, "mod_assign_get_submission_status", {"assignid": task["id"]})
            attempt = (x.get("lastattempt") or {}) if isinstance(x, dict) else {}
            submission = (attempt.get("submission") or {})
            task["submission_status"] = submission.get("status", "unknown")
            feedback = (x.get("feedback") or {}) if isinstance(x, dict) else {}
            task["graded"] = feedback.get("grade") is not None
        except MoodleError as e:
            errors.append("Задание " + str(task["id"]) + ": " + str(e))
    return errors

def get_report(statuses=False, max_count=None):
    credential = read_credential()
    token, info = site_info(credential)
    courses = get_courses(token, info["userid"])
    tasks, warnings = get_assignments(token, courses)
    if statuses:
        warnings += check_submission(token, tasks, max_count)
    return {"source": BASE, "retrieved_at": int(time.time()),
            "user": {"id": info["userid"], "name": info.get("fullname")},
            "courses": courses, "assignments": tasks,
            "warnings": warnings,
            "limitations": ["Пока включены только задания типа Assign. Тесты, форумы и часть других заданий могут отсутствовать."]}

def main():
    if len(sys.argv)>1 and sys.argv[1] in ("video-frames-index","video-frames-status","video-frames-search"):
        import moodle_video_frames_index as fi
        action={"video-frames-index":"index","video-frames-status":"status",
                "video-frames-search":"search"}[sys.argv[1]]
        return fi.run([action]+sys.argv[2:])
    if len(sys.argv)>1 and sys.argv[1] in ("multimodal-index","multimodal-status","multimodal-search"):
        import moodle_multimodal_index as mi
        action={"multimodal-index":"index","multimodal-status":"status",
                "multimodal-search":"search"}[sys.argv[1]]
        return mi.run([action]+sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("videos-index", "videos-status", "videos-search"):
        import moodle_videos
        action={"videos-index":"index","videos-status":"status","videos-search":"search"}[sys.argv[1]]
        return moodle_videos.run([action]+sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("yadisk-index", "yadisk-status", "yadisk-search"):
        import moodle_yadisk
        action={"yadisk-index":"index","yadisk-status":"status","yadisk-search":"search"}[sys.argv[1]]
        return moodle_yadisk.run([action]+sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("h5p-index", "h5p-status", "h5p-search"):
        import moodle_h5p
        action = {"h5p-index":"index","h5p-status":"status","h5p-search":"search"}[sys.argv[1]]
        return moodle_h5p.run([action] + sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("drive-index", "drive-status", "drive-search"):
        import moodle_drive
        action = {"drive-index":"index","drive-status":"status","drive-search":"find"}[sys.argv[1]]
        return moodle_drive.run([action]+sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("slides-index", "slides-status", "slides-search"):
        import moodle_slides
        action = {"slides-index":"index","slides-status":"status","slides-search":"find"}[sys.argv[1]]
        return moodle_slides.run([action] + sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("hybrid-index", "hybrid-status", "hybrid-search"):
        import moodle_hybrid
        action = {"hybrid-index":"index","hybrid-status":"status","hybrid-search":"search"}[sys.argv[1]]
        return moodle_hybrid.run([action] + sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("lessons", "lesson", "start-practice"):
        import moodle_lessons
        action = {"lessons": "list", "lesson": "inspect", "start-practice": "start-practice"}[sys.argv[1]]
        try:
            return moodle_lessons.run([action] + sys.argv[2:])
        except (moodle_lessons.LessonError, MoodleError) as exc:
            print("ERROR: " + str(exc)[:180], file=sys.stderr)
            return 2
    if len(sys.argv) > 1 and sys.argv[1] in ("browser-index","browser-cache-status","browser-find"):
        import moodle_browser_index
        action={"browser-index":"index","browser-cache-status":"status",
                "browser-find":"search"}[sys.argv[1]]
        return moodle_browser_index.run([action]+sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("browser-status", "browser-read", "browser-login"):
        import moodle_browser
        action = {"browser-status": "status", "browser-read": "read", "browser-login": "login"}[sys.argv[1]]
        return moodle_browser.run([action] + sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("index-materials", "find-materials", "materials-status"):
        import moodle_materials
        action = {"index-materials": "index", "find-materials": "find", "materials-status": "status"}[sys.argv[1]]
        try:
            return moodle_materials.run([action] + sys.argv[2:])
        except (moodle_materials.MaterialError, MoodleError, OSError) as exc:
            print("ERROR: " + str(exc)[:180], file=sys.stderr)
            return 2
    if len(sys.argv) > 1 and sys.argv[1] in ("overview", "deadlines", "search", "grades", "audit"):
        import moodle_extended
        return moodle_extended.run(sys.argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "check", "courses", "tasks", "full", "logout", "selftest"])
    args = parser.parse_args()
    try:
        if args.command == "selftest":
            assert dpapi(dpapi(b"moodle-dpapi-test-123", True), False) == b"moodle-dpapi-test-123"
            print("OK: DPAPI roundtrip passed.")
            return 0
        if args.command == "logout":
            SECRET.unlink(missing_ok=True)
            print("Локально сохранённые данные удалены; серверный ключ не отозван.")
            return 0
        if args.command == "setup":
            if not sys.stdin.isatty():
                raise MoodleError("Для первого входа нужно открыть файл с рабочего стола.")
            print("Вход в Moodle МИСИС. Пароль не сохраняется; локально защищается только API-токен.")
            user = input("Логин Moodle: ").strip()
            if not user:
                raise MoodleError("Пустой логин.")
            password = getpass.getpass("Пароль (ввод скрыт): ")
            if not password:
                raise MoodleError("Пустой пароль.")
            token = auth(user, password)
            info = call(token, "core_webservice_get_site_info")
            if not isinstance(info, dict) or not isinstance(info.get("userid"), int):
                raise MoodleError("Moodle не подтвердил доступ к API.")
            save_credential({"username": user, "token": token})
            print("Успех! Авторизован:", info.get("fullname", "пользователь Moodle"))
            return 0
        report = get_report(args.command == "full")
        if args.command == "check":
            result = {"ok": True, "courses": len(report["courses"]),
                      "assignments": len(report["assignments"]), "warnings": report["warnings"]}
        elif args.command == "courses":
            result = {"user": report["user"], "courses": report["courses"]}
        elif args.command == "tasks":
            result = {"assignments": report["assignments"], "warnings": report["warnings"]}
        else:
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"ok": True, "path": str(REPORT),
                      "courses": len(report["courses"]), "assignments": len(report["assignments"]),
                      "warnings": report["warnings"]}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (MoodleError, OSError) as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nВход отменён.", file=sys.stderr)
        return 130

if __name__ == "__main__":
    sys.exit(main())
