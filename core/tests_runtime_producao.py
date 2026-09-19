import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, override_settings
from django.urls import resolve

from sysvarhub.urls import angular_spa
import runtime.windows_service as windows_service
from runtime.windows_runtime import (
    ACL_ADMINISTRATORS,
    ACL_SYSTEM,
    MYSQL_ADMIN_FILE,
    create_default_env,
    create_mysql_admin_file,
    generate_secret,
    load_env_file,
    local_hostnames_and_ips,
    merge_csv,
    write_locked_file,
)
from runtime.windows_service import HubWaitressRuntime, stop_runtime


class HealthCheckTests(SimpleTestCase):
    def test_health_check_publico(self):
        response = Client().get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "sysvar-hub"})


class AngularSpaFallbackTests(SimpleTestCase):
    def test_rota_spa_retorna_index(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            Path(temp_dir, "index.html").write_text("<app-root></app-root>", encoding="utf-8")

            with override_settings(FRONTEND_DIST_DIR=Path(temp_dir)):
                response = Client().get("/pdv")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<app-root></app-root>")

    def test_api_nao_e_capturada_pelo_fallback(self):
        resolver_match = resolve("/api/terminal/contexto/")

        self.assertIsNot(resolver_match.func, angular_spa)

    def test_asset_existente_e_servido(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            Path(temp_dir, "main.js").write_text("console.log('ok')", encoding="utf-8")
            Path(temp_dir, "index.html").write_text("<app-root></app-root>", encoding="utf-8")

            with override_settings(FRONTEND_DIST_DIR=Path(temp_dir)):
                response = Client().get("/main.js")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console.log('ok')")


class ProductionSettingsTests(SimpleTestCase):
    def test_whitenoise_configurado(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)

    def test_secret_key_padrao_nao_e_aceito_em_producao(self):
        module_globals = {
            "DEBUG": False,
            "SECRET_KEY": "dev-insecure-sysvarhub-change-me",
            "RuntimeError": RuntimeError,
        }

        with self.assertRaises(RuntimeError):
            exec(
                "if not DEBUG and SECRET_KEY == 'dev-insecure-sysvarhub-change-me':\n"
                "    raise RuntimeError('DJANGO_SECRET_KEY deve ser configurado')",
                module_globals,
            )


class WindowsRuntimeTests(SimpleTestCase):
    def test_hosts_lan_preservam_configuracao_manual(self):
        hosts = merge_csv(["hub.local", "127.0.0.1"], ["localhost", "hub.local", "192.168.0.50"])

        self.assertEqual(hosts, ["hub.local", "127.0.0.1", "localhost", "192.168.0.50"])
        self.assertNotIn("*", hosts)

    def test_hosts_locais_incluem_loopback(self):
        hosts = local_hostnames_and_ips()

        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)

    def test_geracao_de_segredo_nao_usa_valor_default(self):
        secret = generate_secret()

        self.assertNotEqual(secret, "dev-insecure-sysvarhub-change-me")
        self.assertGreaterEqual(len(secret), 64)

    def test_env_padrao_instalado_nao_contem_segredos_fixos(self):
        with TemporaryDirectory() as temp_dir:
            import runtime.windows_runtime as windows_runtime

            original_env = windows_runtime.ENV_FILE
            windows_runtime.ENV_FILE = Path(temp_dir) / "sysvarhub.env"
            try:
                with patch("subprocess.run"):
                    create_default_env(
                        install_root=Path(r"C:\Program Files\Sysvar Hub"),
                        program_data=Path(r"C:\ProgramData\SysvarHub"),
                    )
                content = windows_runtime.ENV_FILE.read_text(encoding="utf-8")
            finally:
                windows_runtime.ENV_FILE = original_env

        self.assertIn("DB_PORT=3307", content)
        self.assertIn("SYSVARHUB_FRONTEND_DIST_DIR=", content)
        self.assertNotIn("dev-insecure-sysvarhub-change-me", content)

    def test_load_env_file_aceita_utf8_com_bom_na_primeira_variavel(self):
        with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            env_file = Path(temp_dir) / "sysvarhub.env"
            env_file.write_bytes(
                b"\xef\xbb\xbfDJANGO_SECRET_KEY=segredo-teste\n"
                b"DJANGO_DEBUG=False\n"
            )

            loaded = load_env_file(env_file)

        self.assertEqual(loaded["DJANGO_SECRET_KEY"], "segredo-teste")
        self.assertEqual(loaded["DJANGO_DEBUG"], "False")
        self.assertNotIn("\ufeffDJANGO_SECRET_KEY", loaded)

    def test_load_env_file_aceita_utf8_sem_bom_na_primeira_variavel(self):
        with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            env_file = Path(temp_dir) / "sysvarhub.env"
            env_file.write_text(
                "DJANGO_SECRET_KEY=segredo-teste\n"
                "DJANGO_DEBUG=False\n",
                encoding="utf-8",
            )

            loaded = load_env_file(env_file)

        self.assertEqual(loaded["DJANGO_SECRET_KEY"], "segredo-teste")
        self.assertEqual(loaded["DJANGO_DEBUG"], "False")
        self.assertNotIn("\ufeffDJANGO_SECRET_KEY", loaded)

    def test_mysql_admin_file_usa_formato_canonico_cnf(self):
        with TemporaryDirectory() as temp_dir:
            import runtime.windows_runtime as windows_runtime

            original_admin_file = windows_runtime.MYSQL_ADMIN_FILE
            windows_runtime.MYSQL_ADMIN_FILE = Path(temp_dir) / "mysql-admin.cnf"
            try:
                with patch("subprocess.run"):
                    password = create_mysql_admin_file()
                content = windows_runtime.MYSQL_ADMIN_FILE.read_text(encoding="utf-8")
            finally:
                windows_runtime.MYSQL_ADMIN_FILE = original_admin_file

        self.assertEqual(MYSQL_ADMIN_FILE.name, "mysql-admin.cnf")
        self.assertIn("[client]", content)
        self.assertIn("user=root", content)
        self.assertIn(f"password={password}", content)
        self.assertIn("host=127.0.0.1", content)
        self.assertIn("port=3307", content)
        self.assertNotIn("MYSQL_ADMIN_PASSWORD", content)
        self.assertNotIn("mysql-admin.env", str(MYSQL_ADMIN_FILE))

    def test_arquivos_sensiveis_usam_sids_estaveis_no_runtime(self):
        self.assertEqual(ACL_SYSTEM, "*S-1-5-18:F")
        self.assertEqual(ACL_ADMINISTRATORS, "*S-1-5-32-544:F")

        with TemporaryDirectory() as temp_dir, patch("os.name", "nt"), patch("subprocess.run") as run:
            write_locked_file(Path(temp_dir) / "sysvarhub.env", "SECRET=1\n")

        run.assert_any_call(["icacls", str(Path(temp_dir) / "sysvarhub.env"), "/inheritance:r"], check=False, capture_output=True)
        run.assert_any_call(
            ["icacls", str(Path(temp_dir) / "sysvarhub.env"), "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F"],
            check=False,
            capture_output=True,
        )

    def test_install_hub_script_nao_depende_de_administrators_localizado(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("*S-1-5-18:F", script)
        self.assertIn("*S-1-5-32-544:F", script)
        self.assertIn("/inheritance:r", script)
        self.assertNotIn("Administrators:F", script)
        self.assertNotIn("SYSTEM:F", script)

    def test_install_hub_cria_sysvarhub_env_utf8_sem_bom_compativel_com_ps51(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        env_block = script[script.index("if (-not (Test-Path $EnvFile))"):script.index("if (-not (Test-Path $MyIni))")]

        self.assertIn("$EnvLines = @(", env_block)
        self.assertIn("New-Object System.Text.UTF8Encoding($false)", env_block)
        self.assertIn("[System.IO.File]::WriteAllLines($EnvFile, $EnvLines, $Utf8NoBom)", env_block)
        self.assertNotIn("Set-Content -LiteralPath $EnvFile -Encoding UTF8", env_block)
        self.assertIn("Protect-SecretFile $EnvFile", env_block)
        self.assertIn("SYSVARHUB_DATA_DIR=$DataRoot", env_block)

    def test_install_hub_define_data_root_com_programdata_data(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        data_root_pos = script.index('$DataRoot = Join-Path $ProgramDataRoot "data"')
        mkdir_pos = script.index("New-Item -ItemType Directory")

        self.assertLess(data_root_pos, mkdir_pos)
        self.assertIn("$DataRoot", script[mkdir_pos:mkdir_pos + 180])
        self.assertIn("SYSVARHUB_DATA_DIR=$DataRoot", script)

    def test_install_hub_atualiza_env_antigo_sem_recriar_ou_trocar_segredos(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        env_block = script[script.index("if (-not (Test-Path $EnvFile))"):script.index("if (-not (Test-Path $MyIni))")]
        update_block = env_block[env_block.index("} else {"):]

        self.assertIn("Get-Content -LiteralPath $EnvFile", update_block)
        self.assertIn('$_ -match "^\\s*SYSVARHUB_DATA_DIR\\s*="', update_block)
        self.assertIn('Add-Content -LiteralPath $EnvFile -Value "SYSVARHUB_DATA_DIR=$DataRoot"', update_block)
        self.assertNotIn("[System.IO.File]::WriteAllLines", update_block)
        self.assertNotIn("Protect-SecretFile", update_block)
        self.assertNotIn("New-Secret", update_block)
        self.assertNotIn("DJANGO_SECRET_KEY=", update_block)
        self.assertNotIn("DB_PASSWORD=", update_block)

    def test_install_hub_nao_duplica_data_dir_em_execucao_repetida(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        env_block = script[script.index("if (-not (Test-Path $EnvFile))"):script.index("if (-not (Test-Path $MyIni))")]
        update_block = env_block[env_block.index("} else {"):]
        add_pos = update_block.index('Add-Content -LiteralPath $EnvFile -Value "SYSVARHUB_DATA_DIR=$DataRoot"')
        check_pos = update_block.index('if (-not $DataDirLine)')

        self.assertLess(check_pos, add_pos)
        self.assertEqual(update_block.count('Add-Content -LiteralPath $EnvFile -Value "SYSVARHUB_DATA_DIR=$DataRoot"'), 1)

    def test_install_hub_nao_reescreve_sysvarhub_env_existente_para_remover_bom(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        env_block = script[script.index("if (-not (Test-Path $EnvFile))"):script.index("if (-not (Test-Path $MyIni))")]

        self.assertTrue(env_block.strip().startswith("if (-not (Test-Path $EnvFile))"))
        self.assertIn("[System.IO.File]::WriteAllLines($EnvFile, $EnvLines, $Utf8NoBom)", env_block)
        self.assertNotIn("Remove-Item", env_block)

    def test_pyinstaller_spec_resolve_backend_root_para_collect_e_pathex(self):
        spec = Path(settings.BASE_DIR, "runtime", "SysvarHubService.spec").read_text(encoding="utf-8")
        backend_pos = spec.index("backend_root = spec_root.parent")
        collect_pos = spec.index('collect_submodules("core"')

        self.assertLess(backend_pos, collect_pos)
        self.assertIn("sys.path.insert(0, candidate)", spec)
        self.assertIn("pathex=[str(spec_root), str(backend_root)]", spec)
        self.assertIn('collect_submodules("integracao"', spec)
        self.assertIn('collect_submodules("sysvarhub"', spec)

    def test_pyinstaller_spec_coleta_submodulos_whitenoise(self):
        spec = Path(settings.BASE_DIR, "runtime", "SysvarHubService.spec").read_text(encoding="utf-8")

        self.assertIn('"whitenoise"', spec)
        self.assertIn('collect_submodules("whitenoise"', spec)
        self.assertLess(spec.index('"whitenoise"'), spec.index('collect_submodules("whitenoise"'))

    def test_build_installer_tem_gate_runtime_antes_do_inno(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "build-installer.ps1").read_text(encoding="utf-8")
        gate_pos = script.index("Invoke-RuntimeGate -RuntimeRoot")
        iscc_pos = script.index("& $Iscc")

        self.assertLess(gate_pos, iscc_pos)
        self.assertIn("function Invoke-RuntimeGate", script)
        self.assertIn('throw "Gate runtime falhou em manage check empacotado."', script)

    def test_build_installer_gate_valida_imports_dinamicos_empacotados(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "build-installer.ps1").read_text(encoding="utf-8")

        self.assertIn("& $RuntimeExe manage shell -c $ImportCheck", script)
        self.assertIn("'whitenoise.middleware.WhiteNoiseMiddleware'", script)
        self.assertIn("'whitenoise.storage.CompressedManifestStaticFilesStorage'", script)
        self.assertIn("'corsheaders.middleware.CorsMiddleware'", script)
        self.assertIn("'django_filters.rest_framework.DjangoFilterBackend'", script)
        self.assertIn("'rest_framework.authentication.TokenAuthentication'", script)
        self.assertIn("'rest_framework.authentication.SessionAuthentication'", script)
        self.assertIn("'rest_framework.permissions.IsAuthenticated'", script)
        self.assertIn("'rest_framework.pagination.PageNumberPagination'", script)
        self.assertIn("importlib.import_module(settings.DATABASES['default']['ENGINE'] + '.base')", script)
        self.assertIn("import_string(settings.STATICFILES_STORAGE)", script)

    def test_build_installer_gate_sobe_console_e_testa_http(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "build-installer.ps1").read_text(encoding="utf-8")

        self.assertIn('$StartInfo.ArgumentList.Add("console")', script)
        self.assertIn('Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health/"', script)
        self.assertIn('$HealthResponse.status -ne "ok"', script)
        self.assertIn('$HealthResponse.service -ne "sysvar-hub"', script)
        self.assertIn('Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/"', script)
        self.assertIn('GET / OK', script)

    def test_build_installer_gate_usa_programdata_temporario_e_nao_servicos_reais(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "build-installer.ps1").read_text(encoding="utf-8")
        gate_block = script[script.index("function Invoke-RuntimeGate"):script.index("$ResolvedBackend =")]

        self.assertIn("sysvarhub-runtime-smoke-", gate_block)
        self.assertIn('$StartInfo.Environment["SYSVARHUB_PROGRAMDATA"] = $SmokeRoot', gate_block)
        self.assertIn("Remove-Item -LiteralPath $SmokeRoot -Recurse -Force", gate_block)
        self.assertIn("finally", gate_block)
        self.assertIn("$Process.Kill()", gate_block)
        self.assertNotIn("Start-Service", gate_block)
        self.assertNotIn("Stop-Service", gate_block)
        self.assertNotIn("--startup auto install", gate_block)
        self.assertNotIn("sc.exe", gate_block)

    def test_build_installer_smoke_configura_data_dir_temporario(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "build-installer.ps1").read_text(encoding="utf-8")
        smoke_block = script[script.index("function New-RuntimeSmokeProgramData"):script.index("function Invoke-RuntimeGate")]

        self.assertIn('New-Item -ItemType Directory -Force -Path $ConfigRoot, (Join-Path $Root "logs"), (Join-Path $Root "data")', smoke_block)
        self.assertIn("SYSVARHUB_DATA_DIR=$(Join-Path $Root 'data')", smoke_block)


class WindowsUninstallScriptTests(SimpleTestCase):
    def test_uninstall_hub_nao_remove_install_root(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")

        self.assertIn('$InstallRoot = "C:\\Program Files\\Sysvar Hub"', script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("Test-Path $InstallRoot", script)
        self.assertNotIn("C:\\Program Files\\Sysvar Hub", script.replace('$InstallRoot = "C:\\Program Files\\Sysvar Hub"', ""))

    def test_uninstall_hub_preserva_program_data(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("C:\\ProgramData\\SysvarHub foi preservado", script)
        self.assertNotIn("purge-data.ps1", script)
        self.assertNotIn("ProgramData\\SysvarHub", script.replace("C:\\ProgramData\\SysvarHub foi preservado", ""))

    def test_uninstall_hub_remove_servicos_e_firewall(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")

        self.assertIn('Remove-ServiceAfterStop -Name "SysvarHub" -AllowServicePidKill', script)
        self.assertIn('Remove-ServiceAfterStop -Name "SysvarHubMySQL"', script)
        self.assertIn("Get-Service -Name $Name -ErrorAction SilentlyContinue", script)
        self.assertIn("sc.exe delete $Name", script)
        self.assertIn('Get-NetFirewallRule -DisplayName "Sysvar Hub" -ErrorAction SilentlyContinue', script)
        self.assertIn("Remove-NetFirewallRule", script)

    def test_uninstall_hub_possui_parada_limitada_por_timeout(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("$ServiceStopTimeoutSeconds = 45", script)
        self.assertIn("function Wait-ServiceStopped", script)
        self.assertIn("$deadline = (Get-Date).AddSeconds($TimeoutSeconds)", script)
        self.assertIn("while ((Get-Date) -lt $deadline)", script)
        self.assertIn("throw \"Nao foi possivel parar o servico $Name dentro de $TimeoutSeconds segundos.", script)

    def test_uninstall_hub_fallback_sysvarhub_usa_pid_associado_ao_servico(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")

        self.assertIn('$Name -eq "SysvarHub"', script)
        self.assertIn('$status -eq "StopPending"', script)
        self.assertIn('Get-CimInstance Win32_Service -Filter "Name=\'$Name\'"', script)
        self.assertIn("$serviceProcessId = [int]$wmiService.ProcessId", script)
        self.assertIn("if ($serviceProcessId -gt 0)", script)
        self.assertIn("Stop-Process -Id $serviceProcessId -Force -ErrorAction Stop", script)
        self.assertNotIn("$pid = [int]$wmiService.ProcessId", script)
        self.assertNotIn("Stop-Process -Id $pid", script)

    def test_uninstall_hub_nao_tem_kill_generico_de_hub_ou_mysql(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")
        lower_script = script.lower()

        self.assertNotIn("taskkill", lower_script)
        self.assertNotIn("sysvarhubservice.exe", lower_script)
        self.assertNotIn("mysqld.exe", lower_script)
        self.assertNotIn("get-process mysqld", lower_script)
        self.assertNotIn("stop-process mysqld", lower_script)

    def test_uninstall_hub_mysql_falha_de_forma_controlada_sem_forcar_processo(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "uninstall-hub.ps1").read_text(encoding="utf-8")
        mysql_call_pos = script.index('Remove-ServiceAfterStop -Name "SysvarHubMySQL"')

        self.assertNotIn("AllowServicePidKill", script[mysql_call_pos:mysql_call_pos + 80])
        self.assertIn("throw \"Nao foi possivel parar o servico $Name", script)

    def test_inno_setup_chama_uninstall_hub_com_exit_code_verificado(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        uninstall_proc_pos = iss.index("procedure RunUninstallHubScript")
        uninstall_script_pos = iss.index("uninstall-hub.ps1", uninstall_proc_pos)

        self.assertGreater(uninstall_script_pos, uninstall_proc_pos)
        self.assertIn('if ResultCode <> 0 then', iss[uninstall_proc_pos:])
        self.assertIn("Abort;", iss[uninstall_proc_pos:])


class WindowsInstallScriptTests(SimpleTestCase):
    def test_install_hub_new_secret_usa_rng_compativel_com_windows_powershell(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertNotIn("RandomNumberGenerator]::Fill", script)
        self.assertIn("[System.Security.Cryptography.RandomNumberGenerator]::Create()", script)
        self.assertIn("$rng.GetBytes($bytes)", script)
        self.assertIn("$rng.Dispose()", script)
        self.assertIn('[Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")', script)

    def test_install_hub_nao_depende_de_powershell_7(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertNotIn("pwsh", script.lower())
        self.assertNotIn("pwsh", iss.lower())
        self.assertIn(r"{sys}\WindowsPowerShell\v1.0\powershell.exe", iss)

    def test_install_hub_persiste_credencial_antes_de_proteger_root(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        main_flow = script[script.index("if (-not (Test-Path $MysqlSystemDir))"):]

        initialize_pos = main_flow.index('--initialize-insecure')
        write_admin_pos = main_flow.index("Write-MySqlAdminFile (New-Secret 48)")
        bootstrap_pos = main_flow.index("Set-MySqlRootPasswordFromAdminFile")

        self.assertLess(initialize_pos, write_admin_pos)
        self.assertLess(write_admin_pos, bootstrap_pos)
        self.assertNotIn("$AdminPassword = New-Secret", script)
        self.assertNotIn("FirstRun", script)

    def test_install_hub_recupera_datadir_sem_credencial_quando_root_sem_senha_funciona(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("Test-MySqlRootWithoutPassword", script)
        self.assertIn("if (-not (Test-Path $MysqlAdminFile))", script)
        self.assertIn("Write-MySqlAdminFile (New-Secret 48)", script)
        self.assertIn("Set-MySqlRootPasswordFromAdminFile", script)
        self.assertNotIn("--initialize-insecure --force", script)

    def test_install_hub_reutiliza_credencial_existente_e_nao_regenera(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        existing_credential_pos = script.index("elseif (Test-MySqlAdminCredential)")
        insecure_recovery_pos = script.index("elseif (Test-MySqlRootWithoutPassword)")

        self.assertLess(existing_credential_pos, insecure_recovery_pos)
        self.assertIn("Credencial administrativa existente validada.", script)
        self.assertIn("Get-MySqlAdminPassword", script)
        self.assertNotIn("Remove-Item -LiteralPath $MysqlData", script)

    def test_install_hub_credencial_invalida_sem_root_livre_falha_sem_reset_destrutivo(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("Estado administrativo do MySQL inconsistente", script)
        self.assertNotIn("mysqld --init-file", script)
        self.assertNotIn("--skip-grant-tables", script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("purge-data.ps1", script)

    def test_install_hub_mysql_admin_cnf_e_acl_sao_canonicos(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn('$MysqlAdminFile = Join-Path $ConfigRoot "mysql-admin.cnf"', script)
        self.assertIn('"[client]", "user=root", "password=$Password", "host=127.0.0.1", "port=3307"', script)
        self.assertIn("Protect-SecretFile $MysqlAdminFile", script)
        self.assertIn("/inheritance:r", script)
        self.assertIn("$AclSystem $AclAdministrators", script)
        self.assertNotIn("mysql-admin.env", script)

    def test_install_hub_tem_readiness_mysql_com_retry_e_timeout(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("function Wait-MySqlReady([int]$TimeoutSeconds = 60)", script)
        self.assertIn("System.Net.Sockets.TcpClient", script)
        self.assertIn("while ((Get-Date) -lt $deadline)", script)
        self.assertIn("Start-Sleep -Seconds 1", script)
        self.assertIn("Wait-MySqlReady 60", script)
        self.assertNotIn("Start-Sleep -Seconds 5", script)

    def test_install_hub_mysql_usa_argumentos_explicitos_sem_formas_compactas(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertNotIn("-h127.0.0.1", script)
        self.assertNotIn("-P3307", script)
        self.assertNotIn("-uroot", script)
        self.assertIn('"--host=127.0.0.1"', script)
        self.assertIn('"--port=3307"', script)
        self.assertIn('"--user=root"', script)
        self.assertIn('"--connect-timeout=5"', script)
        self.assertIn('"--defaults-extra-file=$MysqlAdminFile"', script)

    def test_install_hub_mysql_probes_retornam_boolean_por_exit_code_sem_abortar_stderr(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")
        helper_block = script[script.index("function Invoke-MySqlCommand"):script.index("function Get-MySqlAdminArgs")]

        self.assertIn('[switch]$Probe', helper_block)
        self.assertIn('$ErrorActionPreference = "Continue"', helper_block)
        self.assertIn('2>&1', helper_block)
        self.assertIn('$exitCode = $LASTEXITCODE', helper_block)
        self.assertIn("if ($Probe)", helper_block)
        self.assertIn("return ($exitCode -eq 0)", helper_block)
        self.assertNotIn("catch", helper_block)
        self.assertIn('$ErrorActionPreference = $previousErrorActionPreference', helper_block)

    def test_install_hub_mysql_comandos_obrigatorios_falham_com_mensagem_controlada(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn('throw $FailureMessage', script)
        self.assertIn('-FailureMessage "Protecao do usuario administrativo MySQL falhou."', script)
        self.assertIn('-FailureMessage "Preparacao do banco Sysvar Hub falhou."', script)
        self.assertNotIn('if ($LASTEXITCODE -ne 0) { throw "Protecao do usuario administrativo MySQL falhou." }', script)
        self.assertNotIn('if ($LASTEXITCODE -ne 0) { throw "Preparacao do banco Sysvar Hub falhou." }', script)

    def test_install_hub_mysql_senhas_nao_entram_em_argumentos_do_processo(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertNotIn('-e "ALTER USER', script)
        self.assertNotIn('-e "CREATE DATABASE', script)
        self.assertNotIn('IDENTIFIED BY \'$adminPassword\'" ', script)
        self.assertNotIn('IDENTIFIED BY \'$DbPassword\'" ', script)
        self.assertIn('-InputSql "ALTER USER', script)
        self.assertIn('-InputSql "CREATE DATABASE', script)

    def test_install_hub_registra_servico_com_startup_antes_do_install(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("& $ServiceExe --startup auto install", script)
        self.assertNotIn("& $ServiceExe install --startup auto", script)

    def test_runtime_nao_mantem_mysql_admin_env_concorrente(self):
        runtime = Path(settings.BASE_DIR, "runtime", "windows_runtime.py").read_text(encoding="utf-8")

        self.assertIn('MYSQL_ADMIN_FILE = CONFIG_DIR / "mysql-admin.cnf"', runtime)
        self.assertIn('"[client]"', runtime)
        self.assertIn('"user=root"', runtime)
        self.assertIn('"host=127.0.0.1"', runtime)
        self.assertIn('"port=3307"', runtime)
        self.assertNotIn("mysql-admin.env", runtime)
        self.assertNotIn("MYSQL_ADMIN_PASSWORD", runtime)

    def test_inno_setup_verifica_exit_code_do_install_hub(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("[Code]", iss)
        self.assertIn("Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)", iss)
        self.assertIn("if ResultCode <> 0 then", iss)
        self.assertIn("FailInstallHub('A instalacao operacional do Sysvar Hub falhou.", iss)
        self.assertNotIn('[Run]\nFilename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\\scripts\\install-hub.ps1""', iss)


class WindowsInstallerReinstallTests(SimpleTestCase):
    def test_inno_setup_tem_hook_pre_instalacao_para_parar_servicos(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("function PrepareToInstall(var NeedsRestart: Boolean): String;", iss)
        self.assertIn("function StopServiceForInstall(ServiceName: String): String;", iss)
        self.assertIn("StopServiceForInstall('SysvarHub')", iss)
        self.assertIn("StopServiceForInstall('SysvarHubMySQL')", iss)
        self.assertIn("StopSysvarLocalAgentIfNeeded", iss)

    def test_inno_setup_para_hub_mysql_e_agent_antes_da_copia(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        hub_pos = iss.index("StopServiceForInstall('SysvarHub')")
        mysql_pos = iss.index("StopServiceForInstall('SysvarHubMySQL')")
        agent_pos = iss.index("StopSysvarLocalAgentIfNeeded", mysql_pos)
        post_install_pos = iss.index("if CurStep = ssPostInstall then")

        self.assertLess(hub_pos, mysql_pos)
        self.assertLess(mysql_pos, agent_pos)
        self.assertLess(agent_pos, post_install_pos)

    def test_inno_setup_aceita_servico_inexistente_ou_ja_parado(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("Get-Service -Name $serviceName -ErrorAction SilentlyContinue", iss)
        self.assertIn("if ($null -eq $service) { exit 0 }", iss)
        self.assertIn("if ($service.Status -eq ''Stopped'') { exit 0 }", iss)

    def test_inno_setup_para_servico_em_execucao_e_aguarda_com_timeout(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("Stop-Service -Name $serviceName -ErrorAction Stop", iss)
        self.assertIn("$service.WaitForStatus(''Stopped'', ''00:00:30'')", iss)
        self.assertIn("Nao foi possivel parar o servico", iss)
        self.assertIn("dentro do timeout", iss)

    def test_inno_setup_falha_ao_parar_aborta_instalacao_sem_forcar_kill_ou_delete(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        prepare_block = iss[iss.index("function StopServiceForInstall"):iss.index("procedure CurStepChanged")]

        self.assertIn("if ResultCode <> 0 then", prepare_block)
        self.assertNotIn("taskkill", prepare_block.lower())
        self.assertNotIn("/f", prepare_block.lower())
        self.assertNotIn("sc.exe delete", prepare_block.lower())
        self.assertNotIn("delete SysvarHub", prepare_block)
        self.assertNotIn("Remove-Item", prepare_block)
        self.assertNotIn("ProgramData", prepare_block)

    def test_inno_setup_registra_estado_original_do_sysvarlocalagent(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("SysvarLocalAgentWasRunning: Boolean", iss)
        self.assertIn("SysvarLocalAgentTouched: Boolean", iss)
        self.assertIn("GetServiceStatus('SysvarLocalAgent', Status)", iss)
        self.assertIn("if Status = 'Running' then", iss)
        self.assertIn("SysvarLocalAgentWasRunning := True", iss)
        self.assertIn("SysvarLocalAgentTouched := True", iss)

    def test_inno_setup_restaura_sysvarlocalagent_somente_se_estava_rodando(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        restore_block = iss[iss.index("procedure RestoreSysvarLocalAgent"):iss.index("function PrepareToInstall")]

        self.assertIn("if (not SysvarLocalAgentTouched) or (not SysvarLocalAgentWasRunning) then", restore_block)
        self.assertIn("Start-Service -Name ''SysvarLocalAgent'' -ErrorAction Stop", restore_block)
        self.assertIn("$service.Status -eq ''Running''", restore_block)

    def test_inno_setup_restaura_sysvarlocalagent_em_sucesso_falha_e_cancelamento(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("RestoreSysvarLocalAgent;", iss[iss.index("procedure CurStepChanged"):])
        self.assertIn("procedure DeinitializeSetup()", iss)
        self.assertIn("procedure DeinitializeUninstall()", iss)

    def test_inno_setup_desinstalacao_coordena_sysvarlocalagent(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        uninstall_block = iss[iss.index("procedure CurUninstallStepChanged"):iss.index("procedure DeinitializeUninstall")]

        self.assertIn("if CurUninstallStep = usUninstall then", uninstall_block)
        self.assertIn("ErrorMessage := StopSysvarLocalAgentIfNeeded", uninstall_block)
        self.assertIn("RunUninstallHubScript", uninstall_block)
        self.assertIn("if CurUninstallStep = usPostUninstall then", uninstall_block)
        self.assertIn("RestoreSysvarLocalAgent", uninstall_block)

    def test_inno_setup_tem_limpeza_final_segura_do_app(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("[UninstallDelete]", iss)
        self.assertIn('Type: files; Name: "{app}\\is-*.tmp"', iss)
        self.assertIn('Type: dirifempty; Name: "{app}"', iss)
        self.assertNotIn("{commonappdata}\\SysvarHub", iss[iss.index("[UninstallDelete]"):iss.index("[Code]")])

    def test_inno_setup_mantem_install_hub_no_sspostinstall(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        post_install_pos = iss.index("if CurStep = ssPostInstall then")
        install_script_pos = iss.index("install-hub.ps1", post_install_pos)

        self.assertGreater(install_script_pos, post_install_pos)
        self.assertIn("Exec(PowerShell, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)", iss[post_install_pos:])

    def test_inno_setup_falha_do_install_hub_nao_mostra_pagina_final_de_sucesso(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")

        self.assertIn("InstallHubFailed: Boolean", iss)
        self.assertIn("procedure FailInstallHub(Message: String);", iss)
        self.assertIn("InstallHubFailed := True", iss)
        self.assertIn("Abort;", iss)
        self.assertIn("function ShouldSkipPage(PageID: Integer): Boolean;", iss)
        self.assertIn("Result := InstallHubFailed and (PageID = wpFinished);", iss)
        self.assertNotIn("Setup has finished installing Sysvar Hub", iss)

    def test_inno_setup_exit_code_zero_permite_finalizacao(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        post_install_block = iss[iss.index("procedure CurStepChanged"):iss.index("function ShouldSkipPage")]

        self.assertIn("if ResultCode <> 0 then", post_install_block)
        self.assertNotIn("InstallHubFailed := True;", post_install_block[:post_install_block.index("if ResultCode <> 0 then")])


class WindowsServiceLifecycleTests(SimpleTestCase):
    def test_runtime_armazena_servidor_controlavel(self):
        server = Mock()
        runtime = HubWaitressRuntime(application_factory=Mock(return_value=object()), server_factory=Mock(return_value=server))

        with patch("runtime.windows_service.bootstrap"), patch("django.setup"):
            runtime.run()

        server.run.assert_called_once()
        server.close.assert_called_once()
        self.assertIsNone(runtime.server)

    def test_stop_encerra_dispatcher_e_waitress(self):
        server = Mock()
        runtime = HubWaitressRuntime()
        runtime.server = server

        runtime.stop()

        server.task_dispatcher.shutdown.assert_called_once()
        server.close.assert_called_once()
        self.assertIsNone(runtime.server)

    def test_stop_runtime_ignora_runtime_ausente(self):
        stop_runtime(None)

    def test_main_manage_preserva_cli_django(self):
        with patch.object(windows_service.sys, "argv", ["SysvarHubService.exe", "manage", "check"]), \
                patch("runtime.windows_service.run_manage") as run_manage:
            windows_service.main()

        run_manage.assert_called_once_with(["check"])

    def test_main_console_preserva_cli_console(self):
        with patch.object(windows_service.sys, "argv", ["SysvarHubService.exe", "console"]), \
                patch("runtime.windows_service.run_console") as run_console:
            windows_service.main()

        run_console.assert_called_once_with()

    def test_main_comando_administrativo_usa_handle_command_line(self):
        command_line = Mock()
        service_class = object()

        with patch.object(windows_service.sys, "argv", ["SysvarHubService.exe", "install"]), \
                patch.object(windows_service, "win32serviceutil", Mock(HandleCommandLine=command_line)), \
                patch.object(windows_service, "SysvarHubService", service_class, create=True), \
                patch("runtime.windows_service.run_service_dispatcher") as dispatcher:
            windows_service.main()

        command_line.assert_called_once_with(service_class)
        dispatcher.assert_not_called()

    def test_main_sem_argumentos_entra_no_dispatcher_scm(self):
        with patch.object(windows_service.sys, "argv", ["SysvarHubService.exe"]), \
                patch("runtime.windows_service.run_service_dispatcher") as dispatcher, \
                patch.object(windows_service, "win32serviceutil", Mock()):
            windows_service.main()

        dispatcher.assert_called_once_with()

    def test_dispatcher_scm_hospeda_sysvarhubservice_sem_handle_command_line(self):
        service_manager = Mock()
        command_line = Mock()
        service_class = object()

        with patch.object(windows_service, "servicemanager", service_manager), \
                patch.object(windows_service, "win32serviceutil", Mock(HandleCommandLine=command_line)), \
                patch.object(windows_service, "SysvarHubService", service_class, create=True):
            windows_service.run_service_dispatcher()

        service_manager.Initialize.assert_called_once_with()
        service_manager.PrepareToHostSingle.assert_called_once_with(service_class)
        service_manager.StartServiceCtrlDispatcher.assert_called_once_with()
        command_line.assert_not_called()

    def test_check_hub_script_tem_retry_e_timeout(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "check-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("WaitSeconds", script)
        self.assertIn("IntervalSeconds", script)
        self.assertIn("Start-Sleep", script)
        self.assertIn("status -eq \"ok\"", script)
