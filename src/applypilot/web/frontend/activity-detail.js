import { LitElement, css, html } from 'lit';
import { identifiers, label, metadata } from './activity-record.js';

class ActivityDetail extends LitElement {
  static properties = { record: { attribute: false } };
  static styles = css`
    :host { display: grid; gap: var(--wa-space-l); font: inherit; color: var(--wa-color-text-normal); }
    .actions { display: flex; flex-wrap: wrap; gap: var(--wa-space-s); }
    dl { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: var(--wa-space-m); margin: 0; }
    dt { color: var(--wa-color-text-quiet); font-size: var(--wa-font-size-s); text-transform: capitalize; }
    dd { margin: 4px 0 0; overflow-wrap: anywhere; white-space: pre-wrap; }
    p,h3,h4 { margin: 0; }
    section { display: grid; gap: var(--wa-space-s); }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 50vh; overflow: auto;
      padding: var(--wa-space-m); background: var(--wa-color-surface-lowered); border-radius: var(--wa-border-radius-m); }
  `;

  constructor() { super(); this.record = {}; }
  action(name) { this.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail: this.record })); }
  fields(values) {
    return html`<dl>${Object.entries(values).filter(([, value]) => value != null && value !== '').map(([key, value]) =>
      html`<div><dt>${label(key)}</dt><dd>${value}</dd></div>`)}</dl>`;
  }

  render() {
    const record = this.record;
    const details = record.details || {};
    const checks = Array.isArray(details.checks) ? details.checks : [];
    const chain = Array.isArray(details.exception_chain) ? details.exception_chain : [];
    const failures = [details, details.diagnostics, ...chain.map(item => item.diagnostics)]
      .flatMap(item => Array.isArray(item?.failures) ? item.failures : []);
    const diagnostics = Object.assign({}, ...[details, details.diagnostics, ...chain.map(item => item.diagnostics)]
      .filter(Boolean).map(item => Object.fromEntries(Object.entries(item).filter(([key]) =>
        ['exception_type','http_status','status_code','return_code','command','subcommand','timeout_seconds','retry_after','hint','reason_code'].includes(key)))));
    return html`
      <div class="actions"><wa-button size="s" @click=${() => this.action('ap:copy-record')}>Copy record</wa-button>
        <wa-button size="s" @click=${() => this.action('ap:export-record')}>Export JSON</wa-button></div>
      ${[...new Set([record.reason, record.message, record.summary].filter(Boolean))].map(message => html`
        <wa-callout variant=${['error','critical'].includes(record.severity) ? 'danger' : 'neutral'}>${message}</wa-callout>`)}
      ${record.next_action ? html`<p>${record.next_action}</p>` : ''}
      ${record.action_href?.startsWith('/ui/') ? html`<wa-button href=${record.action_href} variant="brand">${record.action_label || 'Review settings'}</wa-button>` : ''}
      ${this.fields(metadata(record))}
      ${checks.length ? html`<section><h3>Connection checks</h3>${checks.map(check => html`<section>
        <h4>${label(check.component || 'Check')} · ${check.skipped ? 'Not checked' : check.ok === true ? 'Passed' : check.ok === false ? 'Failed' : 'Recorded'}</h4>
        <p>${check.detail || check.message || check.error || 'No explanation was recorded.'}</p></section>`)}</section>` : ''}
      ${Object.keys(diagnostics).length ? this.fields(diagnostics) : ''}
      ${failures.length ? html`<section><h3>Failed reads</h3>${failures.map(failure => html`<section>
        <h4>${label(failure.channel || 'Research')} · ${label(failure.operation)}</h4>
        <p>${failure.reason}</p><small>${failure.retryable ? 'Scheduled for retry' : 'Needs attention'}${failure.next_retry_at ? ' · ' + new Date(failure.next_retry_at).toLocaleString() : ''}</small>
      </section>`)}</section>` : ''}
      ${Object.keys(details).length ? html`<wa-details summary="Technical details">${this.fields(identifiers(record))}
        <pre>${JSON.stringify(details, null, 2)}</pre></wa-details>` : html`<p>No additional diagnostic details were recorded.</p>`}`;
  }
}

customElements.define('ap-activity-details', ActivityDetail);
