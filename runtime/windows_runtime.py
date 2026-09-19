import os
import secrets
import socket
import string
from pathlib import Path


PROGRAM_FILES_ROOT = Path(os.environ.get("SYSVARHUB_INSTALL_ROOT", r"C:\Program Files\Sysvar Hub"))
PROGRAM_DATA_ROOT = Path(os.environ.get("SYSVARHUB_PROGRAMDATA", r"C:\ProgramData\SysvarHub"))
CONFIG_DIR = PROGRAM_DATA_ROOT / "config"
LOG_DIR = PROGRAM_DATA_ROOT / "logs"
DATA_DIR = PROGRAM_DATA_ROOT / "data"
BACKUP_DIR = PROGRAM_DATA_ROOT / "backup"
MYSQL_DATA_DIR = PROGRAM_DATA_ROOT / "mysql" / "data"
ENV_FILE = CONFIG_DIR / "sysvarhub.env"
MYSQL_ADMIN_FILE = CONFIG_DIR / "mysql-admin.cnf"
ACL_SYSTEM = "*S-1-5-18:F"
ACL_ADMINISTRATORS = "*S-1-5-32-544:F"


def load_env_file(path=ENV_FILE):
    path = Path(path)
    if not path.is_file():
        return {}

    loaded = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        os.environ.setdefault(name, value)
        loaded[name] = value
    return loaded


def local_hostnames_and_ips():
    values = {"localhost", "127.0.0.1"}
    hostname = socket.gethostname()
    if hostname:
        values.add(hostname)
    fqdn = socket.getfqdn()
    if fqdn:
        values.add(fqdn)

    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                values.add(ip)
    except socket.gaierror:
        pass

    return sorted(values)


def merge_csv(existing, additions):
    values = []
    seen = set()
    for value in [*(existing or []), *additions]:
        item = str(value).strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            values.append(item)
    return values


def generate_secret(length=64):
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_runtime_directories():
    for path in (CONFIG_DIR, LOG_DIR, DATA_DIR, BACKUP_DIR, MYSQL_DATA_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_locked_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if os.name == "nt":
        import subprocess

        subprocess.run(["icacls", str(path), "/inheritance:r"], check=False, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", ACL_SYSTEM, ACL_ADMINISTRATORS], check=False, capture_output=True)


def create_default_env(install_root=PROGRAM_FILES_ROOT, program_data=PROGRAM_DATA_ROOT):
    db_password = generate_secret(48)
    secret_key = generate_secret(64)
    content = "\n".join(
        [
            f"DJANGO_SECRET_KEY={secret_key}",
            "DJANGO_DEBUG=False",
            "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost",
            "DJANGO_CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000",
            "CORS_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000",
            "DB_NAME=sysvarhub_db",
            "DB_USER=sysvarhub",
            f"DB_PASSWORD={db_password}",
            "DB_HOST=127.0.0.1",
            "DB_PORT=3307",
            "HUB_BIND_HOST=0.0.0.0",
            "HUB_PORT=8000",
            f"SYSVARHUB_LOG_DIR={program_data / 'logs'}",
            f"SYSVARHUB_DATA_DIR={program_data / 'data'}",
            f"SYSVARHUB_FRONTEND_DIST_DIR={install_root / 'frontend'}",
            "",
        ]
    )
    write_locked_file(ENV_FILE, content)
    return {"db_password": db_password, "secret_key": secret_key}


def create_mysql_admin_file():
    admin_password = generate_secret(48)
    content = "\n".join(
        [
            "[client]",
            "user=root",
            f"password={admin_password}",
            "host=127.0.0.1",
            "port=3307",
            "",
        ]
    )
    write_locked_file(MYSQL_ADMIN_FILE, content)
    return admin_password
