# How OptiMind thinks about performance

This is the research behind Focus mode, the memory features, the Games
page and the hardware check: how a program's work travels through Windows
and the hardware, where it waits, and which documented Windows switch
addresses each wait. Every claim links to its source at the end. The
numbers marked *measured* come from the PC this was developed on
(Ryzen 5 3400G, 16 GB DDR4-2666, RTX 2060 SUPER, NVMe + two hard disks).

## 1. The path of one frame

When you press a key in a game, the work crosses several parts, each
connected to the CPU by a link with a fixed width and speed:

```
keyboard ─USB─▶ USB controller ──(interrupt: MSI)──▶ CPU core: driver, then Windows input
                                                        │
                                  scheduler wakes the game's thread on a core
                                                        │
         memory (2 channels, DDR4-2666, ≈42.7 GB/s) ◀──┤ the game reads its data
         NVMe SSD (PCIe 3.0 x4, ≈3.9 GB/s) ◀────────────┤ ...or loads it from disk
         hard disk (SATA, ≈10 ms per seek) ◀────────────┘
                                                        │
             GPU (PCIe 3.0 x8, ≈7.9 GB/s) ◀─────────────┘ the frame is submitted
                    │ GPU scheduling (on the card: HAGS)
                    ▼
             desktop compositor ─▶ monitor (165 Hz here)
```

Software can't widen a link: the graphics card runs at x8 because the
3400G gives its graphics slot 8 lanes (*measured*: x8 of x16), and the
NVMe drive runs at PCIe 3.0 because that's what the platform offers
(*measured*: Gen 3 of Gen 4). What software can change is **who waits in
the queue in front of each link**.

## 2. The theory: six kinds of contention

When an app feels slow while nothing is maxed out, it is usually waiting
for something shared with another program. There are six such shared
resources, and Windows has a documented lever for each:

| The app waits for… | Why priority alone doesn't fix it | Windows lever | What OptiMind does |
|---|---|---|---|
| **Time on a core** | — (this is what priority is for) | Priority classes; Windows' own foreground boost | The app in front runs at *Above normal*; busy background apps at *Below normal* |
| **Space on a core** | With SMT, two threads run *at the same time* on one core and share its execution units and caches; a lower-priority thread on the sibling still slows the game's thread (AMD's GPUOpen calls this out for games) | CPU Sets, Windows' "soft" affinity | **Maximum** mode keeps busy background apps on the last core while a game is in focus |
| **Power and boost budget** | All cores share one power and thermal budget; background work spends boost the game could use | Quality of Service: EcoQoS runs a process at the most efficient frequency, and on hybrid CPUs on the efficiency cores | Busy background apps go into efficiency mode; the app in front never does |
| **Room in RAM** | Memory isn't scheduled by priority: Windows trims working sets when RAM runs short | Memory priority (lower = trimmed first); memory compression; page combining | Idle apps are compressed and set to Low memory priority, identical pages are merged, the app in front is never trimmed |
| **The disk queue** | A hard disk serves one seek at a time (~10 ms each) | I/O priority | Background disk hogs get Low I/O priority; the Hardware check flags games on hard disks |
| **Time on the GPU** | CPU priority doesn't reach the graphics card: its scheduler takes work from every app that draws (a browser playing video, an animated launcher, a wallpaper app) | The graphics kernel's per-process scheduling priority class (`D3DKMTSetProcessSchedulingPriorityClass`) | Background apps using the GPU get *Below normal* GPU priority while a game is in focus; recorders are left alone |

Two rules come out of this model, and they shape Focus mode:

1. **Penalties must be cheaper to undo than the gain.** Priority, efficiency
   mode, I/O priority, GPU priority and CPU sets are undone with one system call each
   (measured: 1.6–3.5 ms for a whole switch, restore plus boost). Memory is
   different: a compressed app has to decompress its pages when you come
   back. So CPU-type penalties can hit any busy background app, but memory
   penalties never hit the apps you just used. The two apps before the
   current one are never compressed, which is what keeps Alt+Tab instant.
2. **Promote instantly, demote slowly.** The app you switch to gets
   everything back the moment Windows reports the switch (a WinEvent
   hook, no polling). Background apps are only held back after a grace
   period (30 s normally, 10 s while a game is in focus), so a quick
   Alt+Tab never slows anything down.

