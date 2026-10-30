"""
OptiMind in your language. Each language is one file here, <code>.json:

    {"language": "pt-BR", "name": "Português (Brasil)", "locale": "pt-BR",
     "messages": {"Close it": "Fechar",
                  "{n} processes": {"one": "{n} processo", "other": "{n} processos"}}}

The English text is the key (OptiMind is written in English), and a text that isn't translated
yet stays English. {name} marks a value filled in (a number, an app's name): a translation keeps
every one, in the order its language needs. A text with a number has a form for each plural
category of its language (one, few, many, other), under the English plural. A word that needs
another translation in another place is keyed with its context, "priority|Low".
packaging/i18n_keys.py lists every text and what a language still lacks.

The language is the one chosen in Settings, else Windows' own display language when OptiMind has
it, else English. The page gets its texts with the page itself (inject), so it's translated from
its first moment.

The agent's own texts are made with tx(), txn() and txc(): each is a Text, a string whose value
is the English text, so the agent, its log and the AI keep reading English. A Text remembers how
it was made, and the API translates it as it sends it (localize, app/main.py); a number in it is
written the reader's way (num, pct) and a list too (items). A file keeps a text in English with,
beside it, how to translate it (storable).
"""
from __future__ import annotations

import ctypes
import functools
import json
import logging
import re
from pathlib import Path

from app.config import IS_WINDOWS, settings

ENGLISH = "en"
_DIR = Path(__file__).resolve().parent
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_HTML_TAG = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_STORED = "_i18n"  # a file's "reason" has its translation recipe in "reason_i18n"


