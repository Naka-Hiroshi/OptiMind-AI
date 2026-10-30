# Changelog

## 2.6.1 (2026-10-30)

### Fixed

- **Custom AI provider "empty answer" error.** When a custom OpenAI-compatible provider (NVIDIA Nemotron, local LLMs) returned `content: null` or blank content, "Review on Optimization" showed `Skipped: custom: empty answer`. The error now shows the provider's response body so it can be diagnosed, translated in all 11 languages.

## 2.6.0 (2026-10-30)

### Added

- **24-hour monitoring.** OptiMind now builds a per-hour baseline of normal CPU/RAM usage from the last 7 days, detects anomalies (spikes outside normal ranges for that hour of day), and compares performance against yesterday — improving, degrading or stable. The AI policy brain reviews this full-day view, so it catches slow problems the last-15-minutes view misses (a background app gradually leaking memory, a game that used to run well but is now slower). Everything stays advisory — the AI adjusts policy thresholds, never executes system actions directly.
- **24-hour monitoring API.** `GET /api/monitoring/24h` returns per-hour baselines, recent anomalies and the trend vs yesterday; `GET /api/monitoring/daily` returns daily aggregate history for up to 90 days. The dashboard shows baseline saved time, anomaly count and performance trend at a glance.
- **Custom AI provider.** In Settings → AI providers, you can now enter the base URL for a custom OpenAI-compatible `/chat/completions` endpoint alongside its API key and model, so OptiMind can use any compatible AI service (not just Gemini, Groq and Ollama). The base URL field was missing from the Settings UI — it now appears for any provider that has one, including Custom AI.
- **Game-GPU optimization.** When a game is in focus and the graphics card is heavily used (at or above 85 % for 10 seconds), OptiMind turns on the NVIDIA shader cache and Windows Game Mode so the game gets more FPS on minimal hardware. When the game leaves focus or the GPU calms down (at or below 70 % for 30 seconds), both settings go back the way they were. Nothing in the game or its files is touched — only Windows and driver settings, which are always restored.
- **i18n complete in all 11 languages.** The 4 new monitoring texts (performance stable, CPU/RAM higher than yesterday, CPU/RAM lower than yesterday, broadly the same) are translated in de, es, fr, it, ja, ko, pl, pt-BR, ru, tr and zh-Hans, kept complete by `tests/test_i18n.py`.

### Fixed

- **AI policy review crash.** When recent agent actions existed, `build_24h_context()` crashed because `Counter(...items()` yields `(action_string, count)` tuples and each key is already a string, not a dict. Fixed so the AI review loop and `/api/monitoring/24h` work with any action history.
- **Custom AI provider compatibility.** `chat_completion()` now retries with plain free text when a custom OpenAI-compatible provider (NVIDIA Nemotron, local LLMs, etc.) rejects `json_schema` structured output. Every custom provider gets a second chance, so AI policy reviews and chat work with any compatible endpoint — not just Gemini, Groq and Ollama.

## 2.5.0 (2026-10-29)

Better performance, quality and efficiency on Windows 10 and Windows 11 — optimized for CPU, RAM and GPU usage, so even a PC with minimum hardware stays responsive and games get more FPS.

### Added

- **Hardware-adaptive thresholds.** OptiMind detects your PC's profile (low RAM under 8 GB, weak CPU under 4 logical cores, a hard disk) and acts sooner on minimum-spec hardware: idle apps are compressed earlier on low-RAM PCs, background CPU hogs are reined in faster on weak CPUs, and a weak CPU switches to the High performance power plan sooner.
- **OptiMind yields to your apps.** When the PC is busy (CPU load at or above 70 %), OptiMind steps down to Below normal priority and gives your games and apps the cores back; it returns to Normal priority once the load eases (at or below 50 %).
- **Turn off Windows Search indexing** (Optimize → Storage): stops Windows from constantly indexing your files. On a hard disk this saves a lot of disk activity and CPU, which you feel while you play. Only offered when a hard disk is present.
- **Adjust visual effects for best performance** (Optimize → Visual effects): turns off window animations, shadows and smooth screen fonts so Windows uses less GPU and CPU, making the UI snappier.
- **Better game detection.** Games installed outside the standard library folders are recognized by name (about 45 known game processes), so Focus mode boosts them on minimum-spec PCs too.

### Fixed

- **Test mode now reliably detected.** `OPTIMIND_DRY_RUN=true` set in the environment is read at runtime, not only when `app.config` is first imported, so test mode is honored by `app_state.dry_run`, the Drivers page tools (`scan_hardware`, `backup`, `remove_old_packages`, `restore`) and closing an app no matter which test module loads first. Applied to `app/config.py`, `app/service/orchestrator.py`, `app/tools/driver_actions.py`, `app/tools/driver_updates.py` and their `build/src/app/...` copies.

## 2.4.0 (2026-10-27)

OptiMind keeps Windows 11 responsive, with AI tuning its rules — now on Windows 10 too, with better performance, quality and efficiency across both.

### Added

