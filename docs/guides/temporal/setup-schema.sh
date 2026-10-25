#!/bin/sh
# Explicit idempotent version-table initialization and forward migrations.
# No overwrite/drop flags; a failed migration stops deployment.
set -eu
: "${SQL_HOST:?SQL_HOST is required}"
: "${SQL_USER:?SQL_USER is required}"
: "${SQL_PASSWORD:?SQL_PASSWORD is required}"
for database in temporal temporal_visibility; do
  temporal-sql-tool --plugin postgres12 --ep "$SQL_HOST" -p "${SQL_PORT:-5432}" \
    -u "$SQL_USER" --db "$database" setup-schema -v 0.0
  if [ "$database" = temporal ]; then
    schema=/etc/temporal/schema/postgresql/v12/temporal/versioned
  else
    schema=/etc/temporal/schema/postgresql/v12/visibility/versioned
  fi
  temporal-sql-tool --plugin postgres12 --ep "$SQL_HOST" -p "${SQL_PORT:-5432}" \
    -u "$SQL_USER" --db "$database" update-schema -d "$schema"
done
