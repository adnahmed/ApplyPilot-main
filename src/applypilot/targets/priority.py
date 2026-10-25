"""Persistent weighted turns; callers retain admission and allocation ownership."""
from collections import deque

from applypilot.discovery.repository import transaction

TURNS = ("high", "normal", "high", "low", "high", "normal", "high")


def job_priority(conn, url):
    row = conn.execute("""SELECT MIN(CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END)
        FROM company_targets t JOIN company_target_jobs l ON l.target_id=t.id
        WHERE l.job_url=? AND t.state='active'""", (url,)).fetchone()
    return ("high", "normal", "low")[row[0]] if row and row[0] is not None else "normal"


def select(conn, rows, limit, *, lane, priority=None, consume=False):
    rows = list(rows)
    if not rows or not limit:
        return []
    groups = {name: deque() for name in ("high", "normal", "low")}
    priorities = {}
    if priority is None:
        priorities = {row[0]: ('high','normal','low')[row[1]] for row in conn.execute("""SELECT l.job_url,
            MIN(CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END)
            FROM company_targets t JOIN company_target_jobs l ON l.target_id=t.id
            WHERE t.state='active' GROUP BY l.job_url""")}
    for row in rows:
        key = priority(row) if priority else priorities.get(row['url'], 'normal')
        groups[key if key in groups else "normal"].append(row)
    position = 0
    output = []
    while len(output) < limit and any(groups.values()):
        key = TURNS[position % len(TURNS)]
        position += 1
        if groups[key]:
            output.append(groups[key].popleft())
    return output


def case_priority(conn, case_id):
    case = conn.execute("SELECT target_id,job_url FROM referral_cases WHERE id=?", (case_id,)).fetchone()
    if case and case[0]:
        target = conn.execute("SELECT priority FROM company_targets WHERE id=?", (case[0],)).fetchone()
        return target[0] if target else "normal"
    return job_priority(conn, case[1]) if case and case[1] else "normal"


def consume_job(conn, url, lane='apply'):
    """Compatibility no-op: selection priority is business policy, not stored worker credit."""
    return job_priority(conn, url)
