# Architecture

How OptiMind AI is put together: the agent (one Python process with the
tray icon, a local API and the agent's loops) and OptiMind's window (a
second, short-lived process that shows the interface).

## The app: agent and window

```
 OptiMind.exe (agent, runs until you Quit)                OptiMind.exe --window
┌──────────────────────────────────────────────┐        (only while it's open)
│ tray icon (pystray)   one-at-a-time mutex    │       ┌─────────────────────────┐
│ app/desktop/shell.py: opens / focuses ───────┼──────►│ native Windows 11 window│
│   the window, Quit, sign-out                 │ starts│ (pywebview + WebView2)  │
│                                              │       │ app/desktop/window.py   │
│ local API 127.0.0.1:8765 (FastAPI) ◄─────────┼───────┤ shows dashboard.html,   │
│   interface + /api/...                       │  HTTP │ which polls the API     │
│ the agent's loops (below)                    │       └─────────────────────────┘
└──────────────────────────────────────────────┘
```

- **The agent** is the long-running process: started from the Start
  menu (with the administrator prompt), or at sign-in by a scheduled task
  (`app/desktop/autostart.py`, with administrator rights and no prompt).
  It holds a named mutex, so a second start only asks the running one to
  open its window (`app/desktop/instance.py`).
- **The window** is a separate process, started by the agent when you
  open OptiMind and gone when you close it, so the browser engine that
  draws it (WebView2, part of Windows 11) takes no memory while OptiMind
  works in the background. It only shows the agent's own page: websites,
  and the makers' driver apps from your Start menu, open with your
  normal rights.
- The installed `OptiMind.exe` is `run.py` compiled to machine code with
  Nuitka, with the window's page inside it (minified); from source,
  `python run.py` does exactly the same with Python.

## The agent's loops

```
┌─────────────────────────────────────────────────────────────────┐
│                     the agent (run.py)                          │
│                                                                 │
│  ┌──────────────┐    ┌──────────────────────────────────┐       │
│  │ Tray icon,   │    │      FastAPI (app/main.py)       │       │
│  │ window       │    │  interface /    API  /api/...    │       │
│  │ (desktop/)   │    └────────────────┬─────────────────┘       │
│  └──────────────┘                     │ lifespan starts:        │
│                                       ▼                         │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │        Orchestrator (app/service/orchestrator.py)         │  │
│  └──────────┬───────────────────────────────────┬────────────┘  │
│             ▼                                   ▼               │
│  ┌──────────────────────┐             ┌──────────────────────┐  │
│  │ FAST LOOP (~2s)      │             │ AI LOOP (~15 min)    │  │
│  │ no AI calls          │             │                      │  │
│  │                      │             │ policy_brain         │  │
│  │ monitor.take_snapshot│             │   .review_policy()   │  │
│  │        │             │             │        │             │  │
│  │        ▼             │             │        ▼             │  │
│  │ decision_engine      │◄─ updated ──┤ free AI provider:    │  │
│  │   .decide(snapshot,  │   policy    │ Gemini → Groq →      │  │
│  │           policy)    │             │ local rules (JSON)   │  │
│  │        │             │             │        │             │  │
│  │        ▼             │             │        ▼             │  │
│  │ safety.validate      │             │ validates (pydantic) │  │
│  │        │             │             │ and saves to disk    │  │
│  │        ▼             │             └──────────────────────┘  │
│  │ optimizer.apply      │                                       │
│  │ (checked with Windows│   + Focus mode's thread (focus.py):   │
│  │  and put back later) │     reacts the instant you switch apps│
│  └──────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────┘
```

Two speeds, on purpose:

- **Fast loop (local, no AI):** runs every 1-2 seconds. It has to be
  instant and cheap, so it's 100% deterministic heuristics in
  `app/agent/decision_engine.py`, with no network calls.
- **AI loop:** runs every ~15 minutes (configurable). AI calls take
  seconds and free tiers have daily limits, so they can't truly run in
  "real time". Instead, the AI looks at the aggregated history
  (`app/system/monitor.py` stores a summary, not every raw process) and
  the latest actions taken, and returns a **structured policy**
  (thresholds, which optimizations to turn on/off) as JSON that must
  match a schema: never free text, never direct access to the system.

Plus **Focus mode's own thread** (`app/agent/focus.py`): Windows tells it
the moment another window comes to the front (a WinEvent hook, no
polling), and it reacts within milliseconds, between two fast-loop
cycles.

Each fast-loop cycle also hands the snapshot to the **game tracker**
(`app/agent/games.py`, in a worker thread): it follows the game in front,
samples the CPU, memory and the graphics card's sensors, and starts and
collects the PresentMon captures. PresentMon runs as its own process
(`tools/PresentMon.exe`), and a thread reads its CSV output from a pipe as
it comes, so a capture never holds up the loop. While a game is in focus
the snapshot also reads each process's GPU use (Windows' `GPU Engine`
counters, about 2 ms), which the decision engine uses for GPU priority
and the tracker for spotting background recording.

