"""The build's protection step (packaging/protect.py): docstrings emptied without moving a
line, the window's page minified, the API serving the page the build put inside the
program, and the translations going with it."""
import importlib.util
import re
import sys
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("optimind_protect", ROOT / "packaging" / "protect.py")
protect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protect)

SAMPLE = '''"""Module docstring
over two lines."""
from __future__ import annotations


class Thing:
    """Class docstring with “quotes” and an ellipsis…"""; x = 1

    def method(self):
        """One line."""
        return "kept"

    async def later(self):
        r"""Raw
        docstring."""
        text = """not a docstring"""
        return text


def bare():
    "single quotes"


def fails():
    """Before the error."""
    raise RuntimeError("line 26")
'''


class DocstringTests(unittest.TestCase):
    def test_emptied_and_lines_kept(self):
        out = protect.blank_docstrings(SAMPLE)
        self.assertEqual(out.count("\n"), SAMPLE.count("\n"))
        for gone in ("Module docstring", "Class docstring", "One line.", "Raw", "single quotes", "Before the error."):
            self.assertNotIn(gone, out)
        for kept in ('return "kept"', 'text = """not a docstring"""', "; x = 1", "from __future__ import annotations"):
            self.assertIn(kept, out)
        namespace = {}
        exec(compile(out, "sample.py", "exec"), namespace)  # noqa: S102 - our own sample
        self.assertEqual(namespace["Thing"]().method(), "kept")
        self.assertEqual(namespace["Thing"].x, 1)
        try:
            namespace["fails"]()
        except RuntimeError as e:
            frame = e.__traceback__
            while frame.tb_next:
                frame = frame.tb_next
            self.assertEqual(frame.tb_lineno, 26)  # the line it has in the source
        else:
            self.fail("no error")

    def test_windows_line_ends(self):
        source = SAMPLE.replace("\n", "\r\n")
        out = protect.blank_docstrings(source)
        self.assertEqual(out.count("\r\n"), source.count("\r\n"))
        self.assertNotIn("Module docstring", out)

    def test_every_source_file(self):
        """The real code goes through (blank_docstrings checks that no statement moved)."""
        for path in [ROOT / "run.py", *sorted((ROOT / "app").rglob("*.py"))]:
            with self.subTest(str(path.relative_to(ROOT))):
                out = protect.blank_docstrings(path.read_bytes().decode("utf-8-sig"), str(path))
                compile(out, str(path), "exec")


class PageTests(unittest.TestCase):
    PAGE = ('<!doctype html><head><!-- note --><script>var a = "<!-- kept -->";</script>'
            '<style>p { color: red; }</style></head><body><!-- section -->\n<p id="hi">Hi</p>'
            '<script>"use strict";\nfunction f() {}</script></body>')

    def test_minify(self):
        calls = []

        def fake(code, loader):
            calls.append((loader, code))
            return f"[{loader}]"

        out = protect.minify_page(self.PAGE, minify=fake)
        self.assertEqual(out, '<!doctype html><head><script>[js]</script><style>[css]</style></head><body>\n'
                              '<p id="hi">Hi</p><script>[js]</script></body>')
        self.assertEqual([loader for loader, _ in calls], ["js", "css", "js"])
        self.assertIn('"<!-- kept -->"', calls[0][1])  # comments go only outside the scripts
        self.assertTrue(calls[2][1].startswith('(() => {\n"use strict";'))  # each script inside a function
        self.assertTrue(calls[2][1].endswith("\n})();"))

    def test_checks(self):
        with self.assertRaises(ValueError):
            protect.minify_page("<p>no script</p>", minify=lambda code, loader: code)
        with self.assertRaises(ValueError):  # a minifier that loses an element the scripts look for
            protect.minify_page('<script>x = `<b id="gone">`;</script>', minify=lambda code, loader: "x=1")

    def test_module(self):
        source = protect.page_module("<p>é</p>")
        namespace = {}
        exec(source, namespace)  # noqa: S102 - made just above
        self.assertEqual(zlib.decompress(namespace["PAGE"]).decode("utf-8"), "<p>é</p>")

    @unittest.skipUnless(protect.ESBUILD.is_file(), "esbuild isn't downloaded yet (packaging/get_tools.py)")
    def test_real_page(self):
        original = protect.PAGE.read_text(encoding="utf-8")
        page = protect.minify_page(original)
        self.assertLess(len(page), len(original) * 0.85)
        self.assertNotIn("<!--", page)
        self.assertNotIn("function loadDrivers", page)  # the scripts' names are shortened
        self.assertEqual(re.findall(r'data-tab="(\w+)"', page), re.findall(r'data-tab="(\w+)"', original))

    def test_served_from_the_program(self):
        from app import main

        built = types.ModuleType("optimind_page")
        built.PAGE = zlib.compress("<p>built</p>".encode("utf-8"))
        client = TestClient(main.app, base_url="http://127.0.0.1:8765")
        with mock.patch.dict(sys.modules, {"optimind_page": built}):
            r = client.get("/")
        self.assertEqual(r.text, "<p>built</p>")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["content-type"], "text/html; charset=utf-8")
        self.assertIn('id="page-drivers"', client.get("/").text)  # from source: app/static/dashboard.html

    def test_translations_go_with_the_program(self):
        catalogs = sorted((ROOT / "app" / "i18n").glob("*.json"))
        self.assertTrue(catalogs)
        self.assertEqual([path for path in protect.DATA if path.suffix == ".json"], catalogs)  # staged for Nuitka
        self.assertIn('"--include-data-files=app\\i18n\\*.json=app/i18n/"', (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
