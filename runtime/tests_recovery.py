import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from runtime import recovery as r
from runtime.recovery_guard import assert_operation_allowed


class FakeOutput:
    def __init__(self):
        self.queue = queue.Queue(); self.buffer = bytearray(); self.ended = False
    def emit(self, line):
        self.queue.put(line)
    def read(self, size):
        if not self.buffer and not self.ended:
            value = self.queue.get(timeout=5)
            if value is None:
                self.ended = True
            else:
                self.buffer.extend(value)
        result = bytes(self.buffer[:size]); del self.buffer[:size]
        return result
    def close(self):
        if not self.ended:
            self.queue.put(None)


class FakeMysql:
    def __init__(self, fail=False, wrong_target=False):
        self.wrong_target=wrong_target
        self.code = None; self.stdout = FakeOutput(); self.stderr = io.BytesIO(b'')
        self.captured = bytearray(); self.max_write = 0; self.fail = fail
        owner = self
        class Input:
            def write(self, data):
                if owner.code is not None:
                    raise BrokenPipeError('fictitious SQL must not leak')
                owner.captured.extend(data); owner.max_write=max(owner.max_write,len(data))
                if owner.fail and b'FULL_FICTITIOUS' in data:
                    owner.code=1; owner.stdout.emit(None)
                import re
                match=re.search(rb"SELECT CONCAT\('(__SYSVAR_RECOVERY_[a-f0-9]+_[0-9]+:)'.*",data)
                if match and owner.code is None:
                    owner.stdout.emit(match[1]+(b'1\n' if owner.wrong_target else b'0\n'))
            def flush(self):
                pass
            def close(self):
                if owner.code is None:
                    owner.code=0; owner.stdout.emit(None)
        self.stdin=Input()
    def poll(self):
        return self.code
    def kill(self):
        self.code=-9; self.stdout.emit(None)
    def wait(self, timeout=None):
        return self.code or 0


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.config=self.root/'data'/'config'; self.config.mkdir(parents=True)
        self.package=self.root/'package'; self.package.mkdir()
        self.install=self.root/'install'; (self.install/'mysql').mkdir(parents=True)
        self.datadir=self.root/'data'/'mysql'/'data'; self.datadir.mkdir(parents=True)
        (self.config/r.BLOCK).write_text('{}')
        self.plan=dict(version=1,install_root=str(self.install),program_data=str(self.root/'data'),
            package_dir=str(self.package),point_uuid=str(uuid4()),empresa_id=1,loja_id=20,
            installation_uuid=str(uuid4()),source_server_uuid=str(uuid4()),mysql_version='8.0.46',timeout_seconds=60,resume_after_support=False)
        self.doc=dict(versao=1,integridade='VALIDO_REVALIDADO',restauracao_homologada=False,empresa_id=1,loja_id=20,hub_id=7,
            instalacao_uuid=self.plan['installation_uuid'],servidor_mysql_uuid=self.plan['source_server_uuid'],
            pacote_uuid=str(uuid4()),ponto_uuid=self.plan['point_uuid'],arquivos=[])
        self.payloads=[]
        self.build_chain()
        self.server=dict(version='8.0.46',port=3307,basedir=str(self.install/'mysql'),datadir=str(self.datadir),account='sysvarhub_restore@127.0.0.1',
            server_uuid=str(uuid4()),log_bin=1,schemas=['mysql','information_schema','performance_schema','sys','sysvarhub_db'],objects=0,other_sessions=0)
        self.plan['target_server_uuid']=self.server['server_uuid']

    def coord(self, pos):
        return dict(file='sysvarhub-bin.000001',position=pos,anchor=dict(algorithm='MYSQL8_CRC32_PREFIX_V1',file='sysvarhub-bin.000001',position=pos,sha256=hashlib.sha256(str(pos).encode()).hexdigest()))

    def build_chain(self):
        self.doc['arquivos']=[]; self.payloads=[]
        ids=[str(uuid4()),str(uuid4()),self.plan['point_uuid']]
        self.doc['full_uuid']=ids[0]
        for index, uid in enumerate(ids):
            kind='FULL' if index==0 else 'INCREMENTAL'
            data=(b"-- Server version\t8.0.46\n-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='sysvarhub-bin.000001', SOURCE_LOG_POS=157;\n-- FULL_FICTITIOUS\n"
                  if index==0 else f'-- INCREMENTAL_FICTITIOUS_{index}\nSET @temporary_context=@temporary_context+1;\n'.encode())
            self.payloads.append(data)
            name=f'{index:06d}-{kind.lower()}-{r.UUID(uid).hex}.sql.gz'
            archive=gzip.compress(data); (self.package/name).write_bytes(archive)
            self.doc['arquivos'].append(dict(ordem=index,sequencia=index,tipo=kind,backup_uuid=uid,nome=name,compressao='gzip',
                formato='MYSQLDUMP_SQL_V1' if index==0 else 'MYSQLBINLOG_SQL_BOUNDED_V1',
                tamanho=len(archive),sha256=hashlib.sha256(archive).hexdigest(),manifesto_origem_sha256='a'*64,
                inicio=self.coord(157 if index<2 else 250),fim=None if index==0 else self.coord(250 if index==1 else 400),
                predecessor_uuid=None if index==0 else ids[index-1]))
        self.save_manifest()

    def save_manifest(self):
        self.plan['manifest_sha256']=r.canonical_hash(self.doc)
        (self.package/'pacote.json').write_text(json.dumps(self.doc),encoding='utf-8')

    def confirmation(self):
        return 'RESTAURAR:'+self.plan['point_uuid']+':'+self.plan['manifest_sha256']

    def test_valid_package_exact_order_and_gzip_crc(self):
        doc,paths,sizes=r.load_package(self.plan)
        self.assertEqual([f['tipo'] for f in doc['arquivos']],['FULL','INCREMENTAL','INCREMENTAL'])
        self.assertEqual(sizes,list(map(len,self.payloads)))
        self.assertEqual(len(paths),3)

    def test_central_full_reference_is_metadata_and_anchor_still_enforced(self):
        self.doc['arquivos'][0]['inicio']['reference']='SNAPSHOT_START'
        self.save_manifest()
        self.assertEqual(len(r.load_package(self.plan)[1]),3)
        self.doc['arquivos'][1]['inicio']['anchor']['sha256']='b'*64
        self.save_manifest()
        with self.assertRaisesRegex(r.RecoveryError,'CHAIN_GAP_OR_OVERLAP'): r.load_package(self.plan)

    def test_incomplete_or_missing_selected_point(self):
        self.doc['arquivos'].pop(); self.save_manifest()
        with self.assertRaisesRegex(r.RecoveryError,'TARGET_POINT_MISSING'):
            r.load_package(self.plan)

    def test_missing_archive(self):
        (self.package/self.doc['arquivos'][1]['nome']).unlink()
        with self.assertRaises(FileNotFoundError): r.load_package(self.plan)

    def test_tampered_manifest_does_not_match_separately_trusted_hash(self):
        self.doc['loja_id']=999
        (self.package/'pacote.json').write_text(json.dumps(self.doc))
        with self.assertRaisesRegex(r.RecoveryError,'PACKAGE_HASH_MISMATCH'): r.load_package(self.plan)

    def test_wrong_store_installation_or_mysql_source(self):
        for key,value in [('loja_id',900),('instalacao_uuid',str(uuid4())),('servidor_mysql_uuid',str(uuid4()))]:
            original=self.doc[key]; self.doc[key]=value; self.save_manifest()
            with self.assertRaises(r.RecoveryError): r.load_package(self.plan)
            self.doc[key]=original
        self.save_manifest()

    def test_gap_overlap_duplicate_or_reordered_segments(self):
        for field,value in [('sequencia',4),('predecessor_uuid',str(uuid4())),('inicio',self.coord(300)),('backup_uuid',self.doc['full_uuid'])]:
            original=self.doc['arquivos'][2][field]; self.doc['arquivos'][2][field]=value; self.save_manifest()
            with self.assertRaises(r.RecoveryError): r.load_package(self.plan)
            self.doc['arquivos'][2][field]=original
        self.save_manifest()

    def test_archive_hash_mismatch(self):
        with (self.package/self.doc['arquivos'][0]['nome']).open('ab') as f: f.write(b'corrupt')
        with self.assertRaisesRegex(r.RecoveryError,'ARCHIVE_HASH_MISMATCH'): r.load_package(self.plan)

    def test_corrupt_gzip_rejected_even_if_transport_hash_updated(self):
        item=self.doc['arquivos'][1]; path=self.package/item['nome']
        data=bytearray(path.read_bytes()); data[-5]^=1; path.write_bytes(data)
        item['sha256']=r.file_hash(path); self.save_manifest()
        with self.assertRaises((gzip.BadGzipFile,EOFError)): r.load_package(self.plan)

    def test_path_traversal_and_nonvalidated_package_blocked(self):
        self.doc['arquivos'][0]['nome']='../forbidden.sql.gz'; self.save_manifest()
        with self.assertRaises(r.RecoveryError): r.load_package(self.plan)
        self.build_chain(); self.doc['integridade']='RECEBIDO'; self.save_manifest()
        with self.assertRaises(r.RecoveryError): r.load_package(self.plan)

    def test_full_version_and_snapshot_coordinates_checked(self):
        self.plan['mysql_version']='8.0.45'
        with self.assertRaisesRegex(r.RecoveryError,'DUMP_VERSION_OR_COORDINATE_INVALID'): r.load_package(self.plan)
        self.plan['mysql_version']='8.0.46'; self.doc['arquivos'][0]['inicio']=self.coord(158); self.save_manifest()
        with self.assertRaisesRegex(r.RecoveryError,'DUMP_VERSION_OR_COORDINATE_INVALID'): r.load_package(self.plan)

    def test_target_empty_official_dedicated_and_new_server_uuid(self):
        with patch.object(r,'query',return_value=json.dumps(self.server)): self.assertEqual(r.verify_target(self.plan),self.server)
        for key,value in [('port',3306),('account','root@localhost'),('schemas',self.server['schemas']+['other_company']),('objects',2),
                          ('other_sessions',1),('basedir',str(self.root)),('datadir',str(self.root)),('server_uuid',self.plan['source_server_uuid'])]:
            state=dict(self.server,**{key:value})
            with patch.object(r,'query',return_value=json.dumps(state)),self.assertRaises(r.RecoveryError): r.verify_target(self.plan)

    def test_dry_run_does_not_start_import_or_write_diagnostic(self):
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain') as importer:
            result=r.recover(self.plan)
        self.assertEqual(result['status'],'DRY_RUN_APROVADO'); importer.assert_not_called()
        self.assertFalse((self.config/'recovery-state.json').exists())
        self.assertTrue((self.config/r.BLOCK).exists())

    def test_no_execution_without_confirmation_and_persistent_block(self):
        with patch.object(r,'start_mysql') as process:
            with self.assertRaisesRegex(r.RecoveryError,'EXPLICIT_CONFIRMATION_REQUIRED'): r.recover(self.plan,execute=True)
        process.assert_not_called()
        (self.config/r.BLOCK).unlink()
        with self.assertRaisesRegex(r.RecoveryError,'RECOVERY_MODE_REQUIRED'): r.recover(self.plan)

    def test_stream_single_session_original_order_context_and_bounded_writes(self):
        doc,paths,_=r.load_package(self.plan); fake=FakeMysql(); steps=Mock()
        with patch.object(r,'start_mysql',return_value=fake) as start:
            r.import_chain(self.plan,doc,paths,steps)
        start.assert_called_once()
        captured=bytes(fake.captured)
        self.assertLess(captured.index(self.payloads[0]),captured.index(self.payloads[1]))
        self.assertLess(captured.index(self.payloads[1]),captured.index(self.payloads[2]))
        self.assertEqual(captured.count(b'SET @@SESSION.sql_log_bin=0;'),4)
        self.assertNotIn(b'SET @@GLOBAL',captured); self.assertNotIn(b'COMMIT;',captured)
        self.assertLessEqual(fake.max_write,r.CHUNK)
        self.assertEqual([c.args for c in steps.call_args_list if c.args[0]=='SEGMENTO_CONFIRMADO'],[('SEGMENTO_CONFIRMADO',i) for i in range(3)])

    def test_large_segment_is_streamed_in_small_writes(self):
        self.payloads[1] += b'-- fictitious padding, no real SQL\n' * 10000
        item=self.doc['arquivos'][1]; archive=gzip.compress(self.payloads[1])
        (self.package/item['nome']).write_bytes(archive)
        item['sha256']=hashlib.sha256(archive).hexdigest(); item['tamanho']=len(archive); self.save_manifest()
        doc,paths,_=r.load_package(self.plan); fake=FakeMysql()
        with patch.object(r,'start_mysql',return_value=fake): r.import_chain(self.plan,doc,paths,Mock())
        self.assertEqual(fake.max_write,r.CHUNK)
        self.assertIn(self.payloads[1],fake.captured)

    def test_worker_does_not_read_credentials_or_send_events_when_recovery_is_blocked(self):
        os.environ.setdefault('DJANGO_SETTINGS_MODULE','sysvarhub.settings')
        import django
        django.setup()
        from runtime.sync_worker import SyncWorker
        with patch.dict(os.environ,{'SYSVARHUB_PROGRAMDATA':str(self.root/'data')}), patch('runtime.sync_worker.HubConfig') as hub, patch('runtime.sync_worker.RetaguardaClient') as client:
            with self.assertRaisesRegex(RuntimeError,'bloqueado'): SyncWorker().executar_ciclo()
        hub.objects.first.assert_not_called(); client.assert_not_called()

    def test_cli_failure_does_not_leak_sql_or_credentials(self):
        self.plan['manifest_sha256']='a'*64
        plan_path=self.config/'fictitious-plan.json'; plan_path.write_text(json.dumps(self.plan))
        with patch.object(r,'recover',side_effect=RuntimeError('fictitious sensitive SQL')), patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(r.main(['--plan',str(plan_path)]),1)
        self.assertNotIn('sensitive',output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'],'BLOQUEADO_SUPORTE')

    def test_same_import_session_refuses_changed_or_nonempty_target_before_full(self):
        doc,paths,_=r.load_package(self.plan); fake=FakeMysql(wrong_target=True)
        with patch.object(r,'start_mysql',return_value=fake),self.assertRaisesRegex(r.RecoveryError,'IMPORT_SESSION_TARGET_INVALID'):
            r.import_chain(self.plan,doc,paths,Mock())
        self.assertNotIn(self.payloads[0],fake.captured)
        self.assertIn(b'information_schema.TABLES',fake.captured)
        self.assertEqual(fake.code,-9)

    def test_import_failure_stops_without_feeding_next_segment(self):
        doc,paths,_=r.load_package(self.plan); fake=FakeMysql(fail=True)
        with patch.object(r,'start_mysql',return_value=fake),self.assertRaises((r.RecoveryError,BrokenPipeError)):
            r.import_chain(self.plan,doc,paths,Mock())
        self.assertNotIn(self.payloads[1],fake.captured)

    def test_failures_keep_marker_and_require_controlled_retry_on_empty_destination(self):
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain',side_effect=RuntimeError('fictitious secret')):
            with self.assertRaisesRegex(r.RecoveryError,'DESTINATION_BLOCKED'): r.recover(self.plan,execute=True,confirmation=self.confirmation())
        self.assertEqual(r.read_json(self.config/'recovery-state.json')['status'],'FALHA_SUPORTE')
        self.assertNotIn('fictitious secret',(self.config/'recovery-history.jsonl').read_text())
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain') as importer:
            with self.assertRaisesRegex(r.RecoveryError,'PREVIOUS_ATTEMPT'): r.recover(self.plan,execute=True,confirmation=self.confirmation())
            importer.assert_not_called()
        self.plan['resume_after_support']=True
        with patch.object(r,'verify_target',side_effect=r.RecoveryError('TARGET_NOT_EMPTY')):
            with self.assertRaisesRegex(r.RecoveryError,'TARGET_NOT_EMPTY'): r.recover(self.plan,execute=True,confirmation=self.confirmation())
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain'),patch.object(r,'logical_check',return_value={'tables_checked':3}):
            result=r.recover(self.plan,execute=True,confirmation=self.confirmation())
        self.assertEqual(result['status'],'RESTAURADO_BLOQUEADO'); self.assertFalse(result['pdv_liberado'])
        self.assertTrue((self.config/r.BLOCK).exists())

    def test_logical_check_identity_tables_and_orphaned_fk(self):
        identity=dict(count=1,hub_uuid=self.plan['installation_uuid'],empresa_id=1,loja_id=20,hub_id=7)
        tables='core_hubconfig\ncore_vendahub\ncore_eventosynchub'
        replies=[json.dumps(identity),tables,*['table\tcheck\tstatus\tOK']*3,'core_vendahub\tfk_name\thub_id\tcore_hubconfig\tid\t1','0']
        with patch.object(r,'query',side_effect=replies): self.assertEqual(r.logical_check(self.plan,self.doc)['foreign_keys_checked'],1)
        replies[-1]='1'
        with patch.object(r,'query',side_effect=replies),self.assertRaisesRegex(r.RecoveryError,'FK_ORPHAN'): r.logical_check(self.plan,self.doc)

    def test_logical_failure_never_declares_partial_success(self):
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain'),patch.object(r,'logical_check',side_effect=r.RecoveryError('TABLE_CHECK')):
            with self.assertRaises(r.RecoveryError): r.recover(self.plan,execute=True,confirmation=self.confirmation())
        self.assertEqual(r.read_json(self.config/'recovery-state.json')['status'],'FALHA_SUPORTE')
        self.assertTrue((self.config/r.BLOCK).exists())

    def test_low_space_and_simultaneous_recovery_blocked(self):
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r.shutil,'disk_usage',return_value=Mock(free=0)),self.assertRaisesRegex(r.RecoveryError,'DISK_SPACE'): r.recover(self.plan)
        with r.recovery_lock(self.config):
            with self.assertRaisesRegex(r.RecoveryError,'ALREADY_RUNNING'): r.recover(self.plan)

    def test_runtime_guard_prevents_django_bootstrap_and_keeps_old_credentials_inactive(self):
        from runtime import windows_service
        with patch.dict(os.environ,{'SYSVARHUB_PROGRAMDATA':str(self.root/'data')}):
            with self.assertRaisesRegex(RuntimeError,'bloqueado'): assert_operation_allowed()
            with patch.object(windows_service,'load_env_file') as load,self.assertRaises(RuntimeError): windows_service.bootstrap()
            load.assert_not_called()
            (self.config/r.BLOCK).unlink()
            assert_operation_allowed()

    def test_mysql_arguments_no_password_no_dev_no_reconnect_and_login_env_removed(self):
        args=r.mysql_args(self.plan)
        self.assertIn('--port=3307',args); self.assertIn('--binary-mode',args); self.assertIn('--skip-reconnect',args)
        self.assertIn('--local-infile=0',args); self.assertIn('--unbuffered',args)
        self.assertFalse(any('password=' in x or '3306' in x or 'sysvarhub_backup' in x or x=='--force' for x in args))
        with patch.dict(os.environ,{'MYSQL_PWD':'fictitious-only'}): self.assertNotIn('MYSQL_PWD',r.mysql_env())

    def test_no_download_or_original_archive_changes(self):
        hashes={p.name:r.file_hash(p) for p in self.package.iterdir()}
        with patch.object(r,'verify_target',return_value=self.server),patch.object(r,'import_chain'),patch.object(r,'logical_check',return_value={}):
            r.recover(self.plan,execute=True,confirmation=self.confirmation())
        self.assertEqual(hashes,{p.name:r.file_hash(p) for p in self.package.iterdir()})
        self.assertFalse(any(p.suffix=='.sql' for p in self.root.rglob('*')))


if __name__=='__main__': unittest.main()
