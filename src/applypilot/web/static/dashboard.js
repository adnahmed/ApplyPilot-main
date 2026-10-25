import { connectLive } from './live.js';
import './activity.js';
import { initializeCareerForm } from './career.js';
import { componentsReady, controlSelector, buttonSelector } from './components.js';

const toast = (message) => {
  const node = document.getElementById('toast');
  if (node) { node.textContent = message; node.hidden = false; }
};
const signal = (name) => document.body.dispatchEvent(new CustomEvent(name));
const csrf = () => document.querySelector('meta[name="ap-csrf"]')?.content || '';

function statusUnavailable(target) {
  if (target?.id !== 'runtime' || target.querySelector('.runtime-strip')) return;
  const message = document.createElement('span');
  message.className = 'error';
  message.setAttribute('role', 'alert');
  message.textContent = 'Could not load automation status.';
  const retry = document.createElement('wa-button');
  retry.type = 'button';
  retry.size = 's';
  retry.textContent = 'Retry';
  retry.addEventListener('click', () => signal('ap:status'));
  target.replaceChildren(message, ' ', retry);
}

document.addEventListener('ap:form-payloads', (event) => {
  const { view, payloads = {} } = event.detail;
  document.querySelectorAll('form').forEach((form) => {
    if (form.querySelector('[name="_view"]')?.value !== view) return;
    const name = form.querySelector('[name="_form"]')?.value;
    const input = form.querySelector('[name="_payload"]');
    if (input && Object.hasOwn(payloads, name)) input.value = JSON.stringify(payloads[name]);
  });
});
document.addEventListener('ap:search-saved', (event) => {
  const { before, after, changed = [] } = event.detail;
  if (!before || !after) return;
  const affected = new Set(changed);
  const dependencies = {
    'search-queries': ['queries'], 'search-markets': ['markets'],
    'search-preferences': ['markets', 'preference_rules'],
    'search-sources': ['markets', 'sources', 'jobspy_sites', 'defaults', 'exclude_titles', 'location_labels'],
    'search-exploration': ['exploration', 'probes'],
  };
  for (const [name, keys] of Object.entries(dependencies)) {
    if (keys.some(key => affected.has(key))) continue;
    const input = document.querySelector(`#form-${name} [name="_payload"]`);
    if (!input) continue;
    let payload;
    try { payload = JSON.parse(input.value); } catch { continue; }
    if (payload.preferences_revision === before) {
      input.value = JSON.stringify({ ...payload, preferences_revision: after });
    }
  }
});
const draftPrefix = 'applypilot-form:';
const baselines = new WeakMap();
const requests = new WeakMap();
const drafts = new Map();
const restoredFocus = new WeakMap();
const expandedPanels = new Map();
let live, charts, chartModule;
let liveState = 'connecting';
let profileRefreshPending = false, profileRefreshAgain = false;
const controls = (form) => [...form.querySelectorAll(controlSelector)]
  .filter((node) => !['hidden', 'submit', 'button', 'reset'].includes(node.type));
const plainControls = (form) => controls(form).filter((node) => !node.closest('[data-row]'));
const privateField = (node) => ['password', 'file'].includes(node.type)
  || /password|token|secret|credential|api.?key|cookie/i.test(node.name)
  || /password/.test(node.autocomplete || '');
const isCheck = node => node.localName === 'wa-checkbox' || ['checkbox', 'radio'].includes(node.type);
const valueOf = node => isCheck(node) ? node.checked
  : node.multiple ? (Array.isArray(node.value) ? [...node.value] : [...node.selectedOptions].map(option => option.value)) : (node.value ?? '');
