import { LitElement, css, html } from 'lit';

class ReadMore extends LitElement {
  static properties = { label: {}, expanded: { type: Boolean, reflect: true }, overflowing: { state: true } };
  static styles = css`
    :host { display: block; min-width: 0; }
    #content { max-height: var(--read-more-height, 7.5em); overflow: hidden; }
    :host([expanded]) #content { max-height: none; }
    wa-button { margin-top: var(--wa-space-xs); }
  `;

  constructor() {
    super();
    this.label = 'Read more';
    this.expanded = false;
    this.overflowing = false;
    this.observer = new ResizeObserver(() => this.measure());
  }

  connectedCallback() { super.connectedCallback(); this.observer.observe(this); }
  disconnectedCallback() { this.observer.disconnect(); super.disconnectedCallback(); }
  firstUpdated() { this.measure(); }
  measure() {
    const content = this.renderRoot.querySelector('#content');
    if (content && !this.expanded) this.overflowing = content.scrollHeight > content.clientHeight + 1;
  }

  render() {
    return html`<div id="content"><slot @slotchange=${this.measure}></slot></div>
      ${this.overflowing || this.expanded ? html`<wa-button size="s" appearance="plain" variant="brand"
        aria-controls="content" aria-expanded=${String(this.expanded)}
        @click=${() => { this.expanded = !this.expanded; }}>${this.expanded ? 'Show less' : this.label}</wa-button>` : ''}`;
  }
}

customElements.define('ap-read-more', ReadMore);
