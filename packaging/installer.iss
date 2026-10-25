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
#define AppName "ssearch"
#define AppPublisher "Филипп Белобородов (empty-complete)"

[Setup]
AppId={{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL=mailto:filipp.beloborodoff@yandex.ru
AppCopyright=Copyright (c) 2026 empty-complete (Филипп Белобородов) — MIT License
DefaultDirName={localappdata}\Programs\AtomSearch
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\_internal\resources\branding\atom-search.ico
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
VersionInfoDescription=Установка ssearch — Simple Search
VersionInfoCopyright=Copyright (c) 2026 empty-complete (Филипп Белобородов) — MIT License

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
Name: "{group}\Справка {#AppName}"; Filename: "{app}\_internal\resources\help.html"
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
begin
  LegacyUpgrade := False;
  if RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
       'InstallLocation', PreviousDirectory) and
     RegQueryStringValue(HKCU,
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