- **Backup & restore** (Settings → General): back up your current settings to a file (`opti18n_backup.json`) or restore them from a saved backup. Only your settings file is touched; API keys stay encrypted. `POST /api/app/backup/export` returns all settings, `POST /api/app/backup/import` loads them back.
- **Windows 10 support**: installer and app verified to run on Windows 10 as well as Windows 11 (Python 3.10+, WebView2 on Windows 10, administrator rights for power plan and priority management).
- Version bumped to 2.4.0 (`app/__init__.py`).

### Performance

- Settings are saved and applied right away; backup/import only touch known settings so nothing unknown lingers in the settings file.
- The settings page reuses the same localized route pattern as the rest of the app, keeping the API surface small.

### Changed

- `POST /api/app/backup/export` returns every current setting as a dictionary.
- `POST /api/app/backup/import` takes a backup dictionary, imports only known settings, and returns the current settings.
- All 11 non-English language catalogs complete with the new backup/restore texts (`packaging/i18n_keys.py`, `tests/test_i18n.py`).

### Fixed

- **Test mode now reliably detected.** `OPTIMIND_DRY_RUN=true` set in the environment is read at runtime, not only when `app.config` is first imported, so test mode is honored by `app_state.dry_run`, the Drivers page tools (`scan_hardware`, `backup`, `remove_old_packages`, `restore`) and closing an app no matter which test module loads first. Fixes `test_status`, `test_closing_from_the_window_only_and_logged` and the Drivers page ActionTests (`scan_hardware`, `backup`, `remove_old_packages`, `restore`, `test_test_mode_only_says_what_it_would_do`). Applied to `app/config.py`, `app/service/orchestrator.py`, `app/tools/driver_actions.py`, `app/tools/driver_updates.py` and their `build/src/app/...` copies.

## 2.3.0 (2026-10-27)

OptiMind in your language.

### Added

