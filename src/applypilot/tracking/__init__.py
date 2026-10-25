"""ApplyPilot application response tracking.

Monitors Gmail for responses to job applications, classifies them with AI,
and maintains per-job tracking documents with timelines and action items.
"""

from applypilot import operations as ops

import logging

from rich.console import Console
from rich.table import Table

from applypilot.database import (
    commit_with_retry,
    get_applied_jobs,
    get_connection,
    get_action_items,
    get_tracking_stats,
)

log = logging.getLogger(__name__)
console = Console()


def show_action_items() -> None:
    """Display pending action items as a Rich table."""
    items = get_action_items()
    if not items:
        console.print("[dim]No pending action items.[/dim]")
        return

    table = Table(title="Pending Action Items", show_header=True, header_style="bold cyan")
    table.add_column("Due", style="bold")
    table.add_column("Company")
    table.add_column("Title", max_width=25)
    table.add_column("Action", max_width=35)
    table.add_column("Status")

    for item in items:
        due = item["next_action_due"] or "N/A"
        company = item["company"] or "Unknown"
        title = (item["title"] or "Untitled")[:25]
        action = (item["next_action"] or "")[:35]
        status = item["tracking_status"] or ""
        table.add_row(due, company, title, action, status)

    console.print(table)


def remap_stubs(conn=None) -> dict:
    """Re-match emails under multi-company stubs to correct per-company stubs/jobs.

    Identifies stub jobs where multiple distinct companies' emails were collapsed
    together (e.g., 11 emails for 11 different companies all under one Greenhouse
    stub). For each such stub:
      1. Re-runs match_email_to_job() with the improved matcher against all applied jobs
      2. If a pipeline match is found: moves the email to that job
      3. If still unmatched: creates a proper per-company stub via create_stub_job()
      4. Deletes stub jobs that have zero emails remaining

    Returns:
        {remapped: int, new_stubs: int, deleted_stubs: int}
    """
    from applypilot.tracking.matcher import match_email_to_job, extract_company_from_subject
    from applypilot.database import (
        get_applied_jobs,
        create_stub_job,
        update_tracking_status,
    )

    if conn is None:
        conn = get_connection()

    applied_jobs = get_applied_jobs(conn)

    # Find all stub job_urls that have more than one email
    stub_rows = conn.execute("""
        SELECT job_url, COUNT(*) AS email_count
        FROM tracking_emails
        WHERE job_url LIKE 'manual://%'
        GROUP BY job_url
        HAVING COUNT(*) > 1
    """).fetchall()

    if not stub_rows:
        console.print("[dim]No multi-email stubs found to remap.[/dim]")
        return {"remapped": 0, "new_stubs": 0, "deleted_stubs": 0}

    console.print(f"  Found {len(stub_rows)} multi-email stubs to inspect")

    remapped = 0
    new_stubs = 0
    affected_job_urls: set[str] = set()

    for stub_row in stub_rows:
        stub_url = stub_row["job_url"]

        # Fetch all emails under this stub
        emails = conn.execute("""
            SELECT email_id, sender, sender_name, subject, snippet,
                   received_at, classification, body_text
            FROM tracking_emails WHERE job_url = ?
        """, (stub_url,)).fetchall()

        # Check how many distinct companies are mentioned across subjects
        companies = set()
        for e in emails:
            c = extract_company_from_subject(e["subject"] or "")
            if c:
                companies.add(c.lower())

        if len(companies) <= 1:
            # All emails plausibly about the same company — leave as-is
            continue

        console.print(f"  Remapping stub {stub_url[:60]} ({len(emails)} emails, "
                      f"{len(companies)} companies: {', '.join(sorted(companies)[:5])})")

        # Exclude the current stub so emails can't just re-match back to it
        jobs_without_stub = [j for j in applied_jobs if j["url"] != stub_url]

        for email_row in emails:
            email_dict = {
                "id": email_row["email_id"],
                "sender": email_row["sender"] or "",
                "sender_name": email_row["sender_name"] or "",
                "subject": email_row["subject"] or "",
                "snippet": email_row["snippet"] or "",
                "date": email_row["received_at"] or "",
                "body": email_row["body_text"] or "",
            }
            classification = email_row["classification"] or "confirmation"

            # Try improved matcher (excluding the current stub)
            match = match_email_to_job(email_dict, jobs_without_stub)
            if match:
                new_url = match["job_url"]
                log.info("  remap: %s -> pipeline job %s (score=%d)",
                         email_row["email_id"], new_url[:50], match["score"])
            else:
                # Create a proper per-company stub
                new_url = create_stub_job(email_dict, classification, conn)
                if new_url != stub_url:
                    new_stubs += 1
                    log.info("  remap: %s -> new stub %s",
                             email_row["email_id"], new_url[:50])

            if new_url != stub_url:
                conn.execute(
                    "UPDATE tracking_emails SET job_url = ? WHERE email_id = ?",
                    (new_url, email_row["email_id"]),
                )
                affected_job_urls.add(new_url)
                remapped += 1

        commit_with_retry(conn)

    # Re-compute tracking_status for all affected jobs
    for job_url in affected_job_urls:
        rows = conn.execute(
            "SELECT classification FROM tracking_emails WHERE job_url = ? "
            "ORDER BY received_at DESC",
            (job_url,),
        ).fetchall()
        for row in rows:
            if row["classification"]:
                update_tracking_status(job_url, row["classification"], conn)

    # Delete stub jobs that now have zero emails
    orphans = conn.execute("""
        SELECT url FROM jobs
        WHERE url LIKE 'manual://%'
          AND url NOT IN (SELECT DISTINCT job_url FROM tracking_emails)
    """).fetchall()
    deleted_stubs = 0
    for orphan in orphans:
        conn.execute("DELETE FROM jobs WHERE url = ?", (orphan["url"],))
        deleted_stubs += 1
    if deleted_stubs:
        commit_with_retry(conn)

    console.print(f"  Remapped {remapped} emails to correct jobs/stubs")
    console.print(f"  Created {new_stubs} new per-company stubs")
    console.print(f"  Deleted {deleted_stubs} empty stub jobs")
    return {"remapped": remapped, "new_stubs": new_stubs, "deleted_stubs": deleted_stubs}


