"""Pure file-level search quality metrics; no corpus or runtime execution.

quality_at_k accepts ordered canonical file IDs and a complete positive judgment
set for one query. Callers must map chunk hits to canonical file IDs first. IDs
are opaque: no chunk suffix, case, whitespace or path normalization is inferred.
Repeated file IDs are removed in first-occurrence order BEFORE taking top k.

Returned aggregate keys (never IDs):
* precision_at_k: relevant unique files in top k / k (fixed denominator).
* recall_at_k: relevant unique files in top k / all judged relevant files.
* hit_at_k: 1.0 if at least one relevant file appears in top k, otherwise 0.0.
* reciprocal_rank_at_k: 1 / first relevant unique rank within top k, else 0.0.
  This is one query's contribution to MRR; this function does not average queries.

Empty judgments mean unknown quality: every metric is None, including when the
result list is empty. Empty results with nonempty judgments yield zero metrics.
These calculations establish no real-corpus quality, performance or release PASS.

Inputs must be tuple[str, ...], frozenset[str], and a positive non-bool int k.
Wrong types raise TypeError; nonpositive k and blank IDs raise ValueError.
Validation applies even when judgments are empty. IDs are never emitted in errors.
"""


def quality_at_k(ranked_ids: tuple[str, ...], relevant_ids: frozenset[str],
                 k: int = 10) -> dict[str, float | None]:
    """Return one query's file-level metrics after validating the entire input."""
    if type(ranked_ids) is not tuple or type(relevant_ids) is not frozenset:
        raise TypeError('ranked_ids must be a tuple and relevant_ids a frozenset')
    if type(k) is not int:
        raise TypeError('k must be an integer, excluding bool')
    if k <= 0:
        raise ValueError('k must be positive')
    for identifiers in (ranked_ids, relevant_ids):
        for identifier in identifiers:
            if type(identifier) is not str:
                raise TypeError('file identifiers must be strings')
            if not identifier.strip():
                raise ValueError('file identifiers must not be blank')

    if not relevant_ids:
        return dict(precision_at_k=None, recall_at_k=None, hit_at_k=None,
                    reciprocal_rank_at_k=None)

    seen = set()
    top = []
    for identifier in ranked_ids:
        if identifier not in seen:
            seen.add(identifier)
            top.append(identifier)
            if len(top) == k:
                break
    hits = sum(identifier in relevant_ids for identifier in top)
    reciprocal_rank = next((1.0 / rank for rank, identifier in enumerate(top, 1)
                            if identifier in relevant_ids), 0.0)
    return dict(precision_at_k=hits / k, recall_at_k=hits / len(relevant_ids),
                hit_at_k=float(bool(hits)), reciprocal_rank_at_k=reciprocal_rank)
