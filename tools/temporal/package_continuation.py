"""Build and verify a secret-excluded CONTINUATION delta from an exact ZIP.

This does not establish the earlier original baseline or certify migration
completion. It never emits ApplyPilot-Temporal-Complete.patch. It performs no
remote or production operation, and never deletes local secrets from its inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
NAME = 'ApplyPilot-Temporal-Continuation'
LOCAL_PARTS = {'.git', '__pycache__', '.pytest_cache', '.venv', 'venv', 'node_modules',
               '.mypy_cache', '.ruff_cache', '.playwright-mcp', 'chrome-workers', 'apply-workers'}
PRIVATE_NAMES = {'profile.json', 'resume.txt', 'resume.pdf', 'web-dashboard.token',
                 'web-dashboard.json', 'web-runtime.json', 'litellm_metrics_token',
                 '.envrc', 'free_sources_mcp.json'}
PATTERNS = (
    rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----',
    rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
    rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}\b',
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def excluded(name: str) -> bool:
    path = PurePosixPath(name)
    if any(part in LOCAL_PARTS or part.endswith('.pgstore') for part in path.parts):
        return True
    if name.startswith('docs/guides/secrets/'):
        return path.name != 'README.md'
    if path.name in PRIVATE_NAMES:
        return True
    if path.name == '.env' or (path.name.startswith('.env.') and not path.name.endswith('.example')):
        return True
    if path.suffix in {'.pyc', '.pyo', '.pgdump', '.key', '.p12', '.pfx'}:
        return True
    return False


def safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or '..' in path.parts or ':' in path.parts[0]:
        raise ValueError('Unsafe archive member path')
    return path


def baseline_files(archive: Path):
    files, excluded_paths, private_values = {}, [], set()
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() == 'private_key' and isinstance(item, str) and item.strip():
                    private_values.add(item.encode())
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    with zipfile.ZipFile(archive) as z:
        entries = [i for i in z.infolist() if not i.is_dir()]
        members = [safe_member(i.filename) for i in entries]
        roots = {m.parts[0] for m in members}
        strip = len(roots) == 1 and all(len(m.parts) > 1 for m in members)
        seen = set()
        for info, member in zip(entries, members):
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Symlink archive entries are not accepted')
            name = PurePosixPath(*member.parts[1:]).as_posix() if strip else member.as_posix()
            if name in seen:
                raise ValueError('Duplicate archive member: ' + name)
            seen.add(name)
            body = z.read(info)
            if excluded(name):
                excluded_paths.append(name)
                if name.startswith('docs/guides/secrets/') and name.endswith('.json'):
                    collect(json.loads(body))
                continue
            mode = 0o755 if info.external_attr >> 16 & 0o111 else 0o644
            files[name] = (body, mode)
    return files, sorted(excluded_paths), private_values


def source_files(root: Path):
    files, omitted = {}, []
    for path in sorted(root.rglob('*')):
        if not path.is_file() and not path.is_symlink():
            continue
        name = path.relative_to(root).as_posix()
        if excluded(name):
            if not any(part in LOCAL_PARTS for part in path.relative_to(root).parts):
                omitted.append(name)
            continue
        if path.is_symlink():
            raise ValueError('Distributable source contains a symlink: ' + name)
        files[name] = (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
    return files, omitted


def scan(files, private_values):
    failures = []
    for name, (body, _) in files.items():
        if any(value in body for value in private_values) or any(re.search(p, body) for p in PATTERNS):
            failures.append(name)
    if failures:
        # Filenames only: never write credential literals into errors or reports.
        raise ValueError('Potential credentials remain in deliverable files: ' + ', '.join(failures))


def write_tree(root: Path, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, (body, mode) in files.items():
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(mode)


def file_manifest(files):
    return {name: {'sha256': sha256(body), 'bytes': len(body), 'mode': format(mode, '04o')}
            for name, (body, mode) in sorted(files.items())}


def fingerprint(manifest):
    return sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())


def run(command, cwd: Path):
    result = subprocess.run(command, cwd=cwd, capture_output=True)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(command) + '\n' + result.stderr.decode(errors='replace'))
    return result


def build(archive: Path, expected_hash: str, source: Path, output: Path):
    archive, source, output = archive.resolve(), source.resolve(), output.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError('Output directory must be outside the source repository')
    actual_hash = sha256(archive.read_bytes())
    if actual_hash != expected_hash.lower():
        raise ValueError('Baseline archive SHA-256 mismatch; no patch produced')
    baseline, excluded_baseline, private_values = baseline_files(archive)
    final, omitted = source_files(source)
    scan(baseline, private_values)
    scan(final, private_values)
    output.mkdir(parents=True, exist_ok=True)
    patch_path, zip_path = output/(NAME+'.patch'), output/(NAME+'.zip')
    with tempfile.TemporaryDirectory(prefix='applypilot-continuation-') as tmp:
        base = Path(tmp)/'patch-repository'
        write_tree(base, baseline)
        run(['git', 'init', '-q'], base)
        for key, value in [('user.name','Local delivery validator'), ('user.email','validator@localhost'),
                           ('core.autocrlf','false'), ('core.quotePath','false')]:
            run(['git','config',key,value], base)
        run(['git','add','--all','--force'], base)
        run(['git','commit','-q','-m','Exact sanitized uploaded continuation baseline'], base)
        for child in list(base.iterdir()):
            if child.name == '.git':
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        write_tree(base, final)
        run(['git','add','--all','--force'], base)
        patch = run(['git','diff','--cached','--binary','--full-index','--no-renames','HEAD'], base).stdout
        scan({'continuation.patch': (patch, 0o644)}, private_values)
        patch_path.write_bytes(patch)
        checks = []
        # Two independent fresh extractions, not a reverse-applied working tree.
        check_dir = Path(tmp)/'fresh-check'
        fresh, _, _ = baseline_files(archive)
        write_tree(check_dir, fresh)
        cmd = ['git','apply','--check',str(patch_path)]
        result = run(cmd, check_dir)
        checks.append({'name':'continuation_patch_apply_check','command':cmd,
                       'working_tree':'fresh secret-excluded extraction of uploaded continuation',
                       'exit_code':result.returncode,'stdout':result.stdout.decode(), 'stderr':result.stderr.decode()})
        apply_dir = Path(tmp)/'fresh-apply'
        fresh, _, _ = baseline_files(archive)
        write_tree(apply_dir, fresh)
        cmd = ['git','apply',str(patch_path)]
        result = run(cmd, apply_dir)
        checks.append({'name':'continuation_patch_actual_application','command':cmd,
                       'working_tree':'second fresh secret-excluded extraction of uploaded continuation',
                       'exit_code':result.returncode,'stdout':result.stdout.decode(), 'stderr':result.stderr.decode()})
        applied, _ = source_files(apply_dir)
        intended_manifest, applied_manifest = file_manifest(final), file_manifest(applied)
        if intended_manifest != applied_manifest:
            bad = sorted(k for k in intended_manifest.keys() | applied_manifest.keys()
                         if intended_manifest.get(k) != applied_manifest.get(k))
            raise AssertionError('Applied tree mismatch: ' + ', '.join(bad))
        changes = {'added': sorted(final.keys()-baseline.keys()),
                   'removed': sorted(baseline.keys()-final.keys()),
                   'modified': sorted(k for k in final.keys() & baseline.keys() if final[k] != baseline[k])}
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, (body, mode) in sorted(final.items()):
            info = zipfile.ZipInfo('ApplyPilot/'+name, date_time=(2026,9,28,0,0,0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, body)
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            raise AssertionError('Generated ZIP integrity check failed')
        zipped = {i.filename.removeprefix('ApplyPilot/'): (z.read(i), 0o755 if i.external_attr >> 16 & 0o111 else 0o644)
                  for i in z.infolist()}
    scan(zipped, private_values)
    if file_manifest(zipped) != intended_manifest:
        raise AssertionError('ZIP differs from intended source tree')
    report = {'schema_version':1,'delivery_kind':'continuation_only','migration_complete':False,
        'baseline':{'archive':archive.name,'sha256':actual_hash,'sanitized_file_count':len(baseline)},
        'original_baseline_established':False,'original_baseline_patch_produced':False,
        'final_repository_file_count':len(final),'final_tree_fingerprint':fingerprint(intended_manifest),
        'secret_exclusions':sorted(set(excluded_baseline+omitted)),
        'distinct_uploaded_private_keys_scanned':len(private_values),
        'secret_scan':'No known uploaded private-key bytes or configured credential signatures in source ZIP or patch. Not a universal secret detector.',
        'patch_checks':checks,'tree_comparison':{'status':'passed','files_compared':len(final),'mismatches':[],
            'scope':'SHA-256, size, executable mode and path set; documented local/private exclusions applied to both trees'},
        'zip_integrity':'passed','zip_tree_comparison':'passed','changes':changes,
        'artifacts':{p.name:{'sha256':sha256(p.read_bytes()),'bytes':p.stat().st_size} for p in (patch_path,zip_path)},
        'repository_files':intended_manifest,
        'note':'Fresh validation exclusions do not delete or overwrite credentials in the input archive or source tree. This patch contains no secret deletion hunks.'}
    (output/'CONTINUATION_PATCH_VERIFICATION.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in {'repository_files','changes'}}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-zip', required=True, type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--source-root', default=ROOT, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-fA-F]{64}', args.expected_sha256):
        parser.error('--expected-sha256 must be the exact 64-character archive hash')
    build(args.baseline_zip,args.expected_sha256,args.source_root,args.output_dir)


if __name__ == '__main__':
    main()
