#define MyAppName "Sysvar Hub"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Sysvar"

[Setup]
AppId={{5D9F8B3D-2550-4F0D-87D9-38DD0648EF91}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Sysvar Hub
DefaultGroupName=Sysvar Hub
OutputDir=..\dist
OutputBaseFilename=SysvarHubSetup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin

[Files]
Source: "..\staging\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion
Source: "..\staging\frontend\*"; DestDir: "{app}\frontend"; Flags: recursesubdirs ignoreversion
Source: "..\staging\mysql\*"; DestDir: "{app}\mysql"; Flags: recursesubdirs ignoreversion
Source: "..\staging\scripts\*"; DestDir: "{app}\scripts"; Flags: recursesubdirs ignoreversion
Source: "..\staging\config-template\*"; DestDir: "{app}\config-template"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Abrir Sysvar Hub"; Filename: "http://localhost:8000/"
Name: "{group}\Verificar Status"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\check-hub.ps1"""
Name: "{group}\Configurar Sysvar Hub"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\configurar-hub.ps1"""
Name: "{group}\Abrir Logs"; Filename: "{commonappdata}\SysvarHub\logs"

[Run]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\install-hub.ps1"" -InstallRoot ""{app}"""; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\uninstall-hub.ps1"" -InstallRoot ""{app}"""; Flags: runhidden waituntilterminated
