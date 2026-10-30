# OptiMind AI

**Version 2.6.1** · Windows 11 app · runs entirely on your PC, at zero cost

OptiMind AI keeps Windows 11 responsive. It gives the app you're using the PC first, holds back busy background apps, compresses idle memory, switches the power plan with the load, and lets a free AI tune its rules every 15 minutes. While you play, it measures each game's frame rate and tells you what holds it back: the graphics card, the processor or a frame cap. Every change is checked with Windows and put back when it's no longer needed.

## Download

Get **OptiMind-AI-Setup-2.6.1.exe** from the [newest release](https://github.com/Naka-Hiroshi/OptiMind-AI/releases/latest), run it and follow Setup. It works on any Windows 11 PC or laptop (64-bit).

- Windows may show "Windows protected your PC", because the file isn't signed with a paid certificate: click **More info**, then **Run anyway**.
- Each release lists the Setup file's SHA-256. To check your download, open Terminal in the folder with the file, run `(Get-FileHash .\OptiMind-AI-Setup-2.6.1.exe).Hash` and compare the two.
- OptiMind 1.5.0 and newer say in **Settings → About** when a newer version is out here. They never download or install anything by themselves.

Source code is published in this repository (MIT license).

## What it does

- **Focus mode**: the app you switch to gets priority within milliseconds, while switching apps stays instant. While a game is in focus, background work is held back sooner, background apps using the graphics card get a lower GPU priority, and the PC switches to High performance; *Maximum* also keeps background apps on one CPU core.
- **Games**: while you play, short Intel PresentMon captures measure the frame rate and its 1% lows, and whether the graphics card, the processor or a frame cap limited each frame. Each session is summed up with what to do: lower GPU-heavy settings or DLSS/FSR when the card is the limit, raise quality for free when the processor is, move the game to the SSD, cool a GPU that slows down from heat, or turn off a recorder (Instant Replay, SteelSeries Moments…) capturing in the background. *Before you play* lists the apps holding the most memory, each with a Close button that works like its own X (OptiMind never closes an app by itself). Note what you changed on a session, and compare two sessions side by side.
- **Background apps**: busy apps you aren't using get lower priority, Windows 11 efficiency mode and low disk priority, and everything back the moment you return to them. Apps playing sound are never touched.
- **Memory**: idle background apps are compressed by Windows' own memory compression, identical memory pages are merged when no game runs, and the standby cache is purged only under real pressure that lasts. The two apps you used last are always kept ready for Alt+Tab. When memory ran nearly full during a game, the session names the apps holding the most of it.
- **Optimize page**: 22 reversible Windows 11 tweaks, each showing what it would free on your PC, safe disk clean-up, startup apps, and a hardware check that says what software can't fix (memory channels, PCIe links, games on a hard disk, the monitor's refresh rate, drive health), where the safe overclocking headroom is (NVIDIA's automatic tuning, AMD's PBO, a processor upgrade that fits the board), and a stability watch that reads Windows' logs for the hardware errors and driver resets an unstable overclock causes.
- **Drivers page**: every device and its driver (with Device Manager's problem codes explained), who signed each driver, your main drivers' age and where each one's updates come from, the drivers apps installed (anti-cheat, fan and RGB control…) with known risks flagged, what Windows' logs say about drivers, and how Windows guards against bad ones. It installs driver updates from Windows Update and NVIDIA's newest driver (checked to be signed by NVIDIA), after a restore point and never during a game, and never firmware or a BIOS. For the drivers AMD, Intel or your PC's maker deliver through their own app, it names the official one for your PC, says whether you have it and opens it. It also removes old driver versions nothing needs, and backs up every driver to another drive, to put them all back after a clean reinstall.
- **AI providers**: free AI providers (Google Gemini, Groq free tiers, a local model via Ollama, or any OpenAI-compatible custom endpoint such as NVIDIA Nemotron), with built-in rules when none is available. AI is advisory only — it suggests policy changes, never executes system actions directly.
- **24-hour monitoring**: per-hour baselines of normal CPU/RAM usage, anomaly detection, and performance trend vs yesterday — the AI reviews this full-day view to catch slow problems.
- **A light footprint**: OptiMind runs itself in efficiency mode, at about 0% CPU and under 100 MB of RAM. Its window is a separate process that closes with the window, so the agent stays light in the background.

**What no software can do.** Nothing can "increase CPU power"; that's hardware. OptiMind reduces waste and gives what's already there to what you're doing. That's real and measurable, but any tool that promises to "boost your CPU by X%" is overselling.

## Install

Run **`OptiMind-AI-Setup-2.6.1.exe`** (built with [packaging/build.ps1](packaging/build.ps1)) and click Yes on the Windows prompt. Setup:

- installs OptiMind in `C:\Program Files\OptiMind AI` and adds it to the Start menu (a desktop shortcut is optional);
- can start it when you sign in, in the notification area, with administrator rights and no prompt (a checkbox, on by default);
- adds an uninstaller to Settings → Apps. Uninstalling asks whether to keep your settings and data.

Requirements: Windows 11 (64-bit), on any PC or laptop, with an Intel or AMD processor and any graphics card and brand. Nothing else: Python and everything OptiMind needs are inside, and the window uses WebView2, which is part of Windows 11. Optional: a free Gemini and/or Groq API key, or a custom AI endpoint, entered in **Settings**. Without one, everything still works: the built-in rules tune the policy and the chat answers with a summary.
**Settings → About → What works on this PC** says what each PC gets: a few parts need administrator rights, NVIDIA's driver (temperatures and clocks) or a High performance plan (many laptops have Windows' Power mode instead), and the list says what to do when one is missing.