- **Language** (Settings → General): OptiMind in the language Windows
  shows, when OptiMind has it, or the one you pick; English otherwise.
  The page gets its texts with the page itself, so it never shows
  English first, and numbers, percentages, sizes, dates, lists and
  plurals follow the language (`1.234,5`, `50 %`, "1 proceso,
  2 procesos"). The agent's own texts come in it too: the Activity
  list with each result and reason, the games' advice, the tweaks,
  the hardware and driver checks, the errors, the tray menu and
  OptiMind's own dialogs. Now complete in 10 languages:
  de, es, fr, it, ja, ko, pl, ru, tr, zh-Hans; Portuguese (Brazil)
  was already complete.
- The agent, its log and the AI still work in English: each text the
  agent makes keeps its English, and the API translates it as it sends
  it. Files keep the English too, with how to translate it beside it
  (`"result"` and `"result_i18n"` in the activity log, the game
  sessions, the policy history and the driver and update checks), so
  they read as they always did; entries from before 2.3.0 stay
  English. The report for help leaves the translation fields out.
- `packaging/i18n_keys.py` lists every text to translate (2,272: 1,248
  in the window and 1,024 of the agent, about 17,200 words) and what
  each language still lacks;
  `tests/test_i18n.py` keeps every language complete, with the English
  `{names}` and the plural forms its language needs.

### Changed

- A few English texts read better on the way: a list ends with "and"
  ("Code (2.2 GB), Brave (1.5 GB), and Discord (1.0 GB)"), the Library
  says "12 games installed through Steam or Epic", one tip is "1 tip",
  and the AI's policy says "1 min ago, the default rules" before its
  first review.
- A text with a number has a singular and a plural now: "1 instance",
  not "1 instance(s)" (likewise processes, windows, drivers, pages,
  errors and "1 unexpected shutdown"), and "unknown location" or
  "locations".
- The page no longer reads the agent's English to decide what to show:
  `GET /api/logs` gives each entry's `status`, `by_you`, `freed_mb` and
  `target_shown`, `GET /api/processes` why a program is left alone
  (`blocked_kind`), and closing an app or a quick driver change says
  `simulated` in test mode.

## 2.2.1 (2026-10-24)

### Fixed

- **Before you play now names the recorders before you play.** It named
  a recorder only while its program ran, but SteelSeries GG Moments and
  Discord Clips start their capture with the game: on a real PC the hint
  showed while playing, not before. Now a recorder that recorded one of
  your last 2 sessions is named whether it runs or not, and two sessions
  after you turn it off, it's gone. Test in `tests/test_games.py`.

## 2.2.0 (2026-10-23)

Before you play: what holds the memory, closed by your choice, and a
fair way to test a change.

### Added

- **Before you play** (Games page): memory now and the memory committed
  against Windows' limit, the apps holding the most memory of their own
  (150 MB or more; Windows' own processes, security software, OptiMind
  and the game being played aren't listed), and the recorders that
  recorded one of your last 5 sessions and run now, with where to turn
  them off. `GET /api/games/before-play`.
- **Close**, for an app there with an open window: after your Yes,
  OptiMind asks its windows to close, as the app's own X does, so it
  saves or asks about your work as it always does. Nothing is ended by
  force and OptiMind never closes anything by itself. An app that only
  hides in the notification area (Discord, Steam, Spotify…) is said to
  still run, with where to quit it; one still open after a few seconds
  is usually asking something. Only apps of your own account, never
  Windows' own, security software or OptiMind; each close is in the
  activity log, and in test mode nothing is closed. `POST /api/apps/close`,
  `app/tools/close_app.py`. Tests: `tests/test_close_app.py`, with
  throwaway processes and off-screen windows of their own.
- **Your note on a session** (*Add a note: what you changed*), saved
  with the session; the AI chat sees it. `POST /api/games/note`.
- **Compare two sessions**: any two measured sessions side by side, by
  default the newest and the one before it of the same game, with the
  change from one to the other (frame rate, 1% low, frame time spikes,
  what limited it, temperatures, memory and committed memory, the reads
  back from disk, Focus mode, the drive and your notes).
- **Processes**: a program's own memory when you point at its memory,
  and the memory committed against Windows' limit at the top.

### Changed

- The AI chat knows the apps holding the most memory of their own, and
  your notes on your sessions; the built-in answer names the apps too.
- A game session's tip never names OptiMind's own window among the apps
  holding the most memory.
- Changes you make yourself are logged as *Logged only* when test mode
  only said what it would do.
- Tests: 355 in all.

## 2.1.0 (2026-10-23)

Memory, measured: what a game test on a real PC showed, and the good
part of an outside review of OptiMind's memory rules.

### Changed

- **Memory pressure has to last.** The agent frees memory once RAM has
  stayed at the policy's line for 10 seconds (a game loading or a
  moment's spike isn't pressure), and stops once RAM is 5 points below
  the line, instead of switching on and off around it. The standby
  purge waits for the same.
- **Page combining waits for the game to close**, and for a calm
  processor (below 50% for the last 30 seconds). It only waited while a
  game was in front: during an Alt+Tab out of a game it scanned all
  memory for 17.5 s on a 4-core processor already at 94-99%.
- **The AI's protected apps** (AI page). The AI adds an app when you
  keep switching back to it after the agent held it back, and the list
  only grew (on a real PC: 26 apps, VS Code, Brave and Discord among
  them, about 7 GB of memory). Now the list keeps only their CPU, disk
  and GPU priority as they are: their memory can still be compressed
  while they sit idle in the background (never the app in front, one of
  your last apps or one playing sound). Each app leaves the list 14 days
  after it was added, the AI page shows when, and **×** takes one off:
  the AI won't put it back. `POST /api/policy/unprotect`.
- **Game sessions note the commit charge**: the memory Windows has
  promised to apps, against its limit (the RAM plus the page file), and
  what it read back from disk for memory it didn't have. A session near
  the limit gets a tip, *Windows was near its memory limit*, naming the
  apps holding the most memory of their own; *Memory was nearly full*
  names them too, and *Frame time spikes* gives the reads from disk.
- **Performance → Memory** shows the commit charge against its limit
  (amber from 90%), the page file (its size, who sets it, its use now
  and at most) and the reads back from disk. The hardware check's *Page
  file* says whether Windows manages its size, and suggests letting it
  when it's set by hand.
- **The AI chat knows the PC**: its processor, graphics card and RAM,
  the commit charge and the page file (never the PC's name), and that
  memory compression has no fixed maximum ratio. It had called a 16 GB
  PC "a 12-GB system".

### Fixed

- **The update check could show another version's Setup file.** It
  took the first `.exe` on the release page; release 2.0.0 had every
  older Setup attached, so OptiMind 1.5 showed 1.1.0's file and SHA-256
  as the 2.0.0 update. It now takes only `OptiMind-AI-Setup-<version>.exe`.
- **An app opened while OptiMind runs** counted as unused since
  OptiMind started, so a big app opened in the background had its
  memory compressed at once. It counts from when it appeared now.
- A process the agent couldn't find under the name Windows listed it
  with (a Steam helper) was tried every 30 minutes for hours and logged
  as applied. It's logged as unchanged now, left alone for 6 hours, and
  *What OptiMind changed* no longer counts it as memory compressed.
- A game Windows protects was noted in the activity on every Alt+Tab
  back into it: it's noted once per game, unless the result changes.
- Tests for all of it: 346 in all.

## 2.0.0 (2026-10-21)

A new look: black, with blue. It also brings the changes of 1.5.1 below,
which wasn't released on its own.

### Changed

- **Black, with blue.** The window is black, its cards a little lighter,
  and blue marks what you can click and what's selected; numbers are in
  Bahnschrift, Windows' own display typeface. CPU, memory, GPU, disk
  and network have new colors, chosen so any two in one chart can be
  told apart with color blindness too. The window's title bar and the
  page shown while the agent starts are black as well.
- **The pages are a row of icons on the left**, each with its name, and
  a bar at the top has the page's name, what the agent is doing and
  Focus mode (it was in the navigation pane, which no longer folds).
- **Home while you play**: the game takes the top of Home, with its
  frame rate, 1% low, frame time and the graphics card's temperature, a
  chart of each capture's frame rate (the 1% low marked) and what
  limited it. When you're not playing, the verdict is there, with your
  last game of the week and *This week*: memory freed, apps held back,
  games played and how busy the processor typically is (the Agent page
  has it all). Gauges show the CPU, GPU, memory and storage (click one
  for its chart on Performance), network and power are below them, and
  *Agent right now* lists the apps it's holding back. Home asks for the
  game's details only while you play, and for the week's numbers every
  5 minutes.
- **Performance**: a card for each resource across the top (its number
  now and a sparkline; click one for its chart), and the chart full
  width, with the time window beside it and the peak, low and average in
  big numbers below.
- **Games**: the game being played looks like it does on Home (big
  numbers, the graphics card's temperature and power, the frame rate of
  each capture), *Last sessions* puts the last 10 measured sessions side
  by side (average frame rate and 1% low), and each session's numbers
  are bigger. With a graphics card other than NVIDIA's, the game's card
  shows how busy the graphics card is from Windows' counters.
- **Optimize**: the tweaks are cards, each with its switch, what it does,
  when it takes effect and what it frees; the buttons above them show one
  kind (Gaming, Storage…) and a search box finds one. Beside them, the
  hardware check counts what to check, tips, information and what's
  fine, with each check's details folded (open for one that needs a
  look), and clean-up shows what you ticked in big numbers, with one
  **Clean up** button. A link from Home or the hardware check to a tweak
  shows all tweaks again first, so it's found.
- **Drivers** and **Startup & services** show their numbers in big type
  at the top; Startup & services gains them: apps that start with
  Windows, services running, and services not from Microsoft.
- **Agent, AI, Processes and Settings**: the Agent page's week is in big
  numbers, like Home's; Processes gains numbers at the top (processes,
  apps, CPU, memory, and the apps OptiMind holds back); the AI page's
  suggested questions and answers take the new style; and every table's
  column names are in small capitals.
