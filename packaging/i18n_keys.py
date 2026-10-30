"""
OptiMind's texts to translate, and what each translation still lacks (app/i18n):

    python packaging/i18n_keys.py              # each language: texts missing, outdated or wrong
    python packaging/i18n_keys.py --template   # every text, as an empty catalog to translate (JSON)

The texts are the window's page (its HTML's own texts, title, aria-label and placeholder, and
every tx("…") and txn(n, "…", "…") in its script) and every tx()/txn() of the Python code
(app and run.py). The
English text is the key; a text with a number (txn) is keyed by its plural and needs a form for
each plural category of the language. tests/test_i18n.py runs the same checks.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "app" / "static" / "dashboard.html"
CATALOGS = ROOT / "app" / "i18n"
CODE = (ROOT / "app", ROOT / "run.py")  # OptiMind's Python code (run.py: its start-up errors)
ATTRIBUTES = ("title", "aria-label", "placeholder")  # the ones the page translates (translateStatic)
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# The plural categories each language needs (CLDR, for whole numbers and fractions)
CATEGORIES = {"ja": ["other"], "ko": ["other"], "zh": ["other"], "ru": ["one", "few", "many", "other"],
              "uk": ["one", "few", "many", "other"], "pl": ["one", "few", "many", "other"]}
DEFAULT_CATEGORIES = ["one", "other"]


NAMES = {"OptiMind", "OptiMind AI"}  # never translated
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Markup(HTMLParser):
    """The texts of the page's body as translateStatic sees them: each text node (trimmed) and
    the translated attributes, except inside translate="no" (names: products, licenses)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._stack: list[tuple[str, str | None]] = []  # open elements: (tag, its translate="…")
        self._body = False

    def _translated(self) -> bool:
        if not self._body or any(tag in ("script", "style") for tag, _ in self._stack):
            return False
        return next((t for _, t in reversed(self._stack) if t is not None), "yes") != "no"

    def _attributes(self, attrs):
        if self._translated():
            self.texts += [value.strip() for name, value in attrs if name in ATTRIBUTES and value and value.strip()]

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._body = True
        self._stack.append((tag, dict(attrs).get("translate")))
        self._attributes(attrs)
        if tag in _VOID:
            self._stack.pop()

    def handle_startendtag(self, tag, attrs):
        self._stack.append((tag, dict(attrs).get("translate")))
        self._attributes(attrs)
        self._stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        text = data.strip()
        if self._translated() and re.search(r"[^\W\d_]", text):
            self.texts.append(text)


def markup_texts(html: str) -> list[str]:
    parser = _Markup()
    parser.feed(html)
    return [t for t in parser.texts if t not in NAMES]


_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _decode(body: str) -> str:
    """A JavaScript string's text from its source (\\n, \\", \\u00e9…)."""
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            e = body[i + 1]
            if e == "u" and body[i + 2:i + 3] == "{":
                end = body.index("}", i)
                out.append(chr(int(body[i + 3:end], 16)))
                i = end + 1
            elif e in "ux":
                size = 4 if e == "u" else 2
                out.append(chr(int(body[i + 2:i + 2 + size], 16)))
                i += 2 + size
            else:
                out.append(_ESCAPES.get(e, e))
                i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _js_string(src: str, i: int) -> tuple[str, int] | None:
    """The JavaScript string literal starting at src[i] ("…" or '…'), and where it ends."""
    m = _STRING.match(src, i)
    if not m:
        return None
    return _decode(m.group(1) if m.group(1) is not None else m.group(2)), m.end()


def _arguments(src: str, i: int) -> tuple[list[str], int]:
    """The source of each argument of the call whose "(" is at src[i] (split at its own commas)."""
    depth, start, args, j = 0, i + 1, [], i
    while j < len(src):
        c = src[j]
        if c in "\"'":
            s = _js_string(src, j)
            j = s[1] if s else j + 1
            continue
        if c == "`":
            j = src.index("`", j + 1) + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(src[start:j])
                return [a.strip() for a in args], j
        elif c == "," and depth == 1:
            args.append(src[start:j])
            start = j + 1
        j += 1
    raise ValueError(f"unclosed call at {i}")


def _choices(arg: str) -> list[str]:
    """The texts an argument can be: a string, or the strings a condition chooses between
    (cond ? "A" : "B"); the strings it compares (=== "x") aren't texts."""
    out, j = [], 0
    while j < len(arg):
        if arg[j] in "\"'":
            s = _js_string(arg, j)
            if s is None:
                break
            before = arg[:j].rstrip()
            if not before or before[-1] in "?:(":
                out.append(s[0])
            j = s[1]
            continue
        j += 1
    return out


_CALL = re.compile(r"(?<![\w$.])(tx|txn|txc)\(")


def script_texts(script: str, where: str = "page") -> tuple[dict[str, dict], list[str]]:
    """The texts of tx()/txn()/txc() calls in JavaScript: {key: {"plural": bool, "one": str}},
    and the calls whose text isn't written out (so a translation can't know it)."""
    found: dict[str, dict] = {}
    problems = []
    for m in _CALL.finditer(script):
        if script[max(0, m.start() - 9):m.start()].endswith("function "):
            continue  # the definitions
        args, _ = _arguments(script, m.end() - 1)
        line = script[:m.start()].count("\n") + 1
        if m.group(1) == "txc":
            context, text = (_choices(args[0]), _choices(args[1])) if len(args) > 1 else ([], [])
            if len(context) != 1 or len(text) != 1 or args[1][:1] not in "\"'":
                problems.append(f"{where}:{line}: txc() needs its context and text written out")
                continue
            _add(found, f"{context[0]}|{text[0]}", False, None, problems, f"{where}:{line}")
        elif m.group(1) == "tx":
            texts = _choices(args[0]) if args else []
            if not texts or (args[0][:1] not in "\"'" and "?" not in args[0]):
                problems.append(f"{where}:{line}: tx({args[0] if args else ''}) has no text written out")
            for t in texts:
                _add(found, t, False, None, problems, f"{where}:{line}")
        else:
            one = _choices(args[1]) if len(args) > 2 else []
            other = _choices(args[2]) if len(args) > 2 else []
            if len(one) != 1 or len(other) != 1:
                problems.append(f"{where}:{line}: txn() needs its singular and plural written out")
                continue
            _add(found, other[0], True, one[0], problems, f"{where}:{line}")
    return found, problems


