export function initializeCareerForm(form) {
  const picker = form.querySelector('[data-opportunity-picker]');
  if (!picker || picker.dataset.ready) return;
  picker.dataset.ready = 'true';
  const input = picker.querySelector('[data-opportunity-search]');
  const value = picker.querySelector('[data-opportunity-value]');
  const list = picker.querySelector('[role=listbox]');
  const status = picker.querySelector('[role=status]');
  let request, timer = null, active = 0, lastQuery = '';
  const choices = () => [...list.querySelectorAll('[role=option]')];
  const update = () => document.body.dispatchEvent(new CustomEvent('ap:data'));
  function highlight(index) {
    const options = choices();
    active = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((option, i) => {
      option.id = 'opportunity-option-' + i;
      option.setAttribute('aria-selected', String(i === active));
    });
    input.setAttribute('aria-activedescendant', options[active].id);
    options[active].scrollIntoView({ block: 'nearest' });
  }
  function open() { list.hidden = false; input.setAttribute('aria-expanded', 'true'); highlight(0); }
  function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); }
  function choose(option) {
    value.value = option.dataset.url;
    input.value = option.textContent;
    input.setCustomValidity('');
    close();
    value.dispatchEvent(new Event('change', { bubbles: true }));
    update();
  }
  async function search(query, restore = false) {
    request?.abort();
    const current = request = new AbortController();
    lastQuery = query;
    status.textContent = 'Searching…';
    try {
      const response = await fetch('/api/career-ops/opportunities?' + new URLSearchParams({q: query, job_url: value.value}), {signal: current.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not load opportunities.');
      if (current.signal.aborted || !picker.isConnected) return;
      const custom = choices()[0];
      const options = result.items.map(item => {
        const option = document.createElement('button');
        option.type = 'button'; option.tabIndex = -1; option.setAttribute('role', 'option');
        option.dataset.url = item.url; option.textContent = item.label;
        return option;
      });
      list.replaceChildren(custom, ...options);
      status.textContent = result.more ? 'More matches available. Keep typing to narrow the list.'
        : result.items.length ? 'Choose an opportunity or Custom.' : 'No saved matches. Choose Custom to enter the details.';
      if (restore) sync();
      else if (!list.hidden) highlight(0);
    } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
  }
  function sync() {
    const selected = choices().find(option => option.dataset.url === value.value);
    if (selected) { input.value = selected.textContent; input.setCustomValidity(''); }
    else search('', true);
    update();
  }
  input.addEventListener('focus', open);
  input.addEventListener('click', open);
  input.addEventListener('input', () => {
    value.value = '';
    value.dispatchEvent(new Event('change', { bubbles: true }));
    input.setCustomValidity('Choose a saved opportunity or Custom from the list.');
    open(); request?.abort();
    const query = input.value.trim();
    if (timer === null) search(query);
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; if (input.value.trim() !== lastQuery || request?.signal.aborted) search(input.value.trim()); }, 150);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); if (list.hidden) open(); else highlight(active + (event.key === 'ArrowDown' ? 1 : -1));
    } else if (event.key === 'Enter') { event.preventDefault(); if (list.hidden) open(); else choose(choices()[active]); }
    else if (event.key === 'Escape') { event.preventDefault(); close(); }
  });
  list.addEventListener('mousedown', event => event.preventDefault());
  list.addEventListener('click', event => { const option = event.target.closest('[role=option]'); if (option) choose(option); });
  picker.addEventListener('focusout', event => { if (!picker.contains(event.relatedTarget)) close(); });
  value.addEventListener('ap:restore', sync);
  sync();
}

document.addEventListener('htmx:configRequest', event => {
  if (!event.detail.elt?.matches('.panel-career-ops')) return;
  const value = document.querySelector('[data-opportunity-value]');
  if (value) {
    const query = new URLSearchParams(location.search);
    if (value.value !== (query.get('job_url') || '')) query.delete('report_id');
    query.set('job_url', value.value);
    event.detail.path = '/ui/fragments/career/0?' + query;
  }
});
