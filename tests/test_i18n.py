"""OptiMind in other languages (app/i18n): every translation complete and with the English
{names} (packaging/i18n_keys.py), the language from Settings or Windows, plural forms, and the
page getting its texts. The other tests run in English (tests/__init__.py)."""
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from app import i18n
from app.config import SETTINGS_FILE, settings
from app.main import app
from tests import api_client

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("optimind_i18n_keys", ROOT / "packaging" / "i18n_keys.py")
keys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keys)


class CatalogTests(unittest.TestCase):
    def test_every_text_is_written_out(self):
        texts, problems = keys.all_texts()
        self.assertEqual(problems, [])
        self.assertGreater(len(texts), 1000)

    def test_every_language_is_complete(self):
        texts, _ = keys.all_texts()
        catalogs = keys.catalogs()
        self.assertIn("pt-BR", catalogs)
        for code, catalog in catalogs.items():
            with self.subTest(code):
                self.assertEqual(keys.check(code, catalog, texts), [])

    def test_languages(self):
        self.assertEqual(next(iter(i18n.LANGUAGES)), "en")  # English first, then by name
        self.assertEqual(i18n.LANGUAGES["pt-BR"], "Português (Brasil)")
        for code, catalog in keys.catalogs().items():
            with self.subTest(code):
                self.assertRegex(catalog["locale"], r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

    def test_the_check_finds_problems(self):
        texts = {"Close {app}": {"plural": False, "one": None}, "{n} files": {"plural": True, "one": "{n} file"}}
        good = {"language": "pt-BR", "name": "Português (Brasil)", "locale": "pt-BR",
                "messages": {"Close {app}": "Fechar {app}", "{n} files": {"one": "{n} arquivo", "other": "{n} arquivos"}}}
        self.assertEqual(keys.check("pt-BR", good, texts), [])
        bad = json.loads(json.dumps(good))
        bad["messages"]["Close {app}"] = "Fechar {aplicativo}"  # a {name} renamed
        del bad["messages"]["{n} files"]["other"]  # a plural form missing
        bad["messages"]["Old text"] = "Texto antigo"  # no longer used
        self.assertEqual(len(keys.check("pt-BR", bad, texts)), 3)
        russian = {"language": "ru", "name": "Русский", "locale": "ru",
                   "messages": {"Close {app}": "Закрыть {app}", "{n} files": {"one": "{n} файл", "other": "{n} файла"}}}
        self.assertEqual(len(keys.check("ru", russian, texts)), 2)  # Russian needs "few" and "many" too

    def test_script_texts(self):
        found, problems = keys.script_texts('tx("A"); tx(x ? "B {n}" : "C", { n }); txn(n, "{n} D", "{n} Ds"); '
                                            'txc("priority", "Low"); tx(label); x.tx("not a call of ours")')
        self.assertEqual(set(found), {"A", "B {n}", "C", "{n} Ds", "priority|Low"})
        self.assertEqual(found["{n} Ds"], {"plural": True, "one": "{n} D"})
        self.assertEqual(len(problems), 1)  # tx(label): a translation can't know it
        found, _ = keys.script_texts('tx(kind === "vsync" ? "Held by V-Sync" : "Held by a cap")')
        self.assertEqual(set(found), {"Held by V-Sync", "Held by a cap"})  # what it compares isn't a text
        found, _ = keys.script_texts(r'''tx("Apply \"{tweak}\"?"); tx('it\'s'); tx("café")''')
        self.assertEqual(set(found), {'Apply "{tweak}"?', "it's", "café"})
        _, problems = keys.script_texts('tx("Low"); txn(n, "Low", "Low")')
        self.assertEqual(len(problems), 1)  # one key with and without a number

    def test_markup_texts(self):
        html = ('<head><title>OptiMind AI</title></head><body><p>Hello <b>world</b></p><input placeholder="Search">'
                '<span title=" Tip ">&#xE7E8;</span><ul translate="no"><li>MIT · <span translate="yes">part of Windows</span>'
                '</li></ul><script>tx("x")</script><style>p { color: red }</style></body>')
        self.assertEqual(keys.markup_texts(html), ["Hello", "world", "Search", "Tip", "part of Windows"])


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.saved = settings.optimind_language

    def tearDown(self):
        settings.optimind_language = self.saved

    def test_match(self):
        for tag, code in (("pt-BR", "pt-BR"), ("PT-br", "pt-BR"), ("pt-PT", "pt-BR"), ("pt", "pt-BR"), ("en-GB", "en"),
                          ("en", "en"), ("", None), (None, None), ("xx-YY", None)):
            with self.subTest(tag):
                self.assertEqual(i18n.match(tag), code)

    def test_current(self):
        with mock.patch.object(i18n, "windows_language", return_value="pt-BR"):
            settings.optimind_language = ""
            self.assertEqual(i18n.current(), "pt-BR")  # Windows' own
            settings.optimind_language = "en"
            self.assertEqual(i18n.current(), "en")  # the one chosen in Settings wins
        with mock.patch.object(i18n, "windows_language", return_value="xx-YY"):
            settings.optimind_language = ""
            self.assertEqual(i18n.current(), "en")  # a language OptiMind doesn't have: English
        with mock.patch.object(i18n, "windows_language", return_value=None):
            settings.optimind_language = "zz"
            self.assertEqual(i18n.current(), "en")

    @unittest.skipUnless(sys.platform == "win32", "Windows' display language")
    def test_windows_language(self):
        self.assertRegex(i18n.windows_language(), r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")

    def test_texts(self):
        settings.optimind_language = "pt-BR"
        close = i18n.tx("Close")
        self.assertEqual(close, "Close")  # its value is the English: the agent, its log and the AI read that
        self.assertEqual(close.shown(), "Fechar")
        self.assertEqual(i18n.tx("Close {app}", app="Discord").shown(), "Fechar Discord")
        self.assertEqual(i18n.tx("Not translated {x}", x=1).shown(), "Not translated 1")
        self.assertEqual(i18n.txn(1, "{n} process", "{n} processes").shown(), "1 processo")
        self.assertEqual(i18n.txn(0, "{n} process", "{n} processes").shown(), "0 processo")  # Portuguese: 0 and 1 are singular
        self.assertEqual(i18n.txn(5, "{n} process", "{n} processes").shown(), "5 processos")
        self.assertEqual((i18n.txc("priority", "Low").shown(), i18n.tx("Low").shown()), ("Baixa", "Mínimo"))
        settings.optimind_language = "en"
        self.assertEqual(i18n.txn(1, "{n} process", "{n} processes").shown(), "1 process")
        self.assertEqual(i18n.txn(2, "{n} process", "{n} processes"), "2 processes")
        self.assertEqual(i18n.txc("priority", "Low").shown(), "Low")
        self.assertEqual(i18n.shown(i18n.tx("Close"), "pt-BR"), "Fechar")
        self.assertEqual((i18n.shown("a name"), i18n.shown(None)), ("a name", ""))

    def test_plural_rules(self):
        cases = {"en": {0: "other", 1: "one", 2: "other"}, "de": {1: "one", 2: "other"},
                 "pt-BR": {0: "one", 1: "one", 2: "other"}, "fr": {0: "one", 1: "one", 2: "other"},
                 "ru": {1: "one", 21: "one", 2: "few", 4: "few", 22: "few", 0: "many", 5: "many", 11: "many", 12: "many", 111: "many"},
                 "pl": {1: "one", 2: "few", 22: "few", 0: "many", 5: "many", 12: "many", 21: "many"},
                 "ja": {1: "other", 2: "other"}, "ko": {5: "other"}, "zh-Hans": {1: "other"}}
        for code, numbers in cases.items():
            for n, category in numbers.items():
                with self.subTest(code=code, n=n):
                    self.assertEqual(i18n.plural(code, n), category)
        self.assertEqual(i18n.plural("ru", 1.5), "other")  # a fraction

    def test_the_page_gets_its_texts(self):
        settings.optimind_language = "pt-BR"
        texts = i18n.page_texts()
        self.assertEqual((texts["language"], texts["locale"], texts["chosen"]), ("pt-BR", "pt-BR", "pt-BR"))
        self.assertIn({"code": "en", "name": "English"}, texts["languages"])
        fake = {"language": "pt-BR", "locale": "pt-BR", "chosen": "", "windows": "en", "languages": [],
                "messages": {"x": "</script><b>"}}
        page = b'<!doctype html><html lang="en" class="x"><head><script>var a;</script></head></html>'
        with mock.patch.object(i18n, "page_texts", return_value=fake):
            out = i18n.inject(page).decode("utf-8")
        self.assertIn('<html lang="pt-BR">', out)
        self.assertNotIn("</script><b>", out)  # a text can't end the script early
        self.assertLess(out.index("window.OPTIMIND_I18N="), out.index("var a;"))  # there before the page's script
        self.assertEqual(json.loads(re.search(r"window\.OPTIMIND_I18N=(.*?);</script>", out).group(1)), fake)
        self.assertEqual(i18n.inject(b"<p>no script</p>"), b"<p>no script</p>")


class TextTests(unittest.TestCase):
    """The agent's texts (tx/txn/txc): numbers and lists the reader's way, texts inside texts, and
    texts kept in files and sent by the API."""

    def test_numbers_the_readers_way(self):
        size = i18n.tx("{mb} MB", mb=i18n.num(1234.5, ",.1f"))
        self.assertEqual((size, size.shown("pt-BR")), ("1,234.5 MB", "1.234,5 MB"))
        self.assertEqual(i18n.txn(1234, "{n} process", "{n} processes").shown("pt-BR"), "1.234 processos")
        self.assertEqual(i18n.txn(1234, "{n} process", "{n} processes"), "1,234 processes")
        for code, shown in (("en", "28%"), ("pt-BR", "28%"), ("de", "28\xa0%"), ("fr", "28\u202f%"), ("tr", "%28")):
            with self.subTest(code):
                self.assertEqual(i18n.pct(28.4).shown(code), shown)
        self.assertEqual((i18n.num(1234).shown("es"), i18n.num(12345).shown("es")), ("1234", "12.345"))  # grouped from 5 digits
        self.assertEqual((i18n.num(12345.6, ",.1f").shown("fr"), i18n.num(-1500).shown("pt-BR")), ("12\u202f345,6", "-1.500"))  # (CLDR's spaces, as the page's Intl writes them)
        self.assertEqual(i18n.num(7, ".1f").shown("pt-BR"), "7,0")

    def test_lists(self):
        three, two = i18n.items(["A", "B", "C"]), i18n.items(["A", i18n.tx("Close")])
        self.assertEqual((str(three), three.shown("pt-BR")), ("A, B and C", "A, B e C"))
        self.assertEqual((str(two), two.shown("pt-BR")), ("A and Close", "A e Fechar"))
        self.assertEqual((i18n.items(["A"]).shown("pt-BR"), i18n.items([]).shown("pt-BR")), ("A", ""))

    def test_texts_inside_texts(self):
        point = i18n.tx("Restore point: {reason}", reason=i18n.txc("restore point", "created"))
        self.assertEqual((point, point.shown("pt-BR")), ("Restore point: created", "Ponto de restauração: criado"))
        both = i18n.joined([i18n.tx("Close"), None, "", i18n.tx("Low")])
        self.assertEqual((both, both.shown("pt-BR")), ("Close Low", "Fechar Mínimo"))
        self.assertEqual(i18n.joined([]), "")
        sentence = i18n.sentence(i18n.tx("not signed"))
        self.assertEqual((sentence, sentence.shown("pt-BR")), ("Not signed.", "Sem assinatura."))
        later = i18n.tx("applied — takes effect: {when}", when=i18n.lowercase(i18n.tx("After a restart")))
        self.assertEqual(later, "applied — takes effect: after a restart")
        self.assertEqual(later.shown("pt-BR"), "aplicado — entra em vigor: depois de reiniciar")

    def test_errors_keep_their_text(self):
        error = ValueError(i18n.tx("Close"))
        self.assertEqual(str(error), "Close")  # str() is only its English
        self.assertEqual(i18n.message(error).shown("pt-BR"), "Fechar")
        self.assertEqual(i18n.message(ValueError("plain words")), "plain words")

    def test_files_keep_english_with_how_to_translate_it(self):
        text = i18n.txn(2, "{n} process uses {size}", "{n} processes use {size}", size=i18n.tx("{mb} MB", mb=i18n.num(1536)))
        data = json.loads(json.dumps(i18n.storable({"result": text, "texts": [text], "count": 2})))
        self.assertEqual(data["result"], "2 processes use 1,536 MB")  # the file reads as it always did
        self.assertEqual(data["texts"], ["2 processes use 1,536 MB"])  # in a list: the English only
        back = i18n.restored(data)
        self.assertNotIn("result_i18n", back)
        self.assertIsInstance(back["result"], i18n.Text)
        self.assertEqual((back["result"], back["result"].shown("pt-BR")), ("2 processes use 1,536 MB", "2 processos usam 1.536 MB"))
        self.assertEqual(i18n.localize(data, "pt-BR"), {"result": "2 processos usam 1.536 MB",
                                                        "texts": ["2 processes use 1,536 MB"], "count": 2})
        data["result"] = "changed since"  # its recipe no longer makes it: the English stays
        self.assertNotIsInstance(i18n.restored(data)["result"], i18n.Text)
        self.assertEqual(i18n.localize(data, "pt-BR")["result"], "changed since")
        data["result_i18n"] = {"$": "text"}  # an unreadable recipe
        self.assertEqual(i18n.recalled("changed since", data["result_i18n"]), "changed since")

    def test_the_api_answer(self):
        answer = {"a": i18n.tx("Close"), "b": [i18n.tx("Low"), 3, None], "c": {"d": i18n.pct(50)},
                  "reason": "Close", "reason_i18n": i18n.tx("Close").to_json()}
        self.assertEqual(i18n.localize(answer, "pt-BR"), {"a": "Fechar", "b": ["Mínimo", 3, None], "c": {"d": "50%"},
                                                          "reason": "Fechar"})
        self.assertEqual(i18n.localize(answer, "en")["a"], "Close")

    def test_another_language_redraws_the_tray(self):
        calls = []
        i18n.on_change(lambda: calls.append(1))
        i18n.on_change(lambda: 1 / 0)  # one that fails doesn't stop the others
        self.addCleanup(i18n._listeners.clear)
        i18n.changed()
        self.assertEqual(calls, [1])


class LanguageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = api_client(app)

    def test_answers_and_errors_in_the_language(self):
        for code, reason, error in (("pt-BR", "precisa que o OptiMind rode como administrador", "ajuste desconhecido 'nope'"),
                                    ("en", "needs OptiMind to run as administrator", "unknown tweak 'nope'")):
            with self.subTest(code):
                settings.optimind_language = code
                with mock.patch("app.tools.restore_point.is_elevated", return_value=False):
                    r = self.client.get("/api/restore-point?refresh=true")
                self.assertEqual(r.json(), {"available": False, "reason": reason})
                r = self.client.post("/api/tweaks/nope/apply")
                self.assertEqual((r.status_code, r.json()["detail"]), (404, error))
        logged = self.client.get("/api/logs?limit=1").json()[-1]
        self.assertEqual((logged["result"], logged["status"], logged["by_you"]), ("unknown tweak 'nope'", "Error", True))
        settings.optimind_language = "pt-BR"  # the log keeps English; the entry is shown in the language
        logged = self.client.get("/api/logs?limit=1").json()[-1]
        self.assertEqual((logged["result"], logged["reason"], logged["status"]),
                         ("ajuste desconhecido 'nope'", "Mudado por você na aba Otimizar.", "Error"))
        self.assertNotIn("result_i18n", logged)

    def setUp(self):
        self.saved = settings.optimind_language

    def tearDown(self):
        settings.optimind_language = self.saved
        SETTINGS_FILE.unlink(missing_ok=True)

    def test_choose_a_language(self):
        r = self.client.put("/api/settings", json={"language": "pt-BR"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["language"], "pt-BR")
        self.assertEqual(settings.optimind_language, "pt-BR")  # the running agent, right away
        self.assertIn("OPTIMIND_LANGUAGE=pt-BR", SETTINGS_FILE.read_text(encoding="utf-8"))
        info = self.client.get("/api/app/language").json()
        self.assertEqual((info["language"], info["chosen"]), ("pt-BR", "pt-BR"))
        self.assertEqual(info["messages"]["Close"], "Fechar")
        page = self.client.get("/").text
        self.assertIn('<html lang="pt-BR">', page)
        self.assertIn('"Close":"Fechar"', page)
        self.client.put("/api/settings", json={"language": ""})  # back to Windows' language
        self.assertEqual(settings.optimind_language, "")
        self.assertIn("OPTIMIND_LANGUAGE=\n", SETTINGS_FILE.read_text(encoding="utf-8"))

    def test_unknown_languages_are_refused(self):
        for value in ("xx", "pt-PT", "en-US", "../../etc", "a" * 30):
            with self.subTest(value):
                self.assertEqual(self.client.put("/api/settings", json={"language": value}).status_code, 422)
        self.assertFalse(SETTINGS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
