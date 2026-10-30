# Optimize page: tweaks, hardware check and clean-up

Nothing on this page runs on its own: only when you turn a tweak's switch
on or off (after a confirmation), or press **Apply all safe tweaks**,
**Create restore point** or **Clean up**. Turning a tweak off is
its **Undo**.

## Windows 11 tweaks

The **Optimize** page brings the most useful tweaks of the popular
open-source Windows optimizers, [WinUtil](https://github.com/ChrisTitusTech/winutil)
(Chris Titus Tech), [Win11Debloat](https://github.com/Raphire/Win11Debloat)
(Raphire) and [Sparkle](https://github.com/Parcoil/Sparkle) (Parcoil),
into OptiMind with four rules:

1. **Reversible.** Before changing anything, OptiMind saves the old
   values in `data/tweaks_backup.json`, and **Undo** puts them back
   exactly. Undo only ever writes the tweak's own settings, even if that
   file has been edited.
2. **Safe.** Nothing that weakens security, and no app removal. One
   exception, kept apart under *Security trade-offs* and never part of
   **Apply all safe tweaks**: turning off Memory integrity, which
   Microsoft itself lists as an option for gamers (see below).
3. **Microsoft's own switches** wherever one exists: group policies, or
   the same values the Settings app writes. A policy that Windows
   protects from direct changes is set through Local Group Policy, the
   way the Local Group Policy Editor (`gpedit.msc`) does.
4. **Measured on your PC.** Each tweak shows what it would free right
   now (for example "Widgets: 3 processes use 198 MB"), and the Home
   page suggests the two that would free the most.

**Apply all safe tweaks** creates a System Restore point first.

On the page, each tweak is a card with its switch, what it does, when it
takes effect and what it frees; the buttons above them show one kind
(Gaming, Storage…), and the search box finds a tweak by its words.

| Tweak | Frees | Risk | How |
|---|---|---|---|
| Turn off Widgets | RAM | Safe | "Allow widgets" policy (`Dsh\AllowNewsAndInterests = 0`), set through Local Group Policy (see below) |
| Stop Edge running after you close it | RAM, CPU | Safe | Edge policies `StartupBoostEnabled` and `BackgroundModeEnabled` = 0 |
| Stop Store apps running in the background | RAM, CPU | Moderate | `BackgroundAccessApplications\GlobalUserDisabled = 1` |
| Turn off Copilot and Recall | RAM, privacy | Safe | `TurnOffWindowsCopilot` and `DisableAIDataAnalysis` policies |
| Keep Windows memory compression and page combining on | RAM | Safe | `Enable-MMAgent -MemoryCompression -PageCombining` (only what's off; Undo turns back off only what was off) |
| Turn off telemetry and compatibility scans | CPU, disk | Safe | DiagTrack service disabled; Compatibility Appraiser, CEIP and feedback tasks disabled |
| Stop sharing updates with other PCs | Network | Safe | Delivery Optimization `DownloadMode = 0` |
| Turn off background game recording | CPU/GPU in games | Safe | Game DVR off (`GameDVR_Enabled`, `AppCaptureEnabled`, `AllowGameDVR`) |
| Keep Game Mode on | Games | Safe | `GameBar\AutoGameModeEnabled = 1` (absent counts as on, Windows' default) |
| Optimizations for windowed games | Lower latency in DX10/11 windowed games | Safe | `SwapEffectUpgradeEnable=1` in `DirectXUserGlobalSettings` (what Settings > Graphics writes); only its own flag is set and undone, the string's other settings are kept |
| Variable refresh rate for games | Smoother DX11 games on a G-SYNC / FreeSync monitor | Safe | `VRROptimizeEnable=1` in the same string, the same way; does nothing without a variable refresh rate monitor |
| Hardware-accelerated GPU scheduling | CPU, games | Safe | `GraphicsDrivers\HwSchMode = 2`; its state is read from the graphics driver, and the tweak says "not available" on a card without support |
| Turn off web results in Start search | CPU | Safe | `DisableSearchBoxSuggestions`, search highlights off |
| Turn off tips, ads and auto-installed apps | Disk, CPU | Safe | ContentDeliveryManager suggestion values |
| Turn off transparency effects | GPU | Safe | `EnableTransparency = 0` |
| Turn off animations | Snappier UI | Safe | `SystemParametersInfo`, the Settings "Animation effects" switch |
| Open menus instantly | Snappier UI | Safe | `SystemParametersInfo(SPI_SETMENUSHOWDELAY)`: 100 ms instead of 400 ms |
| Turn off hibernation | Disk (the size of `hiberfil.sys`) | Safe (moderate on laptops) | `powercfg /hibernate off` |
| Keep hard disks spinning (plugged in) | No spin-up wait when a game starts from a hard disk | Safe | `powercfg /setacvalueindex <plan> SUB_DISK DISKIDLE 0` on the Balanced and High performance plans (and the active one); battery settings untouched; Undo puts each plan's own value back. Only on a PC with a hard disk |
| Turn on Storage Sense | Disk | Safe | `StorageSense\Parameters\StoragePolicy\01 = 1` |
| Group Windows services into fewer processes | RAM | Moderate | `SvcHostSplitThresholdInKB` = installed RAM |
| Turn off Memory integrity (for gaming) | CPU in games | **Security trade-off** | `DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity\Enabled = 0`; "not available" when a policy manages it or it's locked in the firmware |

**Memory integrity, both ways.** Core isolation's Memory integrity has
the hypervisor check every driver's code. Processors with MBEC (Intel 7th
gen and newer) or GMET (AMD Zen 2 and newer) do it in hardware; older
ones, such as a Ryzen 3000G APU, rely on an emulation that Microsoft says
"will have a bigger impact on performance", and Microsoft lists turning it
off as an option for gamers. The hardware check says whether your CPU
has the hardware feature; the tweak is there if you choose the trade-off,
it shows a stronger warning before it applies, and Undo turns protection
back on (both need a restart).

**Why Widgets goes through Local Group Policy.** Since 2026, Windows'
UserChoice Protection Driver (UCPD) refuses direct writes to the Widgets
policy from anything but Windows' own programs, even as administrator
("Access is denied"). Hiding the Widgets button doesn't help either: the
Widgets processes keep running. So OptiMind does what `gpedit.msc` does:
it stores the setting in the local policy file
(`C:\Windows\System32\GroupPolicy\Machine\Registry.pol`, keeping every
other setting in it), raises the policy version in `gpt.ini`, and runs
`gpupdate`, so Windows' own Group Policy service applies it. It then
checks that Windows really did; if not, it puts the file back as it was.
The setting shows in gpedit under *Computer Configuration > Administrative
Templates > Windows Components > Widgets > Allow widgets*, and Undo sets
it back to *Not configured*. This is for Windows Pro, Enterprise and
Education; Windows Home has no Group Policy Editor, and if Windows
doesn't apply the setting there, OptiMind says so and leaves nothing
changed.

**Left out on purpose:** turning off Defender, BitLocker, UAC,
SmartScreen or updates; removing Edge, OneDrive or built-in apps;
"set services to manual" presets; and undocumented "gaming" registry
values (such as `NetworkThrottlingIndex`) or boot-option timer tweaks.
They weaken security, can't be cleanly undone, break features
(WinUtil's service preset, for example, disables Internet Connection
Sharing, which Mobile Hotspot needs), or have no documented effect.
Also left out: forcing devices into MSI interrupt mode (a wrong driver
setting can stop Windows from starting; the hardware check only reports
it), changing the NVIDIA shader cache size (tests disagree on whether it
helps), and any overclocking done by OptiMind itself (see *Headroom*
below).

## Hardware check

Beside the tweaks (below them in a narrow window), the hardware check
reads, without changing anything, how the parts are connected and set
up, and says what to do about what software can't fix. It counts what
to check, tips, information and what's fine; each check's details fold
away, and a check that needs a look shows them open:

- memory: speed against what it's rated for (XMP / EXPO / D.O.C.P. in the
  BIOS), how many channels are in use, and how full RAM usually is;
- the PCIe links of the graphics card and the NVMe drive (lanes and
  generation, read from Windows' device properties), with what a
  narrower or slower link costs;
- every drive's type (NVMe, SSD, hard disk), the page file's drive, size
  and highest use, and whether Windows manages its size (a tip to let it
  when it's set by hand: a fixed page file can't grow when memory runs
  out), and **games installed on a hard disk** (Steam and Epic
  libraries), with how to move them;
- **drive health**: Windows' own verdict for every disk and, with
  administrator rights, its temperature, wear (SSDs) and errors it
  couldn't correct (`Get-StorageReliabilityCounter`);
- **hard disk spin-down**: whether the hard disk your games are on stops
  after a few idle minutes (then starting a game waits for it);
- **the monitor's refresh rate**: a monitor running below the highest
  rate it offers at its resolution (a 144 Hz monitor left at 60 Hz is
  common, and costs nothing to fix);
- Memory integrity against your CPU's hardware support, a hypervisor
  running without it (Virtual Machine Platform, WSL 2…), GPU
  scheduling, Game Mode, and whether devices use message-signaled
  interrupts.

### Headroom: overclocking, the safe way

Overclocking is where a PC can get more than it runs at out of the box,
and also where it can lose stability. OptiMind doesn't overclock on its
own: what a chip can hold differs from one chip to the next, and the safe
path is the makers' own tools, which test your specific part (NVIDIA's
scan) or keep the setting where it belongs (PBO in the BIOS). The
hardware check says what's available and what it costs:

- **Graphics card tuning** (NVIDIA RTX): NVIDIA App's *Automatic tuning*
  (System > Performance) scans the card for 10–20 minutes and keeps the
  highest clocks it found stable, usually a few percent more frames where
  the card is the limit. NVIDIA said at its launch that it doesn't void
  the warranty. The check also shows the card's power target when its
  firmware allows more than it runs at, and points out how many of your
  recent game sessions the card limited.
- **Processor boost (PBO)** on Ryzen processors that support it:
  Precision Boost Overdrive, AMD's own automatic overclocking, set in
  the BIOS. Usually 0–5% in games. AMD states that PBO, like any
  overclocking, voids the processor's warranty, and the check says so.
- **Processor upgrade**: on an AM4 board (A320 to X570) with a BIOS from
  2021 or later and a Ryzen 1000–3000 processor (or a 4000-series APU),
  a Ryzen 5000 fits the same socket: in games the processor limits, the
  biggest single upgrade the PC can get. Check the board's CPU support
  list first.
- **Stability**: Windows' own logs over the last 30 days (`stability.py`):
  hardware errors (WHEA, with what they point to: machine checks for the
  processor or memory, PCIe errors), graphics driver resets (Display
  4101, nvlddmkm 153), bluescreens, unexpected shutdowns (Kernel-Power
  41) and which apps crashed. These are the first signs that an
  overclock, an undervolt or a memory profile (XMP/EXPO) went too far, so
  after trying any of the above, check back here.

## Clean up disk space

Measures and empties your temporary files, Windows temporary files, the
Windows Update download cache, the Delivery Optimization cache, error
reports, crash dumps, the thumbnail cache and, only if you tick it, the
Recycle Bin. Only files untouched for 24 hours are deleted, files in use
are skipped, links and junctions are never followed, and each folder is
checked to really be what it claims (a TEMP variable pointing at `C:\`
is refused). What you tick adds up in big numbers above the list, and
**Clean up** deletes it.

## Starting from a clean install (advanced)

The lightest Windows 11 comes from a fresh install: the official
Microsoft ISO on a USB drive plus an `autounattend.xml` answer file that
removes bloat and applies tweaks while Windows installs, the method many
YouTube channels show (for example with the
[Schneegans unattend generator](https://schneegans.de/windows/unattend-generator/)
or [UnattendedWinstall](https://github.com/memstechtips/UnattendedWinstall)).
It erases the PC, so OptiMind only links to it and applies the same kind
of tweaks to the Windows you already have, each with its own undo.
