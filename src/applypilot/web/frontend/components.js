import './icons.js';
import '@awesome.me/webawesome/dist/components/button/button.js';
import '@awesome.me/webawesome/dist/components/input/input.js';
import '@awesome.me/webawesome/dist/components/select/select.js';
import '@awesome.me/webawesome/dist/components/option/option.js';
import '@awesome.me/webawesome/dist/components/textarea/textarea.js';
import '@awesome.me/webawesome/dist/components/checkbox/checkbox.js';
import '@awesome.me/webawesome/dist/components/badge/badge.js';
import '@awesome.me/webawesome/dist/components/card/card.js';
import '@awesome.me/webawesome/dist/components/callout/callout.js';
import '@awesome.me/webawesome/dist/components/details/details.js';
import '@awesome.me/webawesome/dist/components/dialog/dialog.js';
import '@awesome.me/webawesome/dist/components/spinner/spinner.js';
import './read-more.js';
import './activity-detail.js';
import './worker-log.js';
import { allDefined } from '@awesome.me/webawesome/dist/utilities/defined.js';

export { recordText } from './activity-record.js';

export const controlSelector = 'input[name],select[name],textarea[name],wa-input[name],wa-select[name],wa-textarea[name],wa-checkbox[name]';
export const buttonSelector = 'button,wa-button';

export async function componentsReady(root = document) {
  await allDefined({ root });
  const elements = [...root.querySelectorAll('wa-input,wa-select,wa-textarea,wa-checkbox,wa-button,wa-details,wa-dialog,ap-read-more')];
  if (root.updateComplete) elements.push(root);
  await Promise.all(elements.map(element => element.updateComplete));
}
