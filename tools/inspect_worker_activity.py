#!/usr/bin/env python3
"""Read worker history without running browsers, selection or model requests."""
from __future__ import annotations
import argparse
from collections import deque
import json
import os
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,help='Workspace root (defaults to APPLYPILOT_DIR or ~/.applypilot)')
    parser.add_argument('--worker',help='Application worker number, or preparation worker-key suffix')
    parser.add_argument('--run-id',help='Restrict to an Operations run ID')
    parser.add_argument('--event',help='Event prefix, e.g. selection or application')
    parser.add_argument('--last',type=int,default=100)
    parser.add_argument('--output',type=Path,help='Write the filtered JSON report to a new file')
    args=parser.parse_args()
    if args.last<1 or args.last>10000:parser.error('--last must be 1..10000')
    root=(args.root or Path(os.environ.get('APPLYPILOT_DIR',Path.home()/'.applypilot'))).expanduser()
    folder=root/'logs'/'worker-activity'
    if not folder.is_dir():parser.error(f'No worker activity directory: {folder}')
    # Collect worker-policy references before filtering shared explanation records.
    policies=set(); records=[];malformed=0;files=0
    for path in sorted(folder.glob('worker-*.jsonl')):
        if path.is_symlink():continue
        files+=1
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row=json.loads(line)
                        if not isinstance(row,dict) or row.get('format')!='applypilot_worker_activity':continue
                        if args.run_id and row.get('run_id')!=args.run_id:continue
                        match=(not args.worker or str(row.get('worker_id',''))==args.worker or
                               (bool(row.get('worker_key')) and row['worker_key'].endswith(args.worker)))
                        if match and row.get('policy_id'):policies.add((row.get('run_id'),row['policy_id']))
                        records.append((row,match))
                    except (ValueError,TypeError):malformed+=1
        except OSError as exc:
            print(f'Cannot read {path.name}: {type(exc).__name__}',file=sys.stderr)
    selected=[row for row,match in records if
              (match or (row.get('event')=='selection.explained' and (row.get('run_id'),row.get('policy_id')) in policies))
              and (not args.event or row.get('event','').startswith(args.event))]
    selected.sort(key=lambda r:r.get('at',''))
    report={'format':'applypilot_worker_activity_report','format_version':1,'files_read':files,
            'malformed_or_partial_lines_skipped':malformed,'matching_records':len(selected),
            'truncated':len(selected)>args.last,'records':selected[-args.last:]}
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        # Never overwrite a source journal or any existing workspace artifact.
        with args.output.open('x',encoding='utf-8') as out:out.write(text+'\n')
        print(f'Wrote {len(report["records"])} records to {args.output}')
    else:
        sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout,'reconfigure') else None
        print(text)

if __name__=='__main__':main()
