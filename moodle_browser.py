#!/usr/bin/env python3
"""Optional browser-session fallback for authorized Moodle web pages.

This is a separate Chrome profile, not a clone of the user's normal browser.
No extraction/export of the user's normal Chrome cookies or password.
After successful user-initiated login, only this isolated Moodle profile's
session cookies are protected with Windows CurrentUser DPAPI and restored
on later browser launches. Browser GET
may create Moodle viewing events; never use automatically in bulk indexing.
"""
import argparse
import json
import os
import tempfile
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, parse_qsl

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

SITE = "https://newlms.misis.ru"
PROFILE = Path(__file__).resolve().parent / ".moodle_browser_profile"
STATE = Path(__file__).resolve().parent / ".moodle_browser_session.dpapi"
ALLOWED_PREFIXES = (
    "/course/view.php", "/mod/page/view.php", "/mod/resource/view.php",
    "/mod/lesson/view.php", "/mod/book/view.php", "/mod/assign/view.php",
    "/mod/quiz/view.php", "/mod/folder/view.php", "/mod/h5pactivity/view.php",
    "/mod/glossary/view.php", "/my/", "/login/index.php",
)


class BrowserError(Exception):
    pass


def validate_url(url):
    if not isinstance(url, str):
        raise BrowserError("URL is not a string")
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname != "newlms.misis.ru" or p.port not in (None,443):
        raise BrowserError("Only HTTPS Moodle MISIS pages are allowed")
    if p.username or p.password or p.fragment:
        raise BrowserError("Unsafe URL")
    if any(k.lower() in ("token", "wstoken", "sesskey", "password", "key") for k,v in parse_qsl(p.query)):
        raise BrowserError("Sensitive parameters forbidden in web URLs")
    if p.path not in ALLOWED_PREFIXES:
        raise BrowserError("Page type not on allowlist")
    return url


def guard_requests(route):
    p = urlsplit(route.request.url)
    if (p.scheme == "https" and p.hostname == "newlms.misis.ru"
            and p.port in (None,443) and not p.username and not p.password):
        return route.continue_()
    # Never use a Moodle-authenticated browser session on third-party sites.
    return route.abort()


def _restore_session(context):
    """Restore only isolated-profile Moodle cookies, never regular Chrome data."""
    if not STATE.exists():
        return
    import moodle_client as m
    try:
        raw=m.dpapi(STATE.read_bytes(), False)
        cookies=json.loads(raw.decode("utf-8"))
        if not isinstance(cookies,list) or len(cookies)>64:
            raise BrowserError("Session cookie data is invalid")
        for item in cookies:
            if not isinstance(item,dict) or item.get("domain","").lstrip(".") not in (
                    "newlms.misis.ru",):
                raise BrowserError("Unexpected cookie domain")
        if cookies:
            context.add_cookies(cookies)
    except (ValueError,OSError,UnicodeError,BrowserError) as exc:
        # Do not expose token/cookie bytes in an exception message.
        raise BrowserError("Protected Moodle session unreadable: "+type(exc).__name__) from None


def _save_session(context):
    """Persist Moodle-only cookies DPAPI-encrypted; never write plaintext cookies."""
    import moodle_client as m
    cookies=[x for x in context.cookies([SITE])
             if x.get("domain","").lstrip(".") == "newlms.misis.ru"]
    if not cookies:
        raise BrowserError("No Moodle session cookies available")
    data=json.dumps(cookies,ensure_ascii=False).encode("utf-8")
    encrypted=m.dpapi(data,True)
    fd,tmp=tempfile.mkstemp(dir=STATE.parent,prefix=".moodle_session_tmp_")
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(encrypted)
        os.replace(tmp,STATE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def open_session(headless=True):
    playwright = sync_playwright().start()
    try:
        PROFILE.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=headless,
            accept_downloads=False, args=["--disable-extensions"]
        )
        try:
            _restore_session(context)
        except Exception:
            context.close()
            raise
        return playwright, context
    except Exception:
        playwright.stop()
        raise


