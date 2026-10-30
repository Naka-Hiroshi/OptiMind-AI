# The agent

What OptiMind does on its own, how Focus mode works, the safety nets
around every change, administrator rights, and the AI that tunes the
rules.

## What the agent can do

| Action | How | Code |
|---|---|---|
| Deprioritize a background process hogging the CPU | `SetPriorityClass` (via psutil) → below normal. Only Normal/Above normal apps: one already running low, or set to High on purpose, is left as it is | `agent/optimizer.py` |
| Put it in Windows 11 **Efficiency mode** (EcoQoS) | `SetProcessInformation(ProcessPowerThrottling)`, the same switch as Task Manager's Efficiency mode. Apps already in it (browsers do it for background tabs) are left as they are | `agent/optimizer.py` |
| Lower the **I/O priority** of a background disk hog | `psutil.Process.ionice()` → low | `agent/optimizer.py` |
| Undo all of the above when the app comes back to the foreground (or starts playing sound) | each app gets back **its own** original settings, all at once (not a blanket "Normal") | `agent/optimizer.py` |
| **Focus mode**: the app you switch to runs at Above normal priority, within milliseconds | `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` + `SetPriorityClass`; only an app at Normal is raised | `agent/focus.py`, `agent/optimizer.py` |
| Maximum focus mode: keep busy background apps on one core while a game is in focus | `SetProcessDefaultCpuSets` (Windows' soft affinity): the last core with both threads, or the efficiency cores of a hybrid CPU | `agent/optimizer.py` |
| Lower the **GPU priority** of a background app using the graphics card while a game is in focus | `D3DKMTSetProcessSchedulingPriorityClass` (gdi32.dll), the graphics scheduler's priority for the app's GPU work → below normal. Only apps at Normal or Above normal: one set higher on purpose (Discord and NVIDIA's container run at Realtime) is left as it is, and apps recording or streaming are never touched. OptiMind never sets a GPU priority above Above normal | `agent/decision_engine.py`, `agent/optimizer.py` |
| **Compress the memory of idle background apps** you haven't used for a while (10 min by default, sooner when RAM is usually full; an app opened while OptiMind runs counts only from when it appeared), and of big idle apps under memory pressure (RAM at the policy's line for 10 s; it ends 5 points below the line) | `EmptyWorkingSet` (psapi.dll): Windows compresses the changed pages into its memory compression store and keeps the rest as cache; they come back when the app needs them. The app also gets **Low memory priority** (`SetProcessInformation(ProcessMemoryPriority)`), so Windows trims it before the app in front if RAM runs short. At most 3 apps per cycle, never an app that's working, playing sound, or one of the two you used last (Alt+Tab), and the same app at most every 5 min (pressure) / 30 min (idle) | `agent/decision_engine.py`, `agent/optimizer.py` |
| **Merge identical memory pages** (page combining): every 30 min at most, with RAM 60%+ full, no game running (in front or behind another window) and the processor calm (below 50% for the last 30 s) | `NtSetSystemInformation(SystemCombinePhysicalMemoryInformation)`, the same page combining Windows' memory manager runs on its own schedule; reports how much Windows merged | `agent/optimizer.py` |
| Purge the system RAM standby list: only under real memory pressure that lasts (90%+ of the RAM in use; below that Windows reuses the cache by itself), when the free list is nearly empty while over 2 GB sits in cache (like ISLC), at most every 10 min | `NtSetSystemInformation` (same technique as Sysinternals' EmptyStandbyList/RAMMap) | `agent/optimizer.py` |
| Switch the power plan: High performance after a few seconds of sustained load (or right away when a game is in focus) while you're at the PC and plugged in, back to Balanced after three calm minutes (a game's menu or a quick Alt+Tab doesn't flip it). Only to a plan the PC has: many laptops have no High performance plan (Windows' Power mode replaces it), and there OptiMind leaves the plan alone | `powercfg /setactive`, `GetLastInputInfo`, `psutil.sensors_battery()` | `agent/decision_engine.py` |
| Disable/enable a startup app (reversible; apps for all users need administrator rights) | The `StartupApproved` flag Task Manager uses; the Run entry itself is never touched | `tools/startup_manager.py` |
| Limit the core affinity of a noisy process (API only; the agent doesn't do it on its own) | `SetProcessAffinityMask` | `agent/optimizer.py` |

The agent changes only the instances of an app that are doing the work,
and never an app you're using: if any instance of it is in the
foreground, all of them are left alone, and so are its helper processes
(what it started that has no window of its own: the renderer and GPU
processes of a browser engine, `msedgewebview2.exe` of a WebView2 app).
Apps playing sound (a voice call, music) are never held back either.
Essential Windows processes are protected in code (`agent/safety.py`).

At most 10 new changes are made per cycle; the rest wait for the next
one (2 seconds later). Giving something back never waits. An app that
turns out to have chosen its own setting (set high on purpose, or
already low) is left alone for an hour instead of being asked again,
and a process the agent couldn't find under the name Windows listed it
with is left alone for 6 hours (logged as unchanged).

**The AI's protected apps** (AI page): when the agent keeps holding an
app back and you keep switching back to it, the AI puts the app on its
list, and the agent leaves its CPU, disk and GPU priority alone. Its
memory can still be compressed while it sits idle in the background
(the rules above never touch the app in front, one of your last apps or
one playing sound): while you play, those apps are often the memory the
game needs. Each app leaves the list 14 days after it was added (the AI
puts it back if it still has a reason), the AI page shows when it was
added, and **×** takes one off now: the AI won't put it back
(`POST /api/policy/unprotect`).

## Focus mode

The app you're using gets the PC first, and switching apps stays
instant. Switch it in the window's top bar, on the Agent page or
in the tray icon's menu (**Off**, **Balanced**, **Maximum**); it's your
choice, so the AI never changes it.

- **The moment you switch** (Windows reports it, measured at 2–4 ms for
  the whole switch): everything the agent was holding back on that app
  is put back, and it runs at **Above normal** priority. The app that
  had the boost gets its own priority back. Only an app at Normal is
  raised: one you set lower or higher keeps yours.
- **Nothing is ever slowed on a switch**: background apps are only held
  back by the engine after its grace period (30 s, or 10 s while a game
  is in focus). The **two apps you used last are never compressed**, so
  Alt+Tab back to them doesn't wait for memory to come back. The Alt+Tab
  switcher, Start, the taskbar and OptiMind's own window don't count as
  a switch: looking at OptiMind while you play leaves the game in front.
- **Apps playing sound** (read from Windows' audio sessions) are never
  held back: Windows itself treats audible apps as high priority, and a
  voice call held back in the background would stutter.
- **A game in focus** (an app in a Steam / Epic / GOG / Xbox / EA /
  Ubisoft / Riot library, or one whose own window fills its screen, in
  exclusive or borderless full screen, that isn't a browser, video
  player, chat app or launcher): background apps using the CPU are held back
  after 10 s instead of 30 s and from 10% of a core instead of 15%, the
  power plan switches to High performance right away, and page combining
  waits until the game has closed. Background apps keeping one of the
  graphics card's engines 5% or more busy for 10 s (a browser playing
  video, a launcher's animated window, a wallpaper app) get **Below
  normal GPU priority**, so the card serves the game's frames first;
  they get their own back half a minute after you leave the game (not
  on a quick Alt+Tab). Apps playing sound and apps that record or stream
  the game (OBS, Discord and its Clips, NVIDIA's overlay, SteelSeries
  GG…) are left alone: holding back a recorder would ruin the recording.
- **Maximum** adds one thing while a game is in focus: busy background
  apps run on one core only (on a hybrid CPU, the efficiency cores), so
  the game has the other cores and their caches to itself. On a CPU with
  two threads per core this also stops a background thread from sharing
  a core with the game's thread, which priority alone can't do.
  Background downloads, installs or recording get slower while you play;
  they get every core back half a minute after you leave the game.

Everything Focus mode changes is put back like the rest: when the app
loses focus, when you turn Focus mode off, and when OptiMind quits or
restarts after an abrupt close.

## Game sessions

The Games page follows each game you play, whether Focus mode is on or
off (`agent/games.py`).

- **A session** starts when a game comes to the front (the same game
  detection as Focus mode) and ends when the game closes or has stayed
  in the background for 5 minutes; an Alt+Tab doesn't end it. A session
  with at least a minute of play is saved in `data/game_sessions.jsonl`
  (the last 200 are kept).
- **While the game is in front**, each agent cycle notes the CPU (overall
  and its busiest core), memory, the memory committed against Windows'
  limit (the RAM plus the page file) and what Windows read back from
  disk for memory it didn't have, the game's own CPU, memory and disk use
  (with its helper processes) and, on an NVIDIA card, the graphics card's
  temperature, clocks, power, video memory and why it held its clocks
  back (heat, its power limit…). When memory is fullest (90% or more of
  the RAM, or of the limit), it notes the apps holding the most memory of
  their own, leaving out Windows' own processes and the game.
- **Frame times** come from Intel's PresentMon, shipped with OptiMind
  (`tools/PresentMon.exe`): a 30-second capture 45 seconds after the game
  comes to the front (past its menus and loading), then one every
  3 minutes while it stays there, and 60 seconds when you press
  **Measure** on the Games page. A capture reads Windows' own graphics
  event tracing (the ETW events every Direct3D, Vulkan and OpenGL
  presentation reports); unlike an overlay, nothing is loaded into the
  game. It follows the game's helper processes too: a game built on a
  browser engine (Electron, NW.js, as many RPG Maker games are) draws its
  frames in one of them, not in the process that owns its window. It
  stops when the game leaves the front,
  since the frames of a game in the background say nothing. It needs
  administrator rights; Settings → *Measure frame rates while you play*
  turns it off.
- **The verdict** for each frame: the graphics card was the limit when
  it was busy for at least 85% of the frame (Intel's "GPU Busy" rule).
  The other frames waited for the processor or for a cap: a frame cap or
  V-Sync when three in four frame times sat within 4% of one value, at
  the monitor's refresh rate or a common cap (60, 120, 141, 144…), with
  V-Sync on, the processor idle part of each frame, or nearly every
  frame that steady; otherwise the processor (the game's main thread,
  or the driver). The session shows the share of each, the average
  frame rate, the 1% lows and the frame time spikes.
- **Background recording**: an app keeping the graphics card's video
  encoder 3% or more busy during at least 30% of the session is
  recording or streaming. The session names it (SteelSeries GG Moments,
  NVIDIA Instant Replay, Xbox Game Bar, OBS, Medal…) and says where its
  recording is turned off.
- **What to do**: each session ends with advice from what was measured:
  lower GPU-heavy settings or turn on DLSS/FSR when the card is the
  limit (and cool it if it slowed down from heat), raise quality for
  free when the processor is the limit, raise or remove a cap below the
  monitor's refresh rate, move a game off the hard disk (with the drive
  and the Steam steps), turn off a background recorder, close the apps
  holding the most memory (named, with how much) when memory was nearly
  full or Windows was near its memory limit. Compressing memory doesn't
  lower the committed memory; closing apps does. The last session's
  warnings and tips also show on Home under *Needs attention* for a week.

While a game runs, the Games page (and Home) show the frame rate of each
capture so far. Below it, *Last sessions* puts the last 10 measured
sessions side by side, each with its average frame rate and 1% low.

- **Before you play** (Games page): memory now and the memory committed
  against Windows' limit, the apps holding the most memory of their own
  (150 MB or more; Windows' own processes, security software, OptiMind
  and the game being played aren't listed), and the recorders that
  recorded one of your last 2 sessions, with where to turn them off
  (named whether they run or not: SteelSeries GG Moments and Discord
  Clips only start with a game; two sessions after you turn one off,
  it's gone). An app with an open window has a **Close** button: after
  your Yes, OptiMind asks its windows to close, as its own X does, so it
  saves your work or asks about it as it always does (`tools/close_app.py`).
  Nothing is ended by force, and OptiMind never closes an app by itself.
  Many apps only hide in the notification area when their window closes
  (Discord, Steam, Spotify…): OptiMind then says so, and that they're
  quit from their icon there. Only apps of your own account are closed,
  and each close is in the activity log.
- **Your note on a session**: what you changed for it ("render scale
  Quality", "closed the browser"), saved with the session. The AI chat
  sees it too.
- **Compare two sessions**: any two measured sessions side by side, by
  default the newest and the one before it of the same game: frame rate,
  1% low, the frames that took twice as long, what limited the game, the
  busiest core, the graphics card's temperature, memory and committed
  memory at most, the reads back from disk, Focus mode, the drive and
  your notes, with the change from one to the other. Change one thing at
  a time and play the same kind of scene, and the difference is your
  change.

The Games page also lists your installed Steam and Epic games with their
drive, how long you played them and their last frame rate, and *Move
these to the SSD first*: the games on a hard disk that you've played,
the most recently played first.

## Safety nets

The agent **really changes** background apps and the power plan, with
these safety nets:

- **Every change is checked**: after each one, Windows is asked again; a
  change Windows didn't keep is logged as an error, never as "applied",
  and memory actions log how many MB were really freed.
- **Everything is put back**: each app's original priority, efficiency
  setting, I/O and memory priority, CPU cores, GPU priority and Focus
  mode's boost are remembered (also in `data/held_back.json`) and restored when the
  app comes back to the front, and when OptiMind stops (**Quit OptiMind**
  in the tray menu or in Settings, signing out or shutting down, and from
  source Ctrl+C or closing the console). Closing the window doesn't stop
  OptiMind. If it's ended abruptly, the next start puts them back.
- **Protected in code**: essential Windows processes, security software
  and typing/accessibility helpers are on a fixed list the AI can't
  change, and a change Windows refuses is left alone for 10 minutes
  instead of being retried every cycle.
- **OptiMind stays light**: it runs itself in efficiency mode with low
  I/O and memory priority, hands its own idle memory back to Windows
  every 10 minutes, and only loads the AI client when the AI is called.
  Focus mode's thread sleeps until Windows reports a switch, and reading
  which apps play sound costs about 2 ms every 3 seconds.

Everything the agent does is in the Activity log (Agent page), with the
reason and the checked result, grouped by day. You can remove entries
you don't want to see there: one at a time (the button at the end of an
entry), a day (*Clear* next to its date) or all of them (*Clear all*,
or only the errors, blocked or applied ones with that filter on). They
are deleted from OptiMind's log, so the AI no longer sees them either;
nothing the agent did is undone.

## Administrator rights

When you start OptiMind yourself, it asks for administrator rights:
Windows shows its usual *"Do you want to allow this app to make changes
to your device?"* prompt, and OptiMind starts again with them. Started
with Windows (Settings → Start with Windows, or the installer's
checkbox), it gets them without a prompt: a Task Scheduler task set to
"Run with highest privileges" starts it when you sign in. With them it
can also:

- empty the memory standby list and merge identical memory pages;
- read how well Windows compresses memory (the ratio on the Agent page);
- measure games' frame rates (PresentMon reads Windows' graphics event
  tracing, which only administrators and the *Performance Log Users*
  group may start);
- read your drives' temperature, wear and uncorrectable errors (the
  Hardware check's *Drive health*);
- manage your own apps that run as administrator;
- turn startup apps for all users on and off.

Click **No** and it simply runs without those. Even as administrator,
OptiMind only manages processes of your own account: Windows services,
other people's programs and processes Windows protects (such as
Microsoft Defender) are always left alone. The Processes page shows why
with *Protected*, *Service* or *Needs admin*.

- Clicked No? Settings → **Restart as administrator** asks again.
- Turn off Settings → *Ask for administrator rights when OptiMind
  starts* (`OPTIMIND_RUN_AS_ADMIN=false`) to never ask. It doesn't ask
  under the VS Code debugger either (the debugger can't follow it into a
  new process); start VS Code as administrator to debug with
  administrator rights.
- On a standard (non-administrator) Windows account it doesn't ask:
  the prompt would need someone else's password, and OptiMind would then
  run as that other person.
- To stop an OptiMind that runs as administrator, use **Quit OptiMind**
  in its tray menu or in Settings: a normal terminal isn't allowed to
  stop it (`OptiMind.exe --quit` asks it to quit, as the installer does).

The local API only answers OptiMind's own window and tools on this PC: a
website open in your browser can't call it (requests carrying another
site's `Origin` or `Host` header are refused). Anything that changes
something also needs a secret, new at every start, that the agent hands
only to the window it starts (in the window's environment, never through
the API): other programs calling the API, and other Windows accounts on
the PC, can read but not change. Opening the window and quitting stay
open, for a second start of OptiMind and for the installer
(`app/service/access.py`). The secret isn't a wall against a program
already running under your own account: Windows lets programs of one
account read each other's memory, the window's included. For that case,
the riskiest changes, turning off Memory integrity and installing
drivers from a backup, also wait for a Yes in a Windows dialog the agent
shows itself (`app/desktop/confirm.py`): No is the default, no answer
within 90 seconds counts as No, and Windows doesn't let programs with
fewer rights than the agent click into it.

## The AI layer (all free)

The AI never acts on the system. Every ~15 minutes it reviews how the PC
has been used and returns a new **policy** (thresholds and which
optimizations are on) as JSON that must match a schema; the policy is
validated before the fast loop uses it, and fields the AI may not change
(such as startup apps or Focus mode) are ignored.

The AI loop and the chat try the providers in the order set in
**Settings → AI providers** (`OPTIMIND_AI_PROVIDERS`, default
`gemini,groq,local`). If one fails (no API key, free quota used up, no
internet, or an invalid answer), the next one is used automatically.
Settings is also where you paste the free API keys (saved encrypted for
your Windows account), change the models, and press **Test** to check a
key works.

| Provider | Default model | What you need | Notes |
|---|---|---|---|
| Google Gemini | `gemini-3.8-flash` | Free key from [AI Studio](https://aistudio.google.com/apikey) | Highest quality option. The free daily quota is small and Google may use free-tier data to improve its products. |
| Groq | `openai/gpt-oss-120b` | Free key from the [Groq console](https://console.groq.com/keys) | Very fast; free plan of ~1,000 requests/day; strict JSON schema output. |
| Ollama (optional) | your choice | [Ollama](https://ollama.com) installed + a pulled model | Runs fully on your PC, no limits, but uses a lot of RAM/CPU while answering. |
| `local` | — | Nothing | Built-in rule-based brain (`app/ai/local_brain.py`). Always works, offline. Tunes thresholds from usage percentiles and protects apps you keep bringing back to the foreground. |

The AI page shows which provider answered last and how many requests
each one answered today, and the Agent page why the others were skipped
(`GET /api/status`: `ai_last_provider`, `ai_loop_last_error`).

**What goes to the provider.** The 15-minute review sends usage numbers
(CPU and memory), the process names of the apps that used the most, the
latest actions (with the process names they acted on) and the policy.
A question you ask in the chat also sends the
current state (with the PC's processor, graphics card and RAM, the
memory committed against Windows' limit and the page file, never the
PC's name), your last three game sessions (with the games' names) and
the hardware check's open items, so the answer can be about your PC.
With the `local` provider nothing leaves the PC.