Every action, whether it comes from the local heuristics or from a
policy adjusted by the AI, goes through `app/agent/safety.py` before
being applied. There is a fixed list of essential Windows processes
that can **never** be touched, even if the AI "suggests" it; this
validation is done in code, not delegated to the model.

The research behind these choices (where a program's work waits on the
way through Windows and the hardware, and which switch addresses each
wait) is in [performance-model.md](performance-model.md).

## Code layout

```
optimind-ai/
├── run.py                     # entry point (= OptiMind.exe): agent, window, --quit, start with Windows
├── OptiMind.cmd               # double-click launcher from source (uses .venv)
├── requirements.txt           # what OptiMind needs; requirements-build.txt adds Nuitka
├── .env.example               # settings template (copy to .env when running from source)
├── app/
│   ├── __init__.py            # version number
│   ├── config.py              # settings (environment / settings file), file locations
│   ├── models.py              # shared pydantic schemas (snapshot, actions, policy, settings)
│   ├── main.py                # FastAPI app, lifespan, interface route, local-only guard
│   ├── desktop/               # the Windows app around the agent
│   │   ├── shell.py           # tray + window + Quit + sign-out, on the agent's side
│   │   ├── window.py          # the window process: WebView2, title bar, size and place
│   │   ├── tray.py            # notification-area icon and menu
│   │   ├── instance.py        # one OptiMind at a time; --quit
│   │   ├── confirm.py         # OptiMind's own Windows dialog for the riskiest changes
│   │   ├── autostart.py       # start with Windows (a scheduled task, highest privileges)
│   │   └── icon.py            # the logo, drawn in code (tray icon, app icon, installer)
│   ├── agent/                 # decides and acts
│   │   ├── decision_engine.py # fast-loop rules: what to hold back, compress, merge, switch
│   │   ├── focus.py           # Focus mode: reacts the instant you switch apps
│   │   ├── games.py           # game sessions: frame rates, what limited them, advice
│   │   ├── impact.py          # what OptiMind changed, in numbers; games with Focus mode Off and on
│   │   ├── optimizer.py       # applies actions on Windows, checks them, puts them back
│   │   ├── safety.py          # protected processes, validation of every action
│   │   ├── policy_store.py    # policy persistence and review history
│   │   └── logger.py          # action log (JSON lines)
│   ├── system/                # reads the PC
│   │   ├── monitor.py         # snapshots: CPU/RAM/processes (one PDH query), GPU use per process, history
│   │   ├── sysinfo.py         # dashboard details: cores, real CPU speed, memory, drives, GPU, network
│   │   ├── processes.py       # every program, grouped like Task Manager
│   │   ├── memory.py          # Windows' memory lists and compression statistics
│   │   ├── audio.py           # apps playing sound (never held back)
│   │   ├── gpu.py             # NVIDIA sensors through NVML (read-only)
│   │   ├── frames.py          # PresentMon: runs it, reads its frames, what limited them
│   │   ├── display.py         # monitors: resolution, refresh rate and the most they offer
│   │   ├── stability.py       # Windows' event logs: hardware errors, driver resets, crashes
│   │   ├── hardware.py        # hardware check: memory, PCIe links, drives, games, headroom
│   │   ├── drivers.py         # the Drivers page: findings, main drivers, where updates come from
│   │   ├── devices.py         # every device and its driver, Device Manager's problem codes (CfgMgr32)
│   │   ├── driver_store.py    # driver packages from other makers (pnputil XML), old versions, signers
│   │   ├── maker_tools.py     # the makers' own driver apps for this PC, and whether each is installed
│   │   ├── software_drivers.py # kernel drivers apps installed, who made them, known risks
│   │   ├── driver_events.py   # driver events in Windows' logs; Code Integrity's protection state
│   │   ├── capabilities.py    # what works on this PC, and why not (any PC, laptop or brand)
│   │   ├── admin_rights.py    # administrator rights (the UAC prompt) and privileges
│   │   └── dpapi.py           # API keys encrypted for your Windows account
│   ├── tools/                 # what you start from the dashboard
│   │   ├── tweaks.py          # Windows 11 tweaks with backup and undo
│   │   ├── local_policy.py    # Local Group Policy files (what gpedit.msc edits)
│   │   ├── cleanup.py         # safe disk clean-up
│   │   ├── close_app.py       # closing an app you pick (Before you play), as its own X does
│   │   ├── startup_manager.py # startup apps (Task Manager's StartupApproved switch)
│   │   ├── restore_point.py   # System Restore points
│   │   ├── driver_updates.py  # Windows Update (its own API) and NVIDIA's newest driver: check, install
│   │   ├── driver_actions.py  # PnPUtil: scan, restart/turn on a device, old versions, backup, restore
│   │   ├── driver_jobs.py     # one long driver task at a time, with its progress
│   │   └── report.py          # the report to send for help: a zip without names, keys or games
│   ├── ai/
│   │   ├── providers.py       # free AI providers + fallback chain
│   │   ├── policy_brain.py    # AI loop: policy review
│   │   ├── local_brain.py     # built-in rule-based fallback
│   │   ├── prompts.py         # prompts + policy JSON schema
│   │   └── chat.py            # "Ask OptiMind" chat
│   ├── api/
│   │   ├── __init__.py        # LocalizedRoute: each answer keeps its texts until it's translated
│   │   ├── routes.py          # REST endpoints under /api: the agent, Optimize, Startup
│   │   ├── games_routes.py    # /api/games: the Games page
│   │   ├── drivers_routes.py  # /api/drivers: the Drivers page
│   │   └── app_routes.py      # /api/app and /api/settings: window, Quit, start with Windows, Settings page
│   ├── i18n/                  # OptiMind in other languages (see development.md)
│   │   ├── __init__.py        # the language now, tx()/txn() (texts the API translates as it sends them), plural rules, the page's texts
│   │   └── pt-BR.json, …      # one language each: the English text, and its translation
│   ├── service/
│   │   ├── orchestrator.py    # fast loop + AI loop
│   │   ├── state.py           # shared in-memory state
│   │   ├── preferences.py     # the Settings page: reads and saves the settings file
│   │   ├── updates.py         # the update check: the newest release on OptiMind's GitHub release page
│   │   └── access.py          # who may change things: only OptiMind's window has this start's secret
│   └── static/
│       ├── dashboard.html     # the interface (one self-contained page, works offline)
│       └── optimind.ico       # the app's icon (made by packaging/make_assets.py)
├── packaging/
│   ├── build.ps1              # builds the installer in one command (see development.md)
│   ├── protect.py             # the code ready for compiling: docstrings emptied, the page minified
│   ├── installer.iss          # Inno Setup script for OptiMind-AI-Setup-<version>.exe
│   ├── get_tools.py           # downloads PresentMon.exe and esbuild.exe and checks their SHA-256
│   ├── i18n_keys.py           # every text to translate, and what each language still lacks
│   ├── make_assets.py         # draws the icon and the installer's images
│   ├── notices.py             # writes THIRD-PARTY-NOTICES.txt
│   └── third_party/           # licenses of the tools shipped as programs (PresentMon)
├── tools/                     # PresentMon.exe (from get_tools.py; not part of the code)
├── tests/                     # automated tests (python -m unittest), see development.md
├── docs/                      # this documentation
├── data/                      # from source: created at runtime (see below), not part of the code
└── build/, dist/              # created by the build, not part of the code
```

