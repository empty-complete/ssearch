"""File-level quality metrics and a limited disposable metadata benchmark.

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

run_benchmark requires explicit absolute corpus/judgments paths and an exclusive
new absolute profile with an existing parent. It never discovers corpora, adopts
profiles, imports legacy data, loads a model, or accepts a DSN. It opens the shared
ServiceOwner with bundled PostgreSQL, one metadata source and automatic indexing
disabled. Only metadata is scanned; document contents are never read.

Judgments JSON: {"schema_version": 1, "judgments": {query: [relative_file_path]}}.
Paths must name existing files within this one corpus, using slash separators.
The caller maps this single-source file namespace to canonical IDs before quality
calculation; no IDs/paths/queries appear in the returned aggregates. Unjudged
queries contribute null quality, never success. Latency is full metadata response
time (milliseconds), using nearest-rank p50/p95; this is not first-frame latency.

The corpus must remain under caller control during the run. Preflight permits at
most max_files (default128, hard ceiling1024), bounded directory traversal,30
queries and30 repetitions. No source mutation or load generator is provided.
cancel is an optional threading.Event passed to the actual catalog scheduler;
cancelled/incomplete scans return coverage partial and total None. No synthetic
report closes cancellation or platform acceptance. Results prove no scale target.

Cleanup first closes the actual owner/PG, then verifies the new directory identity
and secret ownership marker and refuses any reparse path before removing only
that profile. Cleanup failure retains data and raises benchmark_cleanup_failed.
Input refusals are ValueError with fixed codes; execution failure is RuntimeError
benchmark_failed without underlying details. No publish/upload/file logging occurs.
"""
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import threading
import time
import uuid


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


