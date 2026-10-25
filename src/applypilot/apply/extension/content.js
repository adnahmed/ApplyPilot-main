// ApplyPilot content script — runs on every http(s) page.
//
// Show the "Add to ApplyPilot" pill on job pages so users can save
// jobs to the running pipeline.

let _myWorkerId = null;      // which worker owns us (null if not in-set)
let _workerPort = null;      // port of the live worker we found
let _pill = null;
let _state = 'idle';

// --- Job page detection --------------------------------------------------
const JOB_URL_PATTERNS = [
  /\/jobs?\//i, /\/careers?\//i, /\/job-detail/i, /\/position/i,
  /\/openings?\//i, /\/apply/i, /\/posting/i, /\/vacancy/i,
  /workday\.com/, /greenhouse\.io/, /lever\.co/, /icims\.com/,
  /myworkdayjobs\.com/, /taleo\.net/, /ashbyhq\.com/, /jobvite\.com/,
  /linkedin\.com\/jobs/, /indeed\.com\/viewjob/, /dice\.com\/job-detail/,
];

function looksLikeJobPage() {
  const url = location.href;
  if (JOB_URL_PATTERNS.some(re => re.test(url))) return true;
  const title = document.title.toLowerCase();
  return /engineer|developer|devops|platform|backend|frontend|staff|principal|senior/.test(title)
    && /job|position|opening|career|apply/.test(title);
}

// --- "Add to ApplyPilot" pill (existing feature, preserved) -------------

function createPill() {
  const pill = document.createElement('div');
  pill.id = 'applypilot-capture-pill';
  pill.style.cssText = `
    position: fixed;
    bottom: 18px;
    right: 18px;
    z-index: 2147483647;
    background: #312e81;
    color: #e0e7ff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 500;
    padding: 7px 14px 7px 10px;
    border-radius: 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.4);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s, opacity 0.15s;
    user-select: none;
    border: 1px solid #4f46e5;
  `;
  pill.addEventListener('mouseenter', () => {
    if (_state === 'idle') pill.style.background = '#3730a3';
  });
  pill.addEventListener('mouseleave', () => {
    if (_state === 'idle') pill.style.background = '#312e81';
  });
  pill.addEventListener('click', captureJob);
  return pill;
}

function setPillState(state, msg) {
  _state = state;
  if (!_pill) return;
  const configs = {
    idle:    { bg: '#312e81', border: '#4f46e5', text: '📥 Add to ApplyPilot' },
    loading: { bg: '#1e1b4b', border: '#3730a3', text: '⏳ Adding…' },
    success: { bg: '#064e3b', border: '#059669', text: '✓ Added to pipeline' },
    exists:  { bg: '#1e3a5f', border: '#2563eb', text: msg || '↩ Already in pipeline' },
    error:   { bg: '#450a0a', border: '#b91c1c', text: '✗ ' + (msg || 'Error') },
  };
  const cfg = configs[state] || configs.idle;
  _pill.style.background = cfg.bg;
  _pill.style.borderColor = cfg.border;
  _pill.textContent = cfg.text;

  if (state !== 'idle' && state !== 'loading') {
    setTimeout(() => setPillState('idle'), 4000);
  }
}

async function captureJob() {
  if (_state === 'loading') return;
  setPillState('loading');

  const url   = location.href;
  const title = document.title.replace(/\s*[-|].*$/, '').trim();

  if (!_workerPort) await findWorker();
  if (!_workerPort) {
    setPillState('error', 'No active workers');
    return;
  }

  try {
    const resp = await fetch(`http://localhost:${_workerPort}/api/add-job`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, title }),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.status === 'queued') {
      setPillState('success');
    } else if (data.status === 'exists') {
      const label = data.applyStatus ? `Already ${data.applyStatus}` : 'Already in pipeline';
      setPillState('exists', label);
    } else {
      setPillState('error', 'Unexpected response');
    }
  } catch (err) {
    _workerPort = null;
    setPillState('error', err.message.slice(0, 40));
  }
}

async function findWorker() {
  try {
    const registry = await chrome.runtime.sendMessage({type: 'discover-workers'});
    const candidates = (registry?.workers || []).filter(w => Number.isInteger(w.serverPort) && w.serverPort > 0 && w.serverPort <= 65535);
    const worker = candidates.find(w => w.workerId === _myWorkerId) || candidates[0];
    if (worker) { _workerPort = worker.serverPort; return _workerPort; }
  } catch { /* No current runtime listener available. */ }
  return null;
}

// --- Bootstrap ----------------------------------------------------------

async function init() {
  // Identify the owning worker before adding a job from this tab.
  try {
    const reply = await chrome.runtime.sendMessage({ type: 'is-in-set' });
    _myWorkerId = (reply && reply.workerId) ?? null;
  } catch (_e) {
    // SW unavailable — no active worker for the capture pill.
    return;
  }

  // Pill is job-page-gated as before.
  if (!looksLikeJobPage()) return;

  const port = await findWorker();
  if (!port) return;

  _pill = createPill();
  setPillState('idle');
  if (document.body) document.body.appendChild(_pill);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
