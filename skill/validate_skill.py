from pathlib import Path
import hashlib, re
main = Path(r"C:\Users\user\.codex\skills\misis-moodle\SKILL.md")
backup = Path(r"C:\Users\user\Documents\MISIS-Moodle-Bridge\skill\SKILL.md")
a, b = main.read_bytes(), backup.read_bytes()
assert a == b
text = a.decode("utf-8-sig")
assert re.search(r"\A---\r?\nname: misis-moodle\r?\ndescription: .+\r?\n---\r?\n", text)
assert (Path(r"C:\Users\user\Documents\MISIS-Moodle-Bridge")/"moodle_client.py").is_file()
for name in ["tasks", "courses", "full", "selftest", "mod_page_get_pages_by_courses", "mod_lesson_get_page_data"]:
    assert name in text, name
for forbidden in ["wstoken=", "password=", "Authorization: Bearer"]:
    assert forbidden not in text, forbidden
print("SKILL_VALID", len(a), "bytes", "sha256", hashlib.sha256(a).hexdigest())
print("COPIES_IDENTICAL", a == b)
