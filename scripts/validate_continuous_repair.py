#!/usr/bin/env python3
"""Offline regression checks for the continuous-engine/support-download repair.

Uses a disposable workspace, synthetic jobs and stubbed stage/network runners.
Never connects a provider, browser, live Redis server, application or outreach account.
Run from the source checkout: python scripts/validate_continuous_repair.py --report repair-validation.json
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
from applypilot import postgres
import sys
import tempfile
import threading
import time
import types
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    checks = []

    def check(name, condition, **details):
        checks.append({"name": name, "passed": bool(condition), **details})
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        if not condition:
            raise AssertionError(name)

    def until(predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    with tempfile.TemporaryDirectory(prefix="applypilot-offline-validation-") as directory:
        root = Path(directory)
        # Unconditionally isolate BEFORE importing any project modules.
        os.environ["APPLYPILOT_DIR"] = str(root)
        os.environ["APPLYPILOT_RUNTIME_ID"] = "offline-validation"
        from applypilot import config, pipeline, operations as ops
        from applypilot.database import init_db, count_jobs_by_stage
        from applypilot.discovery.context import build_context, _thaw
        from applypilot.discovery.repository import activate_context, DiscoveryCycleBusy
        from applypilot.discovery.diagnostics import scoring_breakdown
        from applypilot.continuous import run_continuous
        from applypilot.operations.bundle import build_bundle, Limits

        conn = init_db()
        check("workspace_is_disposable", config.APP_DIR == root)
        ctx = build_context(raw_config={}, profile={}, runtime={})
        key = activate_context(ctx, conn, refresh_limit=0)
        conn.execute("INSERT INTO discovery_cycle_lease VALUES(1,?,?,?,?)",
                     ("source-owner", ctx.cycle_id, time.time()+60, "synthetic"))
        conn.commit()
        before = tuple(conn.execute("SELECT * FROM discovery_cycle_lease").fetchone())
        other = replace(ctx, cycle_id="preparation-reader")
        check("same_context_preparation_not_blocked_by_discovery",
              activate_context(other, conn, refresh_limit=0, share_current=True) == key)
        check("sharing_does_not_steal_discovery_lease",
              tuple(conn.execute("SELECT * FROM discovery_cycle_lease").fetchone()) == before)
        modified = _thaw(ctx.runtime)
        modified["admission"]["allow_off_portfolio_prepare"] = not modified["admission"]["allow_off_portfolio_prepare"]
        blocked = False
        try:
            activate_context(replace(other, runtime=modified), conn, refresh_limit=0, share_current=True)
        except DiscoveryCycleBusy:
            blocked = True
        check("different_policy_cannot_share_or_steal_lease", blocked)
        modified = _thaw(ctx.runtime)
        modified["rollout_mode"] = "shadow"
        blocked = False
        try:
            activate_context(replace(other, runtime=modified), conn, refresh_limit=0, share_current=True)
        except DiscoveryCycleBusy:
            blocked = True
        check("same_key_but_different_runtime_policy_is_not_silently_ignored", blocked)
        conn.execute("DELETE FROM discovery_cycle_lease")
        conn.execute("INSERT INTO jobs(url,title,company,full_description,detail_scraped_at,discovered_at) "
                     "VALUES('https://example.invalid/job','Synthetic role','Synthetic company',"
                     "'Synthetic description',datetime('now'),datetime('now'))")
        conn.commit()
        check("legacy_enriched_row_can_have_zero_eligible_score_queue",
              count_jobs_by_stage(conn, stage="score", max_age_days=7) == 0)
        reasons = scoring_breakdown(conn)
        check("zero_queue_explains_missing_source_observation",
              reasons["raw_candidates"] == 1 and reasons["reasons"] == [{"reason": "missing_source_observation", "jobs": 1}])
        conn.close()

        # All external work is stubbed; the scheduler and stop/wakeup mechanisms are real.
        @dataclass(frozen=True)
        class FakeContext:
            runtime: dict
            cycle_id: str = "offline"

        fake_context = FakeContext({})
        ordered = ["discover", "enrich", "score", "tailor", "cover", "pdf", "brief"]
        stop = threading.Event()
        release = threading.Event()
        discovery_started = threading.Event()
        counts = {s: 0 for s in ordered}
        queues = {s: (0 if s in {"pdf", "brief"} else 100) for s in ordered}
        calls = {s: 0 for s in ordered}
        lock = threading.Lock()
        observed_workers = {}
        outcome = {}

        def pending(stage, *a, **kw):
            with lock:
                return queues[stage]

        def runner(stage):
            def run(**kw):
                if stage in {"discover", "enrich"}:
                    if stage == "discover":
                        discovery_started.set()
                    release.wait(8)
                    return {"status": "ok", "processed": 0}
                with lock:
                    observed_workers[stage] = kw.get("workers", 1)
                    n = min(5, queues[stage])
                    queues[stage] -= n
                    counts[stage] += n
                    calls[stage] += 1
                time.sleep(0.002)
                return {"status": "ok", "processed": n}
            return run

        stubs = {s: runner(s) for s in ordered}
        def invoke():
            try:
                outcome["result"] = run_continuous(ordered, min_score=8, max_age_days=7,
                    limit=200, workers=100, context=fake_context, discovery_interval=600,
                    stop_event=stop, poll_seconds=0.02)
            except BaseException as exc:
                outcome["exception"] = repr(exc)

        with patch.dict(pipeline._STAGE_RUNNERS, stubs, clear=True), \
             patch.object(pipeline, "_count_pending", pending), \
             patch.object(pipeline, "_run_company_research_streaming", lambda *a: {"status": "skipped"}), \
             patch("applypilot.discovery.repository.refresh_current_assessments", lambda *a, **kw: None), \
             patch("applypilot.discovery.buffer.read_snapshot", lambda *a, **kw: {"available": True, "held": False}), \
             patch.object(ops, "record", lambda *a, **kw: None), \
             patch.object(ops, "capture", lambda *a, **kw: None), \
             patch.object(ops, "thread_target", lambda f: f):
            thread = threading.Thread(target=invoke)
            thread.start()
            try:
                check("discovery_started", discovery_started.wait(2))
                check("downstream_processes_multiple_batches_while_discovery_and_enrichment_blocked",
                      until(lambda: all(counts[s] == 100 for s in ("score", "tailor", "cover")), 3),
                      synthetic_processed=dict(counts))
                check("no_global_one_pass_cap", all(calls[s] > 1 for s in ("score", "tailor", "cover")))
                with lock:
                    queues["brief"] = 20
                check("initially_empty_stage_consumes_later_arrivals_without_restart",
                      until(lambda: counts["brief"] == 20, 2))
                check("explicit_100_worker_width_reaches_llm_stage_runners",
                      all(observed_workers.get(s) == 100 for s in ("score", "tailor", "cover", "brief")))
            finally:
                stop.set()
                release.set()
                thread.join(10)
            check("continuous_shutdown_completes", not thread.is_alive() and "exception" not in outcome, outcome=outcome)
            beat = json.loads((root / "preparation-runtime.json").read_text())
            check("shutdown_report_is_not_left_running", beat["running"] is False)

        # A queue-query exception must be handled in the stage, not kill its thread.
        stop = threading.Event()
        fault = {"score": 0, "tailor": 0}
        def faulty_pending(stage, *a, **kw):
            if stage == "score" and fault["score"] == 0:
                fault["score"] += 1
                raise postgres.OperationalError("synthetic read fault")
            return 1 if fault[stage] < (2 if stage == "score" else 1) else 0
        def recovered(stage):
            def run(**kw):
                fault[stage] += 1
                return {"processed": 1}
            return run
        with patch.dict(pipeline._STAGE_RUNNERS, {s: recovered(s) for s in ("score", "tailor")}, clear=True), \
             patch.object(pipeline, "_count_pending", faulty_pending), \
             patch("applypilot.discovery.repository.refresh_current_assessments", lambda *a, **kw: None), \
             patch.object(ops, "record", lambda *a, **kw: None), \
             patch.object(ops, "capture", lambda *a, **kw: None), \
             patch.object(ops, "thread_target", lambda f: f):
            thread = threading.Thread(target=run_continuous, args=(["score", "tailor"],),
                kwargs=dict(min_score=8, max_age_days=7, limit=20, workers=2,
                    context=fake_context, stop_event=stop, poll_seconds=0.02))
            thread.start()
            try:
                check("queue_error_does_not_halt_other_stage", until(lambda: fault["tailor"] == 1, 2))
                check("failed_queue_reader_recovers_without_engine_restart", until(lambda: fault["score"] == 2, 7))
            finally:
                stop.set(); thread.join(10)

        # Configuration changes are explicit reloads, never periodic engine cycles.
        stop = threading.Event()
        reload_result = {}
        def reload_runner():
            reload_result.update(run_continuous(["score"], min_score=8, max_age_days=7,
                limit=20, workers=2, context=fake_context, stop_event=stop, poll_seconds=0.02))
        with patch.object(pipeline, "_count_pending", lambda *a, **kw: 0), \
             patch("applypilot.discovery.repository.refresh_current_assessments", lambda *a, **kw: None), \
             patch.object(ops, "record", lambda *a, **kw: None), \
             patch.object(ops, "thread_target", lambda f: f):
            thread = threading.Thread(target=reload_runner)
            thread.start()
            try:
                def is_running():
                    return json.loads((root / "preparation-runtime.json").read_text()).get("running") is True
                check("configuration_watch_starts_with_live_heartbeat", until(is_running, 3))
                (root / "company_limits.yaml").write_text("synthetic: changed\n")
                thread.join(5)
                check("actual_configuration_change_requests_reload", not thread.is_alive()
                      and reload_result.get("restart_requested") is True)
            finally:
                stop.set(); thread.join(10)
                (root / "company_limits.yaml").unlink(missing_ok=True)

        from applypilot.continuous import stage_workers
        with patch.dict(os.environ, {"APPLYPILOT_SCORE_WORKERS": "123"}):
            check("per_stage_width_override_is_independent", stage_workers("score", 2) == 123)
        invalid_width = False
        with patch.dict(os.environ, {"APPLYPILOT_SCORE_WORKERS": "0"}):
            try:
                stage_workers("score", 2)
            except ValueError:
                invalid_width = True
        check("invalid_stage_width_is_rejected", invalid_width)

        # Real journal aggregation must not infer zero from its 200-card UI limit.
        from datetime import datetime, timezone
        from applypilot.operations.store import connect_writer
        from applypilot.web.engine import _journal
        journal = connect_writer(root)
        now = time.time()
        run_id = "a" * 32
        journal.execute("INSERT INTO processes VALUES(?,?,?,?,?,?,?,?,?)",
            ("synthetic-process", run_id, 12345, "preparation", now, 0, 0, 0, 1))
        stage_totals = {"discovery": 250, "score": 100, "tailor": 100, "cover": 100, "brief": 100}
        for stage, total in stage_totals.items():
            for index in range(total):
                journal.execute("INSERT INTO active VALUES(?,?,?,?,?,?,?)",
                    (f"{stage}-{index}", "synthetic-process", run_id, stage, stage,
                     "fetch_page" if stage == "discovery" else "job",
                     now - (8 if stage == "discovery" else 5)))
        journal.commit(); journal.close()
        snapshot = _journal(root, {"running": True, "stale": False, "operations_run_id": run_id,
            "started_at": datetime.fromtimestamp(now-10, timezone.utc).isoformat()}, now)
        check("high_concurrency_counts_are_not_capped_at_200_cards",
              snapshot["current"] and all(snapshot["stages"][stage]["active"] == 100
                                         for stage in ("score", "tailor", "cover", "brief")))
        check("high_concurrency_detail_cards_remain_bounded", len(snapshot["active"]) <= 200
              and snapshot["truncated"] is True and snapshot["stages"]["discover"]["active"] == 250)

        # Real ZIP generation, read transactions, privacy filtering and CRC checks.
        (root / "logs").mkdir(exist_ok=True)
        secret = "offline-secret-123456789-DO-NOT-EXPORT"
        (root / ".env").write_text("EXAMPLE_API_KEY=" + secret + "\n")
        (root / "logs" / "aaa-new.log").write_text("synthetic log " + secret + " reader@example.invalid\n")
        (root / "logs" / "zzz-old.log").write_text("old log\n")
        os.utime(root / "logs" / "zzz-old.log", (1, 1))
        start = time.monotonic()
        bundle = build_bundle(root, limits=Limits(files=1, database_seconds=2, total_seconds=8))
        zip_bytes = bundle.read_bytes()
        with zipfile.ZipFile(bundle) as z:
            check("real_support_zip_is_nonempty_and_crc_valid", len(zip_bytes) > 22 and z.testzip() is None,
                  bytes=len(zip_bytes), elapsed_seconds=round(time.monotonic()-start, 3))
            manifest = json.loads(z.read("manifest.json"))
            check("support_manifest_declares_sampling_and_limits", manifest["format_version"] == 2 and "scope" in manifest)
            check("newest_log_selected_before_older_log", "logs/aaa-new.log" in z.namelist() and "logs/zzz-old.log" not in z.namelist())
            all_bytes = b"\n".join(z.read(name) for name in z.namelist())
            check("known_secret_not_exported", secret.encode() not in all_bytes)
            check("credential_file_not_exported", all(not name.endswith("/.env") for name in z.namelist()))
        bundle.unlink()
        small = build_bundle(root, limits=Limits(total_seconds=0.000001))
        with zipfile.ZipFile(small) as z:
            m = json.loads(z.read("manifest.json"))
            check("deadline_returns_valid_partial_zip_not_zero_bytes", small.stat().st_size > 22 and z.testzip() is None
                  and any(e.get("reason") == "export_time_limit" for e in m["entries"]))
        small.unlink()

        from applypilot.operations.export_tasks import SupportExports
        gate = threading.Event()
        def delayed_bundle(*a, **kw):
            gate.wait(5)
            fd, path = tempfile.mkstemp(suffix=".zip"); os.close(fd)
            Path(path).write_bytes(zip_bytes)
            return Path(path)
        manager = SupportExports(root)
        with patch("applypilot.operations.export_tasks.build_bundle", delayed_bundle):
            first = manager.start(); second = manager.start()
            check("duplicate_click_reuses_in_progress_export", first["id"] == second["id"])
            refused = False
            try:
                manager.download(first["id"])
            except ValueError:
                refused = True
            check("unfinished_zip_cannot_be_downloaded", refused)
            gate.set()
            check("completed_task_becomes_ready", until(lambda: manager.status(first["id"])["state"] == "ready"))
            path, _, _ = manager.download(first["id"])
            check("retryable_file_is_retained", path.is_file() and manager.download(first["id"])[0] == path)
        manager.close()
        check("retained_export_removed_on_shutdown", not path.exists())

        # The actual API, session, CSRF middleware and FileResponse; no socket required.
        async def http_checks():
            import httpx
            from applypilot.web.server import API
            api = API(root, "x"*48, 8765)
            app = api.application()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
                    response = await client.post("/api/operations/support-bundle", json={})
                    check("support_export_requires_authenticated_session", response.status_code == 401)
                    session = await client.post("/api/session", json={"token": "x"*48})
                    csrf = session.json()["csrf"]
                    response = await client.post("/api/operations/support-bundle", json={})
                    check("support_export_requires_csrf", response.status_code == 403)
                    response = await client.post("/api/operations/support-bundle", json={"path": "/tmp"}, headers={"X-ApplyPilot-CSRF": csrf})
                    check("support_export_rejects_user_paths", response.status_code == 400)
                    response = await client.post("/api/operations/support-bundle", json={}, headers={"X-ApplyPilot-CSRF": csrf})
                    check("support_start_is_202_task_not_an_empty_zip", response.status_code == 202 and response.headers["content-type"].startswith("application/json"))
                    task = response.json()
                    for _ in range(300):
                        response = await client.get("/api/operations/support-bundle/"+task["id"])
                        task = response.json()
                        if task["state"] != "building":
                            break
                        await asyncio.sleep(0.05)
                    check("authenticated_export_reaches_ready", task["state"] == "ready", task=task)
                    response = await client.get(task["download_url"])
                    check("actual_download_length_and_zip_integrity", response.status_code == 200
                          and len(response.content) == int(response.headers["content-length"]) == task["bytes"]
                          and zipfile.ZipFile(io.BytesIO(response.content)).testzip() is None)
                    digest = hashlib.sha256(response.content).hexdigest()
                    second = await client.get(task["download_url"])
                    check("repeat_download_is_byte_identical", hashlib.sha256(second.content).hexdigest() == digest)
                    head = await client.head(task["download_url"])
                    check("head_does_not_destroy_download", head.status_code == 200 and not head.content and int(head.headers["content-length"]) == task["bytes"])
                    partial = await client.get(task["download_url"], headers={"Range": "bytes=0-15"})
                    check("range_download_is_retryable", partial.status_code == 206 and partial.content == response.content[:16])
                    third = await client.get(task["download_url"])
                    check("full_file_survives_head_and_range", hashlib.sha256(third.content).hexdigest() == digest)
                    bad = await client.get("/api/operations/support-bundle/not-a-task/download")
                    check("arbitrary_download_identifier_rejected", bad.status_code == 404)
            finally:
                api.support_exports.close()
                await api.live.close()
        asyncio.run(http_checks())

        # No Redis dependency or connection is needed to exercise the actual broker guards.
        sys.path.insert(0, str(PROJECT / "docs" / "guides"))
        try:
            import redis.asyncio  # noqa: F401
        except ImportError:
            redis_module = types.ModuleType("redis")
            redis_async = types.ModuleType("redis.asyncio")
            class OfflineRedis:
                def __init__(self, *a, **kw):
                    raise RuntimeError("Live Redis is not permitted in this offline check")
            redis_async.Redis = OfflineRedis
            redis_module.asyncio = redis_async
            sys.modules["redis"] = redis_module
            sys.modules["redis.asyncio"] = redis_async
        from warp_egress.state import Worker, EgressState, choose_worker, backend_allowed
        from warp_egress.policy import classify_failure, EgressAction
        worker = Worker("exit-1", "socks5://synthetic:20000", True, "available", "203.0.113.10", None,
            time.time(), 0, exit_verified=True, backend="singbox", verified_at=time.time(), verification_ttl=300)
        warp = replace(worker, worker_id="warp-01", backend="warp")
        unknown = replace(worker, worker_id="legacy-01", backend="")
        with patch.dict(os.environ, {"EGRESS_ALLOWED_BACKENDS": "singbox,raw_proxy,openvpn"}):
            check("retired_warp_not_selected_even_when_healthy_and_verified", choose_worker("credential", [warp], require_verified_exit=True) is None)
            check("unknown_legacy_backend_fails_closed", not backend_allowed(unknown))
            check("non_warp_verified_worker_remains_eligible", choose_worker("credential", [warp, unknown, worker], require_verified_exit=True) == worker)
            check("retagged_warp_worker_id_is_still_rejected", not backend_allowed(replace(warp, backend="singbox")))
            check("duplicate_exits_still_respect_exclusions", choose_worker("credential", [worker], excluded_exit_ips={worker.exit_ip}, require_verified_exit=True) is None)
            class MemoryRedis:
                async def hgetall(self, key):
                    return {"proxy_url": worker.proxy_url, "healthy": "1", "state": "available",
                            "exit_ip": worker.exit_ip, "exit_verified": "1", "backend": self.backend}
                async def get(self, key):
                    return "credential"
            fake = MemoryRedis()
            state = EgressState(redis=fake)
            async def leases():
                fake.backend = "warp"
                denied = await state._lease_from_hash("credential", {"worker_id": "warp-01"})
                fake.backend = "singbox"
                accepted = await state._lease_from_hash("credential", {"worker_id": "exit-1"})
                return denied, accepted
            denied, accepted = asyncio.run(leases())
            check("existing_warp_lease_is_rejected_without_redis_reset", denied is None)
            check("existing_non_warp_lease_remains_valid", accepted is not None)
        class ProviderLimit(Exception):
            status_code = 429
        check("provider_429_does_not_trigger_proxy_rotation", classify_failure(ProviderLimit())[1] == EgressAction.LET_LITELLM_HANDLE)

        import yaml
        compose = yaml.safe_load((PROJECT / "docs/guides/docker-compose.litellm.yml").read_text())
        check("default_compose_does_not_launch_warp_manager", "warp-egress-manager" not in compose["services"])
        check("litellm_does_not_depend_on_warp_manager", "warp-egress-manager" not in compose["services"]["litellm"]["depends_on"])
        gateway = yaml.safe_load((PROJECT / "docs/guides/litellm.yaml").read_text())
        check("runtime_uses_backend_neutral_callback", "plugins.egress.egress_callback" in gateway["litellm_settings"]["callbacks"])
        check("generator_will_not_restore_old_warp_callback", '"plugins.warp_egress.warp_egress_callback"' not in (PROJECT / "docs/guides/generate_from_inventories.py").read_text())

    report = {"scope": "Offline synthetic regression checks; no live models, browsers, provider endpoints or Redis server were used.",
              "versions": {name: importlib.metadata.version(name) for name in ("httpx", "starlette")},
              "passed": sum(c["passed"] for c in checks), "total": len(checks), "checks": checks,
              "not_verified": ["User's live database blocker distribution", "User's original 0 KB browser/network failure",
                  "Real LiteLLM/Docker deployment and current proxy exits", "Hundreds of actual simultaneous model/browser/application/outreach operations"]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"passed": report["passed"], "total": report["total"]}))


if __name__ == "__main__":
    main()
