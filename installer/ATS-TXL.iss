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
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes
UninstallDisplayName=ATS TXL

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\ATS TXL"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\ATS TXL"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng trên màn hình Desktop"; GroupDescription: "Biểu tượng bổ sung:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Mở ATS TXL"; Flags: nowait postinstall skipifsilent
