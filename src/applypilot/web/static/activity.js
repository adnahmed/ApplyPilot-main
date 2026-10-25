import { recordText, buttonSelector } from './components.js';
const read = (template) => JSON.parse(template.content.textContent);
const label = (value) => String(value || '').replaceAll('_', ' ');
const notify = (message) => {
  const toast = document.getElementById('toast');
  if (toast) { toast.textContent = message; toast.hidden = false; }
};

// A live replacement removes every row. Native scroll anchoring can then pick
// the footer and carry the viewport to the end when the feed grows back.
const refreshingFeeds = new WeakMap();
document.addEventListener('htmx:beforeSwap', (event) => {
  const { target, xhr, shouldSwap } = event.detail;
  if (!shouldSwap || xhr.status >= 400 || target?.id === 'workspace' || !target?.querySelector('[data-activity-feed]')) return;
  const current = target.querySelector('[data-activity-feed]');
  const response = document.createElement('template');
  response.innerHTML = event.detail.serverResponse;
  const incoming = response.content.querySelector('[data-activity-feed]');
  if (!incoming) return;
  const previous = snapshot(current), next = snapshot(incoming);
  const rows = [...target.querySelectorAll('[data-activity-id]')];
  const unchanged = value => JSON.stringify([value.filters, value.limit, value.warnings, value.items]);
  if (unchanged(previous) === unchanged(next) && rows.every(row => /^[A-Za-z][A-Za-z0-9_-]*$/.test(row.id))) {
    current.querySelector('[data-activity-context]').replaceWith(incoming.querySelector('[data-activity-context]'));
    event.detail.shouldSwap = false;
    return;
  }
  const byId = new Map(rows.map(row => [row.id, row]));
  for (const row of incoming.querySelectorAll('[data-activity-id]')) {
    const existing = byId.get(row.id);
    if (existing && existing.querySelector('[data-activity-record]').content.textContent
      === row.querySelector('[data-activity-record]').content.textContent) row.setAttribute('hx-preserve', 'true');
  }
  event.detail.serverResponse = response.innerHTML;
  const visible = rows.find(row => row.getBoundingClientRect().bottom > 0 && row.getBoundingClientRect().top < innerHeight);
  const active = document.activeElement;
  const focusRow = active?.closest('[data-activity-id]');
  refreshingFeeds.set(xhr, { x: scrollX, y: scrollY, id: current.getBoundingClientRect().top < 0 ? visible?.dataset.activityId : null,
    offset: visible?.getBoundingClientRect().top, focusId: focusRow?.dataset.activityId,
    focusIndex: focusRow ? [...focusRow.querySelectorAll('button,wa-button,a')].indexOf(active) : -1,
    anchorStyle: document.documentElement.style.overflowAnchor });
  document.documentElement.style.overflowAnchor = 'none';
});

function restoreFeedViewport(event, settled) {
  const saved = refreshingFeeds.get(event.detail.xhr);
  if (!saved) return;
  const target = event.detail.target;
  const row = saved.id && target?.querySelector(`[data-activity-id="${CSS.escape(saved.id)}"]`);
  const y = row ? scrollY + row.getBoundingClientRect().top - saved.offset : saved.y;
  if (saved.focusId && saved.focusIndex >= 0 && document.activeElement === document.body) {
    const focusRow = target?.querySelector(`[data-activity-id="${CSS.escape(saved.focusId)}"]`);
    focusRow?.querySelectorAll('button,wa-button,a')[saved.focusIndex]?.focus({ preventScroll: true });
  }
  window.scrollTo({ left: saved.x, top: y, behavior: 'instant' });
  if (settled) {
    document.documentElement.style.overflowAnchor = saved.anchorStyle;
    refreshingFeeds.delete(event.detail.xhr);
  }
}
document.addEventListener('htmx:afterSwap', event => restoreFeedViewport(event, false));
document.addEventListener('htmx:afterSettle', event => restoreFeedViewport(event, true));
document.addEventListener('htmx:swapError', event => restoreFeedViewport(event, true));

function snapshot(feed) {
  const items = [...feed.querySelectorAll('.activity-row > [data-activity-record]')].map(read);
  return { ...read(feed.querySelector('[data-activity-context]')), exported_at: new Date().toISOString(),
    count: items.length, items };
}

function activityText(data) {
  return `ApplyPilot activity — ${data.count} records\nCaptured: ${data.at || data.exported_at}\n`
    + `Filters: ${JSON.stringify(data.filters)}\nLimit: ${data.limit} most recent matching records\n`
    + (data.warnings.length ? `Warnings: ${data.warnings.join('; ')}\n` : '')
    + '\n' + data.items.map(recordText).join('\n\n---\n\n');
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    notify('Copied, including recorded error details.');
  } catch {
    // The fallback must stay inside the active dialog's focus trap.
    const active = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text; field.className = 'clipboard-buffer';
    (document.querySelector('wa-dialog[open],dialog[open]') || document.body).append(field);
    field.focus({ preventScroll: true }); field.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } catch { /* Offer the download if clipboard access fails. */ }
    field.remove(); active?.focus({ preventScroll: true });
    notify(copied ? 'Copied, including recorded error details.' : 'Clipboard access failed. Use Export JSON to save the details.');
  }
}

function download(value) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url; link.download = `applypilot-activity-${new Date().toISOString().replaceAll(':', '-')}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function showRecord(record) {
  const dialog = document.getElementById('details');
  const content = document.getElementById('detail-content');
  if (!dialog || !content) return;
  const detail = document.createElement('ap-activity-details');
  detail.record = record;
  content.replaceChildren(detail);
  dialog.label = record.title || label(record.operation || record.event) || 'Activity details';
  dialog.open = true;
}

document.addEventListener('ap:copy-record', event => copy(recordText(event.detail)));
document.addEventListener('ap:export-record', event => download(event.detail));

document.addEventListener('click', (event) => {
  const button = event.target.closest(buttonSelector);
  if (!button) return;
  const row = button.closest('[data-error-record], .activity-row');
  if (button.hasAttribute('data-open-activity')) showRecord(read(row.querySelector('[data-activity-record]')));
  if (button.hasAttribute('data-copy-record')) copy(recordText(read(row.querySelector('[data-activity-record]'))));
  const feed = button.closest('[data-activity-feed]');
  if (button.hasAttribute('data-copy-activity')) copy(activityText(snapshot(feed)));
  if (button.hasAttribute('data-export-activity')) download(snapshot(feed));
});

document.getElementById('details')?.addEventListener('wa-after-hide', (event) => {
  event.target.removeAttribute('aria-labelledby');
});
