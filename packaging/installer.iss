; OptiMind AI installer (Inno Setup 7). packaging\build.ps1 compiles it;
;
;     ISCC.exe /DAppVersion=1.1.0 packaging\installer.iss
;
; It installs the folder Nuitka compiled (build\dist\OptiMind) into Program Files, adds
; OptiMind to the Start menu (and, if you choose, to the desktop and to Windows' start-up),
; and writes one Setup file: dist\OptiMind-AI-Setup-<version>.exe.

#ifndef AppVersion
  #error Pass the version: ISCC.exe /DAppVersion=x.y.z installer.iss (build.ps1 does)
#endif
#define AppName "OptiMind AI"
#define AppExe "OptiMind.exe"
; Same as app/desktop/__init__.py (APP_ID) and app/desktop/instance.py (MUTEX_NAME)
#define AppUserModelId "OptiMindAI.OptiMind"
#define AgentMutex "OptiMindAI.Agent"

[Setup]
AppId={{6F1A3C52-7B0E-4E8B-9D7A-2C4E5F6A8B91}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OptiMind AI
AppComments=Keeps Windows 11 responsive, with AI tuning its rules.
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
DefaultDirName={autopf}\{#AppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=admin
SetupArchitecture=x64
MinVersion=10.0.18362
WizardStyle=modern dynamic windows11
WizardImageFile=assets\wizard-*.png
WizardSmallImageFile=assets\corner-*.png
; The same images in dark mode (otherwise Setup shows its own dark pictures)
WizardImageFileDynamicDark=assets\wizard-*.png
WizardSmallImageFileDynamicDark=assets\corner-*.png
SetupIconFile=..\app\static\optimind.ico
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
OutputDir=..\dist
OutputBaseFilename=OptiMind-AI-Setup-{#AppVersion}
; OptiMind is closed by the [Code] below (it gives apps their settings back first)
CloseApplications=no
RestartApplications=no
; Show language selection dialog (enabled for 2.4.0 with 10 languages)
ShowLanguageDialog=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Default.isl"
Name: "fr"; MessagesFile: "compiler:Default.isl"
Name: "it"; MessagesFile: "compiler:Default.isl"
Name: "ja"; MessagesFile: "compiler:Default.isl"
Name: "ko"; MessagesFile: "compiler:Default.isl"
Name: "pl"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Default.isl"
Name: "zh_Hans"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nOptiMind keeps Windows 11 responsive: the app you're using gets the PC first, busy background apps are held back, and idle memory goes back to Windows. An AI reviews its rules every few minutes.%n%nIt works from the notification area, with administrator rights.
FinishedLabel=[name] is installed. It works from the notification area: open it there or from the Start menu.

[Tasks]
Name: "autostart"; Description: "Start OptiMind when I sign in (in the notification area, with administrator rights and no prompt)"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Versions up to 1.3.0 (built with PyInstaller) kept everything but OptiMind.exe in _internal:
; an update from one of them removes it, so nothing from the old version lingers
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\build\dist\OptiMind\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Comment: "Keeps Windows 11 responsive"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--register-autostart"; StatusMsg: "Setting OptiMind to start with Windows..."; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\{#AppExe}"; Parameters: "--unregister-autostart"; Flags: runhidden waituntilterminated; Tasks: not autostart
Filename: "{app}\{#AppExe}"; Description: "Open OptiMind AI"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only a downloaded NVIDIA driver installer is kept there, until that driver is installed
Type: filesandordirs; Name: "{commonappdata}\OptiMind"

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitOptiMind"
Filename: "{app}\{#AppExe}"; Parameters: "--unregister-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "NoAutostart"

[Code]
function OptiMindRunning: Boolean;
begin
  Result := CheckForMutexes('{#AgentMutex}');
end;

{ Before an update: ask the running OptiMind to quit (it gives apps their own settings back first). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode, Waited: Integer;
  Exe: String;
begin
  Result := '';
  if not OptiMindRunning then
    Exit;
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while OptiMindRunning and (Waited < 20000) do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  if OptiMindRunning then
    Result := 'OptiMind AI is running. Quit it from its icon in the notification area (right-click it, then Quit OptiMind), and run Setup again.';
end;

{ After uninstalling: your settings and data stay unless you choose to delete them. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{localappdata}\OptiMind');
  if DirExists(DataDir) and not UninstallSilent then
    if MsgBox('Also delete your OptiMind settings and data?' + #13#10#13#10 +
              'That is your API keys, the activity log, the AI''s rules and the backups OptiMind uses to undo tweaks. ' +
              'Tweaks you applied stay applied: undo them in OptiMind first if you want them back.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir, True, True, True);
end;

{ Language selection in the setup dialog }
function CurLanguageChanged(wNewLanguageIndex: Integer): Boolean;
var
  LangCode: String;
begin
  { Get the language code selected in the dialog }
  LangCode := ExpandConstant('{cm:Language}');
  { Show confirmation - the app will read the language from its settings on first startup }
  if LangCode <> '' then
    MsgBox(format('OptiMind will start in %s language after installation.', [LangCode]), mbInformation, MB_OK);
  
  Result := True;
end;