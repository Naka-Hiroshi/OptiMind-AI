"""
Downloads the programs OptiMind ships next to its own files, and one the build itself
uses, each checked against a pinned SHA-256, so a changed or tampered download is refused:

    python packaging/get_tools.py      (packaging/build.ps1 runs it; from source, run it once)

  tools/PresentMon.exe     Intel PresentMon 2.6.0, the console app (MIT license, text in
                           packaging/third_party/PresentMon-LICENSE.txt; the release is
                           signed by Intel Corporation): frame-time measurement for the
                           Games page (app/system/frames.py).
  build/tools/esbuild.exe  esbuild 0.28.2 (MIT license), from the npm registry: minifies the
                           window's page for the installed app (packaging/protect.py). Only
                           the build uses it; it isn't shipped.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = [  # (file, download, SHA-256 of the file itself, its path inside the download when that's a .tgz)
    ("tools/PresentMon.exe",
     "https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe",
     "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af", None),
    ("build/tools/esbuild.exe",
     "https://registry.npmjs.org/@esbuild/win32-x64/-/win32-x64-0.28.2.tgz",
     "c7bee37877d0aa6a046e52783fa0a2cf1a9ce5579d68bb3083bda99d4bff18ef", "package/esbuild.exe"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    for name, url, expected, member in TOOLS:
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha256(target) == expected:
            print(f"{target}: already there (checked)")
            continue
        partial = target.with_name(target.name + ".download")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as f:
            shutil.copyfileobj(response, f)
        if member:  # only that one file comes out of the archive
            archive = partial.with_name(partial.name + ".tgz")
            partial.replace(archive)
            with tarfile.open(archive, "r:gz") as tar, partial.open("wb") as f:
                shutil.copyfileobj(tar.extractfile(member), f)
            archive.unlink()
        got = sha256(partial)
        if got != expected:
            partial.unlink()
            print(f"Stopped: {url} gave a file with SHA-256 {got}, not the pinned {expected}.", file=sys.stderr)
            return 1
        partial.replace(target)
        print(f"{target}: downloaded and checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
