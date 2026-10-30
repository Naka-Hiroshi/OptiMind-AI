# Drivers

The Drivers page checks every device and its driver, and the drivers that
apps install. It reads only Windows' own records and the makers' own
sites. Updates come from where Microsoft says they should: Windows Update,
then the device maker's website. It can install them, back up all your
drivers, and put them back after a clean reinstall. It never changes
anything until you press a button.

## What it checks

| Part | What OptiMind reads | Where from |
|---|---|---|
| **Devices** | Every device Windows knows, connected or not: its driver's provider, version, date and INF file, who signed it, and the problem code Device Manager shows next to its yellow mark | Windows' Plug and Play manager (CfgMgr32, the API Device Manager reads); the signer is the driver package's (below), or Windows itself for the drivers that came with Windows |
| **Main drivers** | Graphics, chipset, sound, network, Bluetooth, storage and USB: how old each driver is, and where its updates come from | The same, plus the board and BIOS from the firmware's own tables |
| **Driver store** | The driver packages other makers added to Windows (`oemNN.inf`), which devices use each one, who signed it, its size, and the older versions a newer one replaced | `pnputil /enum-drivers /devices /format xml` (Windows 11 26H2 and later); on older Windows, the INF files themselves (SetupAPI), with the signer from Windows' own check of each package's catalog (`SetupVerifyInfFile`, Device Manager's *Digital Signer*) |
| **Drivers apps added** | Kernel drivers that belong to no device: anti-cheat, antivirus, virtual machines, VPNs, fan and RGB control, virtual audio. Who made each one, whether it runs, and known risks | The Service Control Manager's driver list, the registry, each file's version information |
| **Windows' logs** (30 days) | Drivers that failed to load, devices that had trouble starting, drivers that didn't start, devices that stopped responding and were reset (network adapters, storage controllers, disks, the graphics driver), drivers Code Integrity blocked or would block | System (Kernel-PnP 219, Service Control Manager 7000 and 7026, NDIS 10400, storahci/stornvme 129, disk 153 and 7, nvlddmkm 153, Display 4101), Kernel-PnP/Configuration (411), CodeIntegrity/Operational (3076, 3077, 3033, 3004; driver files only) |
| **Protection** | The Windows Driver Policy's mode, the vulnerable driver blocklist, Memory integrity, test signing, and whether Windows Update may deliver drivers | Code Integrity's policy folder and the registry (no administrator rights needed) |
| **Firmware** | The board or PC model, and the BIOS version and date | `HKLM\HARDWARE\DESCRIPTION\System\BIOS` (Windows' copy of the firmware's tables) |

### What it reports

Each finding has a status, what was found, why it matters and what to do,
as on the hardware check. The worst comes first. The most serious ones also
show on Home under *Needs attention*.

- **A device with a problem**: Microsoft's own explanation of the Device
  Manager code, what fixes it, and a button where one exists. *Restart
  the device* clears codes 10, 31, 43 and 54. *Scan for hardware
  changes* reinstalls a missing or broken driver (28, 18, 19, 24…).
  *Look for a driver on Windows Update* helps with codes 28, 48 and 52.
  A device you turned off (code 22) is only information, with a *Turn it
  on* button.
- **Devices that stopped responding**: a network adapter its driver had
  to reset ("the network driver detected that its hardware has stopped
  responding"), a storage controller reset, a disk whose reads or writes
  had to be retried or that reported a bad block, graphics driver
  errors. Each one is listed with how often and when. When several
  failed during the same hours, the finding says so: the cause is then
  usually something they share (the chipset driver or BIOS, power, heat,
  one failing part) rather than each driver. It also says whether it
  stopped after the last restart, and gives the steps that fit: update
  an old driver of the device that reset, check a disk's cables and
  health, install the chipset drivers.
- **The graphics card on Windows' basic display driver**: no 3D speed,
  no G-SYNC or FreeSync. Points to NVIDIA, AMD or Intel by the card's
  PCI vendor ID.
- **The graphics driver** (NVIDIA): whether a newer Game Ready driver
  is out, from NVIDIA's own driver lookup (the one nvidia.com/drivers
  uses). Checked at most every 12 hours; offline, the check is retried
  after an hour.
- **Old drivers**: main drivers from other makers that are more than 6
  years old, with where to look: the board maker's page for on-board
  sound and network, Realtek for its network controllers, AMD's chipset
  page for the board's chipset. Old isn't wrong by itself, so it's a tip,
  not a warning.
- **Driver updates on Windows Update**: each one next to the driver it
  would replace (newer, the same, older, or for a device Windows will
  match).
- **Firmware on Windows Update**: Windows Update also lists firmware
  among its driver updates (class *Firmware*: the PC's BIOS/UEFI or a
  device's own firmware, written into the hardware at the next restart).
  OptiMind shows it as firmware and never installs it: one that fails
  can leave the PC or the device unable to start.
- **Driver signatures**: the drivers from other makers that your devices
  use and that aren't signed through Microsoft's hardware program,
  either signed only with the maker's own certificate (the older way;
  the 2026 driver policy stops trusting the kernel drivers among them)
  or not signed at all. When all are, it says so. Every device, main
  driver and package also shows who signed it: Windows' own, signed
  through Microsoft, signed only by its maker, or not signed.
- **The Windows Driver Policy**: since the April 2026 update, Windows no
  longer trusts drivers signed only through the retired cross-signing
  program. A PC is first evaluated: drivers it would block are only
  logged (event 3076), and each one restarts the evaluation. After 250
  hours and 3 restarts with none, Windows enforces the policy and blocks
  them (3077). OptiMind names any driver on this PC that is flagged or
  blocked, and says the PC's mode.
- **Known risky drivers**: WinRing0 (fan control, RGB and monitoring
  apps). Any program can use it to take full control of Windows
  (CVE-2020-14979), and Microsoft Defender flags it since March 2025.
- **Drivers left behind**: an app was uninstalled, and its driver is
  still set to start with Windows without its file.
- **Drivers that didn't start**: from other makers only. Windows' own
  drivers that don't load are listed in the logs card, as Windows' own,
  but aren't findings. Some Windows PCs report one of them, such as
  `dam`, at every start.
- **The vulnerable driver blocklist off**, **test signing on**,
  **signature checks off**, **drivers excluded from Windows Update** by a
  policy.
- **Old driver versions** in the driver store that nothing needs.
- **The BIOS**: version, date, and the maker's page. OptiMind never
  installs firmware or a BIOS.
- **No driver backup yet**.

## Installing drivers

Every install needs administrator rights, refuses to start while a game
is running, and creates a System Restore point first (type
`DEVICE_DRIVER_INSTALL`, as Windows itself does before a driver change).
If System Protection is off, the install goes ahead and says that no
restore point was made. One driver task runs at a time, on its own thread.
The page shows its progress, and the result lands in the activity log.

- **Windows Update**: OptiMind installs the driver updates Windows Update
  offers, through the Windows Update Agent API. This is the same list as
  Settings > Windows Update > Advanced options > Optional updates > Driver
  updates. It refuses any driver older than the one installed, and any
  firmware: the agent checks the list, and its installer checks again,
  on what Windows Update itself says about each update right before
  installing it (a driver, and not of the *Firmware* class). Drivers
  there are signed through Microsoft's hardware program, and Windows
  checks each signature as it installs them.
  *Check Windows Update now* asks Microsoft's servers, like *Check for
  updates* in Settings. In the background, OptiMind only reads what
  Windows Update found at its own last check (every 6 hours at most,
  about a second, nothing sent).
- **NVIDIA**: OptiMind downloads the newest driver's installer from
  NVIDIA's download server (`us.download.nvidia.com`; any other address,
  or a redirect elsewhere, is refused). It downloads in background mode,
  at low CPU, disk and memory priority. The file goes to
  `%ProgramData%\OptiMind\downloads`, a folder locked to administrators
  and SYSTEM before each download, with anything else in it deleted.
  NVIDIA's installer starts with administrator rights, so no ordinary
  program may swap the file or plant a DLL next to it. OptiMind then
  checks that Windows confirms the file is signed by NVIDIA Corporation,
  and starts NVIDIA's own installer. If the signature check fails, the
  file is deleted and nothing starts. The downloaded installer is deleted
  once that version is installed.