- **The icon** (taskbar, Start menu, notification area, Settings → About
  and the installer) is OptiMind's blue square, without the gradient,
  and the installer's side panel is black.
- `GET /api/games` gives the frame rate and 1% low of the current
  game's last 20 captures, for the charts of each capture. Test in
  `tests/test_games.py`.

### Fixed

- The Agent page said "over the last 0 h" while OptiMind had less than
  an hour of readings; it says the minutes now.

### Removed

- **The Light/Dark/Windows theme setting and Windows' accent color**:
  OptiMind is always dark now. `GET /api/app` no longer has
  `appearance`, `app/desktop/appearance.py` is gone, and the choice
  saved in `window.json` is forgotten.

## 1.5.1 (2026-10-21)

A fairer Focus mode comparison.

### Changed

- **The Agent page compares Focus modes capture by capture.** Each frame
  capture now notes the Focus mode that was on for all of it, and the
  comparison uses only those: a capture during which the mode was
  switched, and one you started yourself (it goes on while the game is
  in the background), aren't used. Sessions from before 1.5.1 didn't
  note this and aren't compared. Each mode shows how many captures it
  has; fewer than three is an early result, and the page says how long
  to play (about 10 minutes in each mode). OptiMind never switches Focus
  mode because of this comparison. Tests in `tests/test_games.py` and
  `tests/test_impact.py`.

### Fixed

- **A game's frame rate over several captures came out too high** when
  they differed: each capture's rate counted as often as it had frames,
  so the faster ones counted more. Now it's all the frames over all the
  time measured (one capture at 50 fps for 2 s and one at 100 fps for
  3 s make 80 fps, not 87.5); the 1% low is weighted by time, and the
  share of frames that stutter by frames.

## 1.5.0 (2026-10-20)

Drivers from official sources, for every kind of device.

### Added

