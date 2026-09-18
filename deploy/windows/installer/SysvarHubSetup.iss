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
var
  InstallHubFailed: Boolean;

procedure FailInstallHub(Message: String);
begin
  InstallHubFailed := True;
  MsgBox(Message, mbError, MB_OK);
  Abort;
end;

function StopServiceForInstall(ServiceName: String): String;
var
  ResultCode: Integer;
  PowerShell: String;
  Parameters: String;
begin
  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$serviceName = ''' + ServiceName + '''; ' +
    '$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue; ' +
    'if ($null -eq $service) { exit 0 }; ' +
    'if ($service.Status -eq ''Stopped'') { exit 0 }; ' +
    'try { ' +
    '  Stop-Service -Name $serviceName -ErrorAction Stop; ' +
    '  $service.WaitForStatus(''Stopped'', ''00:00:30''); ' +
    '  if ((Get-Service -Name $serviceName).Status -ne ''Stopped'') { exit 2 }; ' +
    '  exit 0; ' +
    '} catch { exit 2 }"';

  if not Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Falha ao iniciar a parada do servico ' + ServiceName + '.';
    exit;
  end;

  if ResultCode <> 0 then
  begin
    Result := 'Nao foi possivel parar o servico ' + ServiceName + ' dentro do timeout. Feche o Sysvar Hub e tente novamente.';
    exit;
  end;

  Result := '';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := StopServiceForInstall('SysvarHub');
  if Result <> '' then
  begin
    exit;
  end;

  Result := StopServiceForInstall('SysvarHubMySQL');
end;

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
      FailInstallHub('Falha ao iniciar a instalacao operacional do Sysvar Hub.');
    end;

    if ResultCode <> 0 then
    begin
      FailInstallHub('A instalacao operacional do Sysvar Hub falhou. Verifique os logs em C:\ProgramData\SysvarHub\logs antes de tentar novamente.');
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := InstallHubFailed and (PageID = wpFinished);
end;
