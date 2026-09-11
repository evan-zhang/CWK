#!/usr/bin/env python3
"""RT-055 experimental adapters; never a production SearchBackend.

No network, corpus reads, or service startup on import. The OPS owner supplies
isolated services and a frozen private corpus after separate authorization.
Only score_cases returns exportable aggregates; documents/hits remain private.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

import kb_retrieval_decision as decision
import kb_stage_b_poc as projection
from kb_stage_b_opensearch_benchmark import mapping_for

TOP_K = 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_QUERY_CHARS = 4096
MAX_PARENT_BYTES = 512 * 1024
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_EXACT_TERMS = 32
DASHES = str.maketrans({c: '-' for c in '\u2010\u2011\u2012\u2013\u2014\u2212\ufe63'})
DATE = re.compile(r'(?<!\d)(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?(?!\d)')
IDENTIFIER = re.compile(r'(?<![a-z0-9_])(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)+(?![a-z0-9_])')
FILENAME = re.compile(r'[^\s/\\<>"\'‘’“”「」《》()\[\]{}（）【】,，;；:：!?？!。]+\.[a-z][a-z0-9]{0,7}\b')
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
Transport = Callable[[str, str, Any, float], Any]


class CandidateError(RuntimeError):
    """Constant messages only; never propagate native response text."""


class CandidateTimeout(CandidateError):
    pass


class CandidateLeak(CandidateError):
    pass


def normalize(value: str) -> str:
    return unicodedata.normalize('NFKC', value).translate(DASHES).casefold()


def exact_fields(text: str, filename: str = '') -> dict[str, list[str]]:
    text = normalize(text)
    dates = set()
    for match in DATE.finditer(text):
        try:
            dates.add(dt.date(*(int(v) for v in match.groups())).isoformat())
        except ValueError:
            continue
    filenames = set(FILENAME.findall(text))
    if filename:
        filenames.add(normalize(filename.replace('\\', '/').rsplit('/', 1)[-1]))
    return {'identifiers': sorted(set(IDENTIFIER.findall(text))),
            'date_values': sorted(dates), 'filenames': sorted(filenames)}


def canonical_text(document: projection.SourceDocument) -> str:
    """Identical normalized source payload for native A and B ingestion."""
    return f'{document.title}\n{document.filename}\n\n{document.text}'


def validate_documents(documents: Sequence[projection.SourceDocument]) -> None:
    seen = set()
    if not documents:
        raise CandidateError('empty corpus')
    for doc in documents:
        key = (doc.kb_id, doc.doc_id)
        if (doc.kb_id not in decision.LIBRARIES or not doc.doc_id or key in seen
                or not doc.text.strip() or len(canonical_text(doc).encode()) > MAX_DOCUMENT_BYTES):
            raise CandidateError('invalid corpus document')
        seen.add(key)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CandidateError('redirect refused')


class LoopbackJSON:
    """No ambient proxies, redirects, credentials, response logging or retries.

    An authenticated native B service needs an OPS-owned authenticated transport
    with the same callable contract; credentials are not CLI/config arguments.
    """
    def __init__(self, base_url: str):
        url = urllib.parse.urlsplit(base_url)
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'}
                or url.username or url.password or url.path not in {'', '/'}
                or url.query or url.fragment or not url.port):
            raise CandidateError('explicit numeric loopback endpoint required')
        self.base_url = base_url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, method: str, path: str, payload: Any, timeout: float) -> Any:
        if not path.startswith('/') or path.startswith('//') or '#' in path:
            raise CandidateError('invalid request path')
        if not math.isfinite(timeout) or timeout <= 0:
            raise CandidateTimeout('request deadline exceeded')
        raw = payload if isinstance(payload, bytes) else (
            None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        request = urllib.request.Request(self.base_url + path, data=raw, method=method,
            headers={'Content-Type': 'application/x-ndjson' if isinstance(payload, bytes)
                     else 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise CandidateError('response size exceeded')
            return json.loads(body) if body else {}
        except (TimeoutError, socket.timeout):
            raise CandidateTimeout('request deadline exceeded') from None
        except urllib.error.HTTPError as exc:
            exc.close()
            raise CandidateError('native request failed') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise CandidateTimeout('request deadline exceeded') from None
            raise CandidateError('native request failed') from None
        except (ValueError, OSError):
            raise CandidateError('invalid native response') from None


@dataclass(frozen=True)
class Hit:
    kb_id: str
    doc_id: str = field(repr=False)
    text: str = field(default='', repr=False)


class Deadline:
    def __init__(self, seconds: float):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise CandidateError('invalid timeout budget')
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        result = self.end - time.monotonic()
        if result <= 0:
            raise CandidateTimeout('request deadline exceeded')
        return result


def _query_scope(query: str, kb_id: str) -> None:
    if kb_id not in decision.LIBRARIES or not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise CandidateError('invalid search request')


def _search_hits(response: Any) -> list:
    if (not isinstance(response, dict) or response.get('timed_out') is not False
            or not isinstance(response.get('_shards'), dict)
            or response['_shards'].get('failed') != 0):
        raise CandidateError('incomplete native search')
    hits = response.get('hits', {}).get('hits')
    if not isinstance(hits, list) or len(hits) > TOP_K:
        raise CandidateError('invalid native search')
    return hits


class OpenSearchCandidate:
    """A: exact keywords OR ICU BM25, doc collapse, bounded parent mget.

    Exact-bearing queries require all extracted exact terms; a zero exact match
    does not fall back to BM25. No expected labels or reranker enter this path.
    """
    candidate_id = decision.CANDIDATE_A

    def __init__(self, request: Transport, tenant_id: str):
        if not SAFE_ID.fullmatch(tenant_id):
            raise CandidateError('invalid experiment tenant')
        self.request = request
        self.tenant_id = tenant_id
        prefix = 'rt055-' + uuid.uuid4().hex
        self.child_index, self.parent_index = prefix + '-c', prefix + '-p'
        self._owned: list[str] = []
        self.ready = False
        self._attempted = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def build(self, documents: Sequence[projection.SourceDocument], timeout: float = 3600) -> None:
        validate_documents(documents)
        if self._attempted:
            raise CandidateError('candidate build is single use')
        self._attempted = True
        deadline = Deadline(timeout)
        mapping = mapping_for('analysis_icu', False)
        # No fixture, holdout, or RT-054 query pool is read by projection.
        parent_mapping = {'mappings': {'dynamic': 'strict', 'properties': {
            'tenant_id': {'type': 'keyword'}, 'kb_id': {'type': 'keyword'},
            'doc_id': {'type': 'keyword'}, 'text': {'type': 'text', 'index': False}}},
            'settings': {'number_of_shards': 1, 'number_of_replicas': 0}}
        for index, config in ((self.parent_index, parent_mapping), (self.child_index, mapping)):
            response = self.request('PUT', '/' + index, config, deadline.remaining())
            if response.get('acknowledged') is not True:
                raise CandidateError('index creation unacknowledged; OPS reconciliation required')
            self._owned.append(index)
        # Per-document projection preserves identical input; do not deduplicate
        # whole documents/templates differently from B's native ingestion.
        for doc in documents:
            source = replace(doc, text=canonical_text(doc))
            parents = projection.build_parents(source)
            children = [c for p in parents for c in projection.build_children(p, doc.title, doc.filename)]
            parent_rows = [(p.parent_id, {'tenant_id': self.tenant_id, 'kb_id': p.kb_id,
                'doc_id': p.doc_id, 'text': p.text}) for p in parents]
            child_rows = []
            for child in children:
                row = {'tenant_id': self.tenant_id, 'kb_id': child.kb_id, 'doc_id': child.doc_id,
                    'chunk_id': child.chunk_id, 'parent_id': child.parent_id,
                    'title': child.title, 'section_path': list(child.section_path), 'body': child.body}
                row.update(exact_fields(child.body + '\n' + doc.title, doc.filename))
                child_rows.append((child.chunk_id, row))
            for index, rows in ((self.parent_index, parent_rows), (self.child_index, child_rows)):
                for offset in range(0, len(rows), 100):
                    batch = rows[offset:offset + 100]
                    lines = []
                    for identifier, row in batch:
                        lines.extend((json.dumps({'create': {'_id': identifier}}), json.dumps(row, ensure_ascii=False)))
                    response = self.request('POST', '/' + index + '/_bulk',
                        ('\n'.join(lines) + '\n').encode(), deadline.remaining())
                    items = response.get('items')
                    if (response.get('errors') is not False or not isinstance(items, list)
                            or len(items) != len(batch)
                            or any(item.get('create', {}).get('status') != 201 for item in items)):
                        raise CandidateError('incomplete index build')
        for index in self._owned:
            response = self.request('POST', '/' + index + '/_refresh', None, deadline.remaining())
            if response.get('_shards', {}).get('failed') != 0:
                raise CandidateError('index refresh failed')
        self.ready = True

    def search(self, query: str, kb_id: str, timeout: float = 30) -> list[Hit]:
        _query_scope(query, kb_id)
        if not self.ready:
            raise CandidateError('candidate not ready')
        deadline = Deadline(timeout)
        exact = exact_fields(query)
        terms = [{'term': {name: value}} for name, values in exact.items() for value in values]
        if len(terms) > MAX_EXACT_TERMS:
            raise CandidateError('exact query bound exceeded')
        scope = [{'term': {'tenant_id': self.tenant_id}}, {'term': {'kb_id': kb_id}}]
        body = {'size': TOP_K, 'track_total_hits': False, 'collapse': {'field': 'doc_id'},
            '_source': ['tenant_id', 'kb_id', 'doc_id', 'parent_id'],
            'query': {'bool': {'filter': scope + terms,
                'must': [] if terms else [{'multi_match': {'query': query,
                    'fields': ['title^3', 'section_path^2', 'body'], 'type': 'best_fields'}}]}},
            'sort': [{'_score': 'desc'}, {'doc_id': 'asc'}, {'chunk_id': 'asc'}]}
        hits = _search_hits(self.request('POST', '/' + self.child_index + '/_search', body, deadline.remaining()))
        sources = []
        seen = set()
        for hit in hits:
            source = hit.get('_source', {})
            if source.get('tenant_id') != self.tenant_id or source.get('kb_id') != kb_id:
                raise CandidateLeak('out of scope child')
            if not all(isinstance(source.get(k), str) and source[k] for k in ('doc_id', 'parent_id')) or source['doc_id'] in seen:
                raise CandidateError('invalid collapsed child')
            seen.add(source['doc_id'])
            sources.append(source)
        if not sources:
            deadline.remaining()
            return []
        response = self.request('POST', '/' + self.parent_index + '/_mget',
            {'ids': [s['parent_id'] for s in sources]}, deadline.remaining())
        parents = response.get('docs')
        if not isinstance(parents, list) or len(parents) != len(sources):
            raise CandidateError('incomplete parent expansion')
        result = []
        byte_count = 0
        for source, parent in zip(sources, parents):
            p = parent.get('_source', {})
            if parent.get('found') is not True or parent.get('_id') != source['parent_id']:
                raise CandidateError('missing parent')
            if any(p.get(k) != source[k] for k in ('tenant_id', 'kb_id', 'doc_id')):
                raise CandidateLeak('out of scope parent')
            if not isinstance(p.get('text'), str) or not p['text']:
                raise CandidateError('invalid parent')
            byte_count += len(p['text'].encode())
            if byte_count > MAX_PARENT_BYTES:
                raise CandidateError('parent expansion bound exceeded')
            result.append(Hit(kb_id, source['doc_id'], p['text']))
        deadline.remaining()
        return result

    def close(self, timeout: float = 30) -> None:
        """Only delete indices whose creation this instance acknowledged."""
        self.ready = False
        failures = False
        for index in list(reversed(self._owned)):
            try:
                response = self.request('DELETE', '/' + index, None, timeout)
                if response.get('acknowledged') is not True:
                    raise CandidateError('index deletion unacknowledged')
                self._owned.remove(index)
            except Exception:
                failures = True
        if failures:
            raise CandidateError('cleanup failed; OPS reconciliation required')


class WeKnoraCandidate:
    """B: fixed-upstream manual ingestion + native hybrid-search only.

    Scoped temporary KB creation, source/image provenance, native config,
    credentials and cleanup belong to the OPS runner, never attest themselves
    from a client request. Caller provides *new empty* scoped KB bindings.
    """
    candidate_id = decision.CANDIDATE_B

    def __init__(self, request: Transport, kb_bindings: Mapping[str, str]):
        if (not kb_bindings or not set(kb_bindings) <= set(decision.LIBRARIES)
                or len(set(kb_bindings.values())) != len(kb_bindings)
                or not all(SAFE_ID.fullmatch(v) for v in kb_bindings.values())):
            raise CandidateError('nonempty distinct participating native KB bindings required')
        self.request = request
        self.kb_bindings = dict(kb_bindings)
        self.documents: dict[str, tuple[str, str]] = {}
        self._import_complete = False
        self.ready = False
        self._attempted = False

    def build(self, documents: Sequence[projection.SourceDocument], timeout: float = 3600) -> None:
        validate_documents(documents)
        if self._attempted:
            raise CandidateError('candidate build is single use')
        self._attempted = True
        deadline = Deadline(timeout)
        for doc in documents:
            response = self.request('POST', '/api/v1/knowledge-bases/' + self.kb_bindings[doc.kb_id] + '/knowledge/manual',
                {'title': doc.title, 'content': canonical_text(doc), 'status': 'publish'}, deadline.remaining())
            data = response.get('data', {})
            identifier = data.get('id')
            if (response.get('success') is not True or not isinstance(identifier, str)
                    or not SAFE_ID.fullmatch(identifier) or identifier in self.documents):
                raise CandidateError('invalid native ingestion receipt')
            self.documents[identifier] = (doc.kb_id, doc.doc_id)
        self._import_complete = True
        # One bounded pass. Pending native jobs are not success; OPS may call
        # check_ready again with the same imported corpus, never import twice.
        self.check_ready(deadline.remaining())

    def check_ready(self, timeout: float = 30) -> None:
        if not self._import_complete or not self.documents:
            raise CandidateError('candidate not imported')
        self.ready = False
        deadline = Deadline(timeout)
        for identifier, (kb_id, _) in self.documents.items():
            response = self.request('GET', '/api/v1/knowledge/' + identifier, None, deadline.remaining())
            data = response.get('data', {})
            if (response.get('success') is not True or data.get('id') != identifier
                    or data.get('knowledge_base_id') != self.kb_bindings[kb_id]):
                raise CandidateLeak('native ingestion scope mismatch')
            if data.get('parse_status') != 'completed':
                raise CandidateError('native ingestion not complete')
        deadline.remaining()
        self.ready = True

    def search(self, query: str, kb_id: str, timeout: float = 30) -> list[Hit]:
        _query_scope(query, kb_id)
        if not self.ready:
            raise CandidateError('candidate not ready')
        deadline = Deadline(timeout)
        response = self.request('POST', '/api/v1/knowledge-bases/' + self.kb_bindings[kb_id] + '/hybrid-search',
            {'query_text': query, 'match_count': TOP_K}, deadline.remaining())
        data = response.get('data')
        # The pinned Go handler serializes a native nil result slice as null.
        if response.get('success') is True and 'data' in response and data is None:
            data = []
        if response.get('success') is not True or not isinstance(data, list):
            raise CandidateError('invalid native search')
        # Preserve the native ranking and top-ten chunk budget, including
        # duplicate documents; do not silently overfetch to help B's score.
        result = []
        for item in data:
            reference = self.documents.get(item.get('knowledge_id'))
            if reference is None or reference[0] != kb_id:
                raise CandidateLeak('out of scope native hit')
            if not isinstance(item.get('content'), str):
                raise CandidateError('invalid native hit')
            result.append(Hit(kb_id, reference[1], item['content']))
        deadline.remaining()
        return result[:TOP_K]


@dataclass(frozen=True)
class Case:
    kb_id: str
    query: str = field(repr=False)
    expected: frozenset[str] = field(repr=False)
    exact: bool = False


def score_cases(candidate: Any, cases: Sequence[Case], timeout: float = 30, *,
                expected_libraries=None, before_first_search=None) -> dict[str, dict]:
    """Private OPS-only scoring, not a complete/attested decision report.

    One query is one trial. No warmup queries are generated from this holdout.
    Errors remain in denominators and latency samples; exceptions/hits are
    discarded without serializing details. Resource/Gateway/freeze evidence
    must be measured independently, never populated with successful defaults.
    """
    Deadline(timeout)
    libraries = tuple(decision.LIBRARIES if expected_libraries is None else expected_libraries)
    if (not libraries or len(set(libraries)) != len(libraries)
            or any(kb not in decision.LIBRARIES for kb in libraries)):
        raise CandidateError('invalid expected scoring libraries')
    cases = tuple(cases)
    metrics = {kb: {name: 0 for name in decision.COUNT_FIELDS} for kb in libraries}
    latencies: dict[str, list[float]] = {kb: [] for kb in libraries}
    seen = set()
    for case in cases:
        _query_scope(case.query, case.kb_id)
        if case.kb_id not in metrics:
            raise CandidateError('unexpected scoring library')
        if (not isinstance(case.expected, frozenset) or not all(isinstance(v, str) and v for v in case.expected)
                or type(case.exact) is not bool or (case.exact and not case.expected)
                or (case.kb_id, case.query) in seen):
            raise CandidateError('invalid scoring cases')
        seen.add((case.kb_id, case.query))
        m = metrics[case.kb_id]
        m['total_count'] += 1
        m['answerable_count' if case.expected else 'no_answer_count'] += 1
        m['exact_count'] += int(case.exact)
    if any(min(m['answerable_count'], m['exact_count'], m['no_answer_count']) <= 0 for m in metrics.values()):
        raise CandidateError('missing scoring category')
    for index, case in enumerate(cases):
        m = metrics[case.kb_id]
        # Fail outside the scoring exception handler: a failed durable exposure
        # must abort without a candidate call or a fabricated system-error score.
        if index == 0 and before_first_search is not None:
            before_first_search()
        start = time.monotonic()
        try:
            hits = candidate.search(case.query, case.kb_id, timeout=timeout)
            if time.monotonic() - start > timeout:
                raise CandidateTimeout('request deadline exceeded')
            if not isinstance(hits, list) or len(hits) > TOP_K or any(not isinstance(h, Hit) or not h.doc_id for h in hits):
                raise CandidateError('invalid scored response')
            if any(h.kb_id != case.kb_id for h in hits):
                raise CandidateLeak('out of scope scored hit')
            matched = bool(case.expected.intersection(h.doc_id for h in hits))
            m['recall_hits_at_10'] += int(matched)
            m['exact_hits'] += int(case.exact and matched)
            m['no_answer_correct'] += int(not case.expected and not hits)
        except Exception as exc:
            m['system_error_count'] += 1
            m['answerable_system_error_count' if case.expected else 'no_answer_system_error_count'] += 1
            m['exact_system_error_count'] += int(case.exact)
            m['timeout_count'] += int(isinstance(exc, (CandidateTimeout, TimeoutError)))
            m['leak_count'] += int(isinstance(exc, CandidateLeak))
        finally:
            latencies[case.kb_id].append((time.monotonic() - start) * 1000)
    for kb, m in metrics.items():
        m['recall_at_10'] = m['recall_hits_at_10'] / m['answerable_count']
        m['exact'] = m['exact_hits'] / m['exact_count']
        m['no_answer'] = m['no_answer_correct'] / m['no_answer_count']
        samples = sorted(latencies[kb])
        m['p95_ms'] = samples[math.ceil(.95 * len(samples)) - 1]
    return metrics


def score_library_cases(candidate: Any, cases: Sequence[Case], library: str, timeout: float = 30, *, before_first_search=None) -> dict[str, dict]:
    """Validate all categories of exactly one library before any exposure/call."""
    return score_cases(candidate, cases, timeout, expected_libraries=(library,),
                       before_first_search=before_first_search)
