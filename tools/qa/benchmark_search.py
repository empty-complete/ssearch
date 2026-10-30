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
    """Contract placeholder for the GitHub RED checkpoint; quality unavailable."""
    return dict(precision_at_k=None, recall_at_k=None, hit_at_k=None,
                reciprocal_rank_at_k=None)
