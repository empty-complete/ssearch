"""Lexical retrieval and deterministic engineering-name ranking."""
import re
import unicodedata
from collections.abc import Sequence

from .models import CatalogFile, SearchResult


def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')


def words(text: str) -> list[str]:
    return re.findall(r'[^\W_]+', normalize(text), flags=re.UNICODE)


def compact(text: str) -> str:
    return ''.join(words(text))


def query_terms(text: str) -> list[str]:
    terms = words(text)
    if len(terms) <= 1:
        return terms
    stopwords = {'и', 'в', 'во', 'на', 'с', 'со', 'из', 'к', 'ко', 'по', 'для',
                 'о', 'об', 'от', 'до', 'у', 'за', 'под', 'над', 'the', 'of', 'for', 'and'}
    return [term for term in terms if term not in stopwords]


def lexical_score(query: str, name: str, relative_path: str) -> float:
    tokens = query_terms(query)
    if not tokens:
        return 0.0
    name_text, path_text = normalize(name), normalize(relative_path)
    name_words, path_words = words(name), words(relative_path)
    target = ''.join(tokens)
    score = 0.0
    if target == compact(name.rsplit('.', 1)[0]):
        score += 200.0
    elif target in compact(name):
        score += 90.0
    if target in compact(relative_path):
        score += 15.0
    matched = 0
    for token in tokens:
        if token in name_words:
            score += 25.0
            matched += 1
        elif any(word.startswith(token) for word in name_words):
            score += 15.0
            matched += 1
        elif token in path_words or any(word.startswith(token) for word in path_words):
            score += 6.0
            matched += 1
    if matched == len(tokens):
        score += 20.0
    return score


def ranked_results(query: str, rows: Sequence[CatalogFile], limit: int) -> list[SearchResult]:
    results = []
    for row in rows:
        score = lexical_score(query, row.name, row.relative_path)
        if score:
            results.append(SearchResult(row.file_id, row.root, row.relative_path, row.name,
                                        row.size, row.modified_ns, score,
                                        row.source_status not in ('complete', 'scanning')))
    return sorted(results, key=lambda item: (-item.score, item.relative_path.casefold(), item.file_id))[:limit]