def check_session():
    # Never launch a second Chrome on an active interactive-login profile
    # when there is no successfully persisted protected session.
    if not STATE.exists():
        return {"profile_exists":PROFILE.exists(),"authorized":False,
                "current_path":None,"reason":"protected_session_missing"}
    playwright, context = open_session()
    try:
        page = context.new_page()
        page.route("**/*", guard_requests)
        page.goto(SITE + "/my/", wait_until="domcontentloaded", timeout=18000)
        dest = urlsplit(page.url)
        authorized=_is_moodle_dashboard(page.url)
        return {"profile_exists":True,"authorized":authorized,
                "current_path":dest.path,
                **({} if authorized else
                   {"reason":"session_expired" if dest.path.startswith("/login/")
                    else "unexpected_navigation"})}
    finally:
        context.close()
        playwright.stop()


def inspect(url):
    validate_url(url)
    if not STATE.exists():
        return {"state":"needs_browser_login","url_path":urlsplit(url).path,
                "reason":"protected_session_missing"}
    playwright, context = open_session()
    try:
        page = context.new_page()
        page.route("**/*", guard_requests)
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(400)
        dest = urlsplit(page.url)
        if dest.hostname != "newlms.misis.ru":
            raise BrowserError("Page navigated outside Moodle")
        if dest.path.startswith("/login/"):
            return {"state": "needs_browser_login", "url_path": dest.path}
        if response and response.status >= 400:
            return {"state": "http_error", "http": response.status}
        # Protect against silently indexing a redirect to a different Moodle
        # resource, an access-denied page or a course overview.
        requested=urlsplit(url)
        if dest.path != requested.path:
            return {"state":"unexpected_navigation","url_path":dest.path}
        if requested.path.startswith("/mod/"):
            actual_id=dict(parse_qsl(dest.query)).get("id")
            requested_id=dict(parse_qsl(requested.query)).get("id")
            if requested_id != actual_id:
                return {"state":"unexpected_navigation","url_path":dest.path}
        # Read only rendered content. Do not click forms or start quiz attempts.
        return {"state": "readable", "url_path": dest.path,
                "title": page.title()[:200],
                "text": re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=12000))[:50000],
                "links": [
                    {"title": l.get("text", "")[:150], "url": l["href"]}
                    for l in page.locator("a[href]").evaluate_all(
                        """nodes => nodes.map(n=>({text:n.innerText||"",href:n.href})).slice(0,150)"""
                    )
                    if urlsplit(l.get("url") or l.get("href", "")).hostname == "newlms.misis.ru"
                ][:80]}
    finally:
        context.close()
        playwright.stop()


def _is_moodle_dashboard(url):
    p=urlsplit(url)
    return (p.scheme=="https" and p.hostname=="newlms.misis.ru"
            and p.path.startswith("/my/"))


def authorize_interactive():
    # User signs in on the actual Moodle site in an isolated visible Chrome.
    # No password is received, inspected, logged or saved. No CLI Enter required.
    playwright, context = open_session(headless=False)
    try:
        page = context.new_page()
        page.goto(SITE + "/login/index.php", wait_until="domcontentloaded", timeout=18000)
        print("Moodle login is open. The session will be verified and saved automatically.",flush=True)
        deadline=time.monotonic()+900
        authorized=False
        while time.monotonic()<deadline:
            tabs=list(context.pages)
            if not tabs:
                break
            # Only test when a page has navigated to /my/. No repeated server polling
            # before the student logs in, and no reading passwords from form elements.
            if any(_is_moodle_dashboard(p.url) for p in tabs):
                probe=context.new_page()
                probe.route("**/*", guard_requests)
                try:
                    probe.goto(SITE+"/my/",wait_until="domcontentloaded",timeout=18000)
                    authorized=_is_moodle_dashboard(probe.url)
                    if authorized:
                        _save_session(context)  # encrypt before browser closes
                finally:
                    probe.close()
                break
            tabs[0].wait_for_timeout(900)
        print(json.dumps({"authorized":authorized,"session_persisted":authorized,
                          "mode":"user_interactive_login_auto_save"}),flush=True)
        return 0 if authorized else 2
    finally:
        context.close()
        playwright.stop()


def run(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["status","read","login"])
    p.add_argument("--url")
    a = p.parse_args(argv)
    if a.action == "status":
        value = check_session()
    elif a.action == "login":
        return authorize_interactive()
    else:
        if not a.url:
            p.error("--url required for read")
        value = inspect(a.url)
    print(json.dumps(value, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (BrowserError, BrowserTimeout, OSError) as exc:
        print("Browser: "+str(exc)[:220],file=sys.stderr)
        sys.exit(2)
