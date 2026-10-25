"""Compatibility entry points; core jobs/documents are native Temporal operations.

Company-wide and referral callers remain outside Part 1. Core legacy stage and
recovery entry points fail closed rather than creating a second execution owner.
"""
from __future__ import annotations


def _retired(*args, **kwargs):
    raise RuntimeError("Legacy core stage execution is retired; use JobWorkflow/JobDocumentWorkflow")


run_stage_job = _retired
convert_document = _retired
recover_due = _retired


def maintain_evidence(options):
    from applypilot.orchestration.job_domain import maintain_business_evidence
    result = maintain_business_evidence(options, cursor=options.get('repair_cursor', ''))
    options['repair_cursor'] = result.next_cursor or ''
    return dict(result.detail)


def run_company_job(job_url, options):
    import asyncio
    from applypilot.orchestration.client import run_job
    return asyncio.run(run_job(job_url, {**options, "job_phases": ["company_research"]}))


def prepare_referrals(options):
    import asyncio
    from applypilot.orchestration.client import run_work
    return asyncio.run(run_work("referrals"))