- **Who signed each driver**, as Windows itself checks it: the Drivers
  page shows it for your main drivers, every device and every driver
  package (Windows' own, signed through Microsoft's hardware program,
  signed only by its maker, or not signed). A finding names the drivers
  your devices use that aren't signed through Microsoft, or says that
  all are. On Windows before 26H2, where PnPUtil can't list the
  packages, OptiMind asks Windows' own check of each package's catalog
  (SetupVerifyInfFile, Device Manager's "Digital Signer").
- Each update says where it comes from (Windows Update, NVIDIA's
  download server) and how its signature is checked.
- Tests for both, and for firmware (below), in `tests/test_drivers.py`.
- **The makers' own apps** (Drivers page): for the drivers AMD, Intel
  or the PC's maker deliver through their own app, the official one for
  this PC (NVIDIA App, AMD Software, Intel Driver & Support Assistant,
  Dell SupportAssist, HP Support Assistant, Lenovo Vantage, MyASUS, MSI
  Center, GIGABYTE Control Center, Acer Care Center), whether it's in
  your Start menu, and the maker's own page for it. *Open* starts one
  you have from OptiMind's window, with your normal rights, and the
  window opens only an app the agent lists. A self-built PC's board
  maker app shows only when it's installed. Tests:
  `tests/test_maker_tools.py`.
- **An update check**: about once a day OptiMind asks its GitHub
  release page which version is newest, and when it's newer than the
  one installed, Settings → About and Home say so, with a link to the
  page and the Setup file's SHA-256 as GitHub lists it. It never
  downloads or installs anything, and sends nothing about the PC. The
  page is set in the build (`OPTIMIND_UPDATE_REPO`, here
  `Naka-Hiroshi/OptiMind-AI`); without one, nothing is checked. When
  GitHub can't be reached, the last answer stays. Tests:
  `tests/test_updates.py`.

### Fixed

- **Firmware is never installed, not even from Windows Update.** Windows
  Update lists firmware (the PC's BIOS/UEFI, or a device's own firmware)
  among its driver updates, and OptiMind would have installed one you
  picked. Now it's shown as firmware, without an Install button, and the
  agent refuses it; so does its Windows Update installer, on what
  Windows Update itself says about each update right before installing.
- The docs said other programs can't get the local API's secret. A
  program running under your own account could read it from the
  window's memory, as Windows allows between programs of one account:
  that's why the riskiest changes also wait for OptiMind's own dialog.
  The README, docs/agent.md and docs/limitations.md now say so.
- **The report left some game names in.** It knew each game by one exact
  name, so a game whose library name has a ™ kept its program's name
  (written without the ™ and the space) in the AI's list of the apps it
  protects and in the logs, and the AI's notes in the logs could name a
  game in their own words. Now a game is found however its name is
  written (any case, with or without spaces and signs, its program with
  or without ".exe"), from every game the whole activity log saw in
  front, and the AI's notes are left out of the report. Tests in
  `tests/test_report.py`.

## 1.4.0 (2026-10-20)

Ready for friends' PCs.

### Added

- **A report to send for help** (Settings → About → Create a report):
  one zip file on the Desktop with OptiMind's logs and the PC's details
  (maker and model, board and BIOS, processor, memory, graphics,
  drives), what works on it, the hardware and driver checks, how busy
  the PC was and what OptiMind did last. The Windows user name, the
  PC's name, e-mail addresses, API keys and game names are replaced
  everywhere; the settings and data files themselves aren't included.
  File Explorer opens with the file selected.
- Tests for the report (`tests/test_report.py`): what's in it, and that
  nothing personal is.
- **OptiMind's own confirmation for the riskiest changes**: turning off
  Memory integrity and installing drivers from a backup now also wait
  for your Yes in a Windows dialog the agent shows itself, after the
  page's own question. No is the default, no answer within 90 seconds
  counts as No, and Windows doesn't let programs with fewer rights than
  OptiMind click into it. Tests: `tests/test_confirm.py`.
- **What works on this PC** (Settings → About, and in the report): for
  any PC or laptop, whether administrator rights, frame-rate
  measurement, the graphics card's sensors, the power plan, Maximum
  mode's cores and the newest graphics driver work there, and if not,
  why and what to do (for example Windows' Power mode on laptops, AMD
  Software or Intel Driver & Support Assistant for other graphics).
- **Hardware check, for more brands**: AMD Radeon cards get AMD
  Software's automatic tuning (Undervolt GPU / Overclock GPU), like
  NVIDIA App's for RTX cards; Intel's 13th and 14th generation desktop
  processors get a check of the BIOS's microcode against Intel's fix for
  their instability (0x12B, newest 0x12F, with Intel Default Settings);
  the memory profile and PBO tips name the BIOS menus of ASUS, MSI,
  Gigabyte and ASRock boards. Tests: `tests/test_any_pc.py`.
- **What OptiMind changed** (top of the Agent page), over the last 7
  days, in numbers: memory taken back from idle apps and identical
  memory merged (the amounts Windows reported), busy background apps
  held back, background work kept off a game's cores, lower GPU priority
  for background apps while you played, games played, and how busy the
  PC was (OptiMind helps most under load). For a game measured both with
  Focus mode Off and on, the frame rates side by side with the change
  from Off: the fair way to see what Focus mode does in that game. Game
  sessions now note the Focus mode used (`focus_mode`), so comparisons
  start with the sessions played from this version on. Tests:
  `tests/test_impact.py`.

### Changed

- **Only OptiMind's own window can change things through its local API.**
  OptiMind runs with administrator rights, and until now any program on
  the PC (and any other Windows account) could ask it to change things,
  as the window does. Now every change needs a secret, new at every
  start, that the agent hands only to the window it starts (in the
  window's environment, never through the API). Reading stays open to
  this PC, and so do opening the window (a second start of OptiMind) and
  quitting (the installer). From source, `OPTIMIND_OPEN_API=true` allows
  changes from a browser for trying the API; the installed app ignores it.
- Tests for it (`tests/test_access.py`), and every API test now calls the
  API the way the window does.

### Fixed

- **Laptops without a High performance plan** (Windows' Power mode
  replaces it on many): OptiMind no longer tries to switch to it, which
  failed each time the PC got busy; What works on this PC explains Power
  mode instead.
- A game session's advice now copes with a missing memory reading (one
  would have stopped it with an error; real sessions always had one).

## 1.3.2 (2026-10-20)

Fixes found by measuring what OptiMind did on a real PC over two days.

### Fixed

- **The standby cache is purged only under real memory pressure** (90%
  or more of the RAM in use). Before, it was purged from 75%, while 3 to
  4.5 GB were still available: 21 times in two days, about 70 GB of
  cached files thrown away. Windows reuses that cache by itself as apps
  need memory, so purging it only made apps and games load their files
  from the disk again.
- **The power plan goes back to Balanced after three calm minutes**
  instead of one, so a game's menu, a loading screen or a quick Alt+Tab
  no longer flips the plan back and forth (19 of 56 switches came less
  than two minutes after the previous one).
- **Switching between two windows of one app** (a browser and its file
  dialog, which runs as another process) no longer logs "priority put
  back" each time: Focus mode's boost isn't something the agent held
  back. 101 of these give-backs filled the activity log in two days.
- **"A game is in focus (Discord.exe)"**: the app in front and whether
  it's a game are read together, so a switch at that moment can't pair
  one app with the other's game state.
- **VS Code, Office, terminals and similar work apps never count as
  games**, even when their own title bar lets them cover the whole screen.
- When no frames are seen, the Games page says OptiMind may not be able
  to follow the program the game draws from, instead of saying the game
  didn't draw any.

### Added

- 5 tests for these fixes: 252 tests in all.

## 1.3.1 (2026-10-19)

An installer to give to friends, with the code protected.

### Changed

- **`OptiMind.exe` is compiled with [Nuitka](https://nuitka.net)**
  instead of packaged with PyInstaller: the Python code becomes machine
  code, so the installed app holds no source code, and none of its code
  as Python bytecode (which can be unpacked and turned back into readable
  code). Comments
  are gone, and the build empties the docstrings first
  (`packaging/protect.py`), keeping every line in place so that errors in
  the log still point at the right line.
- **The window's page is minified** (esbuild: no comments or spaces, the
  scripts' names shortened) and kept compressed inside `OptiMind.exe`,
  instead of a readable file in the program folder.
- The installed app leaves out the interactive API documentation
  (`/docs`), which is for developers; running from source still has it.
- Updating from 1.3.0 or earlier removes the old program files and keeps
  your settings, data and start with Windows choice.
- The build downloads esbuild with a pinned SHA-256 and, the first time,
  Nuitka's C compiler (GCC from winlibs).

### Added

- Tests for the protection step (`tests/test_packaging.py`) and for how
  the installed app starts itself (its window, start with Windows and the
  administrator prompt name OptiMind.exe by its full path): 247 tests in
  all.

## 1.3.0 (2026-10-19)

Drivers: every device and its driver, checked against Windows and the
makers' own sites, with safe updates and a full backup.

### Added

- **A Drivers page** ([docs/drivers.md](docs/drivers.md)):
  - **What needs attention**, worst first:
    - devices with a problem, with Microsoft's explanation of the Device
      Manager code and a button where one fixes it (restart the device,
      scan for hardware changes, look on Windows Update, turn it on);
    - devices that stopped responding and had to be reset (network
      adapters, storage controllers, disks, the graphics driver), how
      often and when, and whether several failed during the same hours,
      which points to something they share (chipset, BIOS, power, heat);
    - a graphics card on Windows' basic display driver;
    - your NVIDIA driver compared with NVIDIA's newest Game Ready driver;
    - main drivers more than 6 years old;
    - driver updates Windows Update offers;
    - drivers apps left behind, or that didn't start;
    - known risky drivers (WinRing0);
    - the vulnerable driver blocklist, test signing or signature checks
      turned off, and drivers excluded from Windows Update;
    - old driver versions nothing needs;
    - the BIOS;
    - whether you have a driver backup.
  - **Windows Driver Policy (2026)**: the PC's mode (evaluation or
    enforced), and any driver it flags or blocks.
  - **Your main drivers** (graphics, chipset, sound, network, Bluetooth,
    storage, USB): how old each one is and where its updates come from.
    That's the card's maker, AMD's page for your board's chipset, the
    board maker's page for on-board sound, Realtek for its network chips,
    or Windows Update for Windows' own drivers.
  - **Every device**, grouped as Device Manager groups them, searchable,
    with devices that aren't connected on request.
  - **Drivers apps added**: kernel drivers that belong to no device
    (anti-cheat, fan and RGB control, virtual audio…), with their maker
    and known risks.
  - **How Windows guards your drivers**, and the last 30 days of driver
    events from Windows' logs.
  - **The driver store**: every package from other makers, its size and
    the devices using it.
  - **Where drivers should come from**, with Microsoft's own advice.
- **Installing drivers**, always after a System Restore point, never
  while a game runs, and never a driver older than yours:
  - driver updates from Windows Update, through its own API;
  - NVIDIA's newest driver, downloaded from NVIDIA's server in background
    mode, started only if Windows confirms NVIDIA signed it.
- **Driver tools** (Windows' PnPUtil):
  - scan for hardware changes;
  - restart a device that stopped, or turn one back on;
  - remove old driver versions that a newer version replaced and no
    device uses (the version Windows would roll back to stays);
  - **back up every driver** to another drive (with a list of what each
    device used), and install them all back from a backup after a clean
    reinstall.
- Home shows the most serious driver findings under *Needs attention*,
  and the AI chat knows them.
- Tests for all of it (`tests/test_drivers.py`, 233 tests in all).

### Changed

- A System Restore point made before a driver change is of Windows'
  *driver install* type, as Windows itself makes one.

## 1.2.1 (2026-10-18)

Corrections from a full review of the code and of what 1.2.0 measured on
a real PC in its first hours.

### Fixed

- **An app in front counted as a game when a full-screen game ran
  behind it.** Windows' "a full-screen app is running" state stays on
  while you look at another window, so Discord and OptiMind's own window
  were treated as games: the real game, now in the background, could
  then be held back (lower priority, one core on Maximum, lower GPU
  priority) and a 40-minute "session" of Discord was saved. Now only the
  app's own window filling its screen counts, and chat apps, launchers
  and remote desktops never do.
- **OptiMind's own window counted as "the app you're using"** (it's a
  process of its own since 1.1). Looking at OptiMind while you play now
  leaves the game in front.
- **Games built on a browser engine** (Electron, NW.js, as many RPG
  Maker games are) had no frame rates: their frames are drawn by a
  helper process, not by the one owning the window. Captures now follow
  the game's helper processes, and its CPU, memory and disk figures
  include them.
- **Helper processes of the app you're using** (a browser engine's
  renderer and GPU processes, `msedgewebview2.exe` of a WebView2 app)
  could be held back or compressed as if in the background.