The three folders under `app/` follow one rule: **system** only reads,
**agent** decides and acts on its own (always through `safety.py`, and
always undoable), and **tools** only act when you press a button.

## Files

The installed app keeps everything that is yours in
`%LOCALAPPDATA%\OptiMind` (Settings → Files opens these folders):

| Where | What |
|---|---|
| `settings.env` | Your settings (the Settings page edits it; API keys encrypted) |
| `data\` | The data files below |
| `logs\` | `optimind.log` (the agent) and `window.log` (the window), 1 MB each, the last 3 kept |
| `WebView2\` | The window's browser-engine cache |

Its program files are in `C:\Program Files\OptiMind AI` (read-only,
replaced by an update). Running from source, the settings file is `.env`
and the data and logs are in the project folder.

OptiMind keeps its state in `data\` (or the folder `OPTIMIND_DATA_DIR`
points to). Deleting a file resets that part; nothing else depends on it.

| File | What it holds |
|---|---|
| `action_log.jsonl` | Everything the agent and you did, with the reason and the checked result (the Activity log); in English, with how to translate each text beside it (from 2.3.0) |
| `policy_state.json` | The current policy: thresholds, which optimizations are on, your Focus mode choice, the AI's protected apps with when each was added, and the ones you took off |
| `policy_history.jsonl` | Past policy reviews: who made them (AI provider or local rules) and why |
| `metrics_history.jsonl` | CPU/RAM samples (the charts' history, and the AI's view of how the PC is used) |
| `held_back.json` | Apps the agent is holding back right now, with their original settings, so a restart after a crash can put them back |
| `tweaks_backup.json` | The settings each tweak replaced, so turning it off can put them back exactly. Keep it while tweaks are applied |
| `game_sessions.jsonl` | Your game sessions (the last 200): frame rate, what limited it, the Focus mode used (from 1.4.0) and the frame rate in each mode, from captures that ran in one mode (from 1.5.1), the sensors' summary, the memory committed against its limit, the reads back from disk and the apps holding the most memory when it was fullest (from 2.1.0), the advice, and your note on what you changed for it (from 2.2.0) |
| `driver_updates.json` | The last Windows Update driver search and NVIDIA check, so Home and the Drivers page have them after a restart |
| `nvidia_products.json` | NVIDIA's product list (to find your card's ID for its driver lookup), refreshed every 30 days |
| `window.json` | The window's size and place |
| `notices.json` | One-time tips already shown (e.g. "OptiMind is still running" after closing the window) |
| `update_check.json` | The update check's last answer: the newest version on the release page, its page and Setup file, when it was asked |

## Local API

The interface uses these endpoints; they're also open to scripts on
this PC (interactive documentation at `http://127.0.0.1:8765/docs` when
running from source; the installed app leaves it out).
Requests from websites are refused: see
[agent.md](agent.md#administrator-rights).

| Endpoint | What it does |
|---|---|
| `GET /api/status` | Version, latest snapshot, policy, Focus mode, what is held back, AI provider status |
| `GET /api/system`, `GET /api/processes`, `GET /api/history` | Live details, programs grouped like Task Manager, CPU/RAM history |
| `GET /api/logs` | Recent actions with their results, each with its `status`, `by_you` and `freed_mb` (what the page goes by) |
| `GET /api/impact?days=7` | What OptiMind changed, in numbers (memory taken back and merged, apps held back, help while you played, how busy the PC was), and each game measured with Focus mode Off and on, side by side |
| `POST /api/logs/remove` | Delete activity entries: `{"entries": [{timestamp, action, target}]}`, a time range (`since`/`until`), a result (`status`), or `{"everything": true}` |
| `GET /api/focus`, `POST /api/focus` | Focus mode state; switch it (`{"enabled": true, "mode": "maximum"}`) |
| `GET /api/policy`, `GET /api/policy/history`, `POST /api/policy/refresh-now` | The policy, its review history, a review right now |
| `POST /api/policy/unprotect` | Takes an app off the AI's protected apps (`{"name": "Code.exe"}`); the AI won't put it back |
| `POST /api/chat` | Ask OptiMind (answers from the free AI providers or the local rules) |
| `GET /api/app` | Version, administrator rights, start with Windows, where the files are |
| `GET /api/app/language` | OptiMind's language now, the one chosen in Settings (`""`: Windows'), Windows' own, the languages it has and the translations (the page gets these with itself) |
| `GET /api/app/capabilities` | What works on this PC and why not: administrator rights, frame rates, the graphics card's sensors, the power plan, the cores Maximum mode uses, the newest graphics driver |
| `GET /api/app/update`, `POST /api/app/update/check` | Whether a newer OptiMind is on the release page (a check older than a day runs again in the background); check now |
| `POST /api/app/window`, `POST /api/app/quit`, `POST /api/app/restart-as-admin` | Open the window, quit, restart with administrator rights (desktop app only) |
| `PUT /api/app/autostart`, `POST /api/app/open-folder/{name}` | Start with Windows on/off; open a folder in File Explorer |
| `POST /api/app/report`, `POST /api/app/report/show` | Save a report to send for help (a zip on the Desktop: the PC's details and OptiMind's logs, without the user name, the PC's name, API keys, game names or the AI's notes); show the last one in File Explorer |
| `GET /api/settings`, `PUT /api/settings`, `POST /api/settings/test-ai/{provider}` | The Settings page (API keys are write-only), and its Test button |
| `GET /api/hardware` | The hardware check (read-only) |
| `GET /api/games?sessions=30` | The game being played (live, with advice so far and the frame rate of its last 20 captures, for the charts on Home and the Games page), the last sessions, and the installed games with their drives |
| `POST /api/games/measure` | Measure the running game's frame rate for 60 seconds (409 without a game, or when PresentMon can't run) |
| `GET /api/games/before-play` | Before you play: memory now (and committed, against its limit), the apps holding the most memory of their own (150 MB or more, the game left out) and whether each has a window to close, and the recorders that recorded one of the last 2 sessions (running or not: many start only with a game) |
| `POST /api/games/note` | Your note on a session (`{"started", "exe", "note"}`, up to 120 characters; `""` removes it) |
| `POST /api/apps/close` | Close an app you picked (`{"name": "Discord.exe"}`): its windows are asked to close, as its own X does, and nothing is ended by force. Never Windows' own processes, security software, OptiMind or another account's apps (403); 404 when it isn't running, 409 without a window. In test mode it only says what it would close. Logged in the activity |
| `GET /api/tweaks`, `POST /api/tweaks/{id}/apply`, `POST /api/tweaks/{id}/undo`, `POST /api/tweaks/apply-safe` | Tweaks with their state and what they'd free; apply/undo |
| `GET /api/cleanup`, `POST /api/cleanup` | What clean-up would free; clean the chosen locations |
| `GET /api/restore-point`, `POST /api/restore-point` | Restore point status; create one |
| `GET /api/startup-items`, `POST /api/startup-items/{name}/disable` (or `/enable`) | Startup apps |
| `GET /api/services` | Windows services (read-only) |
| `GET /api/drivers?refresh=false` | The Drivers page: findings, main drivers, every device, drivers apps added, the driver store, driver events, protection, firmware, updates, backups (a scan is kept 10 minutes) |
| `GET /api/drivers/summary`, `GET /api/drivers/job` | What needs attention from the last scan (never waits for one); the driver task running now or last, with its progress |
| `GET /api/drivers/tools` | The makers' own driver apps for this PC from the last scan, with whether each is in the Start menu (the window opens only one of these) |
| `POST /api/drivers/windows-update/check`, `POST /api/drivers/windows-update/install` | Ask Windows Update for driver updates now; install some (`{"ids": [...]}`, never one older than yours) |
| `POST /api/drivers/nvidia/check`, `POST /api/drivers/nvidia/install` | Ask NVIDIA for the newest driver now; download it from NVIDIA, check NVIDIA's signature and start its installer |
| `POST /api/drivers/scan`, `POST /api/drivers/devices/restart`, `POST /api/drivers/devices/enable` | Scan for hardware changes; restart a device, or turn one on (`{"id": "<instance ID>"}`) |
| `POST /api/drivers/packages/remove` | Remove old driver versions (`{"names": ["oem12.inf"]}`, only the ones marked removable) |
| `GET /api/drivers/backups`, `POST /api/drivers/backup`, `POST /api/drivers/backups/restore`, `POST /api/drivers/backups/open` | Driver backups: list, make one (`{"drive": "D:"}`), install one, open its folder (`{"path": ...}`) |