function rowKey(row) {
  if (!row.dataset.rowKey) {
    const index = row.querySelector('[name$="._index"]')?.value;
    row.dataset.rowKey = index !== undefined && Number(index) >= 0 ? `saved:${index}` : `new:${crypto.randomUUID()}`;
  }
  return row.dataset.rowKey;
}
const rowValues = (group) => [...group.querySelector('[data-row-list]').children].map((row) => ({
  ...Object.fromEntries([...row.querySelectorAll('[name]')].map((node) => [node.name.split('.').at(-1), valueOf(node)])),
  _rowKey: rowKey(row),
}));
const valuesOf = (form) => Object.fromEntries([
  ...plainControls(form).map((node) => [node.name, valueOf(node)]),
  ...[...form.querySelectorAll('[data-rows]')].map((group) => [group.dataset.rows, rowValues(group)]),
]);
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const draftKey = (form) => draftPrefix + JSON.stringify([
  form.querySelector('[name="_view"]')?.value || document.querySelector('#workspace')?.dataset.view || '',
  form.getAttribute('hx-post') || form.getAttribute('action') || location.pathname,
  form.id || form.querySelector('[name="_form"]')?.value || '',
  ...(form.id === 'form-career-report' ? ['job_url', 'mode', 'report_id'].map(key => new URLSearchParams(location.search).get(key) || '') : []),
]);
const panelKey = (node) => [document.querySelector('#workspace')?.dataset.view, location.search, node?.id].join(':');
const detailKey = (node) => {
  const parts = [];
  for (let item = node; item?.matches('details,wa-details'); item = item.parentElement?.closest('details,wa-details')) {
    parts.unshift(item.dataset.disclosureKey || item.getAttribute('summary') || item.querySelector(':scope > summary,:scope > [slot=summary]')?.textContent.trim() || '');
  }
  return parts.join(' / ');
};
function conditionalFields(form) {
  if (form.id === 'form-career-report') {
    const required = ['interview_brief', 'evaluate', 'interview_plan', 'interview_practice', 'negotiate']
      .includes(form.querySelector('[name=mode]')?.value);
    form.querySelectorAll('[name=company_name],[name=job_description]').forEach(node => { node.required = required; });
  }
  form.querySelectorAll('[data-limit-mode]').forEach((select) => {
    const wrapper = select.closest('fieldset').querySelector('[data-limit-value]');
    wrapper.hidden = select.value !== 'limited';
    (wrapper.matches('wa-input') ? wrapper : wrapper.querySelector('input')).disabled = wrapper.hidden;
  });
  form.querySelectorAll('[data-show-field]').forEach((wrapper) => {
    const source = controls(form).find((node) => node.name === wrapper.dataset.showField);
    wrapper.hidden = source?.value !== wrapper.dataset.showValue;
    const dependents = wrapper.matches(controlSelector) ? [wrapper] : wrapper.querySelectorAll(controlSelector);
    dependents.forEach(node => { node.disabled = wrapper.hidden; });
  });
}
function markDraft(form, saved, privateChanges = false, persisted = true) {
  const label = form.querySelector('.dirty-label');
  if (!label) return;
  label.replaceChildren();
  if (!saved && !privateChanges) return;
  label.append(saved ? (persisted ? 'Unsaved changes kept in this tab. ' : 'Unsaved changes; tab storage is unavailable. ')
    : 'Unsaved credentials or file selection stay on this page. ');
  if (saved && privateChanges) label.append('Credentials and files are not kept. ');
  const discard = document.createElement('wa-button');
  discard.type = 'button'; discard.size = 's'; discard.appearance = 'plain';
  discard.dataset.discardDraft = ''; discard.textContent = 'Discard changes';
  label.append(discard);
}
function retainDraft(form, baseline = baselines.get(form)) {
  if (!baseline) return;
  const changed = {};
  let privateChanges = false;
  plainControls(form).forEach((node) => {
    const value = valueOf(node);
    if (equal(value, baseline[node.name])) return;
    if (privateField(node)) privateChanges = true;
    else changed[node.name] = value;
  });
  form.querySelectorAll('[data-rows]').forEach((group) => {
    const rows = rowValues(group);
    if (!equal(rows, baseline[group.dataset.rows])) changed[group.dataset.rows] = rows;
  });
  const saved = Object.keys(changed).length > 0;
  const key = draftKey(form);
  drafts.set(key, changed);
  let persisted = true;
  try {
    if (saved) sessionStorage.setItem(key, JSON.stringify(changed));
    else sessionStorage.removeItem(key);
  } catch { persisted = false; }
  markDraft(form, saved, privateChanges, persisted);
}
function setValue(node, value) {
  if (isCheck(node)) node.checked = value === true;
  else if (node.localName === 'wa-select') node.value = value;
  else if (node.multiple && Array.isArray(value)) [...node.options].forEach((option) => { option.selected = value.includes(option.value); });
  else if (typeof value === 'string') node.value = value;
  if (node.matches('[data-opportunity-value]')) node.dispatchEvent(new Event('ap:restore'));
}
function initializeForm(form) {
  if (baselines.has(form)) return;
  baselines.set(form, valuesOf(form));
  let saved;
  const key = draftKey(form);
  if (drafts.has(key)) saved = drafts.get(key);
  else try { saved = JSON.parse(sessionStorage.getItem(key) || '{}'); } catch { saved = {}; }
  form.querySelectorAll('[data-rows]').forEach((group) => {
    const rows = saved?.[group.dataset.rows];
    if (!Array.isArray(rows)) return;
    const list = group.querySelector('[data-row-list]');
    list.replaceChildren();
    rows.forEach((values) => appendRow(group, values));
  });
  plainControls(form).forEach((node) => {
    if (!privateField(node) && saved && Object.hasOwn(saved, node.name)) setValue(node, saved[node.name]);
  });
  conditionalFields(form);
  retainDraft(form);
  initializeCareerForm(form);
}
function renumberRows(group) {
  [...group.querySelector('[data-row-list]').children].forEach((row, index) => {
    row.querySelectorAll('[name]').forEach((node) => {
      node.name = `${group.dataset.rows}.${index}.${node.name.split('.').at(-1)}`;
    });
  });
}
function appendRow(group, values = {}) {
  const fragment = document.importNode(group.querySelector('template').content, true);
  const row = fragment.querySelector('[data-row]');
  if (row && typeof values._rowKey === 'string') row.dataset.rowKey = values._rowKey;
  fragment.querySelectorAll('[name]').forEach((node) => {
    const key = node.name.split('.').at(-1);
    if (Object.hasOwn(values, key)) setValue(node, values[key]);
  });
  group.querySelector('[data-row-list]').append(fragment);
  renumberRows(group);
}
function rebaseSavedRows(form, submitted) {
  form.querySelectorAll('[data-rows]').forEach((group) => {
    const name = group.dataset.rows;
    const rows = submitted[name];
    if (!Array.isArray(rows)) return;
    const positions = new Map(rows.map((row, index) => [row._rowKey, index]));
    for (const row of group.querySelector('[data-row-list]').children) {
      const index = positions.get(rowKey(row));
      if (index === undefined) continue;
      const marker = row.querySelector('[name$="._index"]');
      if (marker) marker.value = String(index);
      row.dataset.rowKey = `saved:${index}`;
    }
    submitted[name] = rows.map((row, index) => ({ ...row, _index: String(index), _rowKey: `saved:${index}` }));
  });
}
async function initialize(root = document) {
  await componentsReady(root);
  if (root !== document && !root.isConnected) return;
  if (root.matches?.('form.edit-form')) initializeForm(root);
  root.querySelectorAll?.('form.edit-form').forEach(initializeForm);
  root.querySelectorAll?.('[data-number]').forEach((node) => {
    const raw = node.dataset.numberValue || node.textContent.trim().replaceAll(',', '');
    if (raw && Number.isFinite(Number(raw))) {
      node.dataset.numberValue = raw;
      node.textContent = new Intl.NumberFormat(undefined, { maximumFractionDigits: 20 }).format(Number(raw));
    }
  });
  root.querySelectorAll?.('time[data-time]').forEach((node) => {
    const raw = node.getAttribute('datetime') || '';
    if (!raw) return;
    const numeric = /^\d{9,13}(?:\.\d+)?$/.test(raw);
    const date = new Date(numeric ? Number(raw) * (Number(raw) < 1e12 ? 1000 : 1) : raw);
    if (!Number.isFinite(date.getTime())) return;
    const options = /^\d{4}-\d{2}-\d{2}$/.test(raw)
      ? { dateStyle: 'medium', timeZone: 'UTC' }
      : { dateStyle: 'medium', timeStyle: node.dataset.timePrecision === 'seconds' ? 'medium' : 'short' };
    node.textContent = new Intl.DateTimeFormat(undefined, options).format(date);
    node.title = raw;
  });
  if (root.matches?.('canvas[data-chart]') || root.querySelector?.('canvas[data-chart]')) {
    chartModule ||= import('./dashboard-charts.js');
    chartModule.then((module) => {
      charts = module;
      if (root === document || root.isConnected) module.mountCharts(root);
    }).catch(() => { /* Readable chart data remains available without JavaScript. */ });
  }
}
function openFormAnchor() {
  let id;
  try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
  if (!id.startsWith('form-')) return;
  const target = document.getElementById(id);
  if (!target) return;
  for (let ancestor = target.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.matches('details,wa-details')) ancestor.open = true;
  }
  target.scrollIntoView({ block: 'start' });
}
function syncDisplayControls() {
  document.querySelectorAll('[data-stream-status]').forEach((node) => {
    node.textContent = liveState === 'paused' && document.hidden ? 'Live when this tab is active'
      : liveState === 'live' ? 'Live' : liveState === 'connecting' ? 'Connecting…' : 'Live: ' + liveState;
  });
}
function startLive() {
  if (live || !csrf()) return;
  live = connectLive({
    onStatus: () => signal('ap:status'), onDataChanged: () => signal('ap:data'),
    onAutomationChanged: () => { signal('ap:automation'); signal('ap:status'); },
    onSessionExpired: () => toast('Your session expired. Open your private dashboard link again. Unsaved noncredential fields are kept in this tab.'),
    onState: (state) => { liveState = state; syncDisplayControls(); },
  });
}
function profileFieldsBusy(grid) {
  return [...grid.querySelectorAll('form.edit-form')].some((form) =>
    form.getAttribute('aria-busy') === 'true' || controls(form).some((node) =>
      privateField(node) && !equal(valueOf(node), baselines.get(form)?.[node.name])));
}
async function refreshProfileFields() {
  if (document.querySelector('#workspace')?.dataset.view !== 'profile' || !window.htmx) {
    profileRefreshAgain = false;
    return;
  }
  const grid = document.querySelector('#workspace .forms-grid');
  if (!grid) return;
  if (profileRefreshPending || profileFieldsBusy(grid)) {
    profileRefreshAgain = true;
    return;
  }
  profileRefreshPending = true;
  profileRefreshAgain = false;
  grid.querySelectorAll('form.edit-form').forEach((form) => retainDraft(form));
  try {
    await window.htmx.ajax('GET', '/ui/profile-fields', { target: grid, swap: 'outerHTML', select: '.forms-grid' });
  } catch {
    toast('Profile updates are saved. Reload this page to refresh the fields; your edits are retained.');
  } finally {
    profileRefreshPending = false;
    if (profileRefreshAgain) refreshProfileFields();
  }
}
document.addEventListener('ap:profile-saved', () => refreshProfileFields());
document.addEventListener('htmx:configRequest', (event) => {
  event.detail.headers['X-ApplyPilot-CSRF'] = csrf();
  const form = event.detail.elt?.closest('form.edit-form');
  if (!form || event.detail.verb !== 'post') return;
  initializeForm(form);
  const route = form.getAttribute('hx-post') || form.getAttribute('action') || '';
  if (/\/setup\/save-(profile|searches)$/.test(route)) {
    const baseline = baselines.get(form);
    const current = valuesOf(form);
    event.detail.parameters._changed_fields = JSON.stringify(Object.keys(current)
      .filter((name) => !equal(current[name], baseline[name])));
  }
});
document.addEventListener('htmx:beforeRequest', (event) => {
  const submitting = event.detail.elt?.closest('form');
  if (submitting && event.detail.requestConfig?.verb === 'post') {
    submitting.setAttribute('aria-busy', 'true');
    const errors = submitting.querySelector('[data-errors]');
    if (errors) errors.textContent = '';
    const button = submitting.querySelector('wa-button[type="submit"],button[type="submit"]');
    if (button) {
      button.loading = true;
      button.dataset.idleLabel = button.textContent;
      button.textContent = /check|verify/i.test(button.textContent) ? 'Checking…'
        : /connect/i.test(button.textContent) ? 'Connecting…' : /import/i.test(button.textContent) ? 'Importing…' : 'Working…';
    }
  }
  const form = event.detail.elt?.closest('form.edit-form');
  const route = form?.getAttribute('hx-post') || form?.getAttribute('action') || '';
  if (form && event.detail.requestConfig?.verb === 'post' && !route.endsWith('/import-preview')) {
    requests.set(event.detail.xhr, { form, values: valuesOf(form) });
  }
});
document.addEventListener('htmx:afterRequest', (event) => {
  const form = event.detail.elt?.closest('form');
  form?.removeAttribute('aria-busy');
  const button = form?.querySelector('[data-idle-label]');
  if (button) { button.loading = false; button.textContent = button.dataset.idleLabel; delete button.dataset.idleLabel; }
  if (profileRefreshAgain && !profileRefreshPending) refreshProfileFields();
});
document.addEventListener('htmx:beforeProcessNode', (event) => {
  if (event.detail.elt.matches?.('a[href^="/ui/"]')) event.detail.elt.setAttribute('hx-disabled-elt', 'this');
});
document.addEventListener('htmx:beforeSwap', (event) => {
  const { xhr, target } = event.detail;
  if (xhr.status >= 400) {
    event.detail.shouldSwap = false;
    statusUnavailable(target);
    let message = xhr.responseText;
    try { message = JSON.parse(message).error || message; } catch { /* Keep HTML errors as text. */ }
    const errors = event.detail.requestConfig?.elt?.closest('form')?.querySelector('[data-errors]');
    if (errors) { errors.textContent = message; errors.tabIndex = -1; errors.focus(); }
    else toast(message || 'The update failed. Your current view has been retained.');
    return;
  }
  if (!event.detail.shouldSwap) return;
  if (event.detail.requestConfig?.path === '/ui/profile-fields') {
    if (document.querySelector('#workspace')?.dataset.view !== 'profile' || (target && profileFieldsBusy(target))) {
      event.detail.shouldSwap = false;
      profileRefreshAgain = document.querySelector('#workspace')?.dataset.view === 'profile';
      return;
    }
    target?.querySelectorAll('form.edit-form').forEach((form) => retainDraft(form));
    const active = document.activeElement;
    const caret = active?.shadowRoot?.activeElement || active;
    if (target?.contains(active) && active.name) {
      restoredFocus.set(xhr, { form: active.closest('form')?.id, name: active.name,
        start: caret.selectionStart, end: caret.selectionEnd, direction: caret.selectionDirection });
    }
  }
  if (target?.querySelector('details,wa-details')) {
    expandedPanels.set(panelKey(target), new Set([...target.querySelectorAll('details[open],wa-details[open]')].map(detailKey)));
  }
  charts?.cleanupCharts(target);
  const submitted = requests.get(xhr);
  if (submitted) {
    // Preserve edits made after the request began for the next save.
    rebaseSavedRows(submitted.form, submitted.values);
    baselines.set(submitted.form, submitted.values);
    retainDraft(submitted.form, submitted.values);
    requests.delete(xhr);
  }
});
document.addEventListener('htmx:sendError', (event) => {
  statusUnavailable(event.detail.target);
  toast('Connection lost. Your current view and unsaved changes are retained.');
});
async function afterSwap(target, request) {
  const open = expandedPanels.get(panelKey(target));
  if (open) target.querySelectorAll('details,wa-details').forEach((node) => { node.open = open.has(detailKey(node)); });
  await initialize(target);
  const completedSelector = '.action-result[data-complete="true"]:not([data-announced])';
  const completed = [...(target.matches?.(completedSelector) ? [target] : []), ...target.querySelectorAll(completedSelector)];
  if (completed.length) {
    completed.forEach((node) => { node.dataset.announced = 'true'; });
    signal('ap:data');
    if (completed.some((node) => node.dataset.profileChanged === 'true' || ['completed', 'complete', 'success'].includes(node.dataset.state))) refreshProfileFields();
  }
  if (target.id === 'detail-content') {
    const dialog = document.getElementById('details');
    const heading = target.querySelector('h2');
    if (dialog) dialog.label = heading?.textContent || 'Details';
    if (heading) heading.hidden = true;
    if (dialog && !dialog.open) dialog.open = true;
  }
  if (target.id === 'workspace') {
    const filtering = request?.elt?.matches('form.filters');
    if (!filtering) {
      document.getElementById('main')?.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'instant' });
      openFormAnchor();
    }
    signal('ap:status'); syncDisplayControls();
  }
}
document.addEventListener('htmx:afterSwap', async (event) => {
  const target = event.detail.target;
  await afterSwap(target?.isConnected ? target : document.getElementById(target?.id) || event.target, event.detail.requestConfig);
  const saved = restoredFocus.get(event.detail.xhr);
  if (saved) {
    restoredFocus.delete(event.detail.xhr);
    const form = document.getElementById(saved.form);
    const field = [...(form?.elements || [])].find((node) => node.name === saved.name);
    if (field && (!document.activeElement || document.activeElement === document.body)) {
      field.focus({ preventScroll: true });
      if (saved.start !== null && saved.start !== undefined && typeof field.setSelectionRange === 'function') {
        try { field.setSelectionRange(saved.start, saved.end, saved.direction); } catch { /* Nontext inputs have no caret. */ }
      }
    }
  }
});
document.addEventListener('htmx:historyRestore', () => initialize());
window.addEventListener('hashchange', openFormAnchor);
for (const name of ['input', 'change']) document.addEventListener(name, (event) => {
  const form = event.target.closest('form.edit-form');
  if (!form) return;
  conditionalFields(form); retainDraft(form);
  if (profileRefreshAgain && !profileRefreshPending) refreshProfileFields();
});
document.addEventListener('click', async (event) => {
  const link = event.target.closest('wa-button[href][hx-get]');
  if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) event.preventDefault();
  const button = event.target.closest(buttonSelector);
  if (button?.hasAttribute('data-close-dialog')) document.getElementById('details').open = false;
  const group = button?.closest('[data-rows]');
  if (group) {
    const form = group.closest('form');
    if (button.hasAttribute('data-add-row')) {
      appendRow(group);
      const row = group.querySelector('[data-row-list]').lastElementChild;
      await componentsReady(row);
      row.querySelector('wa-input,wa-select,wa-textarea,input:not([type=hidden]),select,textarea')?.focus();
    } else if (button.hasAttribute('data-remove-row')) {
      button.closest('[data-row]').remove();
    } else if (button.hasAttribute('data-move-row')) {
      const row = button.closest('[data-row]');
      if (button.dataset.moveRow === 'up') row.previousElementSibling?.before(row);
      else row.nextElementSibling?.after(row);
    }
    renumberRows(group); conditionalFields(form); retainDraft(form);
  }
  if (button?.hasAttribute('data-discard-draft')) {
    const form = button.closest('form.edit-form');
    const baseline = baselines.get(form);
    form.querySelectorAll('[data-rows]').forEach((group) => {
      group.querySelector('[data-row-list]').replaceChildren();
      (baseline[group.dataset.rows] || []).forEach((values) => appendRow(group, values));
    });
    plainControls(form).forEach((node) => {
      if (node.type === 'file') node.value = '';
      else setValue(node, baseline[node.name]);
    });
    conditionalFields(form); retainDraft(form);
    if (profileRefreshAgain && !profileRefreshPending) refreshProfileFields();
  }
});
window.addEventListener('beforeunload', (event) => {
  if ([...document.querySelectorAll('form.edit-form')].some((form) => baselines.has(form) && !equal(valuesOf(form), baselines.get(form)))) {
    event.preventDefault(); event.returnValue = '';
  }
});
document.addEventListener('htmx:confirm', (event) => {
  if (!event.detail.question) return;
  event.preventDefault();
  if (document.getElementById('confirm-action')) return;
  const dialog = document.createElement('wa-dialog');
  dialog.id = 'confirm-action'; dialog.label = 'Confirm this action';
  const message = document.createElement('p'); message.textContent = event.detail.question;
  const actions = document.createElement('div'); actions.slot = 'footer'; actions.className = 'form-footer';
  const cancel = document.createElement('wa-button');
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => dialog.open = false);
  const confirm = document.createElement('wa-button');
  confirm.variant = 'danger'; confirm.textContent = 'Continue';
  confirm.addEventListener('click', () => { dialog.open = false; event.detail.issueRequest(true); });
  dialog.addEventListener('wa-after-hide', () => dialog.remove(), { once: true });
  actions.append(cancel, confirm); dialog.append(message, actions);
  document.body.append(dialog); dialog.open = true;
});
async function unlock(token) {
  const form = document.getElementById('unlock-form');
  const button = form?.querySelector(buttonSelector);
  if (button) button.disabled = true;
  try {
    const response = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Could not unlock workspace');
    location.replace('/ui/overview');
  } catch (error) {
    const message = form?.querySelector('[data-errors]');
    if (message) message.textContent = error.message;
    else toast(error.message);
  } finally { if (button) button.disabled = false; }
}
const fragment = location.hash.slice(1);
const token = new URLSearchParams(fragment).get('token');
if (token) { history.replaceState(null, '', location.pathname + location.search); unlock(token); }
else if (fragment && !fragment.startsWith('token=')) {
  const [view, query = ''] = fragment.split('?');
  const legacyViews = new Set(['overview', 'jobs', 'sources', 'outreach', 'outcomes', 'profile',
    'evidence', 'activity', 'settings', 'workers', 'connections', 'capacity', 'providers', 'backups',
    'searches', 'setup', 'operations', 'database', 'work', 'diary']);
  if (legacyViews.has(view)) location.replace('/ui/' + view + (query ? '?' + query : ''));
}
document.getElementById('unlock-form')?.addEventListener('submit', (event) => {
  event.preventDefault(); unlock(document.getElementById('unlock-token').value.trim());
});
await initialize();
openFormAnchor();
startLive();