- **Giving something back could be skipped** when more than 10 changes
  came in one cycle (as when a game starts or ends): an app could stay
  on one core or at low GPU priority until it came to the front. Giving
  back now never waits, and changes past the limit run the next cycle.
- **An app that chose its own setting was asked again every 10 minutes**
  (the activity showed "nothing changed" for Discord Clips all evening).
  It's now left alone for an hour.
- Asking OptiMind in the chat measured the PC again from another
  thread, which skewed the agent's next reading; it uses the agent's own
  latest reading now.
- Full video memory is reported whatever limited the game (it was only
  reported for games limited by the graphics card).
- The activity list could miss an entry when you changed something at
  the same moment the agent logged an action.
- Log files trim themselves safely however long their lines are.

### Changed

- OptiMind's window pauses its updates while minimized and catches up
  the moment it's shown: the agent isn't asked every 2 seconds for
  nothing (measured: about 1.7% of one core while the window is open).
- Discord Clips is recognized as a recorder (named in the session, with
  where to turn it off, and its GPU priority is never lowered).
- Tests for the disk clean-up's safety rules (only its own folders, only
  old files, never through a link), Focus mode's game detection, the
  per-cycle limit and the frame captures: 185 tests in all.

## 1.2.0 (2026-10-18)

Games and hardware: OptiMind measures what holds each game back, and
says where the PC's real headroom is.

