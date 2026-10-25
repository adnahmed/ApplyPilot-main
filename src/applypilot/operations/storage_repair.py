"""Native PostgreSQL health, vacuum/analyze and verified dump maintenance.

No database-file rebuilding or copying exists in the PostgreSQL release.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
from applypilot import postgres, config
from applypilot.postgres_sql import identifier


def health(path=config.DB_PATH) -> dict:
    with closing(postgres.connect(path, readonly=True, isolation_level=None)) as conn:
        rows = conn.execute('''SELECT c.relname AS table_name,c.reltuples::bigint AS estimated_rows,
            pg_total_relation_size(c.oid) AS total_bytes FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema()
            AND c.relkind='r' ORDER BY c.relname''').fetchall()
        return {'engine':'postgresql', 'schema':conn.schema,
                'version':conn.execute('SELECT version()').fetchone()[0],
                'estimates_only':True,'tables':[dict(row) for row in rows]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=config.DB_PATH)
    parser.add_argument('--vacuum',action='store_true',help='Explicit VACUUM (ANALYZE), not VACUUM FULL')
    parser.add_argument('--dump',type=Path)
    args=parser.parse_args(argv)
    if args.dump:
        postgres.dump_store(args.db,args.dump)
        postgres.validate_dump(args.dump)
    if args.vacuum:
        with closing(postgres.connect(args.db,isolation_level=None)) as conn:
            for row in conn.execute('SELECT tablename FROM pg_tables WHERE schemaname=current_schema()').fetchall():
                conn.execute('VACUUM (ANALYZE) '+identifier(row[0]))
    print(json.dumps(health(args.db),indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
