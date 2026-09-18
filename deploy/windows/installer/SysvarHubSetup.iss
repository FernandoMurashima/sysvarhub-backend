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

[UninstallDelete]
Type: files; Name: "{app}\is-*.tmp"
Type: dirifempty; Name: "{app}"

[Code]
var
  InstallHubFailed: Boolean;
  SysvarLocalAgentWasRunning: Boolean;
  SysvarLocalAgentTouched: Boolean;

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

function GetServiceStatus(ServiceName: String; var Status: String): Boolean;
var
  ResultCode: Integer;
  PowerShell: String;
  Parameters: String;
  OutputFile: String;
  LoadedStatus: AnsiString;
begin
  Result := False;
  Status := '';
  OutputFile := ExpandConstant('{tmp}\sysvarhub-service-status.txt');
  DeleteFile(OutputFile);
  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$service = Get-Service -Name ''' + ServiceName + ''' -ErrorAction SilentlyContinue; ' +
    'if ($null -eq $service) { exit 3 }; ' +
    '[System.IO.File]::WriteAllText(''' + OutputFile + ''', [string]$service.Status); exit 0"';

  if Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
  begin
    if LoadStringFromFile(OutputFile, LoadedStatus) then
    begin
      Status := LoadedStatus;
      Result := True;
    end;
  end;
  DeleteFile(OutputFile);
end;

function StopSysvarLocalAgentIfNeeded: String;
var
  Status: String;
begin
  Result := '';
  if not GetServiceStatus('SysvarLocalAgent', Status) then
  begin
    exit;
  end;

  if Status = 'Running' then
  begin
    SysvarLocalAgentWasRunning := True;
    SysvarLocalAgentTouched := True;
    Result := StopServiceForInstall('SysvarLocalAgent');
  end;
end;

procedure RestoreSysvarLocalAgent;
var
  ResultCode: Integer;
  PowerShell: String;
  Parameters: String;
begin
  if (not SysvarLocalAgentTouched) or (not SysvarLocalAgentWasRunning) then
  begin
    exit;
  end;

  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$service = Get-Service -Name ''SysvarLocalAgent'' -ErrorAction SilentlyContinue; ' +
    'if ($null -eq $service -or $service.Status -eq ''Running'') { exit 0 }; ' +
    'Start-Service -Name ''SysvarLocalAgent'' -ErrorAction Stop; exit 0"';
  Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  SysvarLocalAgentWasRunning := False;
  SysvarLocalAgentTouched := False;

  Result := StopServiceForInstall('SysvarHub');
  if Result <> '' then
  begin
    exit;
  end;

  Result := StopServiceForInstall('SysvarHubMySQL');
  if Result <> '' then
  begin
    exit;
  end;

  Result := StopSysvarLocalAgentIfNeeded;
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

    RestoreSysvarLocalAgent;
  end;
end;

procedure DeinitializeSetup();
begin
  RestoreSysvarLocalAgent;
end;

procedure RunUninstallHubScript;
var
  ResultCode: Integer;
  PowerShell: String;
  Parameters: String;
begin
  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters := ExpandConstant('-NoProfile -ExecutionPolicy Bypass -File "{app}\scripts\uninstall-hub.ps1" -InstallRoot "{app}"');

  if not Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Falha ao iniciar a limpeza operacional do Sysvar Hub.', mbError, MB_OK);
    Abort;
  end;

  if ResultCode <> 0 then
  begin
    MsgBox('A limpeza operacional do Sysvar Hub falhou. Verifique a parada dos servicos SysvarHub e SysvarHubMySQL antes de tentar novamente.', mbError, MB_OK);
    Abort;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ErrorMessage: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    SysvarLocalAgentWasRunning := False;
    SysvarLocalAgentTouched := False;
    ErrorMessage := StopSysvarLocalAgentIfNeeded;
    if ErrorMessage <> '' then
    begin
      MsgBox(ErrorMessage, mbError, MB_OK);
      Abort;
    end;
    RunUninstallHubScript;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    RestoreSysvarLocalAgent;
  end;
end;

procedure DeinitializeUninstall();
begin
  RestoreSysvarLocalAgent;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := InstallHubFailed and (PageID = wpFinished);
end;
