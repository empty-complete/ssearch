import json
from pathlib import Path
import tempfile
import unittest
import pytest


def test_network_guard_allows_private_selector_wakeup_pair_but_no_foreign_connection():
    import asyncio
    import socket
    from types import SimpleNamespace
    from atom_search.diagnostics import owned_loopback_only
    with owned_loopback_only(lambda: SimpleNamespace(host='127.0.0.1', port=12345)):
        left, right = socket.socketpair()
        try:
            left.sendall(b'private wakeup')
            assert right.recv(14) == b'private wakeup'
        finally:
            left.close(); right.close()
        async def ready():
            await asyncio.sleep(0)
            return 'ready'
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            assert runner.run(ready()) == 'ready'
        with socket.socket() as other:
            with pytest.raises(RuntimeError, match='self_test_network_forbidden'):
                other.connect(('127.0.0.1', 5432))
            with pytest.raises(RuntimeError, match='self_test_network_forbidden'):
                other.connect(('192.0.2.1', 443))
        with pytest.raises(RuntimeError, match='self_test_network_forbidden'):
            socket.getaddrinfo('example.invalid', 443)


class DiagnosticsTests(unittest.TestCase):
    def test_missing_model_produces_failure_report_without_network_fallback(self):
        from atom_search.diagnostics import run_self_test
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = run_self_test(base / 'result.json', model_dir=base / 'missing-model')
            report = json.loads((base / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result, 1)
            self.assertFalse(report['ok'])
            self.assertFalse(report['model_ready'])
            self.assertIn('error', report)

    def test_offline_self_test_writes_real_model_and_catalog_results(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / 'src/atom_search/diagnostics.py').is_file(),
                        'Frozen-build diagnostics must exist')
        if not (root / 'models/model_quantized.onnx').is_file():
            self.skipTest('Local model is not prepared')
        from atom_search.diagnostics import run_self_test
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = run_self_test(base / 'result.json', data_dir=base / 'qa', model_dir=root / 'models')
            report = json.loads((base / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result, 0, report)
            self.assertTrue(report['ok'])
            self.assertTrue(report['model_ready'])
            self.assertEqual(report['backend'], 'postgresql')
            self.assertEqual(report['factory'], 'open_search_service')
            self.assertEqual(report['schema_version'], 5)
            self.assertTrue(report['clean_stop'])
            self.assertEqual(report['vector_count'], 3)
            self.assertEqual(report['dimension'], 384)
            self.assertEqual(report['literal_result'], 'КМ-101 узлы фасада.dwg')
            self.assertEqual(report['semantic_result'], 'Контракт монтажных работ.docx')
            import uuid
            self.assertEqual(str(uuid.UUID(report['public_source_id'])), report['public_source_id'])
            self.assertEqual(len(report['public_file_ids']), 3)
            first_ids = report['public_file_ids']
            result = run_self_test(base / 'repeat.json', data_dir=base / 'qa', model_dir=root / 'models')
            repeated = json.loads((base / 'repeat.json').read_text(encoding='utf-8'))
            self.assertEqual(result, 0, repeated)
            self.assertEqual(repeated['prior_schema_version'], 5)
            self.assertEqual(repeated['prior_public_source_id'], report['public_source_id'])
            self.assertEqual(repeated['prior_public_file_ids'], first_ids)
            self.assertEqual(repeated['public_file_ids'], first_ids)
            self.assertTrue(repeated['public_identity_preserved'])


@pytest.mark.parametrize('fault', ['missing_vector', 'wrong_semantic_top'])
def test_prior_missing_vector_is_not_repaired_into_preservation(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    from atom_search.diagnostics import run_self_test
    import atom_search.search_runtime as host
    mutations = []
    model = SimpleNamespace(model_key='pinned|filename-parent-v1', manifest_digest='a'*64,
                            dimension=384, input_schema='filename-parent-v1')
    class Rows:
        def __init__(self, rows): self.rows=rows
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self, sql, *args):
            if 'count(*) FROM atom_search.files' in sql: return Rows([(3,)])
            if "state='active'" in sql and 'SELECT id' in sql: return Rows([(1,)])
            if 'vector_dims' in sql: return Rows([(3 if mutations or fault=='wrong_semantic_top' else 2,384)])
            if 'embedding_models' in sql: return Rows([(model.model_key,model.manifest_digest)])
            if 'server_version' in sql: return Rows([('18.6',)])
            if 'extversion' in sql: return Rows([('0.8.7',)])
            if 'max(version)' in sql: return Rows([(3,)])
            if 'file_embeddings' in sql: return Rows([(1,'vector-one'),(2,'vector-two')])
            raise AssertionError('unexpected diagnostic query')
    class Runtime:
        def status(self): return SimpleNamespace(state='stopped', endpoint=SimpleNamespace(
            host='127.0.0.1',port=1234,installation_id='controlled'))
        def open_connection(self): return Connection()
    class Service:
        runtime=Runtime(); catalog=SimpleNamespace(_model=model)
        def status(self): return {'semantic_ready':True}
        def search(self, query): return [SimpleNamespace(name=(
            'КМ-101 узлы фасада.dwg' if query=='КМ101' else
            'wrong.docx' if fault=='wrong_semantic_top' else 'Контракт монтажных работ.docx'))]
        def scan(self, root): mutations.append('scan'); return {'status':'complete','files':3}
        def index_embeddings(self): mutations.append('embeddings')
        def close(self): pass
    monkeypatch.setattr(host,'open_search_service',lambda *args: Service())
    result=run_self_test(tmp_path/'report.json',data_dir=tmp_path/'profile')
    report=json.loads((tmp_path/'report.json').read_text(encoding='utf-8'))
    assert result==1 and report['prior_index_verified'] is False
    assert mutations==[], 'preservation must be observed before any repair'
