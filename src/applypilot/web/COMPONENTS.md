# Dashboard components

The dashboard uses Jinja and HTMX with **Web Awesome 3.14.0** for standard UI
controls and **Lit 3.3.3** for application-specific components. The templates use
real `wa-button`, `wa-input`, `wa-select`, `wa-option`, `wa-textarea`,
`wa-checkbox`, `wa-badge`, `wa-card`, `wa-callout`, `wa-details`, `wa-dialog`, and
`wa-spinner` elements. Web Awesome supplies their behavior, accessibility,
shadow DOM, and styling. These are runtime dependencies, pinned in
`package.json` and `package-lock.json`.

Use the [Web Awesome component APIs](https://webawesome.com/docs/components)
and [usage guide](https://webawesome.com/docs/usage) when changing standard
controls. Add application behavior with [Lit components](https://lit.dev/docs/components/overview/).
The other libraries in the design references are alternatives; they are not
additional installed frameworks.

Run from the repository root after installing the locked npm dependencies:

```sh
npm run build:ui
```

`scripts/build_dashboard.mjs` uses **esbuild 0.28.2**. Edit the source files and
rebuild; do not hand-edit generated bundles. Python packages include the
generated static assets, so serving the dashboard needs no Node process.

| Source | Generated or served asset | Purpose |
| --- | --- | --- |
| `frontend/components.js` and its imports | `static/components.js` | Web Awesome registrations and Lit components |
| Web Awesome stylesheet | `static/webawesome.css` | Library themes, tokens, utilities, and native element defaults |
| Web Awesome system icon library | `static/webawesome-icons.svg` | Local icons used inside library controls |
| esbuild dependency metadata | `static/components-manifest.json` | Bundled component and library inventory |
| Dependency license files and comments | `static/*LICENSE*`, `static/components.js.LEGAL.txt` | Redistribution notices |

`static/dashboard.js` imports the component bundle. The shell and login page load
`webawesome.css` before `dashboard.css`. `frontend/icons.js` resolves Web Awesome
system icons through the local sprite. Scripts, fonts, stylesheets, and icon
assets are served from the same origin, within the existing content security
policy; no CDN or external icon kit is needed.

- `templates/form.html`: `control()` renders labeled Web Awesome fields and
  constraints; `row_control()` renders repeated fields. Hidden metadata and file
  uploads remain native inputs. Standard forms and semantic tables remain HTML.
- `templates/components.html`: section headers, navigation tabs, detail links,
  and the `read_more()` helper. Plain navigation links retain normal link behavior.
- `templates/macros.html`: actions, metrics, charts, and status variants shared
  by Web Awesome badges. Nested diagnostic records use `wa-details`.
- `frontend/read-more.js`: Lit `<ap-read-more>` displays server-rendered content
  through a shadow DOM slot and uses `wa-button` to expand it. The resize observer
  disconnects when HTMX removes the component.
- `frontend/activity-detail.js`: Lit `<ap-activity-details>` renders an activity
  record with library controls and emits copy/export events.
- `frontend/activity-record.js`: shared presentation and text export of activity
  metadata, preserving one displayed status.
- `templates/source_checks.html`: shared connection-check presentation. Recovery
  guidance comes from `integrations/recovery.py`.
- `templates/profile_completion.html`: the automatic profile-completion state.
- `static/dashboard.css`: application layout and spacing using Web Awesome
  tokens. `static/theme.js` selects the library's light and dark themes.

Form-associated Web Awesome elements participate directly in native form
validation and `FormData`. Keep names, constraints, labels, and application data
attributes on their hosts; do not mirror their values into hidden input proxies.
The HTMX integration waits for controls to upgrade before restoring drafts and
retains request disabling, form submission, and partial updates. Library shadow
DOM owns control internals; use documented properties, events, slots, and CSS
parts to customize them.

Keep diagnostic payloads behind an explicit details action. The primary view
shows a title, one status, the actual failure, and a relevant recovery action.
An old error does not establish current health. Background refreshes preserve
the reader's position and unsaved input.

The underlying platform APIs are documented by MDN:
[custom elements](https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_custom_elements),
[templates and slots](https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_templates_and_slots),
and [shadow DOM](https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_shadow_DOM).
Design references include the supplied
[frontend design skill](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md),
[Vercel interface guidelines](https://github.com/vercel-labs/agent-skills/blob/main/skills/web-design-guidelines/SKILL.md),
and [MengTo UI skills](https://github.com/MengTo/Skills/tree/main/agent-skills/ui).
React-specific APIs and deployment examples do not apply to this Python/HTMX
application; their general composition, state, accessibility, and rendering
guidance remains relevant.
