; ATS TXL Windows installer. Build with ISCC /DMyAppVersion=x.y.z installer\ATS-TXL.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "ATS TXL"
#define MyAppExeName "ATS-TXL.exe"
#define MySourceDir "..\dist\ATS-TXL"

[Setup]
AppId={{C3A537A5-9E72-4D3D-937E-A2DD9BDE2A72}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=VNPT IT
DefaultDirName={autopf}\ATS TXL
DefaultGroupName=ATS TXL
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ATS-TXL-Setup
Compression=zip/9
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes
UninstallDisplayName=ATS TXL

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifdef DiagnosticBuild
[Dirs]
Name: "C:\ATS-TXL-Dumps"

[Registry]
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\chrome.exe"; ValueType: string; ValueName: "DumpFolder"; ValueData: "C:\ATS-TXL-Dumps"; Flags: uninsdeletevalue
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\chrome.exe"; ValueType: dword; ValueName: "DumpCount"; ValueData: "5"; Flags: uninsdeletevalue
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\chrome.exe"; ValueType: dword; ValueName: "DumpType"; ValueData: "2"; Flags: uninsdeletevalue
#endif

[Icons]
Name: "{autoprograms}\ATS TXL"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\ATS TXL"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng trên màn hình Desktop"; GroupDescription: "Biểu tượng bổ sung:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Mở ATS TXL"; Flags: nowait postinstall skipifsilent

#ifdef DiagnosticBuild
[UninstallDelete]
Type: filesandordirs; Name: "C:\ATS-TXL-Dumps"
#endif
