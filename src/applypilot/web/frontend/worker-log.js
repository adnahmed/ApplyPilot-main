import { LitElement, css, html } from 'lit';
import { repeat } from 'lit/directives/repeat.js';

class WorkerLog extends LitElement {
  static properties = {
    workerId: { attribute: 'worker-id' }, traceId: { attribute: 'trace-id' },
    entries: { state: true }, source: { state: true }, filter: { state: true },
    paused: { state: true }, following: { state: true }, message: { state: true },
    updatedAt: { state: true }, loading: { state: true }, before: { state: true },
  };
  static styles = css`
    :host{display:block;color:var(--wa-color-text-normal);font:inherit}
    .toolbar,.filters{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}
    .toolbar{justify-content:space-between}.controls{display:flex;gap:8px;align-items:center}
    select{font:inherit;color:inherit;background:var(--wa-color-surface-default);border:1px solid var(--wa-color-surface-border);border-radius:6px;padding:8px}
    .status{font-size:12px;color:var(--wa-color-text-quiet);margin:8px 0}
    .feed{max-height:60vh;min-height:160px;overflow:auto;overscroll-behavior:contain;padding:0 6px;overflow-anchor:none}
    article{border-left:3px solid var(--wa-color-surface-border);padding:10px 14px;margin:0 0 12px;background:var(--wa-color-surface-lowered);border-radius:0 7px 7px 0}
    article.error{border-color:var(--wa-color-danger-fill-loud)}article.tool{border-color:var(--wa-color-brand-fill-loud)}
    article.session{border-color:var(--wa-color-success-fill-loud)}
    header{display:flex;align-items:baseline;justify-content:space-between;gap:12px}strong{font-size:13px;overflow-wrap:anywhere}
    time{font-size:11px;color:var(--wa-color-text-quiet);white-space:nowrap}
    pre{font:12px/1.65 var(--wa-font-family-code,monospace);white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}
    details{font-size:12px;margin-top:8px}summary{cursor:pointer;color:var(--wa-color-text-quiet)}
    .empty{padding:20px;color:var(--wa-color-text-quiet)}
  `;
  constructor() {
    super(); this.entries = []; this.source = 'worker'; this.filter = 'all';
    this.paused = false; this.following = true; this.loading = false;
    this.message = 'Loading worker output…'; this.after = null; this.before = null;
    this.generation = ''; this.updatedAt = ''; this.request = null; this.timer = null;
  }
  connectedCallback() { super.connectedCallback(); this.source = this.traceId ? 'agent' : 'worker'; this.refresh(false, true); }
  disconnectedCallback() { super.disconnectedCallback(); clearTimeout(this.timer); this.request?.abort(); }
  async refresh(older = false, manual = false) {
    clearTimeout(this.timer);
    const dialog = this.closest('wa-dialog');
    if (!this.isConnected || (!older && !manual && (this.paused || document.hidden || (dialog && !dialog.open)))) {
      if (this.isConnected) this.timer = setTimeout(() => this.refresh(), 2000);
      return;
    }
    if (this.loading) return;
    this.loading = true;
    const controller = new AbortController(); this.request = controller;
    const query = new URLSearchParams({ worker: this.workerId, source: this.source });
    if (this.traceId) query.set('trace', this.traceId);
    if (older && this.before != null) query.set('before', this.before);
    else if (this.after != null) query.set('after', this.after);
    if (this.generation) query.set('generation', this.generation);
    const feed = this.renderRoot.querySelector('.feed');
    const height = feed?.scrollHeight || 0, top = feed?.scrollTop || 0;
    const initial = this.after == null;
    try {
      const response = await fetch('/api/workers/log?' + query, { signal: controller.signal, cache: 'no-store' });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || 'Could not read worker output.');
      if (this.request !== controller) return;
      this.message = value.available ? '' : this.source === 'worker'
        ? 'No output has been recorded for this worker yet.' : 'This session trace is not retained. Worker output may still be available.';
      if (value.available) {
        if (value.reset) this.entries = [];
        const merged = older ? [...value.items, ...this.entries] : [...this.entries, ...value.items];
        this.entries = [...new Map(merged.map(entry => [entry.id, entry])).values()];
        if (older) this.paused = true;
        if (!older || value.reset) { this.after = value.next_after; this.generation = value.generation; }
        if (older || initial || value.reset) this.before = value.next_before;
        if (!older && this.entries.length > 2000) {
          this.entries = this.entries.slice(-2000);
          this.before = Number(this.entries[0].id.split('-').at(-1));
        }
        this.updatedAt = value.updated_at;
        await this.updateComplete;
        const list = this.renderRoot.querySelector('.feed');
        if (list && older) list.scrollTop = top + list.scrollHeight - height;
        else if (list && this.following) list.scrollTop = list.scrollHeight;
      }
    } catch (error) {
      if (error.name !== 'AbortError') this.message = error.message;
    } finally {
      if (this.request === controller) {
        this.loading = false;
        if (this.isConnected) this.timer = setTimeout(() => this.refresh(), 2000);
      }
    }
  }
  changeSource(event) {
    this.request?.abort(); this.request = null; this.loading = false;
    this.source = event.target.value; this.entries = []; this.after = this.before = null;
    this.generation = ''; this.updatedAt = ''; this.following = true; this.message = 'Loading…';
    this.refresh(false, true);
  }
  pause() { this.paused = !this.paused; if (!this.paused) this.refresh(); }
  follow() { this.following = true; const list = this.renderRoot.querySelector('.feed'); if (list) list.scrollTop = list.scrollHeight; }
  render() {
    const shown = this.entries.filter(entry => this.filter === 'all' || (this.filter === 'tools'
      ? ['tool', 'result'].includes(entry.kind) : entry.kind === this.filter));
    return html`
      <div class="toolbar"><label>Log <select @change=${this.changeSource} .value=${this.source}>
        <option value="worker">Worker output · all runs</option>
        ${this.traceId ? ['agent', 'browser', 'learning', 'rlm'].map(source => html`<option value=${source}>${source} · selected session</option>`) : ''}
      </select></label><div class="controls"><wa-button size="s" @click=${this.pause}>${this.paused ? 'Resume updates' : 'Pause updates'}</wa-button>
        <wa-button size="s" ?disabled=${this.following} @click=${this.follow}>Follow latest</wa-button></div></div>
      <div class="filters" role="group" aria-label="Log entries">${[['all','All'],['message','Messages'],['tools','Tools'],['error','Errors']].map(([key,label]) =>
        html`<wa-button size="s" appearance=${this.filter === key ? 'accent' : 'outlined'} @click=${() => this.filter = key}>${label}</wa-button>`)}</div>
      <p class="status" role="status">${this.paused ? 'Updates paused' : 'Updates every 2 seconds while this view is open'}${this.updatedAt ? ' · Last written ' + new Date(this.updatedAt).toLocaleString() : ''}</p>
      ${this.message ? html`<p class="status">${this.message}</p>` : ''}
      <div class="feed" tabindex="0" role="region" aria-label="Worker log" @scroll=${event => { const el=event.target; this.following=el.scrollHeight-el.scrollTop-el.clientHeight<40; }}>
        ${this.before != null ? html`<wa-button size="s" ?disabled=${this.loading} @click=${() => this.refresh(true)}>Load earlier output</wa-button>` : ''}
        ${repeat(shown, entry => entry.id, entry => html`<article class=${entry.kind}>
          <header><strong>${entry.title}</strong>${entry.at ? html`<time>${new Date(entry.at).toLocaleTimeString()}</time>` : ''}</header>
          ${entry.text ? html`<pre>${entry.text}</pre>` : ''}
          ${Object.keys(entry.details || {}).length ? html`<details><summary>Details</summary><pre>${JSON.stringify(entry.details,null,2)}</pre></details>` : ''}
        </article>`)}
        ${!shown.length && !this.loading ? html`<p class="empty">No matching log entries.</p>` : ''}
      </div>`;
  }
}
customElements.define('ap-worker-log', WorkerLog);
