#!/usr/bin/env python3
"""Offline regression checks for the September 17 stall repair.

Uses disposable SQLite workspaces, mocked native processes and injected sharing
failures. No browser, provider, application submission or backup upload is run.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
from applypilot import postgres
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def no_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("Stall repair checks prohibit network access")


class StallRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="applypilot-stall-check-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from applypilot.resource_governor import ResourceGovernor
        self.g = ResourceGovernor(self.root / "main.pgstore", total=1)
        self.g.runtime_id = "new-runtime"
        self.g._journal = Mock()
        self.g._ensure()
        with self.g.transaction() as conn:
            conn.execute("CREATE TABLE application_attempts (reservation_id TEXT PRIMARY KEY, status TEXT, "
                         "side_effect_intent_at DOUBLE PRECISION, browser_ambiguous_action_at DOUBLE PRECISION)")

    def lease(self, *, phase="pending", pid=987654321, reservation=None, launched=None,
              children="[]", browser_units=1, session=None):
        token = uuid4().hex
        with self.g.transaction() as conn:
            conn.execute("INSERT INTO resource_leases(token,owner_id,owner_pid,runtime_id,lane,stage,"
                         "browser_units,children_json,acquired_at,heartbeat_at,launch_phase,"
                         "reservation_id,launch_started_at,browser_session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (token,"old-owner",pid,"old-runtime","applications","applications",browser_units,
                          children,time.time()-100,time.time()-100,phase,reservation,launched,session))
        return token

    def reconcile(self, **kwargs):
        with patch("applypilot.processes.process_alive", return_value=False), \
             patch("applypilot.processes.runtime_owned", return_value=False):
            return self.g.reconcile_inactive_leases(**kwargs)

    def count(self):
        with self.g.reader() as conn:
            return conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0]

    def test_pending_dead_owner_unbound_browser_released(self):
        token = self.lease()
        result = self.reconcile()
        self.assertEqual(result["released"][0]["token"], token)
        self.assertEqual(result["released"][0]["reason"], "owner_absent_and_launch_never_started")
        self.assertEqual(self.count(), 0)

    def test_live_owner_never_reclaimed(self):
        self.lease(pid=os.getpid())
        with patch("applypilot.processes.process_alive", return_value=True), \
             patch("applypilot.processes.runtime_owned", return_value=False):
            result = self.g.reconcile_inactive_leases()
        self.assertIn(result["held"][0]["reason"], {"owner_live_or_uninspectable", "browser_session_not_recorded"})
        self.assertEqual(self.count(),1)

    def test_legacy_without_session_retained(self):
        self.lease(phase="legacy")
        self.assertEqual(self.reconcile()["held"][0]["reason"], "browser_session_not_recorded")
        self.assertEqual(self.count(),1)

    def test_started_without_session_retained(self):
        self.lease(phase="starting",launched=time.time()-60)
        self.assertEqual(self.reconcile()["held"][0]["reason"], "browser_session_not_recorded")

    def test_children_without_session_retained(self):
        self.lease(children="[543210]")
        self.assertEqual(self.reconcile()["held"][0]["reason"], "browser_session_not_recorded")

    def test_uncertain_reservation_not_reclaimed(self):
        self.lease(reservation=uuid4().hex)
        self.assertEqual(self.reconcile()["held"][0]["reason"], "browser_session_not_recorded")

    def test_cancelled_reservation_without_intent_reclaimed(self):
        reservation=uuid4().hex
        with self.g.transaction() as conn:
            conn.execute("INSERT INTO application_attempts VALUES(?, 'cancelled_before_submit', NULL, NULL)", (reservation,))
        self.lease(reservation=reservation)
        self.assertEqual(len(self.reconcile()["released"]),1)
        with self.g.reader() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0],1)

    def test_cancelled_but_ambiguous_reservation_retained(self):
        reservation=uuid4().hex
        with self.g.transaction() as conn:
            conn.execute("INSERT INTO application_attempts VALUES(?, 'cancelled_before_submit', NULL, ?)", (reservation,time.time()))
        self.lease(reservation=reservation)
        self.assertEqual(len(self.reconcile()["released"]),0)

    def test_launch_intent_manifest_retained_even_if_row_pending(self):
        from applypilot.resource_manifest import atomic_record
        token=self.lease()
        atomic_record(self.root/"resource-launches"/(token+".json"),
                      {"token":token,"owner_id":"old-owner","owner":{"pid":987654321},
                       "phase":"starting","children":[]})
        self.assertEqual(len(self.reconcile()["released"]),0)

    def test_lease_mutation_before_delete_retained(self):
        self.lease()
        original=self.g.transaction
        calls=0
        @contextmanager
        def transaction():
            nonlocal calls
            calls+=1
            with original() as conn:
                if calls == 2: # after read/evidence, immediately before CAS delete
                    conn.execute("UPDATE resource_leases SET launch_phase='starting',launch_started_at=?", (time.time(),))
                yield conn
        with patch.object(self.g,"transaction",transaction):
            result=self.reconcile()
        self.assertEqual(len(result["released"]),0)
        self.assertEqual(self.count(),1)

    def test_sharing_conflict_retried_without_losing_record(self):
        from applypilot.resource_manifest import atomic_record
        path=self.root/"record.json"
        atomic_record(path,{"old":True})
        real_replace=os.replace
        calls=0
        def transient(source,target):
            nonlocal calls
            calls+=1
            if calls<=2:
                self.assertEqual(json.loads(path.read_text()),{"old":True})
                raise PermissionError(errno.EACCES,"sharing conflict")
            return real_replace(source,target)
        with patch("applypilot.atomic_io.os.replace",side_effect=transient), \
             patch("applypilot.atomic_io.time.sleep"):
            atomic_record(path,{"new":True})
        self.assertEqual(calls,3)
        self.assertEqual(json.loads(path.read_text()),{"new":True})
        self.assertEqual(list(self.root.glob("*.tmp")),[])

    def test_permanent_publication_error_does_not_truncate(self):
        from applypilot.resource_manifest import atomic_record
        path=self.root/"record.json"
        atomic_record(path,{"old":True})
        with patch("applypilot.atomic_io.os.replace",side_effect=PermissionError("permanent")), \
             patch("applypilot.atomic_io.time.sleep"):
            with self.assertRaises(PermissionError):
                atomic_record(path,{"new":True})
        self.assertEqual(json.loads(path.read_text()),{"old":True})
        self.assertEqual(list(self.root.glob("*.tmp")),[])

    def test_capacity_file_failure_falls_back_to_sqlite(self):
        sample={"runtime_id":"new-runtime","sampled_at":time.time(),"telemetry_available":True,
                "cpu_percent":22,"host_target":1,"host_paused":False}
        with patch("applypilot.resource_manifest.atomic_record",side_effect=PermissionError("locked")):
            self.g._publish_capacity_sample(sample)
        with self.g.reader() as conn:
            saved=json.loads(conn.execute("SELECT sample_json FROM resource_policy").fetchone()[0])
        self.assertEqual(saved,sample)

    def test_old_controller_fallback_cannot_overwrite_new_runtime(self):
        with self.g.transaction() as conn:
            conn.execute("UPDATE resource_policy SET runtime_id='replacement',sample_json='{}'")
        with patch("applypilot.resource_manifest.atomic_record",side_effect=PermissionError("locked")):
            self.g._publish_capacity_sample({"runtime_id":"new-runtime","sampled_at":time.time()})
        with self.g.reader() as conn:
            self.assertEqual(conn.execute("SELECT sample_json FROM resource_policy").fetchone()[0],"{}")

    def test_reconciliation_publication_error_not_a_failed_release(self):
        self.lease()
        with patch("applypilot.resource_manifest.atomic_record",side_effect=PermissionError("locked")):
            result=self.reconcile()
        self.assertEqual(len(result["released"]),1)
        self.assertTrue(result["diagnostic_file_unavailable"])
        self.assertEqual(self.count(),0)

    def test_controller_stays_elected_after_telemetry_failures(self):
        from applypilot.resource_capacity import DEFAULT_CAPACITY
        self.g.configure(total=1,mode="auto",capacity_settings={"pressure_samples":2})
        self.g._reap=Mock()
        sample={"cpu_percent":20,"available_memory_percent":50,"available_memory_bytes":8*1024**3,
                "sampled_at":time.time(),"telemetry_available":True}
        stop=Mock()
        stop.is_set.return_value=False
        stop.wait.side_effect=[False,False,True]
        self.g._controller_stop=stop
        entered=[]
        @contextmanager
        def guard(path):
            entered.append(path)
            yield
        with patch("applypilot.processes.process_guard",guard), \
             patch("applypilot.resource_capacity.PressureSampler.sample",return_value=sample), \
             patch("applypilot.resource_manifest.atomic_record",side_effect=PermissionError("locked")):
            self.g._control_capacity()
        self.assertEqual(len(entered),1)
        self.assertEqual(self.g._reap.call_count,3)
        self.assertTrue(all(call.args[0]==DEFAULT_CAPACITY["sample_seconds"] for call in stop.wait.call_args_list))
        self.assertFalse(self.g.budget().get("host_paused"))

    def test_real_memory_pressure_still_blocks_production(self):
        from applypilot.resource_capacity import host_capacity,DEFAULT_CAPACITY
        sample={"cpu_percent":69,"available_memory_percent":8.8,"available_memory_bytes":2230423552,
                "telemetry_available":True}
        value=host_capacity(sample,DEFAULT_CAPACITY,current=1,active=0,pending=6616,previous={})
        self.assertTrue(value["host_paused"])
        self.assertEqual(value["host_admission_reason"],"host_memory_pressure")

    def test_background_gate_cases(self):
        from applypilot.background_work import maintenance_reason
        healthy={"telemetry_available":True,"cpu_percent":20,"available_memory_percent":50,
                 "available_memory_bytes":8*1024**3,"host_paused":False,"active_permits":0,"total_budget":1}
        for update,expected in [({},None),({"telemetry_available":False},"waiting_resource_measurement"),
                                ({"host_paused":True,"host_admission_reason":"host_memory_pressure"},"host_memory_pressure"),
                                ({"cpu_percent":99},"host_cpu_pressure"),
                                ({"active_permits":1},"foreground_capacity_occupied"),
                                ({"available_memory_percent":8},"host_memory_pressure")]:
            with self.subTest(expected=expected), \
                 patch("applypilot.processes.runtime_owned",return_value=True), \
                 patch("applypilot.resource_governor.ResourceGovernor.budget",return_value=healthy|update):
                self.assertEqual(maintenance_reason(self.root),expected)

    def test_idle_workspace_maintenance_not_blocked_by_old_sample(self):
        from applypilot.background_work import maintenance_reason
        with patch("applypilot.processes.runtime_owned",return_value=False), \
             patch("applypilot.resource_governor.ResourceGovernor.budget") as budget:
            self.assertIsNone(maintenance_reason(self.root))
            budget.assert_not_called()

    def test_backup_defer_keeps_dirty_revision(self):
        from applypilot.backup import runtime
        directory=self.root/"github_sync"
        directory.mkdir()
        (directory/"dirty.json").write_text('{"at":123}')
        with patch.object(runtime,"due",return_value=True), \
             patch("applypilot.background_work.maintenance_reason",return_value="host_memory_pressure"), \
             patch.object(runtime,"spawn") as spawn:
            self.assertIsNone(runtime.spawn_if_due(self.root))
        spawn.assert_not_called()
        self.assertEqual(json.loads((directory/"dirty.json").read_text()),{"at":123})
        self.assertEqual(json.loads((directory/"admission.json").read_text())["reason"],"host_memory_pressure")

    def test_replay_reports_timeout_not_call_exhaustion(self):
        from applypilot.reasoning import replay
        from applypilot.reasoning.budget import CallBudget
        from applypilot.reasoning.application_policy import BEGIN,END
        state={"prompt":BEGIN+"original"+END,"tools":{},"skill_files":{"example/SKILL.md":"inert"},
               "model":"offline","system_prompt":"offline","history":[]}
        proc=Mock()
        proc.communicate.side_effect=subprocess.TimeoutExpired("inert",7)
        budget=CallBudget(160,1800,655360)
        with patch.object(replay.shutil,"which",return_value="inert"), \
             patch("applypilot.gateway_policy.configure_agent_environment"), \
             patch.object(replay.subprocess,"Popen",return_value=proc), \
             patch.object(replay,"stop_tree") as stop, \
             patch.dict(os.environ,{"APPLYPILOT_LEARNING_REPLAY_TIMEOUT_SECONDS":"7"}):
            with self.assertRaises(subprocess.TimeoutExpired):
                replay.choose_action(state,"new",budget=budget)
        self.assertEqual(budget.failure_reason,"browser_replay_timeout")
        self.assertEqual(budget.failure_error_type,"TimeoutExpired")
        self.assertEqual(budget.calls,1)
        self.assertEqual(proc.communicate.call_args.kwargs["timeout"],7)
        stop.assert_called_once_with(proc)


def main():
    sys.addaudithook(no_network)
    with tempfile.TemporaryDirectory(prefix="applypilot-stall-env-") as directory:
        os.environ["APPLYPILOT_DIR"]=directory
        unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