def _add(found, key, plural, one, problems, where):
    seen = found.get(key)
    if seen and seen["plural"] != plural:
        problems.append(f"{where}: {key!r} is used both with and without a number")
    elif seen and plural and seen["one"] != one:
        problems.append(f"{where}: {key!r} has two singulars ({seen['one']!r}, {one!r})")
    found.setdefault(key, {"plural": plural, "one": one})


def python_texts(roots=CODE) -> tuple[dict[str, dict], list[str]]:
    """The texts of tx()/txn()/txc() calls in the Python code (i18n.tx, or tx imported from it)."""
    found: dict[str, dict] = {}
    problems = []
    roots = (roots,) if isinstance(roots, Path) else roots
    for path in [p for root in roots for p in ([root] if root.is_file() else sorted(root.rglob("*.py")))]:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name not in ("tx", "txn", "txc") or (isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", None) != "i18n"):
                continue
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            strings = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            if name == "txc":
                if len(strings) < 2 or None in strings[:2]:
                    problems.append(f"{where}: txc() needs its context and text written out")
                    continue
                _add(found, f"{strings[0]}|{strings[1]}", False, None, problems, where)
            elif name == "tx":
                if not strings or strings[0] is None:
                    problems.append(f"{where}: tx() has no text written out")
                    continue
                _add(found, strings[0], False, None, problems, where)
            else:
                if len(strings) < 3 or None in strings[1:3]:
                    problems.append(f"{where}: txn() needs its singular and plural written out")
                    continue
                _add(found, strings[2], True, strings[1], problems, where)
    return found, problems


def page_texts(html: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """Every text of the window's page: its markup's and its script's."""
    html = PAGE.read_text(encoding="utf-8") if html is None else html
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    found, problems = script_texts(script)
    for text in markup_texts(html):
        _add(found, text, False, None, problems, "page markup")
    return found, problems


def all_texts() -> tuple[dict[str, dict], list[str]]:
    found, problems = page_texts()
    py, py_problems = python_texts()
    for key, info in py.items():
        _add(found, key, info["plural"], info["one"], problems, "app")
    return found, problems + py_problems


def categories(code: str) -> list[str]:
    return CATEGORIES.get(code.split("-")[0], DEFAULT_CATEGORIES)


def check(code: str, catalog: dict, texts: dict[str, dict]) -> list[str]:
    """What's wrong with one language's catalog: texts missing, extra (no longer used), with
    other {names} than the English, or without a form its plural categories need."""
    problems = []
    for field in ("language", "name", "locale", "messages"):
        if field not in catalog:
            problems.append(f"{code}: no {field!r}")
    if catalog.get("language") != code:
        problems.append(f"{code}: its file says it's {catalog.get('language')!r}")
    messages = catalog.get("messages", {})
    for key, info in texts.items():
        if key not in messages:
            problems.append(f"{code}: missing {key!r}")
            continue
        value, names = messages[key], set(PLACEHOLDER.findall(key))
        if info["plural"]:
            if not isinstance(value, dict):
                problems.append(f"{code}: {key!r} needs its plural forms ({', '.join(categories(code))})")
                continue
            for category in categories(code):
                form = value.get(category)
                if not isinstance(form, str) or not form.strip():
                    problems.append(f"{code}: {key!r} has no {category!r} form")
                elif set(PLACEHOLDER.findall(form)) - {"n"} != names - {"n"}:
                    problems.append(f"{code}: {key!r} ({category}) has {sorted(PLACEHOLDER.findall(form))}, the English {sorted(names)}")
            extra = set(value) - set(categories(code))
            if extra:
                problems.append(f"{code}: {key!r} has forms its language doesn't use: {sorted(extra)}")
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"{code}: {key!r} isn't translated")
        elif set(PLACEHOLDER.findall(value)) != names:
            problems.append(f"{code}: {key!r} has {sorted(set(PLACEHOLDER.findall(value)))}, the English {sorted(names)}")
    for key in messages:
        if key not in texts:
            problems.append(f"{code}: {key!r} is no longer used")
    return problems


def catalogs() -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(CATALOGS.glob("*.json"))}


def template(texts: dict[str, dict]) -> dict:
    return {key: ({"one": "", "other": ""} if info["plural"] else "") for key, info in sorted(texts.items(), key=lambda kv: kv[0].casefold())}


def main(argv: list[str]) -> int:
    texts, problems = all_texts()
    if "--template" in argv:
        print(json.dumps(template(texts), ensure_ascii=False, indent=1))
        return 0
    words = sum(len(k.split()) for k in texts)
    print(f"{len(texts)} texts ({words} words), {sum(i['plural'] for i in texts.values())} of them with a number")
    for p in problems:
        print(f"  {p}")
    bad = bool(problems)
    for code, catalog in catalogs().items():
        found = check(code, catalog, texts)
        print(f"{code}: {'complete' if not found else f'{len(found)} problems'}")
        for p in found[:50]:
            print(f"  {p}")
        bad = bad or bool(found)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
