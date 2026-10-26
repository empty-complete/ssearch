; UTF-8 source. Stable AppId preserves the per-user upgrade identity.
#if VER < EncodeVer(6, 4, 0)
  #error Inno Setup 6.4 or newer is required.
#endif
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef SourceDir
  #define SourceDir AddBackslash(SourcePath) + "..\dist\ssearch"
#endif
#ifndef OutputDir
  #define OutputDir AddBackslash(SourcePath) + "..\artifacts"
#endif
#ifdef ElectronBundle
  ; The common build supplies these from the exact Forge product-metadata.json.
  #ifndef AppName
    #error Electron installer requires the common product name.
  #endif
  #ifndef AppPublisher
    #error Electron installer requires the common publisher.
  #endif
  #ifndef AppSupportEmail
    #error Electron installer requires the common support email.
  #endif
  #ifndef AppDescription
    #error Electron installer requires the common description.
  #endif
  #ifndef AppCopyrightText
    #error Electron installer requires the common copyright.
  #endif
#endif
#ifndef AppName
  #define AppName "ssearch"
#endif
#ifndef AppPublisher
  #define AppPublisher "Филипп Белобородов (empty-complete)"
#endif
#ifndef AppSupportEmail
  #define AppSupportEmail "filipp.beloborodoff@yandex.ru"
#endif
#ifndef AppDescription
  #define AppDescription "ssearch — Simple Search"
#endif
#ifndef AppCopyrightText
  #define AppCopyrightText "Copyright (c) 2026 empty-complete (Филипп Белобородов) — MIT License"
#endif

[Setup]
AppId={{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL=mailto:{#AppSupportEmail}
AppCopyright={#AppCopyrightText}
#ifdef ElectronBundle
DefaultDirName={localappdata}\Programs\ssearch
#else
DefaultDirName={localappdata}\Programs\AtomSearch
#endif
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayName={#AppName}
#ifdef ElectronBundle
UninstallDisplayIcon={app}\service\_internal\resources\branding\atom-search.ico
#else
UninstallDisplayIcon={app}\_internal\resources\branding\atom-search.ico
#endif
SetupIconFile=..\resources\branding\atom-search.ico
CloseApplications=yes
CloseApplicationsFilter=ssearch.exe,AtomSearch.exe,KaizenSearch.exe
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=ssearch-Setup-{#AppVersion}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Установка {#AppDescription}
VersionInfoCopyright={#AppCopyrightText}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Retire only known program files for a registered same-path Atom/Kaizen upgrade.
Type: files; Name: "{app}\AtomSearch.exe"; Check: IsLegacyUpgrade
Type: files; Name: "{app}\_internal\resources\eula.txt"; Check: IsLegacyUpgrade
Type: files; Name: "{app}\_internal\resources\branding\ksg.png"; Check: IsLegacyUpgrade
Type: files; Name: "{app}\_internal\resources\branding\astralit.png"; Check: IsLegacyUpgrade
Type: files; Name: "{app}\_internal\resources\branding\sources.json"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Справка Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Удалить Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userdesktop}\Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userprograms}\Atom Search\Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userprograms}\Atom Search\Справка Atom Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userprograms}\Atom Search\Удалить Atom Search.lnk"; Check: IsLegacyUpgrade
Type: dirifempty; Name: "{userprograms}\Atom Search"; Check: IsLegacyUpgrade
; Remove exact legacy program files only for a registered upgrade in this path.
; No directories or user-data files are removed.
Type: files; Name: "{app}\KaizenSearch.exe"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Справка Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{group}\Удалить Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userdesktop}\Kaizen Search.lnk"; Check: IsLegacyUpgrade
; The selected group can change during an upgrade. Also retire default-group
; shortcuts by exact name, preserving all other files in that group.
Type: files; Name: "{userprograms}\Kaizen Search\Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userprograms}\Kaizen Search\Справка Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: files; Name: "{userprograms}\Kaizen Search\Удалить Kaizen Search.lnk"; Check: IsLegacyUpgrade
Type: dirifempty; Name: "{userprograms}\Kaizen Search"; Check: IsLegacyUpgrade

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\ssearch.exe"; WorkingDir: "{app}"
#ifdef ElectronBundle
Name: "{group}\Справка {#AppName}"; Filename: "{app}\service\_internal\resources\help.html"
#else
Name: "{group}\Справка {#AppName}"; Filename: "{app}\_internal\resources\help.html"
#endif
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\ssearch.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ssearch.exe"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent unchecked

; No autostart registration. No network downloads. No user-data delete sections:
; {localappdata}\AtomSearch and legacy {localappdata}\KaizenSearch belong to
; the user and survive upgrade and uninstall.

[Code]
var
  LegacyUpgrade: Boolean;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PreviousDirectory, PreviousName: String;
  RegisteredInstallation: Boolean;
begin
  LegacyUpgrade := False;
  PreviousDirectory := '';
  RegisteredInstallation := RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
       'InstallLocation', PreviousDirectory);
#ifdef ElectronBundle
  // Qt-to-Electron cleanup needs a verified old-file/hash inventory and a real
  // upgrade gate. Until then fail closed instead of deleting unknown files or
  // accepting a mixed payload. User profiles are never installation payload.
  // Selecting a different /DIR must not bypass the registered old Qt payload.
  if DirExists(ExpandConstant('{app}\_internal')) or
     (RegisteredInstallation and DirExists(AddBackslash(PreviousDirectory) + '_internal')) then
  begin
    Result := 'Эта сборка пока не поддерживает обновление найденной прежней установки. Установка остановлена; пользовательские данные сохраняются.';
    Exit;
  end;
#endif
  if RegisteredInstallation and RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
       'DisplayName', PreviousName) then
    LegacyUpgrade := ((PreviousName = 'Kaizen Search') or (PreviousName = 'Atom Search')) and
      (CompareText(RemoveBackslashUnlessRoot(ExpandFileName(PreviousDirectory)),
                   RemoveBackslashUnlessRoot(ExpandFileName(ExpandConstant('{app}')))) = 0);
  Result := '';
end;

function IsLegacyUpgrade: Boolean;
begin
  Result := LegacyUpgrade;
end;
