"""Bound preparatory output; consumers and outcome services never acquire buffer slots."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from applypilot.backlog import validate_backlog


LABELS = {
    "score": "Jobs awaiting scoring",
    "qualified": "Qualified jobs awaiting tailoring",
    "cover": "Resumes awaiting cover letters",
    "documents": "Approved documents awaiting rendering",
    "apply": "Prepared application packages",
}
CAPACITY_BASIS = "automatic-recovery-v1"


def inventory(conn, *, min_score, max_age_days, doc_format):
    from applypilot.database import preparation_inventory, preparation_stock_sql
    from applypilot.apply.queue import delivery_blocker, list_actionable_jobs, queue_checks
    from applypilot.apply.guard import upload_artifact_paths
    from applypilot.scoring.pdf import pending_conversions
    from applypilot.integrations.research import posting_suitability_sql

    result = {
        name: preparation_inventory(conn, stage, min_score=min_score, max_age_days=max_age_days)
        for name, stage in (("score", "score"), ("qualified", "tailor"), ("cover", "cover"))
    }
    for name, producer in (("score", "enrich"), ("qualified", "score"), ("cover", "tailor")):
        result[name]["_producer_ids"] = {producer: result[name].pop("_identities")}
    document_sources = {str(source.resolve()) for source in pending_conversions(doc_format, inventory=True)}
    ready_documents = len(pending_conversions(doc_format))
    documents = len(document_sources)
    document_producers = {"tailor": set(), "cover": set()}
    for row in conn.execute("SELECT url,tailored_resume_path,cover_letter_path FROM jobs "
                            "WHERE tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL"):
        for stage, field in (("tailor", "tailored_resume_path"), ("cover", "cover_letter_path")):
            if row[field] and str(Path(row[field]).with_suffix(".txt").resolve()) in document_sources:
                document_producers[stage].add(row["url"])
    result["documents"] = {"stock": documents, "ready_now": ready_documents,
                           "held_stock": max(0, documents - ready_documents),
                           "deferred": max(0, documents - ready_documents),
                           "_producer_ids": document_producers}
    # Keep prepared output visible even while temporary delivery guards block it.
    checks = queue_checks(min_score, None, max_age_days, None, conn=conn)
    cases = " ".join(f"WHEN NOT COALESCE(({sql}),FALSE) THEN '{code}'" for code, sql, _ in checks)
    params = [item for _, _, values in checks for item in values]
    capacity_checks = [check for check in checks
                       if check[0] not in {"retry_backoff", "automatic_recovery_pending", "referral_hold"}]
    capacity_checks = [(code, "(" + sql + " OR NOT " + posting_suitability_sql("j", require_suitable=False) + ")", values)
                       if code == "company_posting_suitability" else (code, sql, values)
                       for code, sql, values in capacity_checks]
    capacity_sql = " AND ".join(f"COALESCE(({sql}),FALSE)" for _, sql, _ in capacity_checks)
    capacity_params = [item for _, _, values in capacity_checks for item in values]
    rows = conn.execute(
        "SELECT r.target AS application_url,j.url,j.title,j.company,j.site,j.strategy,"
        "j.tailored_resume_path,j.cover_letter_path,j.application_contract_json,j.full_description,r.route AS application_route,"
        "j.posting_suitability_json,j.company_research_revision,j.discovery_content_hash,"
        "j.discovery_source_revision,j.discovery_context_key,"
        "EXISTS (SELECT 1 FROM recovery_work rw WHERE rw.job_url=j.url AND rw.stage='apply' "
        "AND rw.status IN ('pending','running') AND rw.retry_trigger='condition_change') AS condition_held,"
        f"CASE {cases} ELSE NULL END AS queue_blocker, ({capacity_sql}) AS capacity_eligible FROM jobs j "
        "LEFT JOIN application_routes r ON r.job_url=j.url WHERE " + preparation_stock_sql() +
        " AND j.fit_score>=? AND (NULLIF(TRIM(j.tailored_resume_path),'') IS NOT NULL OR "
        "NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,resume,status}'='prohibited')",
        (*params, *capacity_params, min_score)).fetchall()
    prepared = []
    for row in rows:
        try:
            from applypilot.apply.application_contract import selected_documents
            documents = selected_documents(dict(row))
            upload_artifact_paths(documents["resume"], documents["cover_letter"], doc_format, require_resume=False)
        except (OSError, ValueError):
            continue
        prepared.append(row)
    # Timed retries still occupy the bounded buffer. Jobs awaiting an external
    # condition/configuration change are retained separately: they cannot drain
    # through the consumer and must not permanently stop unrelated preparation.
    packages = list_actionable_jobs(conn, min_score=min_score, max_age_days=max_age_days,
                                   doc_format=doc_format, limit=len(rows))
    package_jobs = {job["url"] for job in packages}
    prepared_jobs = {row["url"] for row in prepared}
    package_sources = {
        str(Path(job[field]).with_suffix(".txt").resolve())
        for job in prepared for field in ("tailored_resume_path", "cover_letter_path") if job[field]
    }
    blocked = [row for row in prepared if row["url"] not in package_jobs]
    blocker = delivery_blocker(conn, doc_format) if blocked else None
    capacity_blocker = delivery_blocker(conn, doc_format, check_suitability=False) if blocked else None
    reasons = Counter()
    samples = []
    held_jobs = set()
    from applypilot.operations.worker_journal import job_id
    for row in blocked:
        # A later explanation cannot attribute allocation versus a changed snapshot.
        reason = row["queue_blocker"] or blocker(row) or "allocation_or_snapshot_wait"
        reasons[reason] += 1
        if (not row['capacity_eligible'] or row['condition_held']
                or (capacity_blocker(row) not in {None, 'target_circuit_open'})):
            held_jobs.add(row['url'])
        if len(samples) < 12:
            samples.append({"job_id": job_id(row["url"]), "reason": reason})
    result["apply"] = {"stock": len(prepared_jobs), "consumable_stock": len(package_jobs),
                       "held_stock": len(held_jobs),
                       "ready_now": len(package_jobs), "deferred": len(blocked),
                       "prepared_stock": len(prepared), "prepared_blockers": dict(reasons),
                       "prepared_blocker_samples": samples,
                       "prepared_blocker_samples_truncated": len(blocked) > len(samples),
                       "_producer_ids": {
        "tailor": prepared_jobs, "cover": prepared_jobs, "pdf": package_sources}}
    return result


class PreparationBuffers:
    def __init__(self, policy, ordered, *, root, context_key, doc_format):
        from applypilot.runtime_policy import load_policy
        self.policy = load_policy(root)
        flow = validate_backlog(policy.get("flow_control"),
                                defaults={name: row.model_dump() for name, row in self.policy.buffers.items()},
                                default_mode=self.policy.buffer_mode)
        self.mode = flow["mode"]
        self.limits = (flow["buffers"] if self.mode == "watermarks" else
                       {name: {"low": None, "high": None} for name in flow["buffers"]})
        self.context_key = context_key
        self.doc_format = doc_format
        self.destinations = self.policy.destinations(ordered)
        self.rows = {}
        self.paused = {}
        self._producer_ids = {}
        self._reservations = {}
        from applypilot.live_status import read_component
        previous = read_component(root, "preparation", latest=True) or {}
        previous_buffers = previous.get("buffers")
        if previous.get("buffer_context_key") == context_key and isinstance(previous_buffers, dict):
            for name, row in previous_buffers.items():
                if (name in self.limits and isinstance(row, dict)
                        and self.mode == "watermarks" and row.get("mode", "watermarks") == self.mode
                        and row.get("capacity_basis") == CAPACITY_BASIS
                        and (name != "apply" or "held_stock" in row)
                        and all(row.get(key) == value for key, value in self.limits[name].items())):
                    self.paused[name] = row.get("hysteresis_paused") is True

    def refresh(self, conn, pending, *, min_score, max_age_days, repairs=()):
        stock = inventory(conn, min_score=min_score, max_age_days=max_age_days, doc_format=self.doc_format)
        self.apply_inventory(stock, pending, repairs=repairs)

    def apply_inventory(self, stock, pending, *, repairs=()):
        self._producer_ids = {name: values.pop("_producer_ids", {}) for name, values in stock.items()}
        self._reservations = {name: set() for name in self.limits}
        self.rows = {name: {"name": name, "label": LABELS[name], "mode": self.mode,
                            "capacity_basis": CAPACITY_BASIS, **limits, **stock[name],
                            "prepared": 0, "available": True} for name, limits in self.limits.items()}
        # Rebuild the full occupancy before deciding whether a paused buffer drained.
        for stage, identity in pending.values():
            if (stage, identity) not in repairs:
                self.reserve(stage, identity, update=False)
        for name in self.rows:
            self._update(name)

    def unavailable(self, reason):
        self.rows = {name: {"name": name, "label": LABELS[name], "mode": self.mode, **limits, "available": False,
                            "paused": self.mode == "watermarks", "reason": reason} for name, limits in self.limits.items()}

    def _update(self, name):
        row = self.rows[name]
        occupancy = row["stock"] - row.get("held_stock", 0) + row["prepared"]
        if self.mode == "automatic":
            self.paused[name] = False
            row.update(occupancy=occupancy, paused=False, hysteresis_paused=False,
                       reason=f"{row['label']}: {row['stock']} saved; preparation follows available worker capacity.")
            return
        was_paused = self.paused.get(name, False)
        paused = occupancy >= row["high"] or (was_paused and occupancy > row["low"])
        self.paused[name] = paused
        row.update(occupancy=occupancy, paused=paused, hysteresis_paused=paused,
                   reason=(f"{row['label']}: {occupancy}/{row['high']}; draining to {row['low']} before refilling." if paused
                           else f"{row['label']}: {occupancy}/{row['high']} slots occupied."))

    def reserve(self, stage, identity=None, *, update=True):
        if stage == "pdf" and identity is not None:
            identity = str(Path(identity).resolve())
        for name in self.destinations.get(stage, ()):
            if not self.rows.get(name, {}).get("available"):
                continue
            represented = self._producer_ids.get(name, {}).get(stage, ())
            key = (stage, identity)
            if identity is not None and (identity in represented or key in self._reservations[name]):
                continue
            self._reservations[name].add(key)
            self.rows[name]["prepared"] += 1
            if update:
                self._update(name)

    def headroom(self, stage, capacity):
        capacity = max(0, capacity)
        if self.mode == "automatic":
            return capacity
        for name in self.destinations.get(stage, ()):
            row = self.rows.get(name, {})
            if not row.get("available") or row.get("paused"):
                return 0
            capacity = min(capacity, max(0, row["high"] - row["occupancy"]))
        return capacity

    def stage_buffers(self, stage):
        return {name: self.rows[name] for name in self.destinations.get(stage, ()) if name in self.rows}