- **From a backup**: `pnputil /add-driver <backup>\*.inf /subdirs
  /install`. Windows still only uses a driver that ranks best for a
  device, so a newer driver already installed stays.

AMD and Intel don't publish a lookup like NVIDIA's. For their graphics
drivers and chipsets, OptiMind links to their own tools and pages: AMD
Software or AMD's driver page (with the page for your board's chipset),
and Intel Driver & Support Assistant.

## The makers' own apps

AMD, Intel and the PC makers deliver some drivers through their own app,
which knows each model. The Drivers page names the official ones for
this PC (`app/system/maker_tools.py`), says whether each is in your Start
menu (`Get-StartApps`, read-only, alongside the scan), and links to the
maker's own page for it:

| For | App | Shown when |
|---|---|---|
| NVIDIA graphics | NVIDIA App | an NVIDIA card is connected (OptiMind also checks NVIDIA's newest driver itself) |
| AMD Radeon graphics | AMD Software: Adrenalin Edition | an AMD graphics card or AMD processor graphics |
| Intel parts | Intel Driver & Support Assistant | any Intel part: graphics, Wi-Fi, network, chipset, Bluetooth |
| Dell, HP, Lenovo, ASUS, MSI, GIGABYTE, Acer PCs and laptops | SupportAssist (or Dell Command \| Update), HP Support Assistant, Lenovo Vantage (or Lenovo System Update), MyASUS, MSI Center, GIGABYTE Control Center, Acer Care Center (or AcerSense) | the PC's own maker, from its firmware |
| A board in a self-built PC (ASUS, MSI, GIGABYTE) | Armoury Crate, MSI Center, GIGABYTE Control Center | only when it's installed: the board's download page has the same drivers without it |

*Open* starts an app you have from the Start menu, the way a click there
does (`explorer.exe shell:AppsFolder\<its ID>`), with your normal rights,
from OptiMind's window, never from the agent that runs as administrator.
The window opens only an app the agent lists for this PC at that moment
(`GET /api/drivers/tools`), so the page can't start any other program.
OptiMind never downloads these apps: *Get it* opens the maker's own page.
They can also offer BIOS updates; OptiMind never installs one, and if you
do, follow the maker's steps with the PC on reliable power.

## The driver tools

- **Scan for hardware changes** (`pnputil /scan-devices`).
- **Restart a device** (`pnputil /restart-device`), offered for a device
  with a problem.
- **Turn on a device** (`pnputil /enable-device`), offered for a device
  that's turned off.
- **Remove old driver versions** (`pnputil /delete-driver`, never with
  `/force`). Only packages that no device uses, connected or not, and
  that a newer package of the same driver replaced. The newest of those
  stays, as the version Windows can go back to. A driver none of whose
  versions is in use is left alone, because its device may just be
  unplugged. The list is checked again right before removing, Windows
  itself refuses a package a device still needs, and a restore point
  comes first.
- **Back up drivers** (`pnputil /export-driver *`): every driver package
  from other makers is copied to `<drive>\OptiMind driver backup\<date
  time>`. A list of every device and its driver (`drivers.csv`, opens in
  Excel) and `backup.json` go with it. The page suggests another drive
  than Windows' own, because a clean reinstall erases that one. Backups
  on any fixed drive are listed with *Install drivers* and *Open folder*.

In test mode (`OPTIMIND_DRY_RUN=true`) every tool only says what it would
do, and nothing is downloaded. *Check Windows Update now* reads Windows
Update's own last result instead of asking Microsoft's servers.

## Cost

A full scan takes about 1–2 seconds, of which about 0.4–0.8 s is CPU time
in the agent; reading Windows' logs takes longest when a device logged
thousands of resets. `pnputil` runs as a process of its own for about
half a second, and so does PowerShell's `Get-StartApps` for about a
second, at the same time as the rest of the scan (the scan doesn't take
longer for it).
The first scan after a start also reads each driver file's details once.
The scan is kept 10 minutes. Home asks for the last scan's findings every
30 seconds (about 3 ms) and refreshes the scan in the background when it's
older than 30 minutes, only while OptiMind's window is open. Nothing
driver-related runs while the window is closed, and nothing in the agent's
2-second loop. The update checks run on their own threads, so opening the
page never waits for the internet.

## Where drivers come from (and where they shouldn't)

OptiMind follows Microsoft's advice. Windows Update first: Windows 11
installs recommended drivers itself, and optional ones are listed there.
If Windows Update has nothing, use the hardware maker's website. For
sound and network chips on the motherboard, the board maker's package for
your exact model also brings the control software (Realtek Audio Console,
Nahimic…), which is why OptiMind points there. MSI explains the same for
its boards.

Left out on purpose: "driver updater" apps and download sites. Security
companies classify many of them as potentially unwanted software. They
bundle other programs, fetch drivers from questionable places, and show
inflated scan results to sell a license. Microsoft's own rules for
cleaners and optimizers forbid alarming or coercive messages and require
details for every finding, which is how this page is written: calm, with
the facts and the source of each one.

## Sources

- Microsoft Support, [Automatically get recommended and updated hardware drivers](https://support.microsoft.com/en-us/windows/hardware/drivers/automatically-get-recommended-and-updated-hardware-drivers): Windows Update installs recommended drivers; optional drivers under Advanced options > Optional updates; otherwise the hardware maker's website.
- Microsoft Support, [Error codes in Device Manager in Windows](https://support.microsoft.com/en-us/windows/hardware/drivers/error-codes-in-device-manager-in-windows), and Microsoft Learn, [Device Manager error messages](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/device-manager-error-messages) (the CM_PROB_ codes).
- Microsoft Learn, [PnPUtil command syntax](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-command-syntax): `/enum-drivers`, `/export-driver`, `/add-driver /install`, `/delete-driver`, `/scan-devices`, `/restart-device`, `/enable-device`. The XML output of `/enum-drivers` and `/enum-devices` is listed by `pnputil /?` on Windows 11 26H2.
- Microsoft Learn, [IUpdateSearcher::Search](https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nf-wuapi-iupdatesearcher-search) (search criteria, `Type='Driver'`), [IWindowsDriverUpdate](https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iwindowsdriverupdate) (driver class, provider, model, hardware ID, date) and [Searching, downloading and installing specific updates](https://learn.microsoft.com/en-us/windows/win32/wua_sdk/searching--downloading--and-installing-specific-updates).
- Microsoft Learn, [Authoring an update driver package](https://learn.microsoft.com/en-us/windows-hardware/drivers/bringup/authoring-an-update-driver-package) (firmware updates are driver packages of class `Firmware`, `{f2e7dd72-6468-4e36-b6f1-6488f42c1b52}`) and [Windows UEFI firmware update platform](https://learn.microsoft.com/en-us/windows-hardware/drivers/bringup/windows-uefi-firmware-update-platform) (system and device firmware delivered through Windows Update).
- Microsoft Learn, [SetupVerifyInfFile](https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupverifyinffilew), [Using SetupAPI to verify driver Authenticode signatures](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/using-setupapi-to-verify-driver-authenticode-signatures) (a maker's own signature fails the check but names the signer) and [Signature score](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/signature-score--windows-vista-and-later-).
- Microsoft Support, [The Windows Driver Policy](https://support.microsoft.com/en-us/windows/hardware/drivers/the-windows-driver-policy): the April 2026 change, evaluation and enforcement (250 hours, 3 restarts), events 3076 and 3077, the policy IDs.
- Microsoft Support, [KB5020779: the vulnerable driver blocklist](https://support.microsoft.com/en-us/servicing/os/windows/2022/10/kb5020779-the-vulnerable-driver-blocklist-after-the-october-2022-preview-release), on by default since Windows 11 22H2; Microsoft Learn, [Microsoft recommended driver block rules](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/microsoft-recommended-driver-block-rules) (updated quarterly and with monthly updates); Microsoft Support, [KB5094459](https://support.microsoft.com/en-us/help/5094459) (April 2026: a vulnerable driver blocked, event 3077 with the blocklist's policy ID).
- Microsoft Learn, [Understanding App Control event IDs](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations) (3004, 3033, 3076, 3077, 3089).
- Microsoft Learn, [How Windows selects a driver for a device](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/how-windows-selects-a-driver-for-a-device), and the device properties [DEVPKEY_Device_DriverDate](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/devpkey-device-driverdate), [DriverVersion](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/devpkey-device-driverversion), [DriverInfPath](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/devpkey-device-driverinfpath), [FirstInstallDate](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/devpkey-device-firstinstalldate).
- CVE-2020-14979, [CVE record](https://cveawg.mitre.org/api/cve/CVE-2020-14979) (WinRing0: any user can read and write all memory, published August 11, 2020); the FanControl project's [discussion of the Defender detection](https://github.com/Rem0o/FanControl.Releases/discussions/3017) (March 2025; replaced by PawnIO in version 239).
- NVIDIA's driver pages ([nvidia.com/drivers](https://www.nvidia.com/en-us/drivers/), [NVIDIA App](https://www.nvidia.com/en-us/software/nvidia-app/)); AMD's [driver page](https://www.amd.com/en/support/download/drivers.html) and chipset pages (e.g. [B450](https://www.amd.com/en/support/downloads/drivers.html/chipsets/am4/b450.html)); Intel's [Driver & Support Assistant](https://www.intel.com/content/www/us/en/support/detect.html); MSI, [Which onboard audio drivers should I download for my motherboard?](https://us.msi.com/faq/mb-3201).
- Microsoft Security blog and customer-protection criteria for cleaners and optimizers (no coercive messaging; details for every finding); Trend Micro and PCRisk on driver-updater PUAs.
