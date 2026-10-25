#!/usr/bin/env python3
"""Offline contract checks for buffered producers and independent consumers.

Run from the source checkout: python tools/check_buffered_pipeline.py
Only temporary workspaces are used. Connectors, sends and optimizer compilation
are not exercised; a socket tripwire rejects accidental network access.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
from applypilot import postgres
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch, Mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--admission-only', action='store_true', help='Print the same admission probe for an older checkout.')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    results = []
    network_calls = []

    def no_network(*values, **kwargs):
        network_calls.append(repr(values[1:]))
        raise AssertionError('An offline contract check attempted network access')

    def check(name, function):
        started = time.monotonic()
        try:
            details = function()
            results.append({'check': name, 'passed': True, 'details': details,
                            'seconds': round(time.monotonic() - started, 3)})
            print('PASS', name, flush=True)
        except Exception as exc:
            results.append({'check': name, 'passed': False, 'error': repr(exc)})
            print('FAIL', name, repr(exc), flush=True)
            raise

    with tempfile.TemporaryDirectory(prefix='applypilot-flow-check-') as temporary, \
            patch.object(socket.socket, 'connect', no_network), \
            patch('socket.create_connection', no_network):
        root = Path(temporary)
        os.environ['APPLYPILOT_DIR'] = str(root)
        os.environ['APPLYPILOT_RUNTIME_ID'] = 'offline-flow-contract'
        from applypilot import config
        from applypilot.resource_governor import ResourceGovernor, shared_governor
        from applypilot.resource_capacity import validate_capacity
        from applypilot.flow_control import FlowController, validate_flow
        from applypilot.stage_pool import refill_completed, stage_scope
        from applypilot.work_governor import WorkGovernor

        def registry(name, *, total=2, applications=2):
            directory = root / name
            directory.mkdir()
            r = ResourceGovernor(directory / 'main.pgstore')
            r.configure(total=total, browsers=1, mode='auto', application_workers=applications)
            return r

        def sample(r, *, available=True, age=0, memory=1580220416, cpu=44.3):
            # Nonprivate measurements from the supplied run; no jobs or profile fields.
            with r.transaction() as conn:
                conn.execute('UPDATE resource_policy SET sample_json=?', (json.dumps({
                    'sampled_at': time.time() - age, 'runtime_id': r.runtime_id,
                    'telemetry_available': available, 'available_memory_percent': 6.21600336163916,
                    'available_memory_bytes': memory, 'cpu_percent': cpu,
                    'host_paused': True, 'host_target': 1,
                    'sizing_reason': 'host_memory_pressure',
                    'host_admission_reason': 'host_memory_pressure',
                }),))

        def admission_probe():
            r = registry('captured-pressure')
            sample(r)
            admitted = {}
            for lane in ('score', 'applications', 'network_research', 'network_sync', 'learning'):
                r.demand(lane, 10, ceiling=1)
                lease = r.try_acquire(lane, browser_units=1)
                admitted[lane] = {'admitted': lease is not None, 'reason': r.admission_reason()}
                if lease:
                    admitted[lane]['browser'] = lease.require_browser()
                    assert lease.release()
            return admitted

        if args.admission_only:
            report = admission_probe()
            print(json.dumps(report, indent=2))
            if args.output:
                args.output.write_text(json.dumps(report, indent=2) + '\n')
            return

        def legacy_config():
            assert validate_capacity({'mode': 'auto', 'memory_pause_percent': 10,
                'cpu_pause_percent': 90, 'initial_concurrency': 2}) == {'mode': 'configured', 'sample_seconds': 2}
            settings = validate_flow({'finish_reserve': 9, 'background_total_workers': 0,
                                      'workers': {'score': 0}})
            assert 'finish_reserve' not in settings
            assert settings['workers']['score'] == 0
            for bad in ({'sample_seconds': 0}, {'unknown': 1}):
                try:
                    validate_capacity(bad)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Invalid settings accepted')
            return {'legacy_modes': 'normalized to configured', 'explicit_stage_zero': 'preserved'}
        check('legacy configuration migration', legacy_config)

        def captured_pressure():
            details = admission_probe()
            assert all(row['admitted'] and row['browser'] for row in details.values()), details
            return details
        check('captured pressure admits browsers and every independent lane', captured_pressure)

        def telemetry_not_permission():
            r = registry('telemetry')
            r.demand('applications', 10, ceiling=3)
            modes = [(False, 0, 0, 100), (True, 86400, 0, 100), (True, 0, 0, 100)]
            for available, age, memory, cpu in modes:
                sample(r, available=available, age=age, memory=memory, cpu=cpu)
                with patch.object(r, '_sample', side_effect=AssertionError('Admission read telemetry')):
                    lease = r.try_acquire('applications', browser_units=1)
                    assert lease is not None
                    assert lease.require_browser()
                    assert lease.release()
            state = r.snapshot()
            assert state['mode'] == 'configured' and not state['host_paused']
            assert state['browser_budget'] is None
            return {'missing': 'admitted', 'stale': 'admitted', 'zero_RAM_and_100_percent_CPU': 'admitted'}
        check('telemetry never authorizes a launch', telemetry_not_permission)

        def concurrent_lanes():
            r = registry('concurrent')
            sample(r)
            owners = [ResourceGovernor(r.path) for _ in range(12)]
            for owner in owners:
                owner.demand('applications', 10, ceiling=3)
            barrier = threading.Barrier(len(owners))
            def reserve(owner):
                barrier.wait(timeout=5)
                return owner.try_acquire('applications', browser_units=1)
            with ThreadPoolExecutor(max_workers=len(owners)) as pool:
                admitted = [lease for lease in pool.map(reserve, owners) if lease]
            assert len(admitted) == 3, len(admitted)
            for lane in ('score', 'referrals', 'network_research', 'network_sync', 'learning'):
                r.demand(lane, 1, ceiling=1)
                lease = r.try_acquire(lane)
                assert lease is not None, lane
                assert lease.require_browser()
                admitted.append(lease)
            assert r.snapshot()['active_permits'] == 8
            r.demand('disabled', 1, ceiling=0)
            assert r.try_acquire('disabled') is None and r.admission_reason() == 'lane_paused'
            for lease in admitted:
                assert lease.release()
            assert r.snapshot()['active_permits'] == 0
            return {'competing_owners': 12, 'application_slots_granted': 3, 'simultaneous_independent_tasks': 8}
        check('atomic per-lane concurrency without a shared budget', concurrent_lanes)

        def owned_child_cleanup():
            r = registry('child-cleanup')
            r.demand('score', 2, ceiling=1)
            lease = r.try_acquire('score')
            lease.begin_launch(kind='offline-helper')
            child = subprocess.Popen([sys.executable, '-c',
                'import sys; sys.stdin.read(1)'], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                lease.attach_child(child.pid)
                held = lease.execution_finished()
                assert any(row['token'] == lease.token for row in held['held']), held
                assert r.try_acquire('score') is None
                assert r.admission_reason() == 'lane_capacity_occupied'
                # A draining child holds its own lane, not the application's lane.
                r.demand('applications', 1, ceiling=1)
                application = r.try_acquire('applications', browser_units=1)
                assert application is not None and application.require_browser()
                assert application.release()
                child.communicate(b'x', timeout=5)
                assert lease.child_cleanup_finished()
                released = lease.execution_finished()
                assert any(row['token'] == lease.token for row in released['released']), released
                assert r.snapshot()['active_permits'] == 0
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                if child.stdin and not child.stdin.closed:
                    child.stdin.close()
            return {'live_owned_child': 'retained in its lane',
                    'unrelated_browser_task': 'admitted', 'exited_child': 'released'}
        check('owned child cleanup survives the executor simplification', owned_child_cleanup)

        def busy_storage():
            r = registry('storage')
            r.demand('score', 1, ceiling=1)
            lock = postgres.connect(r.path, isolation_level=None)
            lock.execute('BEGIN')
            try:
                assert r.try_acquire('score') is None
                assert r.admission_reason() == 'resource_database_busy'
            finally:
                lock.rollback(); lock.close()
            lease = r.try_acquire('score')
            assert lease is not None and lease.release()
            return 'A failed reservation makes no lease; retry succeeds after the writer releases.'
        check('SQLite contention retries without false admission', busy_storage)

        def buffer_rules():
            clock = [100.0]
            settings = validate_flow()
            counts = dict.fromkeys(settings['buffers'], 0)
            valid = [True]
            def reader(*args, **kwargs):
                return {'available': valid[0], 'context_key': 'offline', 'counts': dict(counts)}
            flow = FlowController(root / 'buffers', ('score', 'tailor', 'cover'),
                                  clock=lambda: clock[0], reader=reader)
            flow.publish = lambda: None
            flow.refresh()
            counts['qualified'] = settings['buffers']['qualified']['high']
            flow.refresh()
            assert flow.decision('score', 4).capacity == 0
            assert flow.decision('tailor', 2).capacity == 2
            assert flow.decision('applications', 3).capacity == 3
            for lane in ('network_research', 'network_sync', 'learning', 'outreach'):
                assert flow.decision(lane, 2).capacity == 2
            counts['qualified'] = settings['buffers']['qualified']['low'] + 1
            flow.refresh(); assert flow.decision('score', 4).capacity == 0
            counts['qualified'] -= 1
            flow.refresh(); assert flow.decision('score', 4).capacity == 4
            counts['qualified'] = settings['buffers']['qualified']['high'] - 1
            flow.refresh()
            flow.completed('score')
            assert flow.capacity('score', 4) == 0
            flow.refresh(); assert flow.capacity('score', 4) == 1
            clock[0] += 100
            valid[0] = False
            for stage in ('score', 'tailor', 'cover'):
                assert flow.decision(stage, 4).capacity == 1
                flow.dispatched(stage, 1)
                assert flow.decision(stage, 4).capacity == 0
            assert flow.decision('applications', 3).capacity == 3
            scoring_only = FlowController(root, ('score',), reader=reader)
            assert scoring_only._output_buffer('score') == 'qualified'
            return {'high_low_resume': 'passed', 'independent_consumers': 'passed',
                    'unobserved_completion_bound': 'passed', 'unknown_inventory_bounded_for_all_producers': 'passed'}
        check('producer buffer boundaries and independent consumers', buffer_rules)

        def pool_lifecycle():
            token = ContextVar('offline-test-context', default='missing')
            token.set('inherited')
            entered, exited, selected, outcomes = [], [], [], []
            caller = threading.get_ident()
            @contextmanager
            def resources():
                entered.append(threading.get_ident())
                try:
                    yield
                finally:
                    exited.append(threading.get_ident())
            source = iter(range(8))
            def fetch(excluded, capacity):
                # The caller must have persisted every prior yielded result before refilling.
                assert len(outcomes) == len(selected)
                batch = []
                for _ in range(capacity):
                    item = next(source, None)
                    if item is None: break
                    batch.append({'url': str(item)})
                return batch
            def run(job):
                assert token.get() == 'inherited'
                assert threading.get_ident() != caller
                time.sleep(.003)
                if job['url'] == '2': raise ValueError('synthetic operation error')
                return int(job['url'])
            with stage_scope(stage='score'):
                for job, value, error in refill_completed(fetch, run, workers=2, worker_context=resources):
                    selected.append(job['url'])
                    outcomes.append((value, type(error).__name__ if error else None))
            assert len(outcomes) == 8 and sum(error is not None for _, error in outcomes) == 1
            assert sorted(entered) == sorted(exited) and len(entered) == 8
            assert not any(t.name.startswith('score_') for t in threading.enumerate())
            return {'tasks': 8, 'operation_errors_returned': 1, 'thread_owned_cleanup': True, 'context_inherited': True}
        check('standard executor handoff, errors, context and cleanup', pool_lifecycle)

        def pool_stop():
            stop = threading.Event()
            def fetch(excluded, capacity):
                return [{'url': str(n)} for n in range(100) if str(n) not in excluded][:capacity]
            completed = []
            def run(job):
                time.sleep(.02)
                completed.append(job['url'])
                return 1
            with stage_scope(stage='stop-check'):
                generator = refill_completed(fetch, run, workers=3, stop_event=stop, resident=True)
                next(generator)
                stop.set()
                list(generator)
            assert 1 <= len(completed) <= 3
            assert not any(t.name.startswith('stop-check_') for t in threading.enumerate())
            return {'drained_tasks': len(completed), 'no_refill_after_stop': True}
        check('stop drains current writers without refilling', pool_stop)

        def pipeline_handoff():
            from collections import deque
            r = shared_governor()
            r.configure(total=2, browsers=1, application_workers=1)
            sample(r, available=False, memory=0, cpu=100)
            governor = WorkGovernor(2)
            settings = validate_flow({'workers': {'score': 2, 'tailor': 2, 'cover': 2},
                'buffers': {'qualified': {'low': 1, 'high': 3}, 'cover': {'low': 1, 'high': 3},
                            'apply': {'low': 1, 'high': 3}}})
            governor.configure_flow(settings)
            lock = threading.RLock()
            stop, changed = threading.Event(), threading.Event()
            queues = {stage: deque() for stage in ('score', 'tailor', 'cover', 'applications')}
            queues['score'].extend({'url': str(n)} for n in range(18))
            completed = dict.fromkeys(queues, 0)
            first_application = []
            errors = []
            maxima = dict.fromkeys(('qualified', 'cover', 'apply'), 0)
            def reader(*args, **kwargs):
                with lock:
                    counts = dict.fromkeys(settings['buffers'], 0)
                    for name, stage in (('qualified', 'tailor'), ('cover', 'cover'), ('apply', 'applications')):
                        counts[name] = len(queues[stage])
                        maxima[name] = max(maxima[name], counts[name])
                    return {'available': True, 'context_key': 'offline', 'counts': counts}
            flow = FlowController(root, ('score', 'tailor', 'cover'),
                                  runtime={'flow_control': settings}, reader=reader)
            flow.publish = lambda: None
            flow.refresh()
            def stage_worker(stage, downstream):
                def fetch(excluded, capacity):
                    with lock:
                        return [queues[stage].popleft() for _ in range(min(capacity, len(queues[stage])))]
                def run(job):
                    time.sleep(.008)
                    return job
                try:
                    with stage_scope(stage=stage, resident=True, governor=governor, flow_control=flow,
                                     notify=changed.set, wake=changed, stop_event=stop):
                        for job, output, error in refill_completed(fetch, run, workers=2, poll_seconds=.01,
                                                                 retry_revision=lambda _: None):
                            if error: raise error
                            with lock:
                                completed[stage] += 1
                                queues[downstream].append(output)
                except Exception as exc:
                    errors.append(repr(exc)); stop.set()
            def apply_ready():
                try:
                    while not stop.is_set():
                        with lock:
                            job = queues['applications'].popleft() if queues['applications'] else None
                        if job:
                            r.demand('applications', 1, ceiling=2)
                            lease = r.try_acquire('applications')
                            assert lease is not None and lease.require_browser()
                            with lock:
                                if not first_application: first_application.append(completed['score'])
                                completed['applications'] += 1
                            lease.release(); changed.set()
                        else:
                            stop.wait(.003)
                except Exception as exc:
                    errors.append(repr(exc)); stop.set()
            threads = [threading.Thread(target=stage_worker, args=pair) for pair in
                       (('score','tailor'),('tailor','cover'),('cover','applications'))]
            threads.append(threading.Thread(target=apply_ready))
            for thread in threads: thread.start()
            deadline = time.monotonic() + 12
            try:
                while time.monotonic() < deadline and not stop.is_set():
                    flow.refresh(); changed.set()
                    with lock:
                        if completed['applications'] == 18: break
                    time.sleep(.005)
            finally:
                stop.set(); changed.set()
                for thread in threads: thread.join(timeout=5)
            assert not errors, errors
            assert not any(thread.is_alive() for thread in threads), 'Worker did not drain'
            assert completed == dict.fromkeys(queues, 18), completed
            assert first_application[0] < 18, first_application
            assert all(count <= settings['buffers'][name]['high'] for name, count in maxima.items()), maxima
            assert r.snapshot()['active_permits'] == 0
            return {'completed': completed, 'scored_at_first_application': first_application[0],
                    'maximum_observed_buffers': maxima, 'telemetry': 'unavailable; zero RAM; CPU 100%'}
        check('resident scoring-to-documents-to-application handoff', pipeline_handoff)

        def referral_buffers():
            from applypilot.unattended import dispatcher, social, research
            from applypilot.referrals import planner, store
            import applypilot.networking_capacity as networking
            r = registry('referral-buffer')
            conn = Mock()
            calls = []
            def record(name):
                return lambda *a, **k: calls.append(name)
            with patch.object(dispatcher, 'connection', return_value=conn), \
                    patch.object(dispatcher, 'shared_governor', return_value=r), \
                    patch.object(dispatcher, 'active', return_value=True), \
                    patch.object(store, 'policy', return_value={'plan_limit': 3}), \
                    patch.object(networking, 'replenishment_limit', return_value=0), \
                    patch.object(networking, 'new_case_limit', side_effect=AssertionError('New case on full buffer')), \
                    patch.object(research, 'import_networks', side_effect=record('import')), \
                    patch.object(research, 'enqueue', side_effect=record('research')), \
                    patch.object(planner, 'prepare_jobs', side_effect=AssertionError('Initial draft on full buffer')), \
                    patch.object(social, 'unreachable_fallbacks', side_effect=record('social_fallback')), \
                    patch.object(planner, 'create_fallback_drafts', side_effect=record('fallback')), \
                    patch.object(planner, 'create_followup_drafts', side_effect=record('followup')):
                assert dispatcher.preparation_pass({}, {'research_jobs_per_cycle': 2})
            assert 'fallback' in calls and 'followup' in calls and 'research' in calls
            conn.close.assert_called_once()
            assert r.snapshot()['active_permits'] == 0
            return {'full_initial_buffer': True, 'existing_case_actions': calls}
        check('full initial referral buffer does not stop follow-up and fallback work', referral_buffers)

        def referral_timing():
            from applypilot.referrals.store import hold_sql
            conn = postgres.connect(':memory:')
            try:
                conn.executescript('''CREATE TABLE jobs(url TEXT);
                  CREATE TABLE referral_job_links(job_url TEXT, case_id TEXT);
                  CREATE TABLE referral_cases(id TEXT, hold_until TEXT);
                  CREATE TABLE referral_settings(id INTEGER, config_json TEXT);
                  CREATE TABLE unattended_research(case_id TEXT,state TEXT,grace_until TEXT);
                  INSERT INTO jobs VALUES('before'),('alongside'),('after'),('unrelated');
                  INSERT INTO referral_job_links VALUES('before','case1');
                  INSERT INTO referral_cases VALUES('case1',datetime('now','+1 hour'));
                  INSERT INTO referral_settings VALUES(1,'{"mode":"unattended"}');''')
                select = 'SELECT j.url FROM jobs j WHERE ' + hold_sql('j') + ' ORDER BY j.url'
                assert [r[0] for r in conn.execute(select)] == ['after','alongside','unrelated']
                conn.execute("UPDATE referral_cases SET hold_until=datetime('now','-1 second')")
                assert len(conn.execute(select).fetchall()) == 4
                conn.execute("INSERT INTO unattended_research VALUES('case1','pending',datetime('now','+1 minute'))")
                assert len(conn.execute(select).fetchall()) == 3
                conn.execute("UPDATE referral_settings SET config_json='{\"mode\":\"off\"}'")
                assert len(conn.execute(select).fetchall()) == 4
            finally:
                conn.close()
            return 'A live pre-application hold affects only its linked job; expiry/off releases it; unrelated jobs proceed.'
        check('referral timing remains per-case, not a pipeline barrier', referral_timing)

        def learning_independent():
            from applypilot.reasoning import learning
            from applypilot import background_work
            old_gate = Mock(side_effect=AssertionError('Learning consulted foreground capacity'))
            with patch.object(background_work, 'maintenance_reason', old_gate, create=True), \
                    patch.object(learning, '_once', return_value={'state': 'offline_optimizer_called'}) as execute:
                assert learning._coordinated_once('score') == {'state': 'offline_optimizer_called'}
                execute.assert_called_once_with('score')
                old_gate.assert_not_called()
            return 'The real coordination entry reaches the optimizer without consulting foreground capacity; compile boundary mocked.'
        check('learning coordinator is independent of foreground capacity', learning_independent)

        assert not network_calls, network_calls
        report = {'python': sys.version.split()[0], 'platform': sys.platform, 'passed': len(results),
                  'failed': sum(not row['passed'] for row in results), 'network_calls': len(network_calls),
                  'scope': 'Offline temporary-workspace checks; connector sends, real browser launches and GEPA compilation are not run.',
                  'checks': results}
        print(json.dumps(report, indent=2))
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