### Giving it to friends

The Setup file is all another PC needs. It holds none of your settings, API keys or data (each person adds their own free keys in Settings), and no readable source code: the code is compiled to machine code and the window's page is minified inside the program (details in [docs/development.md](docs/development.md#what-the-installer-holds)). It isn't signed with a paid code-signing certificate, so Windows may show "Windows protected your PC" the first time: click **More info**, then **Run anyway**.

If something doesn't work on a friend's PC, they can send you a report: **Settings → About → Create a report** saves one zip file on their Desktop with OptiMind's logs and their PC's details (maker and model, processor, graphics, drives, what works). Their user name, their PC's name, their API keys, their games' names and the AI's notes are left out.

When you publish a new version on OptiMind's GitHub release page ([github.com/Naka-Hiroshi/OptiMind-AI](https://github.com/Naka-Hiroshi/OptiMind-AI/releases)), their OptiMind sees it within a day and says so in **Settings → About** and on Home, with a link to the page (and the Setup file's SHA-256, to check it). It never downloads or installs anything by itself, and sends nothing about the PC. How to set up the page and publish a version: [docs/development.md](docs/development.md#publishing-a-new-version).

## Use

- **Open it** from the Start menu or its icon in the notification area. Starting it again while it runs just brings its window to the front.
- **Close the window** and OptiMind keeps working from the notification area. **Quit** from the icon's menu (or Settings → Quit OptiMind) to stop it: every app gets its own settings back.
- **Focus mode** (Off / Balanced / Maximum) is in the window's top bar, on the Agent page and in the icon's menu.
- OptiMind asks for administrator rights when you start it yourself; click **Yes** so it can also manage your apps that run as administrator, empty the standby list and merge memory pages (click **No** and it runs without those).

| Page | What you see |
|---|---|
| **Home** | A one-line verdict with the week's numbers (while you play: the game, its frame rate, 1% low and a chart of each capture), gauges for CPU, GPU, memory and storage, network and power, what needs attention (each with a button to fix it), what the agent is doing and the apps it's holding back, and the PC's hardware |
| **Performance** | A card for each of CPU, memory, disk, network and GPU with its number now, and its live chart (peak and low labeled, the AI's thresholds drawn in), the CPU's real clock speed, per-core CPU, memory composition, the memory committed against Windows' limit and the page file, drives, adapters, GPU engines and, on NVIDIA cards, temperature, clocks, power and why the card holds its clocks back |
| **Games** | The game you're playing (live, with the frame rate of each capture), *Before you play* (the apps holding the most memory, each with Close, and the recorders that recorded your last games), your last sessions side by side, two sessions compared, each session's frame rate, 1% lows, what limited it, what to do and your note on it, a Measure button, and your installed games with which to move to the SSD first |
| **Processes** | Every program, grouped like Task Manager, with CPU, memory (and its own memory when you point at it), disk, GPU, priority, why the agent leaves a program alone, and the memory committed against Windows' limit |
| **Optimize** | The tweaks as cards with an on/off switch each (by kind, or searched), the hardware check (with overclocking headroom and the stability watch), a restore point button and disk clean-up |
| **Drivers** | What needs attention, updates from Windows Update and NVIDIA (never firmware), the makers' own apps for your PC, your main drivers, who signed them and where their updates come from, every device by kind (as Device Manager groups them), drivers apps added, Windows' driver protection and logs, the driver store, and backup and restore |
| **Startup & services** | How many apps start with Windows and how many services run, startup apps with an on/off switch each, and services with their publisher |
| **Agent** | What OptiMind changed over the last 7 days, in numbers (memory taken back and merged, apps held back, help while you played, how busy the PC was, and each game's frame rate with Focus mode Off and on), what the agent is doing, Focus mode, the apps it's holding back, and the activity log |
| **AI** | Ask OptiMind about your PC, the policy the AI tunes (with the apps it protects, each one you can take off), its review history and today's provider usage |
| **Settings** | Language, start with Windows, administrator rights, measuring frame rates while you play, the AI providers (keys, models, order, a Test button), OptiMind's files, whether a newer version is out, a report to send for help, what works on this PC, and Quit |

The window is black, with blue for what you can click: the pages are a row of icons on the left, and the bar at the top has the page's name, what the agent is doing and Focus mode.

OptiMind speaks the language Windows shows, when it has it (Settings → Language picks another), with numbers, dates and plurals written the local way. It's in English and Portuguese (Brazil) so far, the agent's own texts included (its advice, findings and Activity); more languages are on the way.

## Safety

- The AI never touches the system: it proposes a policy (thresholds and switches) as JSON that is validated before use.
- Essential Windows processes, security software and typing helpers are protected in code, and Windows services, other accounts' programs and protected processes are always left alone.
- Every change is read back from Windows; one that didn't stick is logged as an error, never as "applied".
- Everything the agent changes is put back when the app returns to the front, when OptiMind quits, or at the next start after a crash.
- Tweaks save the old settings first, and turning one off restores them exactly.
- Drivers come only from Windows Update and the makers' own servers; every install starts with a restore point, waits while a game runs, and never puts in a driver older than yours. NVIDIA's installer only starts if Windows confirms NVIDIA signed it. OptiMind never installs firmware or a BIOS, not even the firmware updates Windows Update offers.
- API keys are saved encrypted for your Windows account (DPAPI).
- The local API refuses websites, and only OptiMind's own window can change things through it: it gets a secret, new at every start, that other Windows accounts on the PC can't get. A program running under your own account could still read it from the window's memory, as Windows allows between programs of one account.
- That's why the riskiest changes (turning off Memory integrity, installing drivers from a backup) also wait for your Yes in a Windows dialog OptiMind shows itself, which programs with fewer rights can't answer for you.

## From source

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python packaging\get_tools.py      # PresentMon for the Games page (checked download)
.\OptiMind.cmd                                   # or: .venv\Scripts\python run.py
python -m unittest                               # tests (they never change your PC)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1   # the installer, in dist\
```

Running from source uses `.env` and `data\` in the project folder; the installed app keeps its settings and data in `%LOCALAPPDATA%\OptiMind`. Details in [docs/development.md](docs/development.md).

## Documentation

| | |
|---|---|
| [docs/agent.md](docs/agent.md) | Everything the agent does, Focus mode, safety nets, administrator rights, the AI providers |
| [docs/optimize.md](docs/optimize.md) | Every tweak and how it's applied, the hardware check, disk clean-up |
| [docs/drivers.md](docs/drivers.md) | The Drivers page: what it checks and where from, installing drivers safely, the driver tools, the sources |
| [docs/architecture.md](docs/architecture.md) | How it's built (agent, window, tray), the code layout, files, the local API |
| [docs/performance-model.md](docs/performance-model.md) | The research: where apps wait, and what Windows offers for each |
| [docs/development.md](docs/development.md) | Running from source, testing, settings, building the installer, where new code goes |
| [docs/limitations.md](docs/limitations.md) | Honest limitations and ideas for future work |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each version |

## Project layout

```
optimind-ai/
├── run.py, OptiMind.cmd   # start here (OptiMind.exe is run.py, compiled)
├── app/
│   ├── agent/             # decides and acts: rules, Focus mode, optimizer, safety, game sessions
│   ├── system/            # reads the PC: monitor, processes, memory, GPU sensors, frames, hardware check, drivers
│   ├── tools/             # what you start: tweaks, clean-up, startup apps, restore points, driver updates and tools
│   ├── ai/                # free AI providers, policy review, chat
│   ├── desktop/           # the Windows app: window, tray icon, start with Windows
│   ├── api/  service/     # local API, the agent's loops, the Settings page's file
│   ├── i18n/              # OptiMind in other languages: one file per language
│   └── static/            # the interface (one page) and the icon
├── packaging/             # build.ps1 (Nuitka), protect.py, Inno Setup script, get_tools.py, i18n_keys.py
├── tools/                 # PresentMon.exe (downloaded and checked by get_tools.py; not in the repo)
├── tests/                 # automated tests (they never change your PC)
└── docs/
```
