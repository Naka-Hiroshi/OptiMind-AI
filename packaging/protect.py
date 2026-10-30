"""
Gets OptiMind's code ready for compiling, so that the installed app holds no readable
source (packaging/build.ps1 runs it, then compiles build/src with Nuitka):

    python packaging/protect.py

  - run.py and app/**/*.py go to build/src with their docstrings emptied. Nuitka turns
    the code into machine code and leaves the comments out, but would keep docstrings
    as text. Every line keeps its number, so a traceback in a log still points at the
    right line of the source.
  - The window's page (app/static/dashboard.html) is minified by esbuild (comments and
    spaces gone, the scripts' names shortened) and goes inside the program, compressed,
    as the module optimind_page (app/main.py serves it) instead of a file next to it.
    build/page/dashboard.html keeps a copy to look at.
"""
from __future__ import annotations

import ast
import io
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "src"
ESBUILD = ROOT / "build" / "tools" / "esbuild.exe"  # packaging/get_tools.py downloads it
PAGE = ROOT / "app" / "static" / "dashboard.html"
# Read by the app at run time: the favicon, and the translations (app/i18n)
DATA = [ROOT / "app" / "static" / "optimind.ico", *sorted((ROOT / "app" / "i18n").glob("*.json"))]

_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_BLOCK = re.compile(r"<(script|style)>(.*?)</\1>", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ELEMENTS = re.compile(r'\b(?:id|data-tab)="([\w-]+)"')  # what the scripts look elements up by


def _docstrings(tree: ast.Module) -> list[ast.Constant]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *_DEFINITIONS)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.append(first.value)
    return found


def _layout(tree: ast.Module) -> list[tuple[str, int, int]]:
    return [(type(n).__name__, n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.stmt)]


def blank_docstrings(source: str, name: str = "<source>") -> str:
    """`source` with every docstring emptied; each line keeps its number."""
    tree = ast.parse(source, name)
    # Python counts \n, \r\n and \r as line ends; the offsets are bytes of UTF-8
    lines = [line.encode("utf-8") for line in io.StringIO(source, newline="").readlines()]
    for doc in sorted(_docstrings(tree), key=lambda d: (d.lineno, d.col_offset), reverse=True):
        first, last = doc.lineno - 1, doc.end_lineno - 1
        ends = [line[len(line.rstrip(b"\r\n")):] for line in lines[first:last]]  # each line keeps its own end
        blank = b'"""' + b"".join(ends) + b'"""'
        lines[first:last + 1] = [lines[first][:doc.col_offset] + blank + lines[last][doc.end_col_offset:]]
    result = b"".join(lines).decode("utf-8")
    after = ast.parse(result, name)
    if _layout(after) != _layout(tree) or any(doc.value.strip() for doc in _docstrings(after)):
        raise ValueError(f"{name}: emptying the docstrings would move the code")
    return result


def stage() -> int:
    """Copies the code to build/src without docstrings; returns how many files."""
    if OUT.exists():
        shutil.rmtree(OUT)
    sources = [ROOT / "run.py", *sorted((ROOT / "app").rglob("*.py"))]
    for path in sources:
        name = path.relative_to(ROOT)
        target = OUT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blank_docstrings(path.read_bytes().decode("utf-8-sig"), str(name)).encode("utf-8"))
    for path in DATA:
        target = OUT / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return len(sources)


def esbuild(code: str, loader: str) -> str:
    """`code` ("js" or "css") minified by esbuild."""
    done = subprocess.run([str(ESBUILD), f"--loader={loader}", "--minify", "--charset=utf8", "--log-level=warning"],
                          input=code.encode("utf-8"), capture_output=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"esbuild couldn't minify the page's {loader}:\n{done.stderr.decode('utf-8', 'replace')}")
    return done.stdout.decode("utf-8").strip()


def minify_page(html: str, minify=esbuild) -> str:
    """The page without comments and spaces. Each script is wrapped in a function first, so
    that its top-level names are shortened too: the page has no inline event handlers and
    the window only sends it events, so nothing outside a script uses those names."""
    parts, last, scripts = [], 0, 0
    for match in _BLOCK.finditer(html):
        tag, code = match.groups()
        parts.append(_COMMENT.sub("", html[last:match.start()]))
        if tag == "script":
            code = minify(f"(() => {{\n{code}\n}})();", "js")
            scripts += 1
        else:
            code = minify(code, "css")
        parts.append(f"<{tag}>{code}</{tag}>")
        last = match.end()
    parts.append(_COMMENT.sub("", html[last:]))
    if not scripts:
        raise ValueError("the page has no <script> to minify")
    page = "".join(parts)
    if sorted(_ELEMENTS.findall(page)) != sorted(_ELEMENTS.findall(html)):
        raise ValueError("minifying lost or changed elements of the page")
    return page


def page_module(html: str) -> str:
    """Python source of the module optimind_page: the page, compressed."""
    return ("# The window's page, minified and compressed (packaging/protect.py made this file).\n"
            f"PAGE = {zlib.compress(html.encode('utf-8'), 9)!r}\n")


def main() -> int:
    if not ESBUILD.is_file():
        print(f"Stopped: {ESBUILD} is missing; run packaging/get_tools.py first.", file=sys.stderr)
        return 1
    files = stage()
    page = minify_page(PAGE.read_text(encoding="utf-8"))
    (OUT / "optimind_page.py").write_text(page_module(page), encoding="utf-8")
    copy = ROOT / "build" / "page" / "dashboard.html"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(page, encoding="utf-8")
    print(f"{OUT}: {files} source files without docstrings; the window's page minified from "
          f"{PAGE.stat().st_size // 1024} KB to {len(page.encode('utf-8')) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
