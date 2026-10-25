"""The existing interview-brief generator, owned by the career report lifecycle."""
from __future__ import annotations


def prepare(conn, data, payload, report_id):
    from applypilot.impact.brief import generate_interview_brief, load_cached_brief, save_brief_cache
    from applypilot.impact.defense import build_defense_facts
    from applypilot.impact.pipeline import claim_pack_for_job
    from applypilot.impact.ledger import default_repository

    row = conn.execute('SELECT * FROM jobs WHERE url=?', (data.get('job_url', ''),)).fetchone()
    job = dict(row) if row else {'url': '', 'company': data.get('company_name', ''),
                               'full_description': data.get('job_description', '')}
    job['interview_context'] = {'sources': payload['sources'], 'request': data}
    try:
        pack = claim_pack_for_job(job) if row else None
        record = (default_repository().get(pack.records[0].record.id) or pack.records[0].record
                  if pack and pack.records else None)
        facts = build_defense_facts(record, job=job) if record else None
    except (ValueError, RuntimeError):
        record = facts = None
    brief = load_cached_brief(record, job, report_id=report_id) if row else None
    if not brief:
        brief = generate_interview_brief(facts, record, job=job, seniority='senior')
    result = {'brief': brief, 'sources': payload['sources'], 'input_revisions': payload['input_revisions'],
              'generated_at': payload['as_of'], 'draft_only': True}
    if row:
        result['artifact'] = str(save_brief_cache(record, job, brief, report_id=report_id))
    return result
