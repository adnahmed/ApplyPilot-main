# Bundled dashboard assets

Assets are served locally; the dashboard does not request a third-party CDN.
The component dependencies are pinned in the repository's `package.json` and
`package-lock.json`. Run `npm run build:ui` to regenerate their bundled assets.

- [Web Awesome 3.14.0](https://webawesome.com/docs/usage), npm package `@awesome.me/webawesome` — MIT, see `webawesome-LICENSE.md`. Standard controls are bundled into `components.js`; library styles are bundled into `webawesome.css`. `webawesome-icons.svg` is generated from the package's system icons for same-origin loading. No Web Awesome Pro package or hosted kit is used.
- [Lit 3.3.3](https://lit.dev/docs/), npm package `lit` — BSD-3-Clause, see `lit-LICENSE.txt`. Bundled into `components.js` for Web Awesome and ApplyPilot's `ap-read-more` and `ap-activity-details` components. Additional retained dependency notices are in `components.js.LEGAL.txt`.
- [esbuild 0.28.2](https://esbuild.github.io/), npm package `esbuild` — MIT. Build-time dependency only; its executable is not a browser asset. `scripts/build_dashboard.mjs` bundles JavaScript and CSS, generates the local icon sprite and component inventory, and copies license notices.
- Chart.js 4.5.1 UMD: https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js — MIT, see chart-LICENSE.md. Loaded only when charts are present.
- DM Sans v17, variable weight and optical size, Latin and Latin Extended — Google Fonts: https://fonts.google.com/specimen/DM+Sans. SIL Open Font License, see dm-sans-LICENSE.txt. Used for interface text and charts.
- Manrope v20, variable weight, Latin and Latin Extended — Google Fonts: https://fonts.google.com/specimen/Manrope. SIL Open Font License, see manrope-LICENSE.txt. Used for headings, branding, and key metrics.
- IBM Plex Sans Latin 400 and 600, @fontsource/ibm-plex-sans 5.2.8 — SIL Open Font License, see ibm-plex-sans-LICENSE.txt. Retained for older cached dashboard styles.

Web Awesome, Lit, and esbuild versions above match the installed packages and
lockfile on 2026-09-26. Chart.js and font versions were recorded with the earlier
asset imports. Chart values retain an HTML table fallback.

Web Awesome replaces the former Tabler stylesheet. That unused CSS and its
license copy have been removed. Library controls supply their own shadow DOM
and form association; application code does not maintain hidden copies of their
values. See `../COMPONENTS.md` for source ownership and the build workflow.

DM Sans and Manrope were downloaded from the Google Fonts CSS API on 2026-09-26:
https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..800&family=Manrope:wght@400..800&display=swap
The original WOFF2 files are bundled without modification; fonts.css selects weights 400–800. Latin fonts are preloaded, and extended characters load on demand. Both families use font-display: swap and system fallbacks.
