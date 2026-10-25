"""Full metadata context and the exact, independently prepared E5 input."""
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
import sys

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from atom_search_core.models import CatalogFile, ProjectAlias, ProjectBinding, ProjectDefinition


def context_api():
    api = importlib.import_module('atom_search_core.project_context')
    assert hasattr(api, 'build_document_context'), 'Full document context is required'
    return api


def definitions():
    project = ProjectDefinition('00000000-0000-4000-8000-000000000001', 'Пермские моторы',
        (1,), (ProjectBinding('00000000-0000-4000-8000-000000000001', 1, '', True),))
    alias = ProjectAlias('00000000-0000-4000-8000-000000000002', project.project_id,
                         (1,), ('ПМ', 'ＰＭ', 'pm'), True, 'Owner confirmed')
    return project, alias


def document():
    return CatalogFile(7, 1, 'C:/projects/Зенитные фонари ПМ',
                       'АР/Стадия П/Спецификация.pdf', 'Спецификация.pdf', 80, 123, 'complete', 4)


def test_context_contains_filename_full_parents_root_label_and_confirmed_project():
    api = context_api()
    project, alias = definitions()
    context = api.build_document_context(document(), source_label='Проекты', projects=(project,),
        aliases=(alias,), context_revision=3)
    expected = ('name: Спецификация.pdf\nproject: Пермские моторы\nsource: Проекты\n'
                'path: АР/Стадия П/Спецификация.pdf\nroot: C:/projects/Зенитные фонари ПМ\n'
                'aliases: pm, пм')
    assert api.metadata_embedding_text(context) == expected
    assert api.metadata_input_hash(context) == hashlib.sha256(expected.encode('utf-8')).hexdigest()
    assert (context.file_id, context.source_id, context.project_id, context.context_revision) == (
        7, 1, project.project_id, 3)


def test_input_fingerprint_changes_for_label_alias_project_but_not_revision_or_stat():
    api = context_api()
    project, alias = definitions()
    def build(*, label='Проекты', project=project, aliases=(alias,), revision=3):
        return api.build_document_context(document(), source_label=label, projects=(project,),
            aliases=aliases, context_revision=revision)
    baseline = api.metadata_input_hash(build())
    assert api.metadata_input_hash(build(revision=4)) == baseline
    assert api.metadata_input_hash(build(label='Архив')) != baseline
    assert api.metadata_input_hash(build(project=replace(project, canonical_name='Новый проект'))) != baseline
    assert api.metadata_input_hash(build(aliases=(replace(alias, variants=('Моторы',)),))) != baseline
    assert document().revision == 4


def test_unconfirmed_or_foreign_alias_never_changes_input_and_empty_fields_are_retained():
    api = context_api()
    project, alias = definitions()
    def build(aliases):
        return api.build_document_context(document(), source_label='', projects=(project,),
            aliases=aliases, context_revision=1)
    plain = api.metadata_input_hash(build(()))
    assert api.metadata_input_hash(build((replace(alias, confirmed=False),))) == plain
    assert api.metadata_input_hash(build((replace(alias, source_ids=(2,)),))) == plain
    unrelated = api.build_document_context(document(), source_label='', projects=(), aliases=(alias,),
                                            context_revision=1)
    text = api.metadata_embedding_text(unrelated)
    assert '\nproject: \nsource: \n' in text and text.endswith('\naliases: ')