### Added

- **A Games page**: the game you're playing, live (CPU and its busiest
  core, memory, the graphics card's temperature, clock and power), each
  session afterwards with its frame rate, 1% lows and what limited it
  (a bar with the graphics card's, the processor's and a cap's share),
  and what to do about it. A **Measure** button measures the running
  game for 60 seconds. *Your games* lists the installed Steam and Epic
  games with their drive, and which ones to move to the SSD first (with
  a button to open each in Steam).
- **Frame-time measurement** with Intel's PresentMon 2.6.0 (MIT
  license), shipped in `tools\`: 30 seconds, 45 seconds after a game
  comes to the front, then every 3 minutes while it stays there. Frames
  are classed by Intel's GPU Busy rule (graphics card), their rhythm
  (frame cap or V-Sync) or neither (processor). Needs administrator
  rights; Settings → *Measure frame rates while you play* turns it off.
- **Game sessions** saved in `data\game_sessions.jsonl` (the last 200),
  with advice: lower GPU-heavy settings or DLSS/FSR when the card is the
  limit, raise quality for free when the processor is, cool a card that
  slows down from heat, lower textures when video memory is full, raise
  a cap below the monitor's refresh rate, move a game off the hard disk,
  close apps when memory was nearly full. The last session's warnings
  and tips also show on Home under *Needs attention*.
- **Background recording found**: an app keeping the graphics card's
  video encoder busy while you play (SteelSeries GG Moments, NVIDIA
  Instant Replay, Xbox Game Bar, OBS, Medal…) is named in the session,
  with where to turn it off.
- **GPU priority** (Focus mode, while a game is in focus): background
  apps keeping the graphics card 5% or more busy get *Below normal* GPU
  priority after 10 s, and their own back half a minute after the game.
  Recorders, apps playing sound and apps set higher on purpose (Discord,
  NVIDIA's container) are left alone.
- **The graphics card's own sensors** on NVIDIA cards (NVML, read-only):
  temperature, clocks, power against its limit, fan, video memory, PCIe
  link and why it holds its clocks back, on Home and Performance → GPU.
- **The processor's real speed** (Windows' own counters, with boost) on
  Home and Performance → CPU, next to its base speed.
- **New hardware checks**: the monitor's refresh rate (one left below
  what it offers), graphics card tuning (NVIDIA App's automatic tuning
  and the card's power target), processor boost (AMD's PBO, with AMD's
  warranty statement), a processor upgrade that fits an AM4 board, drive
  health (temperature, wear and errors with administrator rights), hard
  disk spin-down, a hypervisor running without need, and **Stability**:
  hardware errors, graphics driver resets, bluescreens, unexpected
  shutdowns and app crashes from Windows' logs, the safety net for any
  overclock.
- **Two tweaks**: *Variable refresh rate for games* (G-SYNC / FreeSync
  for DX11 games that don't support it themselves) and *Keep hard disks
  spinning (plugged in)*, so a game on a hard disk doesn't wait for it
  to spin up.
- The AI chat knows your last game sessions and the hardware check, and
  starts from what limited a game when you ask how to make it faster.
- `packaging\get_tools.py` downloads PresentMon and checks its SHA-256;
  the build runs it. `GET /api/games`, `POST /api/games/measure`.
- Tests for all of it (`tests/test_games.py`, 158 tests in all).

### Changed

- The Optimize page has 22 tweaks, and a hardware check item that
  points to a page of Windows Settings has a button that opens it.
- The installer bundles PresentMon and its license
  (`THIRD-PARTY-NOTICES.txt`).

### Fixed

- Turning off *Optimizations for windowed games* put back the whole
  DirectX settings string as it was before, undoing anything changed
  since (in Windows Settings, or by another tweak). Each tweak now
  changes and puts back only its own setting in it.

## 1.1.1 (2026-10-18)

### Added

- **Remove activity entries** (Agent → Activity): a remove button on each
  entry (shown when you point at it), *Clear* for a day, and *Clear all*
  (or *Clear errors*, *Clear blocked*… with a filter on). They're deleted
  from OptiMind's log, so the AI no longer sees them either; nothing is
  undone. `POST /api/logs/remove`.

### Changed

- **Dates in the activity**: entries are grouped under *Today*,
  *Yesterday*, the weekday or the date, and sorted by time. Elsewhere a
  time says its day ("Today, 2:05 PM", "Thursday, 7:41 PM",
  "Oct 9, 7:41 PM"), and every time shows the full date when you point at it.

## 1.1.0 (2026-10-18)

OptiMind becomes a Windows app with an installer.

### Added

- **An installer**: one Setup file (`OptiMind-AI-Setup-1.1.0.exe`) that
  installs OptiMind in Program Files with its Start menu entry, an
  optional desktop shortcut, an uninstaller, and *Start OptiMind when I
  sign in*. Built with PyInstaller and Inno Setup by one command,
  `packaging\build.ps1`; Python and everything else is inside.
- **Its own window** instead of a browser tab: a native Windows 11 window
  (WebView2) with its own taskbar button, a title bar in the app's color,
  and its size, place and theme remembered. It's a separate process that
  ends when you close it; OptiMind keeps working from the notification
  area until you choose Quit.
- **Start with Windows**, with administrator rights and no prompt (a
  Task Scheduler task), from Setup or Settings.
- **A Settings page**: start with Windows, administrator rights (with
  *Restart as administrator*), Light/Dark/Windows theme, the AI
  providers (keys, models, order, a Test button, how often the AI
  reviews the rules), OptiMind's files, open-source licenses, and Quit.
  API keys are saved encrypted for your Windows account.
- **An AI page**: Ask OptiMind (with suggested questions), the policy,
  its review history and today's provider usage.
- One OptiMind at a time: starting it again brings its window to the
  front, without a second prompt.
- The app's icon, drawn from the logo: in the window, the taskbar, the
  Start menu, the tray and Setup.
- Tests for the new parts (`tests/test_desktop.py`, 100 tests in all).

### Changed

- **A new design in the style of Windows 11** (Fluent): a navigation
  pane that folds into icons in a narrow window, Windows' accent color
  and light/dark mode, its icon font, switches for tweaks and startup
  apps (as in Windows Settings), Windows-style dialogs and banners
  instead of the browser's pop-ups, and a status banner with a button
  for each item that needs attention.
- The installed app keeps your settings (`settings.env`), data and logs
  in `%LOCALAPPDATA%\OptiMind`. Running from source still uses `.env`
  and `data\`.
- The tray menu: Open OptiMind (also a click on the icon), Focus mode,
  Quit OptiMind. Closing the window no longer stops OptiMind.
- The chat, policy and AI history moved from the Agent page to the AI
  page; the Overview page is now Home.

## 1.0.0 (2026-10-17)

First stable release.

### Added

- A version number, shown in the dashboard, the tray icon's tooltip, the
  console window's title and `GET /api/status` (`version`).
- A new dashboard layout: a sidebar like Windows 11 Settings, a page
  header with live status and administrator badges, Focus mode always at
  hand in the sidebar, a light/dark switch, and the app's icon in the
  browser tab and the tray.
- `OptiMind.cmd`: double-click to start.
- Automated tests (`python -m unittest`) for the decision engine, Focus
  mode, the API, the AI fallback, the Group Policy writer and the safety
  layer. They never change the PC.
- `OPTIMIND_DATA_DIR`: where the data files are kept (the tests use a
  temporary folder).
- Documentation in `docs/`: the agent, the Optimize page, architecture,
  development, limitations.

### Changed

- The code is grouped by job: `app/agent` (decides and acts),
  `app/system` (reads the PC) and `app/tools` (what you start from the
  dashboard). Behavior is unchanged: the same 430 checks pass before and
  after.
- OptiMind always applies its changes for real, each one checked with
  Windows and put back as before.
- The tray menu is Open dashboard, Focus mode and Quit (the API
  documentation is at `/docs`, see docs/development.md).
- A shorter README, with the details in `docs/`.

### Removed

- Simulation mode as a user setting: the dashboard switch, the tray item,
  `POST /api/policy/dry-run/{enabled}`, `OPTIMIND_DRY_RUN` in
  `.env.example` and `data/mode.json`. Developers can still start a copy
  in test mode with `OPTIMIND_DRY_RUN=true` (docs/development.md).

## Before 1.0

Development versions built the agent and its two loops, the free AI
providers, the dashboard, the Optimize page, administrator rights,
Focus mode, memory compression and page combining, and the hardware
check.
