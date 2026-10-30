"""Pure metric contracts using synthetic canonical file IDs, never a real corpus."""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('benchmark_search', ROOT / 'tools/qa/benchmark_search.py')
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)
quality_at_k = METRICS.quality_at_k
KEYS = {'precision_at_k', 'recall_at_k', 'hit_at_k', 'reciprocal_rank_at_k'}


def test_metrics_deduplicate_files_before_cutoff():
    ranked = ('file-a',) * 20 + ('file-b', 'file-c')
    result = quality_at_k(ranked, frozenset({'file-b', 'file-c'}), k=2)
    assert result == pytest.approx(dict(precision_at_k=0.5, recall_at_k=0.5,
                                       hit_at_k=1.0, reciprocal_rank_at_k=0.5))


def test_duplicate_hits_cannot_inflate_recall():
    result = quality_at_k(('file-a',) * 20, frozenset({'file-a', 'file-b'}), k=10)
    assert result == pytest.approx(dict(precision_at_k=0.1, recall_at_k=0.5,
                                       hit_at_k=1.0, reciprocal_rank_at_k=1.0))


def test_first_occurrence_order_determines_reciprocal_rank():
    result = quality_at_k(('irrelevant', 'irrelevant', 'first', 'second'),
                          frozenset({'first', 'second'}), k=3)
    assert result == pytest.approx(dict(precision_at_k=2/3, recall_at_k=1.0,
                                       hit_at_k=1.0, reciprocal_rank_at_k=0.5))


def test_rank_after_cutoff_does_not_contribute():
    result = quality_at_k(('irrelevant', 'relevant'), frozenset({'relevant'}), k=1)
    assert set(result) == KEYS
    assert all(value == 0.0 for value in result.values())


def test_precision_uses_fixed_k_for_short_result_list():
    result = quality_at_k(('relevant',), frozenset({'relevant'}), k=4)
    assert result['precision_at_k'] == 0.25
    assert result['recall_at_k'] == 1.0


def test_default_cutoff_is_ten():
    result = quality_at_k(tuple(f'irrelevant-{i}' for i in range(10)) + ('relevant',),
                          frozenset({'relevant'}))
    assert all(value == 0.0 for value in result.values())


@pytest.mark.parametrize('ranked', [(), ('file-a', 'file-a', 'file-b')])
def test_empty_judgments_mark_unknown(ranked):
    result = quality_at_k(ranked, frozenset())
    assert set(result) == KEYS
    assert all(value is None for value in result.values())


def test_empty_results_with_known_relevance_are_zero():
    result = quality_at_k((), frozenset({'file-a'}))
    assert set(result) == KEYS
    assert all(value == 0.0 for value in result.values())


def test_canonical_ids_are_opaque_without_suffix_or_case_heuristics():
    ranked = ('File', 'file', 'file#chunk-1', 'file#chunk-2', ' file ')
    result = quality_at_k(ranked, frozenset({'file#chunk-2', ' file '}), k=4)
    assert result == pytest.approx(dict(precision_at_k=0.25, recall_at_k=0.5,
                                       hit_at_k=1.0, reciprocal_rank_at_k=0.25))


@pytest.mark.parametrize('ranked,relevant', [([], frozenset({'file'})),
    ('file', frozenset({'file'})), (('file',), {'file'}), (('file',), ('file',)),
    (None, frozenset()), ((), None), ((1,), frozenset({'file'})),
    (('file',), frozenset({1})), ((b'file',), frozenset()),
    ((True,), frozenset()), ((), frozenset({None}))])
def test_invalid_input_types_are_rejected(ranked, relevant):
    with pytest.raises(TypeError):
        quality_at_k(ranked, relevant)


@pytest.mark.parametrize('k', [True, False, 1.0, '1', None])
def test_cutoff_requires_integer_not_bool(k):
    with pytest.raises(TypeError):
        quality_at_k(('file',), frozenset({'file'}), k=k)


@pytest.mark.parametrize('k', [0, -1, -10])
def test_cutoff_must_be_positive(k):
    with pytest.raises(ValueError):
        quality_at_k(('file',), frozenset({'file'}), k=k)


@pytest.mark.parametrize('ranked,relevant', [(('',), frozenset({'file'})),
    ((' \t\n',), frozenset()), ((), frozenset({''})),
    (('file',), frozenset({' '}))])
def test_blank_identifiers_are_rejected_even_with_empty_judgments(ranked, relevant):
    with pytest.raises(ValueError):
        quality_at_k(ranked, relevant)


def test_metrics_return_only_aggregates_and_preserve_inputs():
    ranked = ('first', 'first', 'second')
    relevant = frozenset({'second'})
    result = quality_at_k(ranked, relevant, k=2)
    assert set(result) == KEYS
    assert all(type(value) is float for value in result.values())
    assert ranked == ('first', 'first', 'second')
    assert relevant == frozenset({'second'})