@pytest.fixture
def tokenizer_directory(tmp_path):
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'passage': 1, ':': 2, 'query': 3,
        'alpha': 4, '<pad>': 5, '[CLS]': 6, '[SEP]': 7}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(single='[CLS] $A [SEP]',
        special_tokens=[('[CLS]', 6), ('[SEP]', 7)])
    tokenizer.save(str(tmp_path / 'tokenizer.json'))
    body = (tmp_path / 'tokenizer.json').read_bytes()
    manifest = {'schema_version': 1, 'dimension': 384, 'max_tokens': 512,
        'revision': 'a' * 40, 'model_id': 'fixture@' + 'a' * 40,
        'onnx_file': 'model_quantized.onnx', 'tokenizer_file': 'tokenizer.json', 'files': [
            {'path': 'model_quantized.onnx', 'size': 1, 'sha256': 'b' * 64},
            {'path': 'tokenizer.json', 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return tmp_path


def preparer_type():
    name = 'atom_search_core.tokenization'
    assert importlib.util.find_spec(name), 'An ONNX-independent pinned tokenizer is required'
    return importlib.import_module(name).PinnedTokenizer


def test_metadata_context_token_budget_includes_prefix_and_specials(tokenizer_directory):
    preparer = preparer_type()(tokenizer_directory)
    raw = 'name: alpha\nproject: alpha\nsource: alpha\npath: ' + 'alpha ' * 900
    item = preparer.prepare_input(raw, kind='passage')
    tokenizer = Tokenizer.from_file(str(tokenizer_directory / 'tokenizer.json'))
    actual = tokenizer.encode('passage: ' + item.text)
    assert item.token_count == len(actual.ids) <= 512
    assert actual.ids.count(1) == 1  # Exactly one passage prefix.
    assert actual.ids[0] == 6 and actual.ids[-1] == 7
    assert raw.startswith(item.text) and not item.text.startswith('passage: ')
    assert item.text.startswith('name: alpha\nproject: alpha')
    assert item.input_hash == hashlib.sha256(item.text.encode('utf-8')).hexdigest()
    assert item.input_hash != hashlib.sha256(raw.encode('utf-8')).hexdigest()


def test_pinned_tokenizer_prepares_without_onnx_session(tokenizer_directory, monkeypatch):
    monkeypatch.setitem(sys.modules, 'onnxruntime', None)
    preparer = preparer_type()(tokenizer_directory)
    assert preparer.prepare_input('alpha', kind='query').token_count == 5
    assert not (tokenizer_directory / 'model_quantized.onnx').exists()
    with pytest.raises(ValueError):
        preparer.prepare_input('alpha', kind='passage', max_tokens=513)
    (tokenizer_directory / 'tokenizer.json').write_bytes(b'changed')
    with pytest.raises((ValueError, RuntimeError)):
        preparer_type()(tokenizer_directory)


def test_service_prepares_exact_payload_without_recrawling_documents(tokenizer_directory):
    from atom_search_core.service import SearchService
    from atom_search_core.models import EmbeddingInput
    from types import SimpleNamespace
    assert hasattr(SearchService, 'index_context_batch'), 'Service metadata backfill is required'
    api = context_api()
    project, alias = definitions()
    file = replace(document(), relative_path='alpha/' * 900 + 'alpha.pdf')
    stored, encoded = [], []
    class Projection:
        def configuration(self):
            return 1, ((project,), (alias,), {1: 'Проекты'})
        def pending_context(self, limit, **kwargs):
            assert limit <= 1000
            return [] if stored else [file]
        def publish_batch(self, records, **kwargs):
            stored.extend(records)
            return len(records)
    class Catalog:
        saved = False
        def pending_embeddings(self, model_key, dimension, limit, **kwargs):
            assert model_key.endswith('|filename-path-root-project-v2') and limit <= 16
            if self.saved:
                return []
            context, revision, prepared = stored[0]
            return [EmbeddingInput(file, prepared.text, api.INPUT_SCHEMA, 1, prepared.input_hash)]
        def save_embeddings(self, records, model_key):
            assert records[0].input_hash == stored[0][2].input_hash
            assert records[0].item.text == encoded[0]
            self.saved = True
            return len(records)
    class Model:
        model_id, dimension = 'fixture', 384
        def encode(self, texts, **kwargs):
            encoded.extend(texts)
            return [(1.,) + (0.,) * 383 for _ in texts]
    class NoCrawl:
        def snapshots(self, *args, **kwargs):
            raise AssertionError('Context backfill must not enumerate or open documents')
    core = SearchService(catalog=Catalog(), retrieval=SimpleNamespace(), source=NoCrawl(),
        embedder=Model(), projection=Projection(), preparer=preparer_type()(tokenizer_directory),
        input_schema=api.INPUT_SCHEMA)
    first = core.index_embedding_batch()
    assert first['indexed'] == 0 and first['context_attempted'] == 1
    assert core.index_embedding_batch()['indexed'] == 1
    assert not encoded[0].startswith('passage: ') and stored[0][2].token_count <= 512
    assert metadata_hash(encoded[0]) == stored[0][2].input_hash


def metadata_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def test_status_never_reports_embeddings_ready_while_current_context_is_pending():
    from types import SimpleNamespace
    from atom_search_core import SearchService, SearchScope
    catalog = SimpleNamespace(resolve_scope=lambda root: SearchScope((1,)),
        status=lambda **kwargs: dict(files=1, roots=[dict(status='complete')]),
        pending_embeddings=lambda *args: [])
    projection = SimpleNamespace(pending_context=lambda *args, **kwargs: [document()])
    core = SearchService(catalog=catalog, retrieval=SimpleNamespace(), projection=projection,
        embedder=SimpleNamespace(model_id='fixture', dimension=384))
    assert core.status()['embeddings_ready'] is False


def test_real_pinned_tokenizer_hash_is_the_actual_encoder_payload(monkeypatch):
    from pathlib import Path
    import numpy as np
    import onnxruntime
    from atom_search_core.tokenization import PinnedTokenizer
    from atom_search_core.embeddings import OnnxEmbedder
    directory = Path(__file__).resolve().parents[1] / 'models'
    preparer = PinnedTokenizer(directory)
    raw = 'name: Спецификация.pdf\nproject: Пермские моторы\nsource: Проекты\npath: ' + 'СтадияП/КМ/' * 900
    bounded = preparer.prepare_input(raw, kind='passage')
    expected = Tokenizer.from_file(str(directory / 'tokenizer.json')).encode('passage: ' + bounded.text)
    assert bounded.token_count == len(expected.ids) == 512
    assert preparer.prepare_input(bounded.text, kind='passage') == bounded
    assert bounded.input_hash == hashlib.sha256(bounded.text.encode()).hexdigest()
    assert raw.startswith(bounded.text) and not bounded.text.startswith('passage: ')
    class Session:
        def get_inputs(self):
            from types import SimpleNamespace
            return [SimpleNamespace(name='input_ids', type='tensor(int64)')]
        def run(self, outputs, feeds):
            assert feeds['input_ids'][0].tolist() == expected.ids
            result = np.zeros((1, 384), dtype=np.float32)
            result[:, 0] = 1
            return [result]
    monkeypatch.setattr(onnxruntime, 'InferenceSession', lambda *args, **kwargs: Session())
    encoder = OnnxEmbedder(directory, preparer=preparer)
    assert encoder.encode([bounded.text], kind='passage').shape == (1, 384)
