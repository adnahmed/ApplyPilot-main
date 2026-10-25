"""Search observations and immutable evidence, not runnable tasks or ownership.

Initialization is deployment-only. Callers own transactions and must commit an
observation before submitting its reference to Temporal. Provider I/O never runs
inside these transactions. This module does not import Temporal or any scheduler.
"""
from __future__ import annotations

import json
from . import store
from .search_contracts import SearchActivityInput, SearchEvidence, SearchRequest, PROVIDERS


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS search_observations (
        observation_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES temporal_inputs(input_id),
        providers_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS search_evidence (
        observation_id TEXT NOT NULL REFERENCES search_observations(observation_id),
        provider TEXT NOT NULL,
        request_id TEXT NOT NULL REFERENCES temporal_inputs(input_id),
        evidence_id TEXT NOT NULL REFERENCES temporal_inputs(input_id),
        result_count INTEGER NOT NULL CHECK (result_count >= 0),
        observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(observation_id, provider)
    )""")
    conn.commit()


def capture(conn, request: SearchRequest, observation_id: str, providers=PROVIDERS) -> str:
    """Bind a caller's idempotency key to exactly one immutable business request."""
    from dataclasses import asdict
    request.validate()
    providers = tuple(sorted(providers))
    if not providers or len(set(providers)) != len(providers):
        raise ValueError('Choose unique supported search providers')
    reference = store.put(conn, {'kind': 'search_request', 'request': asdict(request)})
    for provider in providers:
        SearchActivityInput(reference, observation_id, provider).validate()
    encoded = json.dumps(providers, separators=(',', ':'))
    conn.execute('INSERT INTO search_observations(observation_id,request_id,providers_json) '
        'VALUES(?,?,?) ON CONFLICT(observation_id) DO NOTHING', (observation_id, reference, encoded))
    row = conn.execute('SELECT request_id,providers_json FROM search_observations WHERE observation_id=?',
                       (observation_id,)).fetchone()
    if row is None or row[0] != reference or row[1] != encoded:
        raise ValueError('Observation identity already belongs to a different search request or provider set')
    return reference


def validate_observation(conn, request: SearchActivityInput):
    request.validate()
    row = conn.execute('SELECT request_id,providers_json FROM search_observations WHERE observation_id=?',
                       (request.observation_id,)).fetchone()
    if row is None:
        raise LookupError('Search observation is missing')
    if row[0] != request.request_id or request.provider not in json.loads(row[1]):
        raise ValueError('Search observation does not match its immutable request')


def stored(conn, request: SearchActivityInput) -> SearchEvidence | None:
    row = conn.execute('SELECT request_id,evidence_id,result_count FROM search_evidence '
                       'WHERE observation_id=? AND provider=?',
                       (request.observation_id, request.provider)).fetchone()
    if row is None:
        return None
    if row[0] != request.request_id:
        raise ValueError('Stored search evidence does not match its request')
    return SearchEvidence(request.provider, row[1], row[2])


def read_request(conn, request: SearchActivityInput) -> tuple[SearchRequest, SearchEvidence | None]:
    validate_observation(conn, request)
    body = store.get(conn, request.request_id)
    if body.get('kind') != 'search_request':
        raise ValueError('The supplied reference is not a search request')
    result = SearchRequest(**body['request']).validate()
    return result, stored(conn, request)


def save_evidence(conn, request: SearchActivityInput, hits) -> SearchEvidence:
    validate_observation(conn, request)
    if any(hit.provider != request.provider for hit in hits):
        raise ValueError('Provider evidence must retain its real provenance')
    cached = stored(conn, request)
    if cached is not None:
        return cached
    evidence_id = store.put(conn, {'kind': 'search_evidence', 'request_id': request.request_id,
        'provider': request.provider, 'hits': [hit.to_dict() for hit in hits]})
    conn.execute('INSERT INTO search_evidence(observation_id,provider,request_id,evidence_id,result_count) '
        'VALUES(?,?,?,?,?) ON CONFLICT(observation_id,provider) DO NOTHING',
        (request.observation_id, request.provider, request.request_id, evidence_id, len(hits)))
    # Concurrent read-only retries may both have fetched. Return the committed
    # winner; this is evidence identity, not a reservation or a worker claim.
    result = stored(conn, request)
    if result is None:
        raise RuntimeError('Search evidence transaction did not persist its result')
    return result


def observation(conn, observation_id: str, *, include_hits: bool = False) -> dict:
    """Business evidence only; an absent result is NOT proof that work is running."""
    # Reuse identifier validation without creating a request or doing any I/O.
    SearchActivityInput('0' * 64, observation_id, 'ddgs').validate()
    row = conn.execute('SELECT request_id,providers_json FROM search_observations WHERE observation_id=?',
                       (observation_id,)).fetchone()
    if row is None:
        raise LookupError('Search observation is missing')
    results = []
    for provider in json.loads(row[1]):
        saved = stored(conn, SearchActivityInput(row[0], observation_id, provider))
        result = {'provider': provider, 'state': 'no_evidence_recorded'}
        if saved is not None:
            result.update(state=saved.state, evidence_id=saved.evidence_id, count=saved.count)
            if include_hits:
                result['hits'] = store.get(conn, saved.evidence_id)['hits']
        results.append(result)
    return {'observation_id': observation_id, 'request_id': row[0], 'providers': results,
            'workflow_status_checked': False}