def _scan_metadata(owner, source_id, *, cancel=None):
    """One real scheduler scan; the timer cancels, never fabricates completion."""
    cancel = cancel if cancel is not None else threading.Event()
    timer = threading.Timer(60, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        jobs = owner.facade(owner.owner_access).start_catalog((source_id,))
        if len(jobs) != 1:
            raise RuntimeError('benchmark_failed')
        with owner.operation(cancel):
            return owner.scheduler.wait_catalog(jobs[0], cancel=cancel)
    finally:
        timer.cancel()
        timer.join()


def _no_links(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('benchmark_input_invalid')


def _files(corpus, max_files):
    pending, files, directories = [corpus], set(), 0
    while pending:
        directory = pending.pop()
        directories += 1
        if directories > max_files * 4 + 32:
            raise ValueError('benchmark_input_invalid')
        _no_links(directory)
        for entry in directory.iterdir():
            _no_links(entry)
            mode = entry.lstat().st_mode
            if stat.S_ISDIR(mode):
                pending.append(entry)
            elif stat.S_ISREG(mode):
                files.add(entry.relative_to(corpus).as_posix())
                if len(files) > max_files:
                    raise ValueError('benchmark_input_invalid')
            else:
                raise ValueError('benchmark_input_invalid')
    return files


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('benchmark_input_invalid')
        value[key] = item
    return value


def _inputs(corpus, judgments, profile, queries, repetitions, cancel, max_files):
    try:
        if (not all(isinstance(path, Path) and path.is_absolute() for path in (corpus, judgments, profile))
                or type(queries) is not tuple or not 1 <= len(queries) <= 30
                or any(type(query) is not str or not 1 <= len(query.strip()) <= 512 for query in queries)
                or len(set(queries)) != len(queries) or type(repetitions) is not int
                or not 1 <= repetitions <= 30 or type(max_files) is not int
                or not 1 <= max_files <= 1024 or cancel is not None and not isinstance(cancel, threading.Event)):
            raise ValueError()
        # Reject reparse ancestors before resolving; resolution must not hide them.
        _no_links(corpus)
        _no_links(judgments)
        _no_links(profile.parent)
        corpus, judgments, profile = corpus.resolve(), judgments.resolve(), profile.parent.resolve() / profile.name
        if profile.exists() or profile.is_symlink():
            raise FileExistsError()
        if not corpus.is_dir() or not judgments.is_file() or not profile.parent.is_dir():
            raise ValueError()
        if any(left == right or left in right.parents or right in left.parents
               for left, right in ((profile, corpus), (profile, judgments), (corpus, judgments))):
            raise ValueError()
        files = _files(corpus, max_files)
        if judgments.stat().st_size > 65536:
            raise ValueError()
        document = json.loads(judgments.read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
        if (type(document) is not dict or set(document) != {'schema_version', 'judgments'}
                or type(document['schema_version']) is not int or document['schema_version'] != 1
                or type(document['judgments']) is not dict):
            raise ValueError()
        mapping = document['judgments']
        for query, paths in mapping.items():
            if type(query) is not str or not 1 <= len(query.strip()) <= 512 or type(paths) is not list:
                raise ValueError()
            if any(type(path) is not str or not path or '\\' in path
                   or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                   or path not in files for path in paths):
                raise ValueError()
        return corpus, profile, {query: frozenset(paths) for query, paths in mapping.items()}
    except FileExistsError:
        raise ValueError('benchmark_profile_exists') from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError('benchmark_input_invalid') from None


def _cleanup(profile, identity, token):
    _no_links(profile)
    info = profile.stat()
    if (info.st_dev, info.st_ino) != identity:
        raise RuntimeError('benchmark_cleanup_failed')
    marker = profile / '.ssearch-benchmark-owner'
    _no_links(marker)
    if marker.read_text(encoding='ascii') != token:
        raise RuntimeError('benchmark_cleanup_failed')
    # Preflight the entire owned tree. shutil on Windows also refuses to follow
    # directory junctions if another process swaps one after this verification.
    for directory, dirs, files in os.walk(profile, followlinks=False):
        _no_links(Path(directory))
        for name in (*dirs, *files):
            _no_links(Path(directory) / name)
    if (profile.stat().st_dev, profile.stat().st_ino) != identity:
        raise RuntimeError('benchmark_cleanup_failed')
    shutil.rmtree(profile)


def _percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def run_benchmark(corpus: Path, judgments: Path, *, profile: Path,
                  queries: tuple[str, ...], repetitions: int = 30,
                  cancel: threading.Event | None = None, max_files: int = 128) -> dict:
    """Measure one explicit tiny metadata corpus with one exclusively owned PG."""
    corpus, profile, mapping = _inputs(corpus, judgments, profile, queries, repetitions, cancel, max_files)
    owner, identity, token = None, None, uuid.uuid4().hex
    try:
        try:
            profile.mkdir()  # Exclusive creation: never adopt a racing profile.
        except FileExistsError:
            raise ValueError('benchmark_profile_exists') from None
        info = profile.stat()
        identity = (info.st_dev, info.st_ino)
        with (profile / '.ssearch-benchmark-owner').open('x', encoding='ascii') as stream:
            stream.write(token)
        from atom_search.service.owner import open_service_owner
        from atom_search.service.contracts import SearchRequest
        owner = open_service_owner(profile, profile / 'missing-model',
                                   discover_legacy=False, automatic_catalog=False)
        if owner.attached:
            raise RuntimeError('benchmark_failed')
        snapshot = owner.repository.snapshot()
        settings = snapshot.settings
        source_id = str(uuid.uuid4())
        settings['sources'] = [dict(id=source_id, path=str(corpus), label='benchmark', enabled=True)]
        owner.coordinator.owner_apply(settings, snapshot.revision)
        owner.start()
        report = _scan_metadata(owner, source_id, cancel=cancel)
        complete = report['status'] == 'complete' and not (cancel is not None and cancel.is_set())
        latency, scores = [], []
        facade = owner.facade(owner.owner_access)
        if complete:
            for query in queries:
                for _ in range(repetitions):
                    if cancel is not None and cancel.is_set():
                        complete = False
                        break
                    started = time.perf_counter()
                    response = facade.search(SearchRequest(query, source_ids=(source_id,), limit=10, mode='literal'))
                    latency.append((time.perf_counter() - started) * 1000)
                    # Single-source canonical relative paths identify files locally.
                    scores.append(quality_at_k(tuple(item.relative_path for item in response.results),
                                               mapping.get(query, frozenset())))
                if not complete:
                    break
        quality = {}
        for key in ('precision_at_k', 'recall_at_k', 'hit_at_k', 'reciprocal_rank_at_k'):
            measured = [score[key] for score in scores if score[key] is not None]
            quality[key] = sum(measured) / len(measured) if measured else None
        result = dict(status='complete' if complete else 'partial',
            coverage='complete' if complete else 'partial',
            total=report['files'] if complete else None, measurements=len(latency),
            query_count=len(queries), judged_query_count=sum(bool(mapping.get(query)) for query in queries),
            latency_ms=dict(p50=_percentile(latency, .5), p95=_percentile(latency, .95)), quality=quality)
    except ValueError as caught:
        if str(caught) == 'benchmark_profile_exists':
            raise
        raise RuntimeError('benchmark_failed') from None
    except Exception:
        raise RuntimeError('benchmark_failed') from None
    finally:
        if identity is not None:
            try:
                if owner is not None:
                    owner.close()
                    if owner.runtime is not None and owner.runtime.status().state != 'stopped':
                        raise RuntimeError('benchmark_cleanup_failed')
                _cleanup(profile, identity, token)
            except Exception:
                raise RuntimeError('benchmark_cleanup_failed') from None
    return result
