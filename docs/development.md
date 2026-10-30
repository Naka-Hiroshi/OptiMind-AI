# Development

How to run OptiMind AI from source, test it, change it, and build the
installer.

## Set up

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python packaging\get_tools.py   # PresentMon, for the Games page's frame rates
copy .env.example .env   # optional: free AI keys (or set them in the app's Settings page)
```

Python 3.11 or 3.12 on Windows 11. The tests use only the standard
library's `unittest`, so there is nothing else to install.

`get_tools.py` downloads Intel's PresentMon 2.6.0 console app (MIT
license) from its GitHub release into `tools\PresentMon.exe` and checks it
against the SHA-256 pinned in the script; a file that doesn't match is
never used. It isn't part of the repository (`tools/` is in `.gitignore`).
Without it, OptiMind works as usual and the Games page says frame rates
can't be measured.

## Run

- `OptiMind.cmd` (double-click) or `python run.py`: the whole app, as
  installed: the administrator prompt, the agent, the tray icon and
  OptiMind's window. From source it reads `.env` and keeps its data in
  `data\`; the console window shows the log.
- `python run.py --background`: without opening the window (what start
  with Windows does). The other options are listed at the top of
  `run.py`.
- `uvicorn app.main:app --port 8765`: only the API and the agent's loops,
  without the tray icon, the window or the administrator prompt. Open
  `http://127.0.0.1:8765/` in a browser to see the interface; window-only
  actions (Quit, Restart as administrator) answer 409 there. A browser
  doesn't have the window's secret, so its changes are refused; from
  source, `OPTIMIND_OPEN_API=true` at start allows them (the installed app
  ignores it).
- In VS Code, **Run and Debug → OptiMind AI (run.py)**. Under the
  debugger OptiMind doesn't ask for administrator rights (the debugger
  can't follow it into a new process); start VS Code as administrator to
  debug with them.
- `OPTIMIND_WINDOW_DEBUG=true` opens the window with the browser engine's
  developer tools (right-click → Inspect) and its keyboard shortcuts.

Only one OptiMind runs at a time (a named mutex, see
`app/desktop/instance.py`): starting a second one opens the first one's
window. A copy for testing therefore needs `OPTIMIND_API_PORT` set to a
free port, and is best run with plain uvicorn (below).

## Test mode

`OPTIMIND_DRY_RUN=true` (in the environment or the settings file) starts
OptiMind in **test mode**: the agent decides as usual but only logs what
it would do, with "simulated (dry_run)" as the result, and Focus mode
doesn't boost. The interface shows a *Test mode* banner. Changes you make
yourself on the Optimize and Startup pages still apply, as they do
normally. The Drivers page's installs and tools don't, nor does closing an
app from the Games page: they only say what they would do, nothing is
downloaded or closed, and *Check Windows Update now* reads
Windows Update's own last result instead of asking Microsoft. Use it to
run a second copy next to the real one, on another port and with its own
files:

```powershell
$env:OPTIMIND_DRY_RUN = "true"; $env:OPTIMIND_RUN_AS_ADMIN = "false"
$env:OPTIMIND_DATA_DIR = "$env:TEMP\optimind-test"; $env:OPTIMIND_SETTINGS_FILE = "$env:TEMP\optimind-test\settings.env"
$env:OPTIMIND_OPEN_API = "true"  # changes from the browser too (from source only)
uvicorn app.main:app --port 8791
```

## Tests

```powershell
python -m unittest            # all tests, a few seconds
python -m unittest -v tests.test_desktop
```

They never change your PC or your data: `tests/__init__.py` switches on
test mode, uses only the local AI brain (HTTP requests are faked where a
provider is tested), and points `OPTIMIND_DATA_DIR` and
`OPTIMIND_SETTINGS_FILE` at a temporary folder, so your `.env` is never
read or written. Process changes are tried on stand-in objects (GPU
priority on a throwaway process the test starts and ends), the Group
Policy writer works on a temporary folder with `gpupdate` replaced, the
DirectX settings and power plan tweaks on a stand-in registry and
`powercfg`, and no scheduled task is created.