@functools.lru_cache(maxsize=None)
def _catalog(code: str) -> dict:
    if code == ENGLISH:
        return {"language": ENGLISH, "name": "English", "locale": "en", "messages": {}}
    return json.loads((_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _languages() -> dict[str, str]:
    found = {path.stem: _catalog(path.stem)["name"] for path in _DIR.glob("*.json")}
    return {ENGLISH: "English", **dict(sorted(found.items(), key=lambda kv: kv[1].casefold()))}


# The languages OptiMind speaks: code -> the language's own name, English first
LANGUAGES = _languages()


@functools.lru_cache(maxsize=1)
def windows_language() -> str | None:
    """Windows' display language ("pt-BR"), or None when Windows can't say. It changes only when
    you sign in again, so it's read once."""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(85)
        if kernel32.LCIDToLocaleName(kernel32.GetUserDefaultUILanguage(), buffer, 85, 0):
            return buffer.value
    except (AttributeError, OSError):
        pass
    return None


def match(tag: str | None) -> str | None:
    """The language OptiMind has for a language tag: the same one, else the same language of
    another country (es-MX -> es, pt-PT -> pt-BR; Chinese in simplified characters); None if none."""
    tag = (tag or "").strip()
    if not tag:
        return None
    by_lower = {code.lower(): code for code in LANGUAGES}
    if tag.lower() in by_lower:
        return by_lower[tag.lower()]
    base = tag.split("-")[0].lower()
    if base == "zh" and "zh-Hans" in LANGUAGES:
        return "zh-Hans"
    return next((code for code in LANGUAGES if code.split("-")[0].lower() == base), None)


def current() -> str:
    """The language OptiMind speaks now: the one chosen in Settings, else Windows' own, else English."""
    return match(settings.optimind_language) or match(windows_language()) or ENGLISH


_listeners: list = []


def on_change(listener) -> None:
    """listener() runs when another language is chosen in Settings: for what OptiMind draws itself
    (its tray menu)."""
    _listeners.append(listener)


def changed() -> None:
    """Another language was chosen: what OptiMind draws itself is drawn again in it."""
    for listener in list(_listeners):
        try:
            listener()
        except Exception:  # a tray icon that's gone, for one
            logging.getLogger("optimind.i18n").debug("Couldn't redraw in the new language", exc_info=True)


def messages(code: str | None = None) -> dict:
    return _catalog(code or current())["messages"]


def plural(code: str, n: float) -> str:
    """The plural category of the number `n` in a language: "one", "few", "many" or "other"
    (the CLDR rules, for whole numbers; a fraction is "other")."""
    if n != int(n):
        return "other"
    i = abs(int(n))
    base = code.split("-")[0]
    if base in ("ja", "ko", "zh"):
        return "other"
    if base in ("pt", "fr"):
        return "one" if i in (0, 1) else "other"
    if base in ("ru", "uk"):
        if i % 10 == 1 and i % 100 != 11:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    if base == "pl":
        if i == 1:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    return "one" if i == 1 else "other"


def _fill(text: str, params: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


# ------------------------------------------------------------ numbers the reader's way

# Each language's decimal mark, digit group mark, percent pattern and the fewest digits a number
# needs before it's grouped (CLDR: Spanish and Polish write 1234 but 12 345), as the page's
# Intl.NumberFormat writes them.
_NUMBER_FORMATS = {
    "en": (".", ",", "{}%", 4), "pt": (",", ".", "{}%", 4), "es": (",", ".", "{}\xa0%", 5),
    "de": (",", ".", "{}\xa0%", 4), "fr": (",", "\u202f", "{}\u202f%", 4), "it": (",", ".", "{}%", 4),
    "ru": (",", "\xa0", "{}\xa0%", 4), "pl": (",", "\xa0", "{}%", 5), "tr": (",", ".", "%{}", 4),
    "ja": (".", ",", "{}%", 4), "ko": (".", ",", "{}%", 4), "zh": (".", ",", "{}%", 4),
}


def _number_format(code: str) -> tuple[str, str, str, int]:
    return _NUMBER_FORMATS.get(code.split("-")[0], _NUMBER_FORMATS[ENGLISH])


def _local_digits(english: str, code: str) -> str:
    """A number Python wrote the English way ("1,234.5") with the language's marks ("1.234,5")."""
    if code == ENGLISH:
        return english
    decimal, group, _, fewest = _number_format(code)
    sign, body = ("-", english[1:]) if english.startswith("-") else ("", english)
    whole, dot, fraction = body.partition(".")
    grouped = "," in whole
    whole = whole.replace(",", "")
    if grouped and whole.isdigit() and len(whole) >= fewest:
        groups = []
        while len(whole) > 3:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        whole = group.join([whole, *groups])
    return sign + whole + (decimal + fraction if dot else "")


class Num:
    """A number in a text, written the reader's way: `spec` is how English writes it (Python's
    format spec: ",.1f" = 1,234.5) and each language uses its own marks (1.234,5 in Portuguese).
    A percentage gets its language's % too (50 % in German, %50 in Turkish)."""

    def __init__(self, value: float, spec: str = ",.0f", percent: bool = False) -> None:
        self.value, self.spec, self.percent = value, spec, percent

    def shown(self, code: str) -> str:
        text = _local_digits(format(self.value, self.spec), code)
        return _number_format(code)[2].format(text) if self.percent else text

    def __str__(self) -> str:
        return self.shown(ENGLISH)

    def __repr__(self) -> str:
        return f"Num({self.value!r}, {self.spec!r}{', percent=True' if self.percent else ''})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Num) and (self.value, self.spec, self.percent) == (other.value, other.spec, other.percent)


def num(value: float, spec: str = ",.0f") -> Num:
    """A number for a text: num(1234.5, ",.1f") is 1,234.5 in English, 1.234,5 in Portuguese."""
    return Num(value, spec)


def pct(value: float, spec: str = ".0f") -> Num:
    """A percentage for a text (value 28.4 -> "28%"; "28 %" in German, "%28" in Turkish)."""
    return Num(value, spec, percent=True)


def _count(n: float, code: str) -> str:
    """{n} of a text with a number: whole numbers grouped (1,234), at most two decimals otherwise."""
    if n == int(n):
        return _local_digits(format(int(n), ",d"), code)
    return _local_digits(format(n, ",.2f").rstrip("0").rstrip("."), code)


# ------------------------------------------------------------ texts

def _value(v, code: str) -> str:
    """A value filled into a text, in the language: a text or number translated, a name as it is."""
    if isinstance(v, Text):
        return v.shown(code)
    if isinstance(v, (Num, Items, Capital)):
        return v.shown(code)
    if isinstance(v, float) and not isinstance(v, bool):
        return _local_digits(str(v), code)
    return str(v)


class Text(str):
    """A text OptiMind shows. Its value is the English text, so the agent, its log and the AI read
    it as they always did; shown() gives it in a language. It remembers how it was made: the
    English text (the catalog's key), the values filled in, and for a text with a number its
    singular and the number."""

    def __new__(cls, key: str, values: dict | None = None, *, one: str | None = None, n: float | None = None,
                context: str | None = None) -> Text:
        values = dict(values or {})
        template = one if one is not None and n == 1 else key
        english = _fill(template, {**({"n": _count(n, ENGLISH)} if one is not None else {}),
                                   **{k: _value(v, ENGLISH) for k, v in values.items()}})
        self = super().__new__(cls, english)
        self.key, self.values, self.one, self.n, self.context = key, values, one, n, context
        return self

    def shown(self, code: str | None = None) -> str:
        """The text in a language (OptiMind's own when none is given)."""
        code = code or current()
        if code == ENGLISH:
            return str.__str__(self)
        found = messages(code).get(f"{self.context}|{self.key}" if self.context else self.key)
        if self.one is not None:
            if isinstance(found, dict):
                text = found.get(plural(code, self.n)) or found.get("other") or self.key
            elif isinstance(found, str):
                text = found  # a language without plural forms
            else:
                text = self.one if self.n == 1 else self.key
        else:
            text = found if isinstance(found, str) else self.key
        values = {k: _value(v, code) for k, v in self.values.items()}
        if self.one is not None:
            values.setdefault("n", _count(self.n, code))
        return _fill(text, values)

    def to_json(self) -> dict:
        """How to show this text in any language, for a file (load() reads it back)."""
        data: dict = {"$": "text", "t": self.key}
        if self.values:
            data["v"] = {k: _dump(v) for k, v in self.values.items()}
        if self.one is not None:
            data["one"], data["n"] = self.one, self.n
        if self.context:
            data["c"] = self.context
        return data


def tx(text: str, /, **values) -> Text:
    """`text` (English), with its {name}s filled in, shown in the reader's language."""
    return Text(text, values)


def txc(context: str, text: str, /, **values) -> Text:
    """A word whose translation depends on what it names (the "Low" of a priority, not of a
    chart): keyed "context|text", English `text` when it isn't translated."""
    return Text(text, values, context=context)


def txn(n: float, one: str, other: str, /, **values) -> Text:
    """A text with a number: the form `n` needs in the reader's language ("1 process", "2 processes";
    Polish and Russian have more forms). {n} is filled in too, written the reader's way."""
    return Text(other, values, one=one, n=n)


# How a list is written: "A, B" between the items, "A and B" before the last one (Japanese: "A、B")
_LIST_MIDDLE = txc("list", "{a}, {b}")
_LIST_END = txc("list", "{a} and {b}")


class Items:
    """A list in a text, the way the reader's language writes one: "A, B and C"."""

    def __init__(self, values) -> None:
        self.values = list(values)

    def shown(self, code: str) -> str:
        names = [_value(v, code) for v in self.values]
        if len(names) < 2:
            return names[0] if names else ""
        middle, end = _LIST_MIDDLE.shown(code), _LIST_END.shown(code)
        out = names[0]
        for name in names[1:-1]:
            out = _fill(middle, {"a": out, "b": name})
        return _fill(end, {"a": out, "b": names[-1]})

    def __str__(self) -> str:
        return self.shown(ENGLISH)

    def __repr__(self) -> str:
        return f"Items({self.values!r})"


def items(values) -> Items:
    """A list for a text: items(["A", "B", "C"]) is "A, B and C" in English, "A, B e C" in Portuguese."""
    return Items(values)


class Capital:
    """A text that starts a sentence: its first letter in capitals, in whatever language it's shown.
    (lower=True: a text that goes inside a sentence, its first letter small, except in German,
    which writes its nouns with a capital.)"""

    def __init__(self, value, lower: bool = False) -> None:
        self.value, self.lower = value, lower

    def shown(self, code: str) -> str:
        text = _value(self.value, code)
        if self.lower:
            return text if code.split("-")[0] == "de" else text[:1].lower() + text[1:]
        return text[:1].upper() + text[1:]

    def __str__(self) -> str:
        return self.shown(ENGLISH)


def lowercase(value) -> Capital:
    """A text inside a sentence: "After a restart" -> "takes effect: after a restart"."""
    return Capital(value, lower=True)


def joined(parts, sep: str = " ") -> Text:
    """Texts one after the other (a note's sentences, a result's clauses), each in the reader's
    language; parts that are empty or None are left out."""
    parts = [p for p in parts if p]
    return Text(sep.join(f"{{p{i}}}" for i in range(len(parts))), {f"p{i}": p for i, p in enumerate(parts)})


def sentence(text) -> Text:
    """A phrase as a sentence of its own: capitalized, with a full stop ("not measured" -> "Not measured.")."""
    return joined([Capital(text), "."], "")


def _dump(v):
    if isinstance(v, Text):
        return v.to_json()
    if isinstance(v, Num):
        return {"$": "num", "v": v.value, "s": v.spec, **({"p": True} if v.percent else {})}
    if isinstance(v, Items):
        return {"$": "items", "v": [_dump(x) for x in v.values]}
    if isinstance(v, Capital):
        return {"$": "capital", "v": _dump(v.value), **({"lower": True} if v.lower else {})}
    return v


def _undump(v):
    if isinstance(v, dict):
        kind = v.get("$")
        if kind == "text":
            return load(v)
        if kind == "num":
            return Num(v["v"], v.get("s", ",.0f"), bool(v.get("p")))
        if kind == "items":
            return Items(_undump(x) for x in v.get("v", []))
        if kind == "capital":
            return Capital(_undump(v.get("v")), bool(v.get("lower")))
    return v


def load(data: dict) -> Text:
    """A text from a file, as Text.to_json() wrote it."""
    return Text(data["t"], {k: _undump(v) for k, v in (data.get("v") or {}).items()},
                one=data.get("one"), n=data.get("n"), context=data.get("c"))


def recalled(english, recipe) -> str:
    """A file's text with its translation recipe (storable): the Text, when the recipe still makes
    exactly that English; else the English as it is (a field changed since, an unreadable recipe)."""
    if isinstance(english, str) and isinstance(recipe, dict) and recipe.get("$") == "text":
        try:
            text = load(recipe)
        except (KeyError, TypeError, ValueError, AttributeError):
            return english
        if str.__str__(text) == english:
            return text
    return english


def shown(value, code: str | None = None) -> str:
    """One text in the reader's language (a plain string stays as it is): for what OptiMind shows
    itself, its tray menu and dialogs."""
    if isinstance(value, (Text, Num, Items, Capital)):
        return value.shown(code or current())
    return "" if value is None else str(value)


def message(error: BaseException) -> str:
    """What an error says, as a text when it was raised with one (str(error) is only its English)."""
    first = error.args[0] if len(error.args) == 1 else None
    return first if isinstance(first, Text) else str(error)


def localize(value, code: str | None = None):
    """What the API answers (dicts, lists, texts…) with every text in the reader's language: a
    Text translated, and a file's text (a "reason" with its "reason_i18n") too."""
    return _localized(value, code or current())


def _localized(value, code: str):
    if isinstance(value, Text):
        return value.shown(code)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith(_STORED) and key[:-len(_STORED)] in value:
                continue  # it went into its text
            if isinstance(key, str) and f"{key}{_STORED}" in value:
                item = recalled(item, value[f"{key}{_STORED}"])
            out[key] = _localized(item, code)
        return out
    if isinstance(value, (list, tuple)):
        return [_localized(item, code) for item in value]
    if isinstance(value, (Num, Items, Capital)):
        return value.shown(code)
    return value


def storable(value):
    """`value` ready for a JSON file: each text as its English, with beside it ("text_i18n" for
    "text") how to show it in another language, so a file still reads as it always did."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, Text):
                out[key], out[f"{key}{_STORED}"] = str.__str__(item), item.to_json()
            else:
                out[key] = storable(item)
        return out
    if isinstance(value, (list, tuple)):
        return [storable(item) for item in value]
    if isinstance(value, Text):
        return str.__str__(value)
    return value


def restored(value):
    """What storable() wrote, read back: each text that has its recipe beside it is a Text again."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith(_STORED) and key[:-len(_STORED)] in value:
                continue  # it went into its text
            out[key] = recalled(item, value[f"{key}{_STORED}"]) if f"{key}{_STORED}" in value else restored(item)
        return out
    if isinstance(value, list):
        return [restored(item) for item in value]
    return value


def page_texts(code: str | None = None) -> dict:
    """What the page needs: the language, its locale (numbers, dates), the languages to choose from,
    and the translations."""
    code = code or current()
    data = _catalog(code)
    return {"language": code, "locale": data.get("locale", code), "chosen": settings.optimind_language,
            "windows": match(windows_language()) or ENGLISH,
            "languages": [{"code": c, "name": name} for c, name in LANGUAGES.items()],
            "messages": data["messages"]}


def inject(page: bytes) -> bytes:
    """The page with its language: <html lang>, and the translations in a script before its own
    (as window.OPTIMIND_I18N), so they're there before the page draws anything."""
    texts = page_texts()
    payload = json.dumps(texts, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = page.decode("utf-8")
    html = _HTML_TAG.sub(f'<html lang="{texts["language"]}">', html, count=1)
    at = html.find("<script")
    if at < 0:
        return html.encode("utf-8")  # no script to translate it
    return (html[:at] + f"<script>window.OPTIMIND_I18N={payload};</script>" + html[at:]).encode("utf-8")
