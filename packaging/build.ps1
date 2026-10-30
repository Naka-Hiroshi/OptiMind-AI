<#
.SYNOPSIS
    Builds OptiMind's installer: dist\OptiMind-AI-Setup-<version>.exe

.DESCRIPTION
    From the project folder:

        powershell -ExecutionPolicy Bypass -File packaging\build.ps1

    1. Checks the build tools: the project's .venv with requirements-build.txt, and Inno Setup 7.
    2. Runs the tests.
    3. Draws the icon and the installer's images (packaging\make_assets.py).
    4. Downloads the bundled tools and esbuild, checked against their pinned SHA-256 (packaging\get_tools.py).
    5. Gets the code ready for compiling (packaging\protect.py): docstrings emptied, the window's page minified.
    6. Compiles OptiMind.exe with Nuitka: the Python code becomes C, then machine code, so the
       installed app holds no Python source or bytecode (build\dist\OptiMind). The first build
       downloads Nuitka's C compiler (MinGW64 from winlibs, about 270 MB, kept in
       %LOCALAPPDATA%\Nuitka) and takes about half an hour; later builds reuse what didn't change.
    7. Lists the open-source licenses (THIRD-PARTY-NOTICES.txt next to OptiMind.exe).
    8. Compiles the installer with Inno Setup (dist\).

.PARAMETER Python
    The Python to build with (default: .venv\Scripts\python.exe).

.PARAMETER Iscc
    Inno Setup's compiler, ISCC.exe (default: found in the usual places).

.PARAMETER SkipTests
    Skip step 2.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Iscc = "",
    [switch]$SkipTests
)

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Check([string]$What) { if ($LASTEXITCODE -ne 0) { Write-Host "Stopped: $What failed." -ForegroundColor Red; exit 1 } }

Step "Build tools"
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) {
    Write-Host "No Python at $Python. Create the project's .venv first (see docs\development.md)." -ForegroundColor Red
    exit 1
}
$Python = (Resolve-Path $Python).Path  # Nuitka runs from build\src
& $Python -c "import nuitka, webview, pynvml" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install --disable-pip-version-check -r requirements-build.txt
    Check "Installing the build tools"
}
$candidates = @($Iscc, $env:ISCC, (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    "$env:ProgramFiles\Inno Setup 7\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe")
$Iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Iscc) {
    Write-Host "Inno Setup 7 isn't installed. Get it with:  winget install --id JRSoftware.InnoSetup -e" -ForegroundColor Red
    Write-Host "(or download it from https://jrsoftware.org/isdl.php), or pass -Iscc <path to ISCC.exe>." -ForegroundColor Red
    exit 1
}
$Version = (& $Python -c "from app import __version__; print(__version__)").Trim()
Check "Reading the version"
Write-Host "OptiMind AI $Version | Python: $Python | Inno Setup: $Iscc"

if (-not $SkipTests) {
    Step "Tests"
    & $Python -m unittest
    Check "The tests"
}

Step "Icon and installer images"
& $Python packaging\make_assets.py
Check "Drawing the images"

Step "Bundled tools (PresentMon) and esbuild, checked against their pinned SHA-256"
& $Python packaging\get_tools.py
Check "Getting the tools"

Step "Code ready for compiling (docstrings emptied, the window's page minified)"
& $Python packaging\protect.py
Check "Preparing the code"

Step "OptiMind.exe (compiled with Nuitka)"
$Numbers = (($Version.Split(".") + @("0", "0", "0"))[0..3]) -join "."
$Out = Join-Path $Root "build\nuitka"
Push-Location (Join-Path $Root "build\src")  # so that Nuitka compiles this copy of the code
& $Python -m nuitka run.py --standalone --assume-yes-for-downloads --lto=no --remove-output `
    "--output-dir=$Out" --output-filename=OptiMind.exe --windows-console-mode=disable `
    "--windows-icon-from-ico=$Root\app\static\optimind.ico" "--company-name=OptiMind AI" `
    "--product-name=OptiMind AI" "--file-description=OptiMind AI" "--copyright=OptiMind AI" `
    "--file-version=$Numbers" "--product-version=$Numbers" `
    --include-package=app --include-module=optimind_page `
    --include-module=pystray._win32 --include-module=win32timezone --include-module=pynvml `
    --include-module=win32api --include-module=win32service `
    "--include-data-files=app\static\optimind.ico=app\static\optimind.ico" `
    "--include-data-files=app\i18n\*.json=app/i18n/" `
    "--include-data-files=$Root\tools\PresentMon.exe=tools\PresentMon.exe" `
    "--nofollow-import-to=tkinter,watchfiles,websockets,PyQt5,PyQt6,PySide2,PySide6,qtpy,gi,cefpython3,win32ui,pywin,setuptools,pkg_resources" `
    "--report=$Out\compilation-report.xml"
Pop-Location
Check "Nuitka"
$Dist = Join-Path $Root "build\dist\OptiMind"
if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
New-Item -ItemType Directory -Force (Split-Path $Dist) | Out-Null
Move-Item (Join-Path $Out "run.dist") $Dist

Step "Open-source licenses"
& $Python packaging\notices.py build\dist\OptiMind\THIRD-PARTY-NOTICES.txt
Check "Writing the license notices"

Step "Installer (Inno Setup)"
& $Iscc /Qp "/DAppVersion=$Version" packaging\installer.iss
Check "Inno Setup"

$Setup = Get-Item (Join-Path $Root "dist\OptiMind-AI-Setup-$Version.exe")
Write-Host ("`nDone: {0} ({1:N1} MB)" -f $Setup.FullName, ($Setup.Length / 1MB)) -ForegroundColor Green
