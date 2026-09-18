from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, override_settings
from django.urls import resolve

from sysvarhub.urls import angular_spa
from runtime.windows_runtime import (
    ACL_ADMINISTRATORS,
    ACL_SYSTEM,
    MYSQL_ADMIN_FILE,
    create_default_env,
    create_mysql_admin_file,
    generate_secret,
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

    def test_pyinstaller_spec_resolve_backend_root_para_collect_e_pathex(self):
        spec = Path(settings.BASE_DIR, "runtime", "SysvarHubService.spec").read_text(encoding="utf-8")
        backend_pos = spec.index("backend_root = spec_root.parent")
        collect_pos = spec.index('collect_submodules("core"')

        self.assertLess(backend_pos, collect_pos)
        self.assertIn("sys.path.insert(0, candidate)", spec)
        self.assertIn("pathex=[str(spec_root), str(backend_root)]", spec)
        self.assertIn('collect_submodules("integracao"', spec)
        self.assertIn('collect_submodules("sysvarhub"', spec)


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

        self.assertIn('@("SysvarHub", "SysvarHubMySQL")', script)
        self.assertIn("Get-Service -Name $service -ErrorAction SilentlyContinue", script)
        self.assertIn("Stop-Service -Name $service -Force -ErrorAction SilentlyContinue", script)
        self.assertIn("sc.exe delete $service", script)
        self.assertIn('Get-NetFirewallRule -DisplayName "Sysvar Hub" -ErrorAction SilentlyContinue', script)
        self.assertIn("Remove-NetFirewallRule", script)

    def test_inno_setup_chama_uninstall_hub_no_uninstall_run(self):
        iss = Path(settings.BASE_DIR, "deploy", "windows", "installer", "SysvarHubSetup.iss").read_text(encoding="utf-8")
        uninstall_run_pos = iss.index("[UninstallRun]")
        uninstall_script_pos = iss.index("uninstall-hub.ps1", uninstall_run_pos)

        self.assertGreater(uninstall_script_pos, uninstall_run_pos)
        self.assertIn('-InstallRoot ""{app}""', iss[uninstall_run_pos:])


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
        self.assertIn("RaiseException('install-hub.ps1 retornou codigo de erro ' + IntToStr(ResultCode) + '.')", iss)
        self.assertNotIn('[Run]\nFilename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\\scripts\\install-hub.ps1""', iss)


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

    def test_check_hub_script_tem_retry_e_timeout(self):
        script = Path(settings.BASE_DIR, "deploy", "windows", "check-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("WaitSeconds", script)
        self.assertIn("IntervalSeconds", script)
        self.assertIn("Start-Sleep", script)
        self.assertIn("status -eq \"ok\"", script)
