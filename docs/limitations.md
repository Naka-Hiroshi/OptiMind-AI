# Limitations and future work

## Honest limitations

- **Python isn't the "lightest" possible language** for a resident
  agent. A native service in Rust/C++/Go would have a smaller RAM
  footprint. This was a deliberate choice (productivity and easy AI/API
  integration), and it's exactly the kind of architecture decision worth
  being able to justify.
- The real actions were tested on Windows 11 against throwaway
  processes (applied, read back from Windows and put back), plus a real
  power-plan switch. The standby purge needs administrator rights, so it
  is checked with Windows' free-memory counter each time it runs.
- Compressed memory comes back (and is decompressed) when the app needs
  it, and a purged cache refills as you use the PC; that's expected:
  the agent only compresses apps that are idle and that you haven't
  used for a while, and purges the cache only under real pressure.
- How much memory compression saves depends on the data, not on a
  setting: Microsoft's figure is about 2.5:1, text and app heaps pack
  better, images and video barely at all. "1 GB into 100 MB" happens
  only for very repetitive data. The ratio the Agent page shows and page
  combining use memory-manager calls Microsoft doesn't document (the
  ones System Informer and Mem Reduct use); OptiMind checks the numbers
  against the store's real size and shows nothing rather than a wrong
  ratio, and a refused merge isn't retried for hours.
- Compressing memory frees RAM, but not the **commit charge**: Windows
  has promised that memory to the app all the same, and counts it
  against its limit (the RAM plus the page file). Near the limit only
  closing apps helps (and more RAM); a page file Windows manages grows by
  itself. That's why a game session near the limit names the apps
  holding the most memory, and OptiMind never closes an app itself.
- **Close** (Games page, Before you play) does what the app's own X
  does, and no more: an app that only hides in the notification area
  keeps running (OptiMind says where to quit it), and an app with
  unsaved work asks first. OptiMind never ends a process, so an app that
  doesn't answer stays open. The memory it gives back is the app's own
  (private) memory, as Windows counts it.
- The memory read back from disk (hard faults) is Windows' count for
  the whole PC: it includes reading the page file and the files of
  every program, the game's among them, so a session can't say which
  program each read was for.
- Some games protect their process with anti-cheat, and Windows then
  refuses the priority change: Focus mode shows "Windows refused" and
  leaves it there (nothing is forced).
- Maximum focus mode on a 4-core CPU puts busy background work on one
  core while you play: downloads, installs or recording get slower until
  you leave the game.
- The AI never acts directly on the system: it only proposes a
  structured, validated policy. This is intentional (safety over full
  autonomy), but it means the "learning" happens in cycles of minutes,
  not instantly.
- Free tiers change their limits without notice. The fallback chain
  keeps the agent working when that happens, but answer quality
  depends on which provider is available at the moment.
- Efficiency mode needs Windows 11 (on Windows 10 the same call only
  marks the process as "low QoS").
- The agent only manages processes of your own account that Windows
  lets it change. Before acting it asks Windows (once per process) and
  skips Windows services, other people's programs and protected
  processes such as Microsoft Defender (Windows protects those even
  from administrators). Without administrator rights it also skips your
  apps that run as administrator. They show up as "Protected",
  "Service" or "Needs admin" on the Processes page, and if a change is
  still refused, the agent logs an error and leaves that app alone for
  10 minutes.
- The per-process "Disk & I/O" number counts all of a process's I/O
  (disk, network and devices), so it can be higher than Task Manager's
  Disk column. The overall GPU % follows Task Manager's approach (the
  busiest GPU engine) but may differ slightly.
- The Optimize page's "frees" numbers are what the related processes use
  right now (their working set, which includes memory shared with other
  programs), so treat them as an estimate. Some tweaks only take effect
  after you sign out or restart, as each one says.
- The installer and `OptiMind.exe` aren't digitally signed (a code-signing
  certificate costs money every year). Windows SmartScreen may show
  "Windows protected your PC" when you run a Setup file downloaded from
  the internet (**More info → Run anyway**), and an antivirus may look
  twice at a new, unsigned program. A Setup file built on your own PC
  doesn't get that warning.
- The update check only tells. It needs the release page set in the
  build, and it never downloads or installs anything: you download the
  new Setup from the release page and run it. Copies from before 1.5.0
  have no check, so they get 1.5.0 the old way.
- The report hides the games OptiMind knows: the Steam and Epic
  libraries, the Games page's sessions and every app the activity log
  saw as a game in front. A game from elsewhere that was never in front
  keeps its program's name where the agent or the AI named it (its
  actions, the AI's list of the apps it protects).
