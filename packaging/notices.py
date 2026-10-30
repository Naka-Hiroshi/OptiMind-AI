"""
Writes THIRD-PARTY-NOTICES.txt: the open-source software inside OptiMind.exe and
their licenses (packaging/build.ps1 runs it with the build's Python):

    python packaging/notices.py build/dist/OptiMind/THIRD-PARTY-NOTICES.txt
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

# In the build environment, but not inside OptiMind.exe: build tools (also PyInstaller's, used up to
# 1.3.0), or left out by build.ps1
NOT_BUNDLED = {"pip", "setuptools", "wheel", "packaging", "nuitka", "pyinstaller", "pyinstaller-hooks-contrib",
               "altgraph", "pefile", "pywin32-ctypes", "watchfiles", "websockets"}
_LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.IGNORECASE)
_RULE = "=" * 78


def _license_name(meta) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.rsplit(" :: ", 1)[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else "see the license text below"


def _source_header(dist) -> str | None:
    """The license in a top-level module's opening comment, for packages that ship no license
    file (nvidia-ml-py keeps its BSD license at the top of pynvml.py)."""
    for f in dist.files or []:
        if f.suffix != ".py" or "/" in f.as_posix():
            continue
        try:
            lines = Path(f.locate()).read_text(encoding="utf-8", errors="replace").splitlines()[:60]
        except OSError:
            continue
        header = []
        for line in lines:
            if not line.startswith("#"):
                break
            header.append(line.lstrip("#").removeprefix(" ").rstrip().rstrip("#").rstrip())  # no comment-box borders
        text = "\n".join(header).strip("#\n ")
        if "copyright" in text.lower() and ("redistribution" in text.lower() or "permission" in text.lower()):
            return text
    return None


def _license_texts(dist) -> list[str]:
    texts = []
    for f in dist.files or []:
        if _LICENSE_FILE.search(f.as_posix()):
            try:
                texts.append(Path(f.locate()).read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                pass
    if not texts and dist.metadata.get("License") and len(dist.metadata["License"]) > 80:
        texts.append(dist.metadata["License"].strip())
    if not texts:
        header = _source_header(dist)
        if header:
            texts.append(header)
    return texts


# Programs OptiMind ships next to its own files (packaging/get_tools.py downloads them)
BUNDLED_TOOLS = [("Intel PresentMon 2.6.0 (tools/PresentMon.exe) - MIT", "https://github.com/GameTechDev/PresentMon",
                  Path(__file__).parent / "third_party" / "PresentMon-LICENSE.txt")]


def main(out: Path) -> None:
    parts = [
        "OptiMind AI includes the open-source software listed below. Each is used under its own license,",
        "whose text follows. OptiMind's window is drawn by Microsoft Edge WebView2, which is part of",
        "Windows 11; the WebView2 SDK files bundled by pywebview (Microsoft.Web.WebView2.Core.dll,",
        "Microsoft.Web.WebView2.WinForms.dll, WebView2Loader.dll) are Copyright Microsoft Corporation and",
        "redistributed under the Microsoft WebView2 SDK license. Segoe UI and Segoe Fluent Icons are",
        "Windows' own fonts and are not included.",
        "",
        "OptiMind.exe is compiled with Nuitka (https://nuitka.net) and GCC from MinGW-w64 (winlibs).",
        "The runtime code they add is used under the Nuitka Runtime Library Exception and the GCC",
        "Runtime Library Exception, which allow the compiled program to be shared under any terms.",
        "",
    ]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    parts += [_RULE, f"Python {sys.version.split()[0]} - PSF License Agreement", _RULE,
              python_license.read_text(encoding="utf-8", errors="replace").strip() if python_license.exists()
              else "https://docs.python.org/3/license.html", ""]
    dists = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name and name.lower().replace("_", "-") not in NOT_BUNDLED:
            dists[name.lower()] = dist
    for _, dist in sorted(dists.items()):
        meta = dist.metadata
        home = meta.get("Home-page") or next((u.split(", ", 1)[-1] for u in meta.get_all("Project-URL") or []), "")
        parts += [_RULE, f"{meta['Name']} {dist.version} - {_license_name(meta)}", home, _RULE]
        parts += _license_texts(dist) or ["(no license text shipped with the package; see its home page)"]
        parts.append("")
    for title, home, license_file in BUNDLED_TOOLS:
        parts += [_RULE, title, home, _RULE, license_file.read_text(encoding="utf-8").strip(), ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"{out}: Python, {len(dists)} packages and {len(BUNDLED_TOOLS)} bundled tool")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
