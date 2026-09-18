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

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\uninstall-hub.ps1"" -InstallRoot ""{app}"""; Flags: runhidden waituntilterminated

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  PowerShell: String;
  Parameters: String;
begin
  if CurStep = ssPostInstall then
  begin
    PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
    Parameters := ExpandConstant('-ExecutionPolicy Bypass -File "{app}\scripts\install-hub.ps1" -InstallRoot "{app}"');

    if not Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('Falha ao iniciar a instalacao operacional do Sysvar Hub.', mbError, MB_OK);
      RaiseException('Falha ao iniciar install-hub.ps1.');
    end;

    if ResultCode <> 0 then
    begin
      MsgBox('A instalacao operacional do Sysvar Hub falhou. Verifique os logs em C:\ProgramData\SysvarHub\logs antes de tentar novamente.', mbError, MB_OK);
      RaiseException('install-hub.ps1 retornou codigo de erro ' + IntToStr(ResultCode) + '.');
    end;
  end;
end;
