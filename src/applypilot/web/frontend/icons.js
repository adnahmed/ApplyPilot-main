import systemLibrary, { icons } from '@awesome.me/webawesome/dist/components/icon/library.system.js';
import { registerIconLibrary } from '@awesome.me/webawesome/dist/components/icon/library.js';

// Same-origin sprite avoids data-URL fetches and works with the dashboard's CSP.
registerIconLibrary('system', {
  ...systemLibrary,
  spriteSheet: true,
  resolver(name, _family, variant = 'solid') {
    if (!icons[variant]?.[name]) variant = 'regular';
    if (!icons[variant]?.[name]) name = 'circle-question';
    return `/static/webawesome-icons.svg#${variant}-${name}`;
  },
  mutator(svg, host) {
    systemLibrary.mutator(svg, host);
    svg.setAttribute('width', '1em');
    svg.setAttribute('height', '1em');
    const source = icons[host.variant]?.[host.name] || icons.regular[host.name] || icons.regular['circle-question'];
    const viewBox = source.match(/viewBox="([^"]+)"/)?.[1];
    if (viewBox) svg.setAttribute('viewBox', viewBox);
  },
});
