"""Assisted offline recovery. No Django bootstrap, network download or automatic release."""
import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import UUID, uuid4

BLOCK = 'recovery-block.json'
CHUNK = 65536


class RecoveryError(Exception):
    pass


def local_path(value):
    path = Path(value)
    if not path.is_absolute() or str(path).startswith('\\\\'):
        raise RecoveryError('LOCAL_PATH_REQUIRED')
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.stat(), 'st_file_attributes', 0) & 1024):
            raise RecoveryError('LINK_FORBIDDEN')
    return path


def read_json(path):
    path = local_path(path)
    if not path.is_file() or path.stat().st_size > 1024**2:
        raise RecoveryError('INVALID_MANIFEST')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def guid(value):
    result = UUID(str(value))
    if result.int == 0:
        raise RecoveryError('INVALID_IDENTITY')
    return str(result)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(path):
    local_path(path)
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def coordinate(value):
    if not isinstance(value, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.[0-9]{6,}', str(value.get('file', ''))):
        raise RecoveryError('INVALID_COORDINATE')
    pos = value.get('position')
    if type(pos) is not int or not 4 <= pos <= 4294967295:
        raise RecoveryError('INVALID_COORDINATE')
    anchor = value.get('anchor', {})
    if (anchor.get('algorithm') != 'MYSQL8_CRC32_PREFIX_V1' or anchor.get('file') != value['file']
            or anchor.get('position') != pos or not re.fullmatch('[a-f0-9]{64}', str(anchor.get('sha256', '')))):
        raise RecoveryError('INVALID_ANCHOR')


def load_package(plan):
    root = local_path(plan['package_dir'])
    doc = read_json(root / 'pacote.json')
    if not re.fullmatch('[a-f0-9]{64}', plan['manifest_sha256']) or canonical_hash(doc) != plan['manifest_sha256']:
        raise RecoveryError('PACKAGE_HASH_MISMATCH')
    if (doc.get('versao') != 1 or doc.get('integridade') != 'VALIDO_REVALIDADO'
            or doc.get('restauracao_homologada') is not False or doc.get('empresa_id') != plan['empresa_id']
            or doc.get('loja_id') != plan['loja_id'] or type(doc.get('hub_id')) is not int or doc['hub_id'] < 1):
        raise RecoveryError('PACKAGE_SCOPE_INVALID')
    for field, expected in [('instalacao_uuid', 'installation_uuid'), ('servidor_mysql_uuid', 'source_server_uuid'), ('ponto_uuid', 'point_uuid')]:
        if guid(doc[field]) != guid(plan[expected]):
            raise RecoveryError('PACKAGE_IDENTITY_MISMATCH')
    guid(doc['pacote_uuid'])
    files = doc.get('arquivos')
    if not isinstance(files, list) or not 1 <= len(files) <= 10000:
        raise RecoveryError('PACKAGE_INCOMPLETE')
    previous, seen, paths, sizes = None, set(), [], []
    for order, item in enumerate(files):
        backup_id = guid(item['backup_uuid'])
        kind = 'FULL' if order == 0 else 'INCREMENTAL'
        expected_name = f'{order:06d}-{kind.lower()}-{UUID(backup_id).hex}.sql.gz'
        expected_format = 'MYSQLDUMP_SQL_V1' if order == 0 else 'MYSQLBINLOG_SQL_BOUNDED_V1'
        if (item.get('ordem') != order or item.get('sequencia') != order or item.get('tipo') != kind
                or item.get('nome') != expected_name or item.get('compressao') != 'gzip'
                or item.get('formato') != expected_format or backup_id in seen
                or not re.fullmatch('[a-f0-9]{64}', str(item.get('sha256', '')))
                or not re.fullmatch('[a-f0-9]{64}', str(item.get('manifesto_origem_sha256', '')))
                or type(item.get('tamanho')) is not int or item['tamanho'] < 1):
            raise RecoveryError('PACKAGE_ORDER_OR_FORMAT_INVALID')
        coordinate(item['inicio'])
        if previous is None:
            if backup_id != guid(doc['full_uuid']) or item.get('predecessor_uuid') is not None or item.get('fim') is not None:
                raise RecoveryError('FULL_REFERENCE_INVALID')
        else:
            coordinate(item['fim'])
            prior_point = previous['inicio'] if previous['tipo'] == 'FULL' else previous['fim']
            if item.get('predecessor_uuid') != previous['backup_uuid'] or item['inicio'] != prior_point or item['fim'] == item['inicio']:
                raise RecoveryError('CHAIN_GAP_OR_OVERLAP')
        path = local_path(root / expected_name)
        before = fingerprint(path)
        if path.stat().st_size != item['tamanho'] or file_hash(path) != item['sha256']:
            raise RecoveryError('ARCHIVE_HASH_MISMATCH')
        size, header = 0, bytearray()
        with gzip.open(path, 'rb') as source:
            while data := source.read(CHUNK):
                size += len(data)
                if order == 0 and len(header) < 1024**2:
                    header.extend(data[:1024**2-len(header)])
        if size < 1 or before != fingerprint(path):
            raise RecoveryError('ARCHIVE_CHANGED_OR_EMPTY')
        if order == 0:
            text = bytes(header)
            version = re.search(rb'^-- Server version\s+(8\.0\.[0-9]+)(?:[^0-9]|$)', text, re.M)
            coords = re.search(rb"^-- CHANGE (?:REPLICATION SOURCE TO SOURCE|MASTER TO MASTER)_LOG_FILE='([A-Za-z0-9_.-]+\.[0-9]+)', (?:SOURCE|MASTER)_LOG_POS=([0-9]+);", text, re.M)
            if (not version or version[1].decode() != plan['mysql_version'] or not coords
                    or coords[1].decode() != item['inicio']['file'] or int(coords[2]) != item['inicio']['position']):
                raise RecoveryError('DUMP_VERSION_OR_COORDINATE_INVALID')
        seen.add(backup_id); previous = item
        paths.append((path, before)); sizes.append(size)
    if guid(files[-1]['backup_uuid']) != guid(doc['ponto_uuid']):
        raise RecoveryError('TARGET_POINT_MISSING')
    return doc, paths, sizes


def mysql_args(plan):
    root = local_path(plan['install_root'])
    credential = local_path(Path(plan['program_data']) / 'config' / 'mysql-restore.cnf')
    return [str(root / 'mysql' / 'bin' / 'mysql.exe'), f'--defaults-file={credential}',
            '--host=127.0.0.1', '--port=3307', '--protocol=TCP', '--user=sysvarhub_restore',
            '--connect-timeout=5', '--default-character-set=utf8mb4', '--binary-mode', '--batch',
            '--raw', '--unbuffered', '--skip-column-names', '--local-infile=0', '--skip-reconnect', '--skip-line-numbers']


def mysql_env():
    env = dict(os.environ)
    env.pop('MYSQL_PWD', None)
    env['MYSQL_TEST_LOGIN_FILE'] = str(Path(os.environ.get('TEMP', '.')) / ('sysvarhub-no-login-' + uuid4().hex))
    return env


def start_mysql(plan):
    return subprocess.Popen(mysql_args(plan), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=mysql_env(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def discard(stream):
    while stream.read(CHUNK):
        pass


def query(plan, sql):
    process = start_mysql(plan)
    output, overflow = bytearray(), threading.Event()
    def collect():
        while data := process.stdout.read(CHUNK):
            if len(output) + len(data) > 1024**2:
                overflow.set(); process.kill(); return
            output.extend(data)
    threads = [threading.Thread(target=collect, daemon=True), threading.Thread(target=discard, args=(process.stderr,), daemon=True)]
    for thread in threads:
        thread.start()
    timer = threading.Timer(30, process.kill); timer.start()
    try:
        process.stdin.write(sql.encode('utf-8')); process.stdin.close()
        code = process.wait(timeout=35)
        for thread in threads:
            thread.join(timeout=5)
        if code != 0 or overflow.is_set():
            raise RecoveryError('MYSQL_QUERY_FAILED')
        return output.decode('utf-8').strip()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill(); process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


TARGET_SQL = """SELECT JSON_OBJECT('version',VERSION(),'port',@@port,'basedir',@@basedir,'datadir',@@datadir,'account',CURRENT_USER(),'server_uuid',@@server_uuid,'log_bin',@@log_bin,'schemas',(SELECT JSON_ARRAYAGG(SCHEMA_NAME) FROM information_schema.SCHEMATA),'objects',(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db')+(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='sysvarhub_db')+(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='sysvarhub_db'),'other_sessions',(SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID<>CONNECTION_ID() AND USER NOT IN ('event_scheduler','system user')));"""


def verify_target(plan, *, empty=True):
    state = json.loads(query(plan, TARGET_SQL))
    if (state['port'] != 3307 or state['account'] != 'sysvarhub_restore@127.0.0.1'
            or re.match(r'^8\.0\.[0-9]+', state['version'])[0] != plan['mysql_version']
            or state['log_bin'] != 1 or state['other_sessions'] != 0
            or set(state['schemas']) != {'mysql','information_schema','performance_schema','sys','sysvarhub_db'}
            or os.path.normcase(str(local_path(state['basedir']))) != os.path.normcase(str(local_path(Path(plan['install_root']) / 'mysql')))
            or os.path.normcase(str(local_path(state['datadir']))) != os.path.normcase(str(local_path(Path(plan['program_data']) / 'mysql' / 'data')))
            or guid(state['server_uuid']) == guid(plan['source_server_uuid'])):
        raise RecoveryError('TARGET_INSTANCE_INVALID_OR_BUSY')
    if empty and state['objects'] != 0:
        raise RecoveryError('TARGET_NOT_EMPTY_SUPPORT_REQUIRED')
    return state


def write_state(config, value):
    temp = config / ('.recovery-state-' + uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as target:
            json.dump(value, target, sort_keys=True); target.flush(); os.fsync(target.fileno())
        os.replace(temp, config / 'recovery-state.json')
    finally:
        if temp.exists():
            temp.unlink()  # Only our metadata temp, never a backup or SQL file.
    with (config / 'recovery-history.jsonl').open('a', encoding='utf-8') as history:
        history.write(json.dumps(value, sort_keys=True)+'\n'); history.flush(); os.fsync(history.fileno())


@contextlib.contextmanager
def recovery_lock(config):
    import msvcrt
    with (config / '.recovery.lock').open('a+b') as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RecoveryError('RECOVERY_ALREADY_RUNNING') from None
        try:
            yield
        finally:
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def import_chain(plan, doc, paths, step):
    """One binary mysql stdin/session for FULL and every ordered SQL segment."""
    process = start_mysql(plan)
    nonce, acknowledgements = uuid4().hex, {}
    condition = threading.Condition()
    prefix = f'__SYSVAR_RECOVERY_{nonce}_'.encode()
    def receive():
        line = bytearray()
        while data := process.stdout.read(1):
            if data == b'\n':
                if line.startswith(prefix) and re.fullmatch(rb'[0-9]+:[01]', bytes(line[len(prefix):]).strip()):
                    with condition:
                        index, status = bytes(line[len(prefix):]).strip().split(b':')
                        acknowledgements[int(index)] = int(status); condition.notify_all()
                line.clear()
            elif len(line) < 1024:
                line.extend(data)
    threads = [threading.Thread(target=receive, daemon=True), threading.Thread(target=discard, args=(process.stderr,), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + plan['timeout_seconds']
    timer = threading.Timer(plan['timeout_seconds'], process.kill); timer.start()
    def await_ack(index):
        with condition:
            while index not in acknowledgements:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RecoveryError('MYSQL_IMPORT_FAILED')
                condition.wait(timeout=0.1)
            if acknowledgements[index] != 0:
                raise RecoveryError('IMPORT_SESSION_TARGET_INVALID')
    try:
        target = guid(plan['target_server_uuid'])
        # Check the destination within the very connection that will import it.
        objects = "(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db')+(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='sysvarhub_db')+(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='sysvarhub_db')"
        condition_sql = f"@@server_uuid='{target}' AND @@port=3307 AND CURRENT_USER()='sysvarhub_restore@127.0.0.1' AND @@SESSION.sql_log_bin=0 AND ({objects})=0"
        process.stdin.write(f"SET @@SESSION.sql_log_bin=0;\nSELECT CONCAT('__SYSVAR_RECOVERY_{nonce}_10001:',IF({condition_sql},0,1));\n".encode())
        process.stdin.flush(); await_ack(10001)
        for index, ((path, identity), item) in enumerate(zip(paths, doc['arquivos'])):
            if fingerprint(path) != identity or file_hash(path) != item['sha256']:
                raise RecoveryError('ARCHIVE_CHANGED')
            if process.poll() is not None:
                raise RecoveryError('MYSQL_IMPORT_FAILED')
            step('IMPORTANDO', index)
            process.stdin.write(b'\nSET @@SESSION.sql_log_bin=0;\nUSE `sysvarhub_db`;\n')
            with gzip.open(path, 'rb') as source:
                while data := source.read(CHUNK):
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RecoveryError('MYSQL_IMPORT_FAILED')
                    process.stdin.write(data)
            if fingerprint(path) != identity:
                raise RecoveryError('ARCHIVE_CHANGED')
            # No COMMIT/reconnect/context reset between segments; batch aborts at first SQL error.
            process.stdin.write(f"\nSELECT CONCAT('__SYSVAR_RECOVERY_{nonce}_{index}:',@@SESSION.sql_log_bin);\n".encode())
            process.stdin.flush()
            await_ack(index)
            step('SEGMENTO_CONFIRMADO', index)
        process.stdin.close()
        if process.wait(timeout=max(1, deadline-time.monotonic())) != 0:
            raise RecoveryError('MYSQL_IMPORT_FAILED')
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill(); process.wait()
        for thread in threads:
            thread.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def logical_check(plan, doc):
    identity = json.loads(query(plan, "SELECT JSON_OBJECT('count',(SELECT COUNT(*) FROM core_hubconfig),'hub_uuid',hub_uuid,'empresa_id',empresa_id,'loja_id',loja_id,'hub_id',retaguarda_hub_id) FROM core_hubconfig;"))
    if (identity['count'] != 1 or guid(identity['hub_uuid']) != guid(doc['instalacao_uuid'])
            or identity['empresa_id'] != doc['empresa_id'] or identity['loja_id'] != doc['loja_id'] or identity['hub_id'] != doc['hub_id']):
        raise RecoveryError('RESTORED_IDENTITY_MISMATCH')
    tables = query(plan, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db' AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME;").splitlines()
    if not {'core_hubconfig','core_vendahub','core_eventosynchub'} <= set(tables):
        raise RecoveryError('RESTORED_SCHEMA_INCOMPLETE')
    for table in tables:
        if not re.fullmatch('[A-Za-z0-9_]+', table):
            raise RecoveryError('RESTORED_SCHEMA_INVALID')
        result = query(plan, f'CHECK TABLE `sysvarhub_db`.`{table}` QUICK;').splitlines()
        if not result or result[-1].split('\t')[-2:] != ['status','OK']:
            raise RecoveryError('RESTORED_TABLE_CHECK_FAILED')
    # Find every orphaned foreign-key tuple, including composite and nullable keys.
    fks = query(plan, "SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME,ORDINAL_POSITION FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA='sysvarhub_db' AND REFERENCED_TABLE_NAME IS NOT NULL AND REFERENCED_TABLE_SCHEMA='sysvarhub_db' ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION;")
    groups = {}
    for line in fks.splitlines():
        table, name, column, parent, reference, _ = line.split('\t')
        if any(not re.fullmatch('[A-Za-z0-9_]+', x) for x in (table,name,column,parent,reference)):
            raise RecoveryError('RESTORED_FK_INVALID')
        groups.setdefault((table,name,parent), []).append((column,reference))
    for (table, _, parent), columns in groups.items():
        present = ' AND '.join(f'c.`{c}` IS NOT NULL' for c, _ in columns)
        matches = ' AND '.join(f'p.`{r}`=c.`{c}`' for c, r in columns)
        sql = f'SELECT EXISTS(SELECT 1 FROM `sysvarhub_db`.`{table}` c WHERE {present} AND NOT EXISTS(SELECT 1 FROM `sysvarhub_db`.`{parent}` p WHERE {matches}) LIMIT 1);'
        if query(plan, sql) != '0':
            raise RecoveryError('RESTORED_FK_ORPHAN')
    return {'tables_checked':len(tables),'foreign_keys_checked':len(groups)}


def recover(plan, *, execute=False, confirmation=''):
    config = local_path(Path(plan['program_data']) / 'config')
    if plan.get('version') != 1 or not (config / BLOCK).is_file() or not 60 <= plan['timeout_seconds'] <= 86400:
        raise RecoveryError('RECOVERY_MODE_REQUIRED')
    expected = 'RESTAURAR:' + guid(plan['point_uuid']) + ':' + plan['manifest_sha256']
    if execute and confirmation != expected:
        raise RecoveryError('EXPLICIT_CONFIRMATION_REQUIRED')
    with recovery_lock(config):
        doc, paths, sizes = load_package(plan)
        state = verify_target(plan)
        if shutil.disk_usage(local_path(state['datadir'])).free < sum(sizes)*2 + 256*1024**2:
            raise RecoveryError('TARGET_DISK_SPACE_INSUFFICIENT')
        # DRY-RUN is read-only except the ephemeral mutual-exclusion handle.
        result = dict(status='DRY_RUN_APROVADO',ponto_uuid=doc['ponto_uuid'],pacote_uuid=doc['pacote_uuid'],
                      coordenada=doc['arquivos'][-1]['inicio'] if len(paths)==1 else doc['arquivos'][-1]['fim'],
                      arquivos=len(paths),sql_bytes=sum(sizes),destino_mysql_uuid=state['server_uuid'],
                      pdv_liberado=False,restauracao_homologada=False)
        if not execute:
            return result
        previous = config / 'recovery-state.json'
        if previous.exists():
            old = read_json(previous)
            if old['status'] == 'RESTAURADO_BLOQUEADO' or not plan.get('resume_after_support'):
                raise RecoveryError('PREVIOUS_ATTEMPT_REQUIRES_SUPPORT')
        def step(status, index=-1):
            write_state(config, dict(result,status=status,segmento=index,utc=time.time(),manifesto_sha256=plan['manifest_sha256']))
        try:
            step('PRE_VERIFICADO')
            current = verify_target(plan)
            if current['server_uuid'] != state['server_uuid']:
                raise RecoveryError('TARGET_IDENTITY_CHANGED')
            import_chain(dict(plan,target_server_uuid=state['server_uuid']), doc, paths, step)
            step('CONFERENCIA_LOGICA')
            restored = verify_target(plan, empty=False)
            if restored['server_uuid'] != state['server_uuid']:
                raise RecoveryError('TARGET_IDENTITY_CHANGED')
            result.update(logical_check(plan, doc))
            step('RESTAURADO_BLOQUEADO')
            return dict(result,status='RESTAURADO_BLOQUEADO')
        except Exception as exc:
            code = str(exc) if isinstance(exc, RecoveryError) else 'RECOVERY_EXECUTION_FAILED'
            write_state(config, dict(result,status='FALHA_SUPORTE',codigo=code,utc=time.time(),manifesto_sha256=plan['manifest_sha256']))
            raise RecoveryError('RECOVERY_FAILED_DESTINATION_BLOCKED') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Recuperacao assistida offline, desativada por padrao.')
    parser.add_argument('--plan', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirmation', default='')
    args = parser.parse_args(argv)
    try:
        result = recover(read_json(args.plan), execute=args.execute, confirmation=args.confirmation)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, RecoveryError) else 'RECOVERY_VALIDATION_FAILED'
        print(json.dumps({'status':'BLOQUEADO_SUPORTE','codigo':code,'pdv_liberado':False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
