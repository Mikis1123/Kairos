; Kairos – instalátor (Inno Setup 6). Gate 0.
;
;   1) python -m PyInstaller app.spec --noconfirm          -> dist\Kairos\Kairos.exe
;   2) "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\kairos.iss  -> installer\Output\Kairos-Setup.exe
;
; Instaluje se pro aktuálního uživatele (bez UAC) do %LOCALAPPDATA%\Programs\Kairos.
; Data uživatele NEJSOU ve složce aplikace (paths.py):
;   %APPDATA%\Kairos       photos.db, config.json, vault\, backups\
;   %LOCALAPPDATA%\Kairos  faces\, cache\, models\, logs\
; Odinstalace je proto nesmaže – knihovna, alba, tváře i klíče Skrytých přežijí
; odinstalaci i přeinstalaci. (Úmyslně žádná sekce [UninstallDelete] pro tyto složky.)

#define AppName "Kairos"
#define AppVersion "1.0.0-beta"
#define AppExe "Kairos.exe"

[Setup]
AppId={{907BA4C9-793D-487E-993C-2B21F32C8E26}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=Kairos-Setup
SetupIconFile=..\assets\kairos.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "czech"; MessagesFile: "compiler:Languages\Czech.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Kairos\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