- The installed app holds no source code, but its window's page is only
  minified, not hidden: the browser engine needs it as text, so on a PC
  where OptiMind runs, a determined person can read the minified scripts
  (see [development.md](development.md#what-the-installer-holds)).
- The window needs the WebView2 runtime, which is part of Windows 11 (on
  Windows 10 it may have to be installed from Microsoft first).
- Start with Windows, the data folder and the settings belong to the
  Windows account that installs OptiMind. A standard account that
  installs it with an administrator's password gets them set up for that
  administrator instead; install it from an administrator account.
- Only OptiMind's own window can change things through the local API
  (that's how the window talks to the agent); other programs can only
  read. A program already running under your account could still read
  the window's secret from its memory, as Windows allows between
  programs of one account, so the riskiest changes also wait for
  OptiMind's own dialog, which programs with fewer rights can't click.
  Websites can't use the API.
- The agent's memory use (about 80 MB at start, less after it hands its
  idle memory back to Windows) is mostly Python itself; the window adds
  about 100 MB plus its browser engine while it's open, and nothing when
  it's closed.
- **Frame rates need administrator rights.** PresentMon reads Windows'
  graphics event tracing, which only administrators (and the
  *Performance Log Users* group) may start. Without them the Games page
  still follows each session (CPU, memory, the graphics card's sensors,
  recording) and says why the frame rate is missing.
- **Graphics card sensors are NVIDIA-only.** Temperature, clocks, power
  and why the card holds its clocks back come from NVIDIA's driver
  (NVML). On AMD and Intel graphics, OptiMind shows Windows' own GPU
  counters (use per engine, video memory), and the advice relies on the
  frame analysis alone.
- **No processor temperature.** Windows has no standard way to read it
  without a kernel driver, and OptiMind installs none. The Stability
  check and the GPU's own sensors cover what overheating usually causes.
- **The frame analysis can mistake a processor for a cap.** A processor
  that happens to hold exactly 60, 120 or 144 fps, frame after frame,
  looks like a frame cap; real caps hold their rate more tightly, so it's
  rare. Games that present without DirectX's or Vulkan's usual path
  (some streaming and emulator apps) may not be measured at all.
- **GPU priority is a documented Windows call, but its gain isn't
  published.** OptiMind lowers it only for background apps that keep the
  graphics card busy while a game is in front, puts it back after, and
  leaves recorders and apps playing sound alone; how much a game gains
  depends on how much GPU work those apps submit.
- **Overclocking is advice, not automation.** OptiMind never changes
  clocks, voltages or power limits. The hardware check points to NVIDIA
  App's automatic tuning and to PBO in the BIOS, and says what they
  cost: AMD states that PBO voids the processor's warranty.
- **Game sessions follow one game at a time**: the one in front. Two
  games running at once, or a game played in a window next to another
  app you're using, are followed only while they're in front.
- **Newer drivers are found only where a maker publishes them in a way a
  program can read.** Windows Update covers every device it has drivers
  for, and NVIDIA's driver lookup names the newest GeForce driver. AMD,
  Intel, Realtek and the PC and board makers have no such lookup: for
  those, the Drivers page says how old the driver is, names the maker's
  own app for your PC (and opens it if you have it) or its page, and you
  choose the download there.
- **Only NVIDIA's installer is started from OptiMind.** Other makers'
  drivers come as their own setup programs from their sites; Windows
  Update's drivers are installed by Windows Update itself.
- **The BIOS is shown, never updated, and no firmware is installed**, not
  even the firmware updates Windows Update offers. A failed firmware
  update can leave a PC or a device unable to start, so it stays with the
  maker's own tool and steps.
- **Old driver versions stay unless a newer version of the same driver is
  in use.** A driver none of whose versions any device uses is left
  alone (its device may be unplugged), and the version Windows would roll
  back to always stays.
- **The full driver store list needs Windows 11 26H2's PnPUtil** (its XML
  output). On older Windows it's read from the INF files instead: the
  same packages and devices (the signer from Windows' own check of each
  package), and extension packages show no devices.
- **A PC managed by a company's update server** (WSUS) gets its driver
  list from that server, as Windows Update itself does there.

## Ideas for future work

- Sign the installer and `OptiMind.exe` with a code-signing certificate.
- Run the agent as a Windows Service (via `pywin32`/`nssm`), so it also
  works before anyone signs in.
- Export the tweaks you applied as an `autounattend.xml`, so a clean
  install of Windows starts with them.
- Learn patterns by time of day/day of week (e.g. "work" vs. "gaming"
  profile detected automatically).
- Persist disk/network/GPU history (today the window only keeps it
  since it was opened; CPU/RAM come from the saved history).
- Detect games by GPU use as well (today: game libraries and full
  screen), and remember per-game Focus settings.
- Compare sessions of the same game over time (before and after a
  driver update, a tweak or a new part), with the frame-time chart of
  each capture.
- Read AMD graphics cards' sensors (AMD's ADLX library) the way NVIDIA's
  are read today.
- Link a game session to the graphics driver it ran on, so the Games page
  can compare frame rates before and after a driver update.