Apps playing sound are exempt from all of it: Windows runs audible apps
at its highest Quality of Service, and a voice call or music held back in
the background would stutter.

### How to check the theory on your PC

With Focus mode on and a game in focus:

- the Processes tab shows the game at *Above normal* and a "Focus boost" chip;
- background apps using CPU show *Agent: Lower priority / Efficiency mode*
  (and *One core* in Maximum mode) after 10 s, and background apps using
  the graphics card *Low GPU priority (game)*;
- the power plan switches to High performance within one 2-second cycle,
  and back to Balanced after three calm minutes once the game is left;
- switching apps never waits: the activity log shows "Gave settings back"
  for the app you switched to.

### Comparing Focus modes

The Agent page compares a game's frame rate with Focus mode Off and with
it on, from the frame captures (30 seconds every 3 minutes while the game
is in front) that ran in one mode all the way. A capture during which the
mode was switched isn't used, and neither is one you started yourself
(*Measure for 60 s* goes on while the game is in the background).
Sessions from before 1.5.1 didn't note the mode of each capture and
aren't compared.

About 10 minutes of play in each mode gives three captures; fewer are
shown as an early result. Three is a minimum for repeating a result, not
a statistical guarantee: play the same place with the same resolution,
graphics settings, frame cap or V-Sync, and the same apps open. The frame
rate is all the frames captured over all the time captured; the 1% low is
each capture's 1% low weighted by its time (not recomputed over all the
frames); the share of frames that stutter is counted by frames. If a mode
keeps doing worse than Off, switch it back: OptiMind never changes Focus
mode because of this comparison.

## 3. Memory compression: what's possible

Windows compresses memory itself. When an idle app's working set is
trimmed, its changed pages go to the modified list, and the memory
manager's store compresses them (with Microsoft's XPRESS algorithm) into
the *Memory Compression* process instead of writing them to the page
file. When the app touches them again, they are decompressed: a soft
fault, far faster than reading the disk. Microsoft's own figure is that
compressed data takes **about 40% of its original size** (≈2.5:1).

**Why "1 GB into 100 MB" (10:1) isn't a setting.** A lossless compressor
can only remove redundancy that's in the data. App heaps and text pack
well; images, video, already-compressed and encrypted data barely pack at
all. 10:1 happens only for very repetitive pages. No switch in Windows
(or in any tool) changes that. What *can* be done, and what OptiMind does:

- **Compress the right memory**: idle background apps (10 min unused by
  default, sooner when RAM is usually full), never the app in front, the
  apps you just used, or apps playing sound.
- **Merge duplicates (page combining)**: pages with identical content
  across apps are kept once. A duplicate costs nothing, whatever its
  content. Every 30 min at most, with RAM 60% or more full, never during
  a game (the scan takes CPU time). Administrator rights needed.
- **Lower the memory priority** of compressed apps, so if RAM runs short
  Windows trims their pages before the game's.
- **Show the real ratio**: with administrator rights, OptiMind reads the
  memory manager's own statistics (the numbers behind Task Manager's
  "Compressed memory stores an estimated X MB of data" tooltip) and shows
  "Windows packs X of app data into Y (Z:1)" on the Agent page.

## 4. The Windows 11 settings that were researched

