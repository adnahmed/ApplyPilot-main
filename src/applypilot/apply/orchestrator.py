"""Retired application worker entry points.

Applications are durable ApplicationWorkflow entities. Use the explicit Temporal
client run_job() for a start and apply.service for finite Activity operations.
Legacy CLI/dashboard routing is intentionally deferred to migration Part 2.
"""
from __future__ import annotations


def _retired(*args, **kwargs):
    raise RuntimeError("Application worker loops are retired; start a Temporal JobWorkflow with orchestration.client.run_job")


main = _retired
worker_loop = _retired
enqueue_prepared_job = _retired
