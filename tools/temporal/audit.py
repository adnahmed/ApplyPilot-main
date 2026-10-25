"""Repeatable source architecture checks; native validation is reported separately."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SRC = 'src/applypilot/'

# Positive locations document reviewed execution paths. Negative checks reject
# the old mechanisms independently; changing migration labels cannot pass.
RULES = {
 'A': ('No production Dask/distributed imports', [('orchestration/worker.py', r'Worker\(client')]),
 'B': ('No active supervisor execution plane', [('cli.py', r'from applypilot.orchestration'), ('orchestration/client.py', r'start_workflow|create_schedule')]),
 'C': ('No Activity wrapping legacy orchestration', [('orchestration/domain.py', r'service.prepare|execute_job|execute_page'), ('apply/service.py', r'begin_dispatch\(|launcher.run_job\('), ('orchestration/company_domain.py', r'def execute'), ('orchestration/integration_domain.py', r'def execute'), ('orchestration/utility_domain.py', r'def perform'), ('orchestration/target_domain.py', r'def execute')]),
 'D': ('No custom application retry scheduler', [('recovery.py', r'claim_due_recovery = _retired'), ('orchestration/workflows.py', r'RetryPolicy\(|workflow.wait_condition|workflow.continue_as_new')]),
 'E': ('No application worker-membership/assignment system', [('execution.py', r'def activity_scope|def browser_operation'), ('orchestration/worker.py', r'max_concurrent_activities|task_queue=policy.queue')]),
 'F': ('No arbitrary research/model execution budgets', [('reasoning/budget.py', r'def remaining|return None'), ('discovery/jobspy_transport.py', r'check_request_access\(context'), ('orchestration/learning_workflows.py', r'learning_optimize|LearningRunWorkflow')]),
 'G': ('No optimizer reservation system', [('reasoning/optimizers.py', r'dspy.MIPROv2|optimizer.compile'), ('orchestration/learning_workflows.py', r'start_child_workflow|learning_publish')]),
 'H': ('No cohort cooldown scheduler', [('orchestration/learning_workflows.py', r'learning-program|continue_as_new'), ('orchestration/workflows.py', r'RetryPolicy\(')]),
 'I': ('No production periodic sleep-loop scheduler', [('orchestration/client.py', r'Schedule\(|create_schedule'), ('orchestration/account_workflows.py', r'workflow.wait_condition|provider_available_at'), ('orchestration/target_schedules.py', r'ScheduleIntervalSpec')]),
 'J': ('Dashboard application commands use Temporal', [('web/server.py', r'from applypilot.orchestration'), ('web/engine.py', r'from applypilot.operations.store import reader'), ('onboarding/jobs.py', r'submit_utility|submit_report|run_work')]),
 'K': ('CLI application commands use Temporal', [('cli.py', r'from applypilot.orchestration'), ('referrals/cli.py', r'submit_utility|run_utility|utility')]),
 'L': ('PostgreSQL execution queues do not control work', [('orchestration/company_workflows.py', r'command.pending|continue_as_new'), ('orchestration/target_workflows.py', r'command.pending|continue_as_new'), ('orchestration/prediction_workflows.py', r'command.pending|continue_as_new'), ('migrations/retire_execution.py', r'recovery_work|discovery_tasks|company_execution')]),
 'M': ('No scheduler-dependent synchronous diagnostic SQL critical path', [('operations/runtime.py', r'put_nowait|name="operations-journal"|store.connect_writer'), ('execution.py', r'def activity_scope')]),
}
FORBIDDEN = {
 'B': [('cli.py', r'run_engine\(|autopilot_main\(|application_runtime_lease\(')],
 'C': [('orchestration/*.py', r'\b(?:run_cycle|run_round|run_stage_job|worker_loop|preparation_pass|_delivery_task|supervise|once)\(')],
 'D': [('recovery.py', r'def claim_due_recovery|UPDATE recovery_work SET|next_retry_at\s*<=')],
 'E': [('execution.py', r'worker_registry|owner_token|lease_until|Semaphore\(|LocalCluster\('), ('orchestration/*.py', r'worker_registry|renew_lease\(|claim_worker\(')],
 'F': [('reasoning/*.py', r'\bCallBudget\(|waiting_round_budget|search_budget\s*='), ('discovery/jobspy_transport.py', r'MAX_HTTP\s*=|MAX_PAGES\s*=|pages >= payload\[|metadata\["http_attempts"\] >='), ('referrals/discovery.py', r'exhausted\(|SELECT COUNT\(\*\).*referral_search_runs'), ('referrals/planner.py', r'remaining\(used, cfg\[.daily_plan_limit.\]')],
 'G': [('reasoning/learning*.py', r'^def claim\(|INSERT INTO locks|optimizer_reservation|with lock\(')],
 'H': [('reasoning/learning*.py', r'retry_backoff|next_retry_at|cohort_cooldown|delay = min\(cfg\["cooldown"')],
 'I': [('integrations/worker_main.py', r'await page.next\(|range\(25\)')],
 'J': [('web/server.py', r'from applypilot.backup.runtime import Scheduler|autopilot_main\(|run_engine\('), ('web/engine.py', r'run_engine\(|run_cycle\(|supervise\(')],
 'K': [('cli.py', r'run_engine\(|autopilot_main\(|from applypilot.discovery.scheduler'), ('referrals/cli.py', r'from \.discovery import discover as run|from \.planner import prepare_jobs|drafts = create_initial_drafts\(')],
 'L': [('orchestration/*.py', r'claim_due_recovery|next_retry_at|lease_until|owner_token'), ('company_discovery/*.py', r'next_retry_at|verification_retry_at|UPDATE.*worker_id|INSERT INTO.*queue'), ('targets/*.py', r'next_retry_at|owner_token|lease_until')],
 'M': [('execution.py', r'connect_writer\(|_publish_status\(|queue_status\(')],
}
RETIRED = ('engine.py', 'autopilot.py', 'continuous.py', 'discovery/scheduler.py', 'reasoning/learning_budget.py')

# Explicit adapter/UI/diagnostic boundaries. New sleeps require call-path review.
SLEEP_BOUNDARIES = {
 ('atomic_io.py', 'replace_with_retry'): 'Windows atomic file replacement',
 ('llm_diagnostics.py', 'flush'): 'Diagnostic flush',
 ('postgres.py', '_start_embedded'): 'Embedded database startup readiness',
 ('postgres_capacity.py', 'budget_connection_class.connect'): 'Database driver connection capacity',
 ('startup.py', 'dashboard_result'): 'Dashboard readiness',
 ('apply/agent_sdk.py', 'run_application_session.execute.watch_owner'): 'Activity cancellation monitor',
 ('apply/agent_sdk.py', 'run_application_session.execute'): 'MCP connection readiness',
 ('apply/browseros_mcp.py', 'serve.heartbeat'): 'Browser transport heartbeat',
 ('apply/chrome.py', 'launch_chrome'): 'Browser startup',
 ('apply/launcher.py', '_start_worker_listener'): 'Local adapter HTTP listener startup',
 ('apply/launcher.py', '_activate_agent_tab'): 'Browser observation readiness',
 ('evidence/github.py', '_run_pack'): 'Finite subprocess cancellation monitor',
 ('onboarding/gate.py', 'wait'): 'Interactive credential mutation isolation',
 ('onboarding/storage.py', 'setup_lock'): 'Settings file mutation lock',
 ('operations/runtime.py', '_writer'): 'Nonblocking diagnostics sink retries',
 ('operations/worker_journal.py', 'flush'): 'Diagnostic flush',
 ('reasoning/rlm_sandbox.py', 'execute_code'): 'Finite sandbox subprocess readiness',
 ('tracking/gmail_browser.py', '_wait'): 'Browser UI observation wait',
 ('unattended/browser.py', 'pause'): 'Browser UI observation wait',
 ('web/events.py', 'pump'): 'Dashboard SSE transport',
 ('web/provider_health.py', '_probe'): 'Diagnostic subprocess readiness',
 ('web/server.py', 'start'): 'HTTP readiness',
 ('web/server.py', 'serve'): 'HTTP service lifetime',
}


def evidence(path, expression):
    file = ROOT / path
    if not file.is_file():
        return {'path': path, 'missing': True, 'matches': []}
    source = file.read_text(encoding='utf-8-sig')
    tree = ast.parse(source) if file.suffix == '.py' else None
    omitted = set()
    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                omitted.update(range(node.lineno, node.end_lineno + 1))
    return {'path': path, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
            'matches': [{'line': number, 'code': line.strip()[:240]}
                        for number, line in enumerate(source.splitlines(), 1)
                        if number not in omitted and not line.lstrip().startswith('#') and re.search(expression, line)]}


def build():
    failures = {key: [] for key in RULES}
    unverified = {key: [] for key in RULES}
    for key, patterns in FORBIDDEN.items():
        for pattern, expression in patterns:
            for path in sorted((ROOT / SRC).glob(pattern)):
                item = evidence(path.relative_to(ROOT).as_posix(), expression)
                if item['matches']:
                    failures[key].append(item)
    for name in RETIRED:
        if (ROOT / SRC / name).exists():
            failures['B' if name in RETIRED[:3] else 'G' if 'learning' in name else 'C'].append(
                {'path': SRC + name, 'reason': 'Retired execution module reappeared; inspect callers'})
    dask_imports, workflow_defs, activity_defs, sleeps, files = [], [], [], [], []
    for path in sorted((ROOT / SRC).rglob('*.py')):
        relative = path.relative_to(ROOT / SRC).as_posix()
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path), feature_version=(3, 12))
        files.append({'path': SRC + relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

        class Inspect(ast.NodeVisitor):
            def __init__(self):
                self.scope = []

            def visit_FunctionDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                call = ast.unparse(node.func)
                location = {'path': SRC + relative, 'line': node.lineno, 'function': '.'.join(self.scope), 'call': call}
                if call in {'time.sleep', 'asyncio.sleep'}:
                    reason = SLEEP_BOUNDARIES.get((relative, location['function']))
                    sleeps.append({**location, 'classification': reason or 'unverified'})
                    if not reason:
                        unverified['I'].append(location)
                if call.endswith('connect_writer') and (relative, location['function']) not in {
                        ('operations/runtime.py', '_writer'), ('operations/store.py', 'incident_action')}:
                    failures['M'].append(location)
                if relative.startswith('orchestration/') and relative.endswith('workflows.py'):
                    if call in {'open', 'get_connection', 'os.getenv', 'asyncio.sleep', 'time.sleep', 'subprocess.run', 'requests.get', 'httpx.get'}:
                        failures['C'].append({**location, 'reason': 'I/O or wall-clock wait inside Workflow code'})
                self.generic_visit(node)

        Inspect().visit(tree)
        for node in ast.walk(tree):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                       else [name.name for name in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                if module.split('.')[0] in {'dask', 'distributed'}:
                    dask_imports.append({'path': SRC + relative, 'line': node.lineno, 'module': module})
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or ast.unparse(decorator.func) not in {'workflow.defn', 'activity.defn'}:
                        continue
                    kind = ast.unparse(decorator.func)
                    name = next((keyword.value.value for keyword in decorator.keywords
                        if keyword.arg == 'name' and isinstance(keyword.value, ast.Constant)), node.name)
                    item = {'type': name, 'symbol': node.name, 'path': SRC + relative, 'line': node.lineno}
                    (workflow_defs if kind == 'workflow.defn' else activity_defs).append(item)
    failures['A'].extend(dask_imports)
    dependency = evidence('pyproject.toml', r'^\s*["\x27](?:dask|distributed)(?:[\[<>=~!"\x27])')
    if dependency['matches']:
        failures['A'].append(dependency)
    checks = []
    for key, (title, locators) in RULES.items():
        positive = [evidence(SRC + path, expression) for path, expression in locators]
        unverified[key].extend(item for item in positive if not item['matches'])
        status = 'failed' if failures[key] else 'unverified' if unverified[key] else 'passed'
        checks.append({'id': key, 'requirement': title, 'status': status, 'evidence': positive,
                       'violations': failures[key], 'unverified': unverified[key]})
    remaining = [{'check': row['id'], 'evidence': row['violations'] or row['unverified']}
                 for row in checks if row['status'] != 'passed']
    return {'schema_version': 2, 'source_architecture_complete': not remaining,
        'method': 'Reviewed Activity-to-domain call paths, positive source fingerprints, negative mechanism scans, whole-source AST import/call/decorator census. Native behavior and feature coverage are separate validations.',
        'checks': checks, 'remaining_known_legacy_paths': remaining,
        'dask_import_count': len(dask_imports), 'dask_imports': dask_imports,
        'workflow_definitions': workflow_defs, 'activity_definitions': activity_defs,
        'sleep_boundaries': sleeps, 'source_files': files,
        'registration_note': 'Definition census is not live Worker registration; native registration/replay checks run separately.',
        'infrastructure_classifications': [
            'WARP Redis ownership binds provider credentials to network tunnels/proxy exits; these are not ApplyPilot Activity workers.',
            'Onboarding credential enrollment and diagnostic CLI reads are finite infrastructure operations; domain discovery/model work uses Temporal.',
            'Provider wire timeouts, browser observation waits, artifact consistency locks, output page sizes and optimizer trial counts are adapter/domain contracts, not application work admission.',
            'Submission/message receipts, approval windows and business freshness timestamps do not form a SQL runnable queue.',
            'Operations SQL runs in the asynchronous journal writer; activity_scope and hot event emitters do not synchronously publish scheduler status.',
            'operations.store.incident_action writes only explicit dashboard acknowledgement/severity edits; it is not on application execution progress.',
        ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'ARCHITECTURE_AUDIT.json')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    report = build()
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'source_architecture_complete': report['source_architecture_complete'],
                      'dask_import_count': report['dask_import_count'],
                      'checks': {row['id']: row['status'] for row in report['checks']}}))
    if args.check and not report['source_architecture_complete']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