| Setting | What it does (per the source) | On this PC | OptiMind |
|---|---|---|---|
| Quality of Service / EcoQoS | Efficiency-mode processes run at the most efficient frequency (and on efficiency cores) | — | Used for busy background apps |
| Memory priority | Lower-priority pages are trimmed first | 5 (Normal) everywhere | Low for compressed apps and for OptiMind itself |
| Timer resolution | Since Windows 11, a minimized or hidden window's timer requests can be ignored | — | Nothing to do: Windows already handles it |
| MMCSS `SystemResponsiveness` | % of CPU reserved for low-priority work while multimedia threads run (default 20) | 20 | Left at the default |
| `Win32PrioritySeparation` | Quantum length and foreground boost (2 = "Programs") | 2 | Left at the default (already optimal for programs) |
| Game Mode | Game priority; no driver installs or restart prompts while playing | On | Tweak "Keep Game Mode on" |
| Optimizations for windowed games | Flip-model presentation for DX10/11 windowed games: lower latency, VRR | Not set | Tweak |
| Variable refresh rate (`VRROptimizeEnable`) | G-SYNC / FreeSync for DX11 full-screen games that don't support it themselves | Not set | Tweak |
| GPU scheduling priority class | The graphics kernel's per-process priority for GPU work | Normal, except Discord and NVIDIA's container, which set themselves to Realtime | Below normal for background GPU users at Normal or Above normal while a game is in focus; higher ones are left alone |
| Turn off hard disk after (`DISKIDLE`) | A hard disk stops after this many idle seconds; the next read waits for it to spin up | 1,200 s (20 min) | Tweak: Never, while plugged in |
| Hardware-accelerated GPU scheduling | The GPU schedules its own work | On | Tweak (state read from the driver) |
| Memory integrity (HVCI) | Hypervisor-checked kernel code; emulated (slower) on CPUs without MBEC/GMET | On, emulated | Hardware-check advice + opt-in "security trade-off" tweak |
| Memory compression / page combining (MMAgent) | Compress instead of paging; merge identical pages | Compression on | Tweak + agent action |
| Menu show delay | Delay before submenus open (default 400 ms) | 400 ms | Tweak (100 ms) |
| PCIe link power management, core parking, minimum processor state | Power savings with wake-up latency | Balanced plan | The High performance plan while a game is in focus |
| Message-signaled interrupts | Devices signal the CPU without shared interrupt lines | 10 of 12 devices | Reported only (forcing it can break a device) |

Left out on purpose: undocumented "gaming" registry values (for example
`NetworkThrottlingIndex`), boot-option timer tweaks, forcing interrupt
modes, and anything that turns off Defender or updates.

## 5. What limits a game's frame rate

A frame passes through the processor (the game's simulation, then the
draw calls and the driver) and then the graphics card (rendering), and
the slower of the two sets the frame rate, unless a frame cap or V-Sync
holds it lower on purpose. Which one it is decides what helps, and it's
often not what it seems: a card at 60% isn't "underused" by mistake, it's
waiting for the processor.

PresentMon reads, for each frame, how long the processor worked on it
(*CPU Busy*) and how long the graphics card did (*GPU Busy*). Intel's
rule: when GPU Busy fills almost the whole frame, the graphics card is the
limit; when it doesn't, the card waited for something. OptiMind counts a
frame as GPU-limited from 85% up. The waiting frames are split by their
rhythm: a cap holds frames at one steady interval, at the monitor's
refresh rate or a round number, while a processor that can't keep up
gives uneven ones.

| Limited by | What helps | What doesn't |
|---|---|---|
| **The graphics card** | Lower the settings that load it (resolution scale, shadows, ray tracing), DLSS / FSR Quality, NVIDIA's automatic tuning (a few %), cooling if it slows down from heat | A faster processor, more RAM |
| **The processor** | Lower view distance, crowds, physics; Focus mode on Maximum; closing background apps; a faster processor | Lower resolution or graphics settings (the card already waits): those can go *up* for free |
| **A cap / V-Sync** | Nothing needed; raise the cap if it's below the monitor's refresh rate. With G-SYNC / FreeSync, a cap 3 fps under the refresh rate avoids V-Sync's delay | Anything else: the PC has headroom |

Other things a session shows:

- **1% lows**: the frame rate of the slowest 1% of frames (the 99th
  percentile frame time). Far below the average means stutter, even when
  the average looks fine.
- **Frame time spikes**: frames taking more than twice the median. The
  usual causes are shader compilation (the first minutes after a game or
  driver update), a hard disk streaming the game's data, and full
  memory.
- **The presentation model**: a windowed game using the old "copy"
  presentation adds a frame of latency, which the windowed-games tweak
  fixes.

