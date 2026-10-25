export const label = value => String(value || '').replaceAll('_', ' ');

export function metadata(record) {
  const duration = Number(record.duration);
  return {
    status: record.status_label || label(record.state || record.status || record.outcome || record.task_state || 'recorded'),
    source: record.source_label || label(record.source || record.component),
    recorded_at: record.at || record.created_at, action: label(record.operation || record.event),
    severity: record.severity, error_kind: record.error_kind === 'none' ? undefined : label(record.error_kind),
    duration: record.duration != null && Number.isFinite(duration)
      ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(duration)} seconds` : undefined,
  };
}

export function identifiers(record) {
  return Object.fromEntries(['id', 'run_id', 'span_id', 'task_id', 'job_id', 'runtime_build']
    .filter(key => record[key]).map(key => [key, record[key]]));
}

export function recordText(record) {
  const fields = { title: record.title, ...metadata(record), message: record.reason || record.message || record.summary,
    next_action: record.next_action, ...identifiers(record), details: record.details };
  return Object.entries(fields).filter(([, value]) => value != null && value !== '').map(([key, value]) =>
    `${label(key)}: ${typeof value === 'object' ? JSON.stringify(value, null, 2) : value}`).join('\n');
}