def relabel_all_tracked(conn=None) -> int:
    """Apply 'ap-track' Gmail label to all emails stored in tracking_emails.

    Used for one-time backfills and to catch emails tracked before the label
    feature existed. Safe to run repeatedly — Gmail ignores duplicate label adds.

    Returns:
        Count of emails submitted for labeling.
    """
    import asyncio
    from applypilot.tracking.gmail_client import apply_label_to_emails

    if conn is None:
        conn = get_connection()

    rows = conn.execute("SELECT email_id FROM tracking_emails").fetchall()
    email_ids=[]
    for row in rows:
        email_id=row['email_id']
        if email_id.startswith('gmail:'):
            mapped=conn.execute('SELECT message_id FROM feedback_messages WHERE message_key=?',(email_id[6:],)).fetchone()
            if mapped:
                email_ids.append(mapped[0])
        else:
            email_ids.append(email_id)

    if not email_ids:
        console.print("[dim]No tracked emails found in DB.[/dim]")
        return 0

    console.print(f"  Applying 'ap-track' to {len(email_ids)} tracked emails...")
    count = asyncio.run(apply_label_to_emails(email_ids))
    console.print(f"  [green]Labeled {count} emails.[/green]")
    return count


def run_tracking(days: int = 14, ghosted_days: int | None = None,
                 dry_run: bool = False, *, since: str | None = None) -> dict:
    """Client entry point. All mailbox execution belongs to Temporal Workflows."""
    import asyncio
    from applypilot.orchestration.client import run_mailbox
    if type(days) is not int or days < 1:
        raise ValueError('days must be a positive integer')
    options = {'track_days': days, 'track_since': since, 'dry_run': dry_run}
    if ghosted_days is not None:
        if type(ghosted_days) is not int or ghosted_days < 1:
            raise ValueError('ghosted_days must be a positive integer')
        options['silence_days'] = ghosted_days
    result = asyncio.run(run_mailbox(options))
    console.print(result)
    return result