*Measured* on this PC: Counter-Strike 2 kept its busiest processor core at
100% while the graphics card sat at 57–62%: processor-limited, so its
graphics settings can go up without losing frames, and the real fix is a
faster processor. Memory reached 94–96% of 16 GB during play. A
background recorder (SteelSeries GG's Moments) kept the card's video
encoder about 19% busy and took about 3.5% of its 3D engine the whole
time.

**Known limit:** a processor that happens to hold exactly a cap's rate
(say 60.0 fps, steadily) looks like a cap. Real caps hold their rate
much more tightly than a struggling processor, so this is rare, and the
busiest core's load in the same session tells them apart.

## 6. Headroom: overclocking, and what it costs

"Using 100% of the hardware" has two parts. The first is what OptiMind
does all the time: the game gets the processor, memory, disk and
graphics card first. The second is running the parts above their stock
settings, which can add a few percent, needs testing on each specific
chip, and is where stability is lost. So OptiMind measures and advises,
and leaves the change to the makers' own tools:

| Option | Gain | Cost | OptiMind |
|---|---|---|---|
| NVIDIA automatic tuning (NVIDIA App > System > Performance) | A few % where the card is the limit (PCWorld: 217 → 224 fps, +3%, on an RTX 2080 Ti); it scans 10–20 min for what your card holds | NVIDIA said at launch it doesn't void the warranty; a little more heat | Hardware-check tip, with your card's power target |
| A higher GPU power target (same page) | More boost in GPU-limited games | More heat and fan noise | Shows when the card's firmware allows more (here: 175 W, up to 200 W) |
| AMD Precision Boost Overdrive (BIOS) | Usually 0–5% in games, more in multi-core work | **AMD states it voids the processor's warranty**; more heat and power | Hardware-check info/tip, with AMD's statement |
| A Ryzen 5000 on an AM4 board | The biggest single step where the processor limits (Ryzen 5 5600, Ryzen 7 5700X3D) | Money; may need a BIOS update first | Hardware-check tip when the board and BIOS fit |
| Memory profile (XMP / EXPO / D.O.C.P.) | The memory's rated speed instead of the fallback | Can be unstable; above the processor's official memory speed, AMD counts it as overclocking too | Hardware-check tip when memory runs below its rating |

OptiMind changes none of these itself. What a chip holds differs from
one chip to the next, so an overclock has to be tested on the PC it runs
on; one that isn't would trade the "no lost quality" promise for
crashes. NVIDIA's scan does that test, and PBO and memory profiles
belong to the BIOS. What OptiMind adds is the safety net: the Stability
check reads Windows' logs for the first signs that an overclock went too
far (hardware errors, graphics driver resets, bluescreens, unexpected
shutdowns).

Also left out: changing the NVIDIA shader cache size (tests disagree on
whether it helps) and timer-resolution tools (Windows 11 already
handles timer requests per process).

## 7. Sources

- Microsoft Learn, [Quality of Service](https://learn.microsoft.com/windows/win32/procthread/quality-of-service): QoS levels, EcoQoS, audible and foreground apps.
- Microsoft Learn, [SetProcessInformation](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation): memory priority, EcoQoS / HighQoS, ignored timer resolution.
- Microsoft Learn, [MEMORY_PRIORITY_INFORMATION](https://learn.microsoft.com/windows/win32/api/processthreadsapi/ns-processthreadsapi-memory_priority_information).
- Microsoft Learn, [CPU Sets](https://learn.microsoft.com/windows/win32/procthread/cpu-sets).
- Microsoft Learn, [Processor power management options](https://learn.microsoft.com/windows-hardware/customize/power-settings/configure-processor-power-management-options): core parking, performance states, the Game Mode profile.
- Microsoft Learn, [Multimedia Class Scheduler Service](https://learn.microsoft.com/windows/win32/procthread/multimedia-class-scheduler-service).
- Microsoft Learn, [timeBeginPeriod](https://learn.microsoft.com/windows/win32/api/timeapi/nf-timeapi-timebeginperiod): per-process timer resolution since Windows 10 2004.
- Microsoft Learn, [SetWinEventHook](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwineventhook) and [SHQueryUserNotificationState](https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate).
- Microsoft Learn, [Enable-MMAgent](https://learn.microsoft.com/powershell/module/mmagent/enable-mmagent) and the archived blog [Memory combining in Windows 8 and Windows Server 2012](https://learn.microsoft.com/archive/blogs/clinth/memory-combining-in-windows-8-and-windows-server-2012).
- Windows Insider blog (2015), memory compression introduction: compressed data at about 40% of its size, as reported by [Petri](https://petri.com/memory-compression-in-windows-10-threshold-2/).
- Microsoft Learn, [Enable virtualization-based protection of code integrity](https://learn.microsoft.com/windows/security/hardware-security/enable-virtualization-based-protection-of-code-integrity): MBEC / GMET and Restricted User Mode.
- Microsoft Support, Options to optimize gaming performance in Windows 11 (Memory integrity and VMP), as quoted by [Eleven Forum](https://www.elevenforum.com/t/options-to-optimize-gaming-performance-in-windows-11.9492/).
- Microsoft Support, [Optimizations for windowed games in Windows 11](https://support.microsoft.com/topic/3f006843-2c7e-4ed0-9a5e-f9389e535952).
- DirectX Developer Blog, [Hardware Accelerated GPU Scheduling](https://devblogs.microsoft.com/directx/hardware-accelerated-gpu-scheduling/).
- Microsoft Learn, [KMTQUERYADAPTERINFOTYPE](https://learn.microsoft.com/windows-hardware/drivers/ddi/d3dkmthk/ne-d3dkmthk-_kmtqueryadapterinfotype) (reading GPU scheduling support) and [Win32_PhysicalMemory](https://learn.microsoft.com/windows/win32/cimwin32prov/win32-physicalmemory).
- AMD GPUOpen, [CPU core counts](https://gpuopen.com/learn/cpu-core-counts/): SMT contention on game threads.
- Intel, [PresentMon](https://github.com/GameTechDev/PresentMon) and its [console application's metrics](https://github.com/GameTechDev/PresentMon/blob/main/README-ConsoleApplication.md): CPU Busy, GPU Busy, frame time, display latency; its test traces check OptiMind's reader.
- TweakTown, [Intel's GPU Busy metric in PresentMon](https://www.tweaktown.com/news/92908/intel-is-changing-how-gpu-performance-measured-with-the-new-busy-metric-in-presentmon/): GPU Busy against frame time tells a GPU limit from a CPU one.
- Microsoft Learn, [D3DKMTSetProcessSchedulingPriorityClass](https://learn.microsoft.com/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtsetprocessschedulingpriorityclass): a process's GPU scheduling priority.
- NVIDIA, [NVIDIA App Beta Update Adds AV1 Recording & One-Click Performance Tuning](https://www.nvidia.com/en-us/geforce/news/nvidia-app-beta-update-av1-performance-tuning/) (June 2024): the automatic tuning scan, 10–20 minutes, power and temperature targets.
- KitGuru, [NVIDIA App now lets you automatically overclock your GPU without voiding the warranty](https://www.kitguru.net/gaming/joao-silva/nvidia-app-now-lets-you-automatically-overclock-your-gpu-without-voiding-the-warranty/), and TechRadar, [automatic overclocking that won't affect your warranty](https://www.techradar.com/computing/gpu/nvidia-rtx-gpus-get-one-of-the-coolest-features-ever-automatic-overclocking-that-wont-affect-your-warranty): NVIDIA's warranty statement, as reported.
- PCWorld, [How to use Nvidia's performance tuning tool](https://www.pcworld.com/article/394678/how-to-use-nvidias-performance-tuning-tool-for-one-click-geforce-overclocking.html) (2021): +7 fps (217 → 224) on an RTX 2080 Ti, and that a tuned card can be unstable in some games.
- AMD, [Ryzen Master](https://www.amd.com/en/products/software/ryzen-master.html): Precision Boost Overdrive, and AMD's statement that overclocking, PBO included, voids the processor's warranty.
- Tom's Hardware, [Ryzen 5 3400G review](https://www.tomshardware.com/uk/reviews/amd-ryzen-5-3400g-review): PBO on the 3400G.
- Eleven Forum, [Variable refresh rate for games in Windows 11](https://www.elevenforum.com/t/enable-or-disable-variable-refresh-rate-for-games-in-windows-11.12052): what the setting does and where Windows stores it.
- System Informer's [phnt headers](https://github.com/winsiderss/phnt): the memory-compression statistics and page-combining calls (undocumented by Microsoft; also used by System Informer and Mem Reduct).