| File | What it covers |
|---|---|
| `test_decision_engine.py` | The fast loop's rules: power plan, grace periods, memory compression (pressure that lasts, apps opened later, the AI's protected apps), standby purge, I/O priority, Focus mode, page combining (never while a game runs, only with the processor calm), failures (processes that weren't found) |
| `test_api.py` | The API end to end with the loops running: status and version, Focus mode, the local-only guard, the interface, tweaks, the hardware check and the Games page |
| `test_ai.py` | The free providers' fallback chain, validation of the AI's policy, chat (and what it's told about the PC), the AI loop, the AI's protected apps (dated, gone after 14 days, one you took off stays off) |
| `test_desktop.py` | The Settings page (its file, encrypted keys, the API), start with Windows, one OptiMind at a time, the window's helpers (its black title bar and starting page, in OptiMind's language), the tray menu following the language |
| `test_activity.py` | Removing activity entries (one, a day, a filter, everything), and the fields the page lists them by (status, your changes, memory freed) |
| `test_games.py` | PresentMon's output and the frame analysis (GPU-, CPU-limited, V-Sync, frame caps), game sessions (Alt+Tab, five minutes away, recording, memory near its limit, your notes), Before you play, each capture's frame rate for the charts, the advice (with the apps holding the most memory), the page file check, the stability log reader, the DirectX settings flags, the disk spin-down tweak, GPU priority |
| `test_close_app.py` | Closing an app you picked: throwaway processes with an off-screen window of their own (one closes, one only hides, as apps in the notification area do), what's never closed and why, test mode, and the API (the window's secret, the activity log) |
| `test_drivers.py` | The Drivers page: device properties and problem codes, pnputil's XML and old driver versions (the rollback version kept), drivers apps added and known risks, the driver events (devices that reset, alone or together) and Code Integrity policies, NVIDIA's lookup (only its own download server), Windows Update's offers next to the installed driver (never an older one, never firmware: in the list, the API and Windows Update's installer), who signed each driver (pnputil's signer, or Windows' own check on older Windows, read-only on one of Windows' INF files), the findings, the tools in test mode (no pnputil runs), one task at a time, and the API's refusals (no rights, a game running, firmware, bad input) |
| `test_local_policy.py` | The Registry.pol / gpt.ini writer used for the Widgets policy |
| `test_packaging.py` | The build's protection step: docstrings emptied without moving a line (in every source file), the page minified (scripts wrapped, every element kept), the page served from inside the program, the translations going with it |
| `test_i18n.py` | OptiMind in other languages: every text written out where it's used, every language complete with the English `{names}` and the plural forms it needs, the language from Settings or Windows (another country's falls back to its language, else English), plural rules, the page getting its texts (safely, before its own script), the Settings page's Language (unknown ones refused), and the agent's texts: numbers and lists the reader's way, texts inside texts, errors, files that keep the English with how to translate it, and the API answering in the language |
| `test_impact.py` | What OptiMind changed, in numbers: the activity counted with Windows' amounts (only what was done, in the chosen days), how busy the PC was, games compared with Focus mode Off and on (only captures that ran in one mode; frames over time, and how many captures), and the Focus mode a session notes |
| `test_updates.py` | OptiMind's update check, with GitHub's answers faked: a newer version, only that version's Setup file (older ones attached too), the newest one, what isn't trusted (a tag that isn't a version, a page outside the release page, an answer from elsewhere, errors), no check without a release page, when it asks again, and the API |
| `test_maker_tools.py` | The makers' own driver apps: which fit a PC (made-up PCs: self-built with NVIDIA, a Dell laptop with Intel graphics, each brand), their names in the Start menu, Get-StartApps' answer (and one read-only look at this PC's Start menu), and that the window opens only an app the agent lists |
| `test_any_pc.py` | Other PCs, faked: what works on a desktop with NVIDIA, a laptop with Intel graphics and only Balanced, AMD graphics, no administrator rights; no switch to a plan the PC doesn't have; AMD's tuning tip, Intel's 13th/14th gen microcode check and each board maker's BIOS menus |
| `test_confirm.py` | OptiMind's own dialog for the riskiest changes (No by default, on top, gives up after 90 s), and that turning off Memory integrity and restoring drivers wait for it (the tests set `OPTIMIND_NO_DIALOGS=true`: dialogs answer No, unseen) |
| `test_access.py` | Only OptiMind's own window can change things: changes need this start's secret, reading and opening the window or quitting stay open, the window gets the secret from the agent and hands it to its page |
| `test_report.py` | The report to send for help: what's in the zip, and that the user name, the PC's name, e-mail addresses, API keys, game names (however they're written) and the AI's notes are left out |
| `test_safety.py` | Protected processes (and what the AI's list protects), validation, and honest results when Windows refuses a change or the process isn't found |
| `test_focus.py` | Focus mode's view of the app in front: what counts as a game, OptiMind's own window, an app's helper processes, a game noted once (not on every Alt+Tab), and what a give-back puts back |

Two optional checks with real tools:

- `OPTIMIND_TEST_ETL=<trace.etl>` runs the bundled PresentMon on a
  recorded trace (PresentMon's own test traces, from its repository's
  `Tests/Gold` folder, work) and checks OptiMind reads its frames. It
  needs no administrator rights.
- The GPU priority tests start a small Direct3D 11 process of their own
  (and end it); they skip themselves on a PC without a Direct3D 11
  device.

VS Code's Testing panel finds them too (`.vscode/settings.json`).

## Exploring the API

With OptiMind running from source, `http://127.0.0.1:8765/docs` is the
interactive documentation of every endpoint (the installed app leaves it
out; the list is in
[architecture.md](architecture.md#local-api)). In test mode you can call
`GET /api/status` and `GET /api/logs` to see the agent deciding on
actions and recording "simulated (dry_run)" for each one, chat at
`POST /api/chat` (*"what have you done in the last few minutes?"*; the
answer says which provider answered), and run a policy review now with
`POST /api/policy/refresh-now`. `GET /api/tweaks`, `GET /api/cleanup`,
`GET /api/hardware` and `GET /api/app` are read-only.

## Settings

All settings come from environment variables or the settings file:
`.env` in the project folder when running from source,
`%LOCALAPPDATA%\OptiMind\settings.env` for the installed app
([.env.example](../.env.example) documents the ones for users). The
Settings page edits the AI ones and `OPTIMIND_RUN_AS_ADMIN` in that file,
changing the lines in place; the API keys it saves are encrypted for your
Windows account (`dpapi:…`). A key typed into the file by hand works too.

| Variable | Default | What it does |
|---|---|---|
| `OPTIMIND_AI_PROVIDERS` | `gemini,groq,local` | Order to try the AI providers in (`local` is always last) |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | empty | Free API keys |
| `OPTIMIND_GEMINI_MODEL`, `OPTIMIND_GROQ_MODEL` | see `.env.example` | Models to use |
| `OPTIMIND_OLLAMA_URL`, `OPTIMIND_OLLAMA_MODEL` | `http://localhost:11434/v1`, empty | Local AI with Ollama |
| `OPTIMIND_FAST_LOOP_SECONDS` | `2` | Fast loop interval |
| `OPTIMIND_AI_LOOP_SECONDS` | `900` | AI review interval |
| `OPTIMIND_API_PORT` | `8765` | Port of the local API (the window talks to the agent through it) |
| `OPTIMIND_RUN_AS_ADMIN` | `true` | Ask for administrator rights at start |
| `OPTIMIND_LANGUAGE` | empty | OptiMind's language (`pt-BR`…; Settings page); empty is Windows' own when OptiMind has it, else English (the tests set `en`) |
| `OPTIMIND_MEASURE_GAMES` | `true` | Measure games' frame rates with PresentMon while you play (Settings page) |
| `OPTIMIND_UPDATE_REPO` | `Naka-Hiroshi/OptiMind-AI` | OptiMind's release page on GitHub (`owner/name`): the update check asks it about once a day. Its default goes into the build (see *Publishing a new version*); empty means no check (the tests set it empty) |
| `OPTIMIND_PRESENTMON` | `tools\PresentMon.exe` | Another PresentMon console app to use (environment variable only) |
| `OPTIMIND_DRY_RUN` | `false` | Test mode (developers only, see above) |
| `OPTIMIND_DATA_DIR` | `data\` next to the settings file | Where the data files go (the tests use a temporary folder) |
| `OPTIMIND_SETTINGS_FILE` | `.env` / `settings.env` | The settings file itself (environment variable only) |

## Where new code goes

- Reading something from Windows: `app/system/`. Read-only, and quick
  enough for the 2-second loop (or cached).
- Something the agent decides on its own: a rule in
  `app/agent/decision_engine.py` that proposes an `OptimizationAction`,
  and its execution in `app/agent/optimizer.py`. Every action goes
  through `safety.validate_action`, is checked with Windows after it's
  applied, and is remembered so it can be put back.
- Something the user starts from the interface: `app/tools/`, with its
  endpoint in `app/api/routes.py` (through `_logged_change`, so it lands
  in the activity log).
- The app around the agent (window, tray icon, start with Windows,
  settings, quitting): `app/desktop/`, `app/service/preferences.py` and
  `app/api/app_routes.py`.
- The interface is one self-contained file, `app/static/dashboard.html`:
  no build step, no external libraries, works offline. It follows
  Windows 11's design (Fluent): colors are tokens at the top of its
  `<style>`, with a light and a dark set, icons come from Windows' own
  Segoe Fluent Icons font, and the controls (switches, dialogs, banners)
  are plain HTML and CSS in the same file. Every text a person reads goes
  through `tx()` (see *Translations*).

## Translations

OptiMind is written in English, and the English text is the key of its
translation: `app/i18n/<language>.json` holds one language,

```json
{"language": "pt-BR", "name": "Português (Brasil)", "locale": "pt-BR",
 "messages": {"Close {app}": "Fechar {app}",
              "{n} processes": {"one": "{n} processo", "other": "{n} processos"}}}
```

and a text it doesn't have stays English. The language is the one
chosen in Settings (`OPTIMIND_LANGUAGE`), else Windows' display
language when OptiMind has it (another country's falls back to its
language: `pt-PT` uses `pt-BR`), else English. The agent sends the
page its translations with the page itself (`i18n.inject`), so it never
shows English first; choosing another language loads the page again.

- **In the page's script:** `tx("Close {app}", { app: name })`;
  `txn(n, "{n} process", "{n} processes")` for a text with a number
  (`{n}` is filled in, written the local way); `txc("priority", "Low")`
  for a word that another place translates differently (keyed
  `"priority|Low"`). Write the English out in the call (a variable can't
  be found to translate) and keep a sentence whole, with `{names}` for
  what's filled in, never glued from pieces: word order changes between
  languages. Numbers, percentages, sizes, dates and lists go through
  `fmtNum`, `percent`, `fmtBytes`/`fmtMB`, `fmtDate`/`fmtTime` and
  `fmtList`, which follow the language. Compare data by its key or code,
  never by a text a person reads.
- **In the page's HTML:** each text and its `title`, `aria-label` and
  `placeholder` are translated as they stand (`translateStatic`); names
  that stay as they are (products, licenses) go in `translate="no"`.
- **In Python:** `i18n.tx()`, `i18n.txn()` and `i18n.txc()` do the same,
  with `i18n.num()`, `i18n.pct()` and `i18n.items()` for the numbers,
  percentages and lists in a text. Each makes a `Text`: a string whose
  value is the English, so the agent, its log and the AI read English
  as before. The API translates every `Text` in an answer as it sends
  it (`LocalizedResponse`, `app/main.py`); a router makes its routes
  with `APIRouter(route_class=LocalizedRoute)` (`app/api/__init__.py`),
  or FastAPI would turn its texts into plain English first. A file keeps
  the English with how to translate it beside it (`i18n.storable()`:
  `"reason"` and `"reason_i18n"`; `i18n.restored()` reads it back), as
  a model's text fields do (`<field>_i18n`, `app/models.py`). An error
  raised with a `Text` keeps it (`i18n.message(e)`; `str(e)` is only its
  English), and what OptiMind draws itself (the tray menu, its dialogs)
  uses `i18n.shown()`. OptiMind's logs, the report for help and what
  the agent tells the AI stay English.

```powershell
python packaging\i18n_keys.py              # every text, and what each language lacks
python packaging\i18n_keys.py --template   # every text as an empty catalog, to translate
```

A new language is a new file in `app/i18n` with every text: its
`locale` sets how numbers and dates are written, and a text with a
number needs a form for each plural category of the language (one and
other in most; one, few, many and other in Russian and Polish; only
other in Japanese, Korean and Chinese). `tests/test_i18n.py` checks
every language is complete, with the English `{names}`, and that no
text is left that the code no longer uses.

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

It needs the project's `.venv` (it installs `requirements-build.txt`
into it the first time) and [Inno Setup 7](https://jrsoftware.org/isdl.php)
(`winget install --id JRSoftware.InnoSetup -e`). It:

1. checks the build tools (and installs `requirements-build.txt` when
   one is missing);
2. runs the tests;
3. draws the icon and the installer's images (`packaging\make_assets.py`,
   from the logo in `app/desktop/icon.py`);
4. gets PresentMon, and esbuild for step 5 (`packaging\get_tools.py`; an
   internet connection the first time, then it only checks the files);
5. gets the code ready for compiling in `build\src`
   (`packaging\protect.py`): the docstrings emptied, with every line kept
   in place, and the window's page minified into a module;
6. compiles `build\dist\OptiMind\OptiMind.exe` with
   [Nuitka](https://nuitka.net): the Python code becomes C, then machine
   code. One folder, about 90 MB, no console, with PresentMon in its
   `tools` folder;
7. writes `THIRD-PARTY-NOTICES.txt` (`packaging\notices.py`), with
   PresentMon's license;
8. compiles `dist\OptiMind-AI-Setup-<version>.exe` with Inno Setup
   (`packaging\installer.iss`), about 24 MB.

The first build downloads Nuitka's C compiler, GCC from
[winlibs](https://winlibs.com) (about 270 MB), and Dependency Walker,
which it uses to find the DLLs the program needs. Both are kept in
`%LOCALAPPDATA%\Nuitka` (no administrator rights needed; delete that
folder when you stop building). Compiling takes about 30 minutes the
first time; later builds reuse what didn't change and take about 5.

In the installed app, `sys.executable` isn't OptiMind.exe: Nuitka names
a `python.exe` there until `anyio` loads, then the program by its short
8.3 path. Code that starts OptiMind again uses `config.PROGRAM`.

The version comes from `app/__init__.py`. Setup closes a running OptiMind
before updating it (`OptiMind.exe --quit`), and keeps your settings,
data and start with Windows choice.

### Publishing a new version

OptiMind's update check (`app/service/updates.py`) reads the newest
release on a GitHub release page, about once a day, and tells the people
who installed OptiMind when a newer one is out, in Settings → About and
on Home, with a link to that page. It never downloads or installs
anything, and sends nothing about the PC: GitHub gets an ordinary
request, as when a browser opens the page.

1. Once: a free GitHub account, and a **public** repository (for example
   `OptiMind-AI`) that holds only a README: the release page needs no
   code. Its `owner/name` goes into the build as the default of
   `optimind_update_repo` in `app/config.py`: OptiMind's own is
   `Naka-Hiroshi/OptiMind-AI`, since 1.5.0.
2. For each version: on the repository's *Releases* page, *Draft a new
   release*, with the tag `v<version>` (for example `v1.5.0`, exactly the
   version in `app/__init__.py`), what's new in the notes, and the Setup
   file attached (`dist\OptiMind-AI-Setup-<version>.exe`), and no other
   Setup: OptiMind 2.0 and older show the first `.exe` on the page as
   the update (from 2.1.0 it takes the one named for the version).
   *Publish release*.

GitHub lists each attached file's SHA-256, and OptiMind shows it next to
the link, to compare with `(Get-FileHash <Setup>).Hash`. Drafts and
pre-releases aren't offered. Without an account GitHub answers 60
requests an hour from one address, far more than one a day.

### What the installer holds

- **No source code.** No `.py` file, and all of OptiMind's code is machine
  code, not Python bytecode (only parts of Python's own, public standard
  library come along as bytecode). Comments are gone after compiling, and
  `protect.py` empties the docstrings before. Like any program,
  `OptiMind.exe` still holds the text it shows and the names Python needs
  while it runs (modules, functions, settings).
- **The window's page, minified**: no comments or spaces, the scripts'
  names shortened, and stored compressed inside `OptiMind.exe`. A page is
  sent to the browser engine as text, so a determined person can still
  read this minified version on their own PC; no web page can be hidden
  completely.
- **None of your data**: no settings file, API keys, logs or history. Each
  PC starts with empty settings, and each person adds their own free API
  keys in Settings.
- The interactive API documentation (`/docs`) is left out.

The installer isn't signed with a code-signing certificate (they're
paid), so Windows SmartScreen may show "Windows protected your PC" on a
PC that downloaded it: **More info**, then **Run anyway**.
