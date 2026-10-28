"""The desktop and service share the Python product/version build input."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('product_build_support', ROOT / 'packaging/build_support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


def test_export_uses_one_catalog_and_clean_source_identity(tmp_path):
    destination = tmp_path / 'product-metadata.json'
    with patch.object(support, 'verify_source_commit', return_value='a' * 40):
        result = support.export_product_metadata(ROOT, destination)
    metadata = json.loads(destination.read_text(encoding='utf-8'))
    assert result == destination
    assert set(metadata) == {'schema_version', 'source_commit', 'app_version', 'core_version', 'product'}
    assert metadata['schema_version'] == 1
    assert metadata['source_commit'] == 'a' * 40
    assert metadata['app_version'] == support.project_version(ROOT)
    assert metadata['core_version'] == support.project_version(ROOT / 'packages/atom-search-core')
    assert metadata['product']['product_name'] == 'ssearch'
    assert metadata['product']['author_name'] == 'Филипп Белобородов'
    assert metadata['product']['support_email'] == 'filipp.beloborodoff@yandex.ru'


def test_export_preserves_existing_input_and_rejects_dirty_source(tmp_path):
    destination = tmp_path / 'product-metadata.json'
    destination.write_text('previous reviewed input', encoding='utf-8')
    with patch.object(support, 'verify_source_commit', return_value='a' * 40):
        with pytest.raises(support.BuildError):
            support.export_product_metadata(ROOT, destination)
    assert destination.read_text(encoding='utf-8') == 'previous reviewed input'
    destination.unlink()
    with patch.object(support, 'verify_source_commit', side_effect=support.BuildError('dirty')):
        with pytest.raises(support.BuildError):
            support.export_product_metadata(ROOT, destination)
    assert not destination.exists()


@pytest.mark.parametrize('catalog', [
    'PRODUCT = {}',
    'PRODUCT = make_product()',
    'PRODUCT = {"product_name": "ssearch"}\nPRODUCT = {}',
    'PRODUCT = ' + repr(dict(product_name='ssearch', product_name_ru='bad\nname',
                            support_email='a@b', author_name='author', author_title='developer',
                            organization_group='upstream', copyright_holder='author')),
])
def test_invalid_catalog_cannot_become_a_packaged_identity(tmp_path, catalog):
    source = tmp_path / 'src/atom_search/content.py'
    source.parent.mkdir(parents=True)
    source.write_text(catalog, encoding='utf-8')
    destination = tmp_path / 'product-metadata.json'
    with patch.object(support, 'verify_source_commit', return_value='a' * 40):
        with pytest.raises(support.BuildError):
            support.export_product_metadata(tmp_path, destination)
    assert not destination.exists()
