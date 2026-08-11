// The whole single-page app. Lifted out of index.html unchanged so that
// `script-src 'self'` can authorise it without a hash that would need
// regenerating on every edit — and eventually without 'unsafe-inline' at all.
//
// Load it as a CLASSIC script at the end of <body>, never async/defer (the
// bottom of this file wires listeners to elements that must already exist) and
// never type="module" (module scope is not global scope: every function the
// markup calls would stop being reachable from window).
// ===== STATE =====
const NICHE_BOX_PALETTE = ['#ffbf00','#0076cb','#5e17eb','#00bf63','#000000','#ff751f'];

const S = {
  step: 1,
  user: null,              // {email, is_local, is_admin} from /api/auth/me
  appStarted: false,       // guard so startApp() registers intervals once
  authTab: 'login',        // 'login' | 'register'
  landingTab: 'creator',   // 'creator' | 'business' — which product door
  signupAccountType: 'creator', // account_type sent on register (from the door)
  format: 'single',
  source: 'stock',
  ownPhotos: [],           // File objects picked in step 2 for source='upload'
  platform: 'instagram',
  templateStyle: 'branded_card',
  nicheBoxColor: null,
  postId: null,
  hashtags: [],
  seoKeywords: [],
  editingSlide: null,      // slide number being replaced/uploaded
  editSource: 'stock',     // chosen source in the Edit modal
  libraryPickerMode: null, // 'slide' | 'reel' | 'video-seed' | 'edit-clip' — which action a library pick completes
  videoSeedImageId: null, // library image chosen to animate into a video (image-to-video)
  videoModels: [],        // {id,label,price_per_sec}[] from GET /api/models/providers's video bucket
  editVideoAnchorId: null, // the clip clicked to open the edit-video modal (ownership anchor for POST .../edit)
  editVideoClips: [],      // [{asset_id, trim_start_sec, trim_end_sec, title, url}] — the editor's clip list
  publishXAssetId: null,   // library asset the Publish-to-X modal is open for
  publishJobs: {},         // {(asset_id|post_id): VideoPublishJobStatus} — polled from GET /api/publish-jobs
  slideOriginals: {},      // {slide_number: {overlay, niche}} for Reset
  currentPost: null,       // last-rendered PostPreview (for schedule/insights)
  calRef: null,            // {year, month} shown in calendar
  posts: [],               // cached list for calendar/grid
  brandVoice: { preset: 'balanced', custom: '', presets: [] },
  profile: { niche: '', target_audience: '', brand_name: '' },
  slideStyle: { accent_color: '', text_box_color: '', default_accent_color: '', palette: [] },
  ai: { text_provider: '', text_model: '', image_provider: '', image_model: '', keys: {} },
  aiCatalog: null,          // {text:[...], image:[...]} — fetched once
  gridMode: 'mobile',      // grid preview width
  usage: null,             // last /api/usage snapshot
  undoStack: [],           // last N caption/hashtag/seo snapshots for current post
};

const API = window.location.origin;

// ===== THEME (ink light / dark) =====
function applyTheme(t) {
  const theme = (t || localStorage.getItem('theme') || 'light') === 'dark' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? '☀' : '☾';
}
function toggleTheme() {
  applyTheme((localStorage.getItem('theme') || 'light') === 'dark' ? 'light' : 'dark');
}

// ===== AUTH =====
function authHeader() {
  const t = localStorage.getItem('api_token');
  return t ? { 'Authorization': `Bearer ${t}` } : {};
}

// ---- boot gate: who am I? (cloud shows landing; local resolves silently) ----
async function initAuth() {
  try {
    const res = await fetch(`${API}/api/auth/me`, { headers: authHeader() });
    if (!res.ok) throw new Error('unauthenticated');
    S.user = await res.json();
    hideAuthScreen();
    hideLanding();
    renderUserChrome();
    startApp();
    refreshFailedBanner();   // a publish may have failed while the tab was closed
    loadConnections([]);     // banner only — the page itself renders on demand
  } catch {
    showLanding();   // guests see the marketing page first, not the form
  }
}

/** Read ?topic= / ?url= off the address bar and start on it.
 *
 *  For outreach: one link per recipient, so the first thing somebody sees is a
 *  post about their own subject rather than an empty field. Same argument as
 *  the field itself, one step earlier.
 *
 *  Only reached when the landing is being shown, which is what keeps a signed-in
 *  visitor from being thrown out of their own app by a link in an email.
 *
 *  The query is wiped from the address bar the way /verify and /team/accept wipe
 *  theirs: what sits in a URL gets shared, screenshotted and reloaded, and a
 *  reload here would spend a second free try on the same topic.
 */
function startHeroFromLink() {
  const params = new URLSearchParams(window.location.search);
  const topic = (params.get('topic') || '').trim();
  const url = (params.get('url') || '').trim();
  if (!topic && !url) return;
  try { history.replaceState(null, '', '/'); } catch {}

  // The mode as well as the value: a field labelled "a topic" holding a URL
  // sends it under the wrong name on the next run — the one the visitor starts
  // themselves.
  setHeroMode(url ? 'link' : 'topic');
  document.getElementById('hero-input').value = url || topic;
  runHeroPost();
}

function showLanding() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('forgot-screen').classList.add('hidden');
  document.getElementById('reset-screen').classList.add('hidden');
  document.getElementById('landing-screen').classList.remove('hidden');
  startHeroFromLink();
}
function hideLanding() {
  document.getElementById('landing-screen').classList.add('hidden');
}

// Two doors on the landing: Creators (today's app) vs Business (sources → leads).
// One engine underneath — this only swaps the marketing hero + which door a
// Sign-up click carries into registration.
function setLandingTab(tab) {
  S.landingTab = tab === 'business' ? 'business' : 'creator';
  const biz = S.landingTab === 'business';
  const creatorEl = document.getElementById('landing-creator');
  const businessEl = document.getElementById('landing-business');
  if (creatorEl) creatorEl.classList.toggle('hidden', biz);
  if (businessEl) businessEl.classList.toggle('hidden', !biz);
  for (const [id, on] of [['ltab-creator', !biz], ['ltab-business', biz]]) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.classList.toggle('border-purple-500', on);
    b.classList.toggle('bg-purple-900', on);
    b.classList.toggle('text-white', on);
    b.classList.toggle('border-gray-700', !on);
    b.classList.toggle('bg-gray-800', !on);
    b.classList.toggle('text-gray-300', !on);
  }
}
function backToHome() {
  hideAuthScreen();
  showLanding();
}

// ===== THE LANDING FIELD (no auth) — a topic or a link → a finished post =====
// Everything a visitor sees before they have given us anything. The result is
// held in the browser and nowhere else: the server writes no row for it, which
// is what lets Download work without a second request and without an account.
S.heroMode = 'topic';
S.heroPost = null;
S.heroRunning = false;

//: How many posts a visitor gets before being asked to make an account. Two:
//: the first shows the product, the second shows the first was not a fluke.
//:
//: The count lives in localStorage, which makes it a polite request rather than
//: a defence — clearing it takes two clicks. That is fine, and saying so is
//: better than pretending otherwise: what actually bounds our spending is the
//: per-IP limit and the daily ceiling, both on the server. This exists to pick
//: the MOMENT to ask, which is after somebody has seen it work twice.
const HERO_FREE_TRIES = 2;
const HERO_TRIES_KEY = 'landing_tries';

function heroTriesUsed() {
  try { return parseInt(localStorage.getItem(HERO_TRIES_KEY) || '0', 10) || 0; }
  catch { return 0; }
}

function heroCountTry() {
  try { localStorage.setItem(HERO_TRIES_KEY, String(heroTriesUsed() + 1)); } catch {}
}

function showHeroGate() {
  document.getElementById('hero-gate').classList.remove('hidden');
}

const HERO_PLACEHOLDER = {
  topic: 'Sourdough starters for beginners',
  link: 'https://your-site.com',
};

function setHeroMode(mode) {
  S.heroMode = mode === 'link' ? 'link' : 'topic';
  const input = document.getElementById('hero-input');
  input.placeholder = HERO_PLACEHOLDER[S.heroMode];
  input.value = '';
  document.querySelectorAll('[data-hero-mode]').forEach(b => {
    const on = b.dataset.heroMode === S.heroMode;
    b.classList.toggle('border-purple-500', on);
    b.classList.toggle('bg-purple-900', on);
    b.classList.toggle('text-white', on);
    b.classList.toggle('border-gray-700', !on);
    b.classList.toggle('bg-gray-800', !on);
    b.classList.toggle('text-gray-300', !on);
  });
}

function heroStatus(message) {
  const el = document.getElementById('hero-status');
  el.textContent = message || '';
  el.classList.toggle('hidden', !message);
}

function resetHero() {
  S.heroPost = null;
  document.getElementById('hero-result').classList.add('hidden');
  heroStatus('');
  document.getElementById('hero-input').focus();
}

function runHeroExample(btn) {
  setHeroMode('topic');
  document.getElementById('hero-input').value = btn.textContent.trim();
  runHeroPost();
}

/** Generate one post for somebody with no account.
 *
 *  The two checks before the request are not validation theatre: the server
 *  would refuse both with a 422, and spending a visitor's first round trip on
 *  a refusal it could have read locally is a poor first impression. */
async function runHeroPost() {
  if (S.heroRunning) return;          // every run costs a model call and a picture
  if (heroTriesUsed() >= HERO_FREE_TRIES) {
    // Nothing is spent and nothing is taken away: the last post stays on
    // screen, Download keeps working, and the gate says what is on the other
    // side rather than only saying no.
    showHeroGate();
    return;
  }
  const value = (document.getElementById('hero-input').value || '').trim();
  if (S.heroMode === 'topic' && value.length < 3) {
    return heroStatus('Give it a little more to go on — a few words.');
  }
  if (S.heroMode === 'link' && !/^https?:\/\//i.test(value)) {
    return heroStatus('Paste a full link, starting with http:// or https://');
  }

  S.heroRunning = true;
  const btn = document.getElementById('hero-run');
  btn.disabled = true;
  document.getElementById('hero-result').classList.add('hidden');
  heroStatus('Starting…');
  try {
    const res = await fetch(`${API}/api/demo/post`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(S.heroMode === 'link' ? { url: value } : { topic: value }),
    });
    if (res.status === 429) return heroStatus("That's a few in a row — give it an hour, or sign up to keep going.");
    // Relay the server's reason. There are two 503s behind this and they mean
    // opposite things — "today's budget is spent" and "nobody configured a key
    // here" — and printing the first for both told every visitor on an
    // unconfigured deployment to come back tomorrow, where tomorrow says the
    // same thing. The 429 above is left worded here on purpose: slowapi's own
    // text is a bare limit string, and the limit really does lift with time.
    if (res.status === 503) {
      let detail = '';
      try { detail = ((await res.json()) || {}).detail || ''; } catch(e) {}
      return heroStatus(detail || 'The free demo is not available right now.');
    }
    if (!res.ok) return heroStatus('Something went wrong. Please try again.');

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value: chunk } = await reader.read();
      if (done) break;
      buf += dec.decode(chunk, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === 'progress') heroStatus(ev.message);
        else if (ev.type === 'error') heroStatus(ev.message);
        else if (ev.type === 'complete') renderHeroPost(ev.post);
      }
    }
  } catch {
    heroStatus('Something went wrong. Please try again.');
  } finally {
    S.heroRunning = false;
    btn.disabled = false;
  }
}

function renderHeroPost(post) {
  S.heroPost = post || null;
  if (!post) return;
  // Counted here rather than at the start of the run: an error produced nothing
  // to be asked about, and charging a try for our own failure spends a
  // stranger's patience on it.
  heroCountTry();
  parkHeroDraft(post);
  heroStatus('');
  document.getElementById('hero-hook').textContent = post.hook || '';
  document.getElementById('hero-caption').textContent = post.caption || '';
  document.getElementById('hero-hashtags').textContent = (post.hashtags || []).join(' ');
  const img = document.getElementById('hero-image');
  // A data URL, not a link: there is no file on the server to link to, which is
  // the point. safeUrl() would reject it, so the shape is checked here instead.
  if (typeof post.image_data_url === 'string' && post.image_data_url.startsWith('data:image/')) {
    img.src = post.image_data_url;
    img.classList.remove('hidden');
  } else {
    img.removeAttribute('src');
    img.classList.add('hidden');
  }
  document.getElementById('hero-download').classList.toggle('hidden', !img.src);
  document.getElementById('hero-result').classList.remove('hidden');
}

//: Where the last landing post waits for an account. The server keeps nothing,
//: so this is the only copy — and it has to survive the reload that signing up
//: performs, which rules out anything held in memory.
const HERO_DRAFT_KEY = 'landing_draft';

function parkHeroDraft(post) {
  try { localStorage.setItem(HERO_DRAFT_KEY, JSON.stringify(post)); } catch {}
}

/** Hand the parked post to the account that just appeared.
 *
 *  Removed from storage BEFORE the call, not after: a failure here must not
 *  leave a draft that gets carried again on the next boot, which would quietly
 *  make a second copy of the same post. If the handover fails, the topic still
 *  reaches the composer — losing the picture is a bad day, losing the idea as
 *  well is a worse one, and the topic is one line of text we already have. */
async function carryHeroDraft() {
  let draft = null;
  try {
    draft = JSON.parse(localStorage.getItem(HERO_DRAFT_KEY) || 'null');
    localStorage.removeItem(HERO_DRAFT_KEY);
  } catch { return; }
  if (!draft || !draft.caption) return;

  try {
    const res = await apiFetch(`${API}/api/posts/from-draft`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topic: draft.topic, caption: draft.caption, hook: draft.hook || null,
        cta: draft.cta || null, hashtags: draft.hashtags || [],
        image_data_url: draft.image_data_url || null,
      }),
    });
    if (!res.ok) throw new Error('carry failed');
    const post = await res.json();
    setSection('create');
    bindPost(post);
    showStep(4);
    toast('Your post came with you.', 'success');
  } catch {
    const topic = document.getElementById('topic');
    if (topic && draft.topic) topic.value = draft.topic;
    toast("We couldn't bring the picture over — the idea is in the composer.", 'warn');
  }
}

/** Save the picture. No request and no account: it is already in the browser. */
function downloadHeroImage() {
  const src = S.heroPost && S.heroPost.image_data_url;
  if (!src) return;
  const a = document.createElement('a');
  a.href = src;
  a.download = 'content-engine-post.jpg';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ===== BUSINESS DEMO (no auth) — paste a public link → draft starters =====
// Every field here comes from an external source, so esc()/safeUrl() are mandatory
// (same XSS class as competitor data in the old Trend Finder).
async function runDemo() {
  const input = document.getElementById('demo-url');
  const btn = document.getElementById('demo-run');
  const status = document.getElementById('demo-status');
  const results = document.getElementById('demo-results');
  const url = (input.value || '').trim();
  results.innerHTML = '';
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  if (!/^https?:\/\//i.test(url)) return setStatus('Enter a public http(s) URL.');
  btn.disabled = true;
  setStatus('Reading the source…');
  try {
    const res = await fetch(`${API}/api/demo/from-url`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (res.status === 429) return setStatus('You have reached the demo limit — please try again later.');
    if (res.status === 503) return setStatus('The demo is temporarily unavailable.');
    if (!res.ok) return setStatus('Something went wrong. Please try again.');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '', count = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === 'progress' || ev.type === 'empty' || ev.type === 'error') setStatus(ev.message);
        else if (ev.type === 'lead') { results.appendChild(renderDemoLead(ev.lead)); count++; }
        else if (ev.type === 'complete') {
          setStatus(count ? `${count} draft${count > 1 ? 's' : ''} from your latest updates. Sign up to connect this source and post them.`
                          : 'No newsworthy updates found in the last 90 days.');
        }
      }
    }
  } catch {
    setStatus('Something went wrong. Please try again.');
  } finally {
    btn.disabled = false;
  }
}

function renderDemoLead(lead) {
  const d = (lead.drafts && lead.drafts[0]) || {};
  const wrap = document.createElement('div');
  wrap.className = 'ce-card p-4';
  const missing = (lead.missing || []).map(m => `<li>${esc(m)}</li>`).join('');
  const tags = (d.hashtags || []).map(h => esc(h)).join(' ');
  wrap.innerHTML = `
    <div class="text-xs text-gray-500 mb-1">${esc(lead.reason || 'Worth posting')}</div>
    <div class="font-semibold">${esc(lead.what_happened || lead.title || '')}</div>
    <a href="${safeUrl(lead.source_url)}" target="_blank" rel="noopener noreferrer" class="text-xs underline break-all" style="color:var(--accent)">${esc(lead.source_url || '')}</a>
    ${lead.why_interesting ? `<div class="text-sm text-gray-400 mt-1">${esc(lead.why_interesting)}</div>` : ''}
    <div class="mt-3 p-3 rounded-lg" style="background:var(--accent-dim)">
      ${d.hook ? `<div class="font-semibold text-sm">${esc(d.hook)}</div>` : ''}
      <div class="text-sm mt-1 whitespace-pre-line">${esc(d.caption || '')}</div>
      ${d.cta ? `<div class="text-sm mt-1">${esc(d.cta)}</div>` : ''}
      ${tags ? `<div class="text-xs text-gray-400 mt-1">${tags}</div>` : ''}
      ${d.unverified ? `<div class="text-xs mt-2" style="color:#f59e0b">⚠ Mentions a figure not found in the source — verify before posting.</div>` : ''}
    </div>
    ${missing ? `<div class="text-xs text-gray-400 mt-3"><div class="font-semibold">What's missing from the source:</div><ul class="list-disc ml-5 mt-1">${missing}</ul></div>` : ''}
  `;
  return wrap;
}

// ===== BUSINESS APP-SHELL (account_type=business) — Sources / Leads / Drafts =====
// All source data is untrusted → esc()/safeUrl() everywhere. Authenticated calls
// go through apiFetch (401 handled). Backend gates everything on the workspace.
S.bizDigestSel = new Set();

const SOURCE_STATUS = {
  ok: { label: 'OK', color: '#5aa17c' },
  unreachable: { label: 'Unreachable', color: '#c2704e' },
  rate_limited: { label: 'Rate limited — will retry', color: '#c2a04e' },
  format_changed: { label: 'Format changed', color: '#c2a04e' },
};

async function loadSources() {
  const box = document.getElementById('biz-sources-list');
  box.textContent = 'Loading…';
  try {
    const res = await apiFetch(`${API}/api/business/sources`);
    const list = res.ok ? await res.json() : [];
    if (!list.length) { box.innerHTML = '<div class="text-sm text-gray-400">No sources yet. Add a public link above.</div>'; return; }
    box.innerHTML = '';
    list.forEach(s => box.appendChild(renderSourceRow(s)));
  } catch { box.innerHTML = '<div class="text-sm text-gray-400">Couldn\'t load sources.</div>'; }
}

function renderSourceRow(s) {
  const st = SOURCE_STATUS[s.status] || SOURCE_STATUS.ok;
  const row = document.createElement('div');
  row.className = 'ce-card p-3 flex items-center gap-3';
  row.innerHTML = `
    <div class="min-w-0 flex-1">
      <a href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer" class="text-sm underline break-all" style="color:var(--accent)">${esc(s.url)}</a>
      <div class="text-xs text-gray-500 mt-0.5">${esc(s.kind)} · <span style="color:${st.color}">${st.label}</span></div>
    </div>
    <button class="ce-btn-ghost px-2 py-1 text-xs" data-act="refresh">Refresh</button>
    <button class="ce-btn-ghost px-2 py-1 text-xs" data-act="delete">✕</button>
  `;
  row.querySelector('[data-act="refresh"]').onclick = () => refreshSource(s.id);
  row.querySelector('[data-act="delete"]').onclick = () => deleteSource(s.id);
  return row;
}

async function addSource() {
  const input = document.getElementById('biz-source-url');
  const btn = document.getElementById('biz-source-add');
  const status = document.getElementById('biz-source-status');
  const url = (input.value || '').trim();
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  if (!/^https?:\/\//i.test(url)) return setStatus('Enter a public http(s) URL.');
  btn.disabled = true; setStatus('Adding and checking the source…');
  try {
    const res = await apiFetch(`${API}/api/business/sources`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) { setStatus('Couldn\'t add that source.'); return; }
    const d = await res.json();
    input.value = '';
    setStatus(`Added. Found ${d.leads_found} lead${d.leads_found === 1 ? '' : 's'} in the last 90 days.`);
    await loadSources();
  } catch { setStatus('Something went wrong. Please try again.'); }
  finally { btn.disabled = false; }
}

async function refreshSource(id) {
  try {
    const res = await apiFetch(`${API}/api/business/sources/${encodeURIComponent(id)}/refresh`, { method: 'POST' });
    if (res.ok) { const d = await res.json(); toast(`Found ${d.leads_found} new lead(s)`, 'success'); }
    else {
      // Say what the server said. A hardcoded "couldn't reach" told someone
      // whose source was merely over quota that it was broken.
      const e = await res.json().catch(() => ({}));
      toast(e.detail || 'Couldn\'t reach that source', 'warn');
    }
    await loadSources();
  } catch { toast('Refresh failed', 'error'); }
}

async function deleteSource(id) {
  try {
    await apiFetch(`${API}/api/business/sources/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadSources();
  } catch { toast('Delete failed', 'error'); }
}

const STRENGTH_BADGE = {
  worthy: { label: 'Worth posting', color: '#5aa17c' },
  weak: { label: 'Maybe', color: '#c2a04e' },
};

async function loadLeads() {
  const box = document.getElementById('biz-leads-list');
  box.textContent = 'Loading…';
  S.bizDigestSel.clear();
  updateDigestBtn();
  try {
    const res = await apiFetch(`${API}/api/business/leads?status=new`);
    const list = res.ok ? await res.json() : [];
    if (!list.length) { box.innerHTML = '<div class="text-sm text-gray-400">No leads yet. Add a source and we\'ll collect what\'s worth posting.</div>'; return; }
    box.innerHTML = '';
    list.forEach(l => box.appendChild(renderLeadRow(l)));
  } catch { box.innerHTML = '<div class="text-sm text-gray-400">Couldn\'t load leads.</div>'; }
}

function renderLeadRow(lead) {
  const b = STRENGTH_BADGE[lead.strength] || STRENGTH_BADGE.weak;
  const row = document.createElement('div');
  row.className = 'ce-card p-4';
  row.innerHTML = `
    <div class="flex items-start gap-2">
      <input type="checkbox" class="mt-1 biz-lead-check" />
      <div class="min-w-0 flex-1">
        <div class="text-xs mb-1" style="color:${b.color}">${b.label} · ${esc(lead.reason || '')}</div>
        ${lead.sensitive ? '<div class="text-xs mb-1" style="color:#e0a52e">⚠ Sensitive — check the mood before posting</div>' : ''}
        <div class="font-semibold">${esc(lead.what_happened || '')}</div>
        <a href="${safeUrl(lead.source_url)}" target="_blank" rel="noopener noreferrer" class="text-xs underline break-all" style="color:var(--accent)">${esc(lead.source_url || '')}</a>
        ${lead.quote ? `<div class="text-sm text-gray-400 mt-1 whitespace-pre-line">${esc(lead.quote)}</div>` : ''}
      </div>
    </div>
    <div class="flex gap-2 mt-3 flex-wrap items-center">
      <select class="ce-input px-2 py-1.5 text-xs" data-role="platform">
        <option value="instagram">Instagram</option>
        <option value="x">X</option>
      </select>
      <select class="ce-input px-2 py-1.5 text-xs hidden" data-role="xmode" title="X post shape">
        <option value="short">Short</option>
        <option value="thread">Thread</option>
      </select>
      <button class="ce-btn px-3 py-1.5 text-xs" data-act="post">Make a post</button>
      <button class="ce-btn-ghost px-3 py-1.5 text-xs" data-act="dismiss">Skip</button>
      <button class="ce-btn-ghost px-3 py-1.5 text-xs" data-act="snooze">Don't show this kind</button>
    </div>
  `;
  row.querySelector('.biz-lead-check').onchange = (e) => {
    if (e.target.checked) S.bizDigestSel.add(lead.id); else S.bizDigestSel.delete(lead.id);
    updateDigestBtn();
  };
  const platSel = row.querySelector('[data-role="platform"]');
  const xmodeSel = row.querySelector('[data-role="xmode"]');
  platSel.onchange = () => xmodeSel.classList.toggle('hidden', platSel.value !== 'x');
  row.querySelector('[data-act="post"]').onclick = () => {
    if (lead.sensitive && !confirm('This looks like sensitive/negative news. Draft a post anyway?')) return;
    makePost(lead.id, row, platSel.value, xmodeSel.value);
  };
  row.querySelector('[data-act="dismiss"]').onclick = () => leadAction(lead.id, 'dismiss', row);
  row.querySelector('[data-act="snooze"]').onclick = () => leadAction(lead.id, 'snooze-kind', row);
  return row;
}

async function leadAction(id, action, row) {
  try {
    await apiFetch(`${API}/api/business/leads/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    row.remove();
    S.bizDigestSel.delete(id); updateDigestBtn();
  } catch { toast('Action failed', 'error'); }
}

function updateDigestBtn() {
  const btn = document.getElementById('biz-digest-btn');
  if (!btn) return;
  btn.classList.toggle('hidden', S.bizDigestSel.size < 2);
  btn.textContent = `Make digest from ${S.bizDigestSel.size} selected`;
}

// makePost / makeDigest / loadDrafts are defined with the Phase 3 draft flow below.
//: What a post looks like before it has gone anywhere. Anything else belongs
//: to Results — a queue that keeps everything ever made stops being a queue on
//: the day it would start being useful.
const QUEUE_STATUSES = ['draft', 'preview', 'scheduled', 'failed'];

async function loadQueue() {
  const business = !!(S.user && S.user.account_type === 'business');
  const own = document.getElementById('queue-list');
  const biz = document.getElementById('biz-drafts-list');
  document.getElementById('queue-title').textContent = business ? 'Drafts' : 'Queue';
  document.getElementById('queue-sub').textContent = business
    ? 'Posts drafted from your leads. A person reviews and approves before anything goes out.'
    : "Everything you've made that hasn't gone out yet — drafts, previews and anything scheduled.";
  own.classList.toggle('hidden', business);
  biz.classList.toggle('hidden', !business);
  // Above the Business branch on purpose: the "not to Business" decision then
  // lives in the offer, where it is one testable guard, instead of being an
  // accident of this return sitting first.
  maybeOfferSources();      // not awaited: the queue itself must not wait on an offer
  if (business) return loadDrafts();

  // The Queue is bounded by how much work is waiting, not by how much history
  // exists — so it asks for its four statuses rather than filtering everything.
  const rows = await fetchPosts({ status: QUEUE_STATUSES });
  if (!rows.length) {
    own.innerHTML = '<div class="text-gray-500">Nothing waiting. Anything you make lands here until it goes out.</div>';
    return;
  }
  // One idea is one card, however many networks it went to. Collapsed by
  // default because the common case is one thought going out; expandable
  // because the siblings are separate posts with separate schedules and each
  // has to be reachable.
  own.innerHTML = groupPosts(rows).map(g => {
    const st = groupStatus(g.posts);
    const many = g.posts.length > 1;
    const inner = many ? g.posts.map(p =>
      `<div data-group-row class="hidden flex items-center gap-3 pl-6 py-1 cursor-pointer text-sm"
            data-action="open-post" data-arg="${esc(p.id)}">
        <span>${queueDot(p.status)}</span>${netBadge(p)}
        <span class="text-gray-400">${esc(queueLine(p))}</span>
      </div>`).join('') : '';
    return `<div class="ce-card p-3">
      <div class="flex items-center gap-3 cursor-pointer" data-action="open-post" data-arg="${esc(g.primary.id)}">
        <span>${queueDot(st)}</span>${netBadges(g.posts)}
        <div class="flex-1 min-w-0">
          <div class="text-gray-200 truncate">${esc(g.primary.topic || 'post')}</div>
          <div class="text-xs text-gray-500">${esc(queueLine(g.primary, st))}</div>
        </div>
        ${many ? `<button data-expand-group data-action="expand-group" class="ce-btn-ghost px-2 py-1 text-xs">${g.posts.length} networks</button>` : ''}
      </div>
      ${inner}
    </div>`;
  }).join('');
}

function queueDot(status) {
  return ({ scheduled: '🟣', failed: '🔴', published: '🟢' })[status] || '⚪';
}

function queueLine(p, status) {
  const when = p.scheduled_at ? new Date(p.scheduled_at).toLocaleString() : '';
  return `${status || p.status}${when ? ' · ' + when : ''}`
    + (p.schedule_error ? ' · ' + p.schedule_error : '');
}

async function loadDrafts() {
  const box = document.getElementById('biz-drafts-list');
  box.textContent = 'Loading…';
  try {
    const res = await apiFetch(`${API}/api/business/drafts`);
    const list = res.ok ? await res.json() : [];
    if (!list.length) { box.innerHTML = '<div class="text-sm text-gray-400">No drafts yet. Turn a lead into a post from the Leads tab.</div>'; return; }
    box.innerHTML = '';
    list.forEach(p => box.appendChild(renderDraftRow(p)));
  } catch { box.innerHTML = '<div class="text-sm text-gray-400">No drafts yet.</div>'; }
}

const DRAFT_STATUS = {
  draft: { label: 'Draft', color: '#8a8a8a' },
  in_review: { label: 'In review', color: '#c2a04e' },
  approved: { label: 'Approved', color: '#5aa17c' },
  rejected: { label: 'Rejected', color: '#c2704e' },
  preview: { label: 'Draft', color: '#8a8a8a' },
  // Without these a published or failed post fell through to the grey "Draft"
  // badge — the most misleading state of all for something that never went out.
  published: { label: 'Published', color: '#5aa17c' },
  scheduled: { label: 'Scheduled', color: '#7c6ad1' },
  failed: { label: 'Failed', color: '#c2704e' },
};

function renderVerifyBlock(p) {
  const claims = p.checked_claims || [];
  const bf = p.brand_flags || {};
  const forbidden = bf.forbidden || [];
  const missing = bf.missing_disclaimers || [];
  if (!claims.length && !forbidden.length && !missing.length) return '';
  let html = '<div class="mt-3 rounded-lg p-3 text-xs" style="background:var(--accent-dim)"><div class="font-semibold mb-1">Verify before approving</div>';
  claims.forEach(c => {
    const ok = c.status === 'confirmed';
    const col = ok ? '#5aa17c' : '#c2a04e';
    const tip = ok && c.evidence ? ` title="${esc(c.evidence)}"` : '';
    html += `<div style="color:${col}"${tip}>${ok ? '✓' : '?'} ${esc(c.claim)}${ok ? '' : ' — not found in source'}</div>`;
  });
  if (forbidden.length || missing.length) {
    html += '<div class="mt-2" style="color:#e06b52"><div class="font-semibold">Fix before approving:</div>';
    forbidden.forEach(f => { html += `<div>✕ forbidden phrase: “${esc(f)}”</div>`; });
    missing.forEach(m => { html += `<div>✕ missing disclaimer: “${esc(m)}”</div>`; });
    html += '</div>';
  }
  return html + '</div>';
}

function renderDraftRow(p) {
  const row = document.createElement('div');
  row.className = 'ce-card p-4';
  const tags = (p.hashtags || []).map(h => esc(h)).join(' ');
  const origin = p.source_kind === 'digest' ? 'Weekly digest' : 'From a source lead';
  const st = DRAFT_STATUS[p.status] || DRAFT_STATUS.draft;
  const bf = p.brand_flags || {};
  const blocked = (bf.forbidden || []).length || (bf.missing_disclaimers || []).length;
  const status = p.status || 'draft';
  let actions = '';
  if (status === 'draft') actions = `<button class="ce-btn px-3 py-1.5 text-xs" data-act="submit">Submit for review</button>`;
  else if (status === 'in_review') actions =
    `<button class="ce-btn px-3 py-1.5 text-xs" data-act="approve"${blocked ? ' disabled title="Fix brand-rule issues first"' : ''}>Approve</button>` +
    `<button class="ce-btn-ghost px-3 py-1.5 text-xs" data-act="reject">Reject</button>`;
  const editable = status === 'draft' || status === 'in_review';
  const netLabel = netBadge(p);
  const threadN = (p.thread_parts || []).length;
  row.innerHTML = `
    <div class="flex items-center gap-2 mb-1 flex-wrap">
      <span class="text-xs font-semibold" style="color:${st.color}">● ${st.label}</span>
      <span class="text-xs px-1.5 py-0.5 rounded" style="background:var(--accent-dim)">${netLabel}</span>
      ${threadN > 1 ? `<span class="text-xs px-1.5 py-0.5 rounded" style="background:var(--accent-dim)">${threadN} tweets</span>` : ''}
      <span class="text-xs text-gray-500">${origin}${p.source_url ? ` · <a href="${safeUrl(p.source_url)}" target="_blank" rel="noopener noreferrer" class="underline" style="color:var(--accent)">source</a>` : ''}</span>
    </div>
    ${p.hook ? `<div class="font-semibold text-sm">${esc(p.hook)}</div>` : ''}
    ${editable
      ? `<textarea class="ce-input w-full px-3 py-2 text-sm mt-1" rows="4" data-role="caption">${esc(p.caption || p.topic || '')}</textarea>`
      : `<div class="text-sm mt-1 whitespace-pre-line">${esc(p.caption || p.topic || '')}</div>`}
    ${tags ? `<div class="text-xs text-gray-400 mt-1">${tags}</div>` : ''}
    ${p.schedule_error ? `<div class="text-xs mt-2 px-2 py-1 rounded" style="background:var(--accent-dim);color:#f0a2a2">⚠️ Publish failed: ${esc(p.schedule_error)}</div>` : ''}
    ${renderVerifyBlock(p)}
    <div class="flex gap-2 mt-3 flex-wrap">
      ${editable ? `<button class="ce-btn-ghost px-3 py-1.5 text-xs" data-act="save">Save edits</button>` : ''}
      ${actions}
    </div>
  `;
  const cap = () => row.querySelector('[data-role="caption"]')?.value ?? '';
  const btn = (a) => row.querySelector(`[data-act="${a}"]`);
  if (btn('save')) btn('save').onclick = () => saveDraftEdit(p.id, cap());
  if (btn('submit')) btn('submit').onclick = () => draftAction(p.id, 'submit');
  if (btn('approve')) btn('approve').onclick = () => draftAction(p.id, 'approve');
  if (btn('reject')) btn('reject').onclick = () => draftAction(p.id, 'reject');
  return row;
}

async function saveDraftEdit(id, caption) {
  try {
    const res = await apiFetch(`${API}/api/business/drafts/${encodeURIComponent(id)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ caption }),
    });
    toast(res.ok ? 'Saved' : 'Couldn\'t save', res.ok ? 'success' : 'error');
  } catch { toast('Save failed', 'error'); }
}

async function draftAction(id, action) {
  try {
    const res = await apiFetch(`${API}/api/business/posts/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    if (res.ok) { toast(`Post ${action}d`, 'success'); loadDrafts(); }
    else { const d = await res.json().catch(() => ({})); toast(d.detail || 'Action failed', 'warn'); }
  } catch { toast('Action failed', 'error'); }
}

// ===== BUSINESS: Brand rules =====
async function loadBrandRules() {
  try {
    const res = await apiFetch(`${API}/api/business/brand-rules`);
    const d = res.ok ? await res.json() : { forbidden: [], required_disclaimers: [] };
    document.getElementById('rules-forbidden').value = (d.forbidden || []).join('\n');
    document.getElementById('rules-disclaimers').value = (d.required_disclaimers || []).join('\n');
    const lr = await apiFetch(`${API}/api/business/limits`);
    const l = lr.ok ? await lr.json() : {};
    document.getElementById('limit-day').value = l.max_per_day ?? '';
    document.getElementById('limit-week').value = l.max_per_week ?? '';
  } catch { /* 401 handled */ }
}

async function saveLimits() {
  const status = document.getElementById('limits-status');
  const num = (id) => { const v = parseInt(document.getElementById(id).value, 10); return Number.isFinite(v) ? v : null; };
  try {
    const res = await apiFetch(`${API}/api/business/limits`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_per_day: num('limit-day'), max_per_week: num('limit-week') }),
    });
    status.textContent = res.ok ? 'Saved.' : 'Couldn\'t save (values must be 1–100).';
    status.classList.remove('hidden');
  } catch { status.textContent = 'Something went wrong.'; status.classList.remove('hidden'); }
}

async function saveBrandRules() {
  const status = document.getElementById('rules-status');
  const lines = (id) => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  try {
    const res = await apiFetch(`${API}/api/business/brand-rules`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ forbidden: lines('rules-forbidden'), required_disclaimers: lines('rules-disclaimers') }),
    });
    status.textContent = res.ok ? 'Saved. New drafts are checked against these rules.' : 'Couldn\'t save.';
    status.classList.remove('hidden');
  } catch { status.textContent = 'Something went wrong.'; status.classList.remove('hidden'); }
}

// ===== BUSINESS: Journal =====
async function loadJournal() {
  const box = document.getElementById('biz-journal-list');
  box.textContent = 'Loading…';
  const qs = journalQuery();
  try {
    const res = await apiFetch(`${API}/api/business/journal${qs}`);
    const list = res.ok ? await res.json() : [];
    if (!list.length) { box.innerHTML = '<div class="text-sm text-gray-400">No approvals recorded yet.</div>'; return; }
    box.innerHTML = '';
    list.forEach(a => box.appendChild(renderJournalRow(a)));
  } catch { box.innerHTML = '<div class="text-sm text-gray-400">Couldn\'t load the journal.</div>'; }
}

function journalQuery() {
  const f = document.getElementById('journal-from')?.value;
  const t = document.getElementById('journal-to')?.value;
  const p = [];
  if (f) p.push('from=' + encodeURIComponent(f));
  if (t) p.push('to=' + encodeURIComponent(t + 'T23:59:59'));
  return p.length ? '?' + p.join('&') : '';
}

function renderJournalRow(a) {
  const row = document.createElement('div');
  row.className = 'ce-card p-3 text-sm';
  const when = a.approved_at ? new Date(a.approved_at).toLocaleString() : (a.created_at || '');
  const edited = (a.ai_draft || '') !== (a.human_edits || '');
  row.innerHTML = `
    <div class="text-xs text-gray-500">${esc(when)}${a.source_url ? ` · <a href="${safeUrl(a.source_url)}" target="_blank" rel="noopener noreferrer" class="underline" style="color:var(--accent)">source</a>` : ''}</div>
    <div class="mt-1 whitespace-pre-line">${esc(a.human_edits || a.ai_draft || '')}</div>
    ${edited ? '<div class="text-xs text-gray-400 mt-1">edited by a human before approval</div>' : ''}
  `;
  return row;
}

function _saStat(label, val) {
  return `<div class="bg-gray-900 rounded-lg px-2 py-2 text-center">
    <div class="text-base font-semibold">${esc(String(val))}</div>
    <div class="text-[10px] text-gray-400 uppercase tracking-wide">${esc(label)}</div>
  </div>`;
}

async function loadSourceAnalytics() {
  const box = document.getElementById('biz-analytics-list');
  const totBox = document.getElementById('biz-analytics-totals');
  box.textContent = 'Loading…'; totBox.innerHTML = '';
  try {
    const res = await apiFetch(`${API}/api/business/source-analytics`);
    const data = res.ok ? await res.json() : { sources: [], totals: {}, digests: 0 };
    const t = data.totals || {};
    totBox.innerHTML = `<div class="grid grid-cols-4 sm:grid-cols-8 gap-2">
      ${_saStat('Sources', t.sources || 0)}${_saStat('Leads', t.leads || 0)}${_saStat('Worthy', t.worthy || 0)}
      ${_saStat('Drafts', t.drafts || 0)}${_saStat('Approved', t.approved || 0)}${_saStat('Published', t.published || 0)}
      ${_saStat('Reach', t.reach || 0)}${_saStat('Engagement', t.engagement || 0)}
    </div>${(t.measured_posts ? '' : '<div class="text-xs text-gray-500 mt-2">Reach/engagement fill in once published Instagram posts have their insights refreshed (X has no in-app metrics).</div>')}${data.digests ? `<div class="text-xs text-gray-500 mt-2">+ ${esc(String(data.digests))} digest post(s) span multiple leads — not attributed to a single source.</div>` : ''}`;
    const list = data.sources || [];
    if (!list.length) { box.innerHTML = '<div class="text-sm text-gray-400">No sources yet — add some in Sources.</div>'; return; }
    box.innerHTML = '';
    list.forEach((s, i) => box.appendChild(renderSourceAnalyticsRow(s, i)));
  } catch { box.innerHTML = '<div class="text-sm text-gray-400">Couldn\'t load analytics.</div>'; }
}

function renderSourceAnalyticsRow(s, i) {
  const row = document.createElement('div');
  row.className = 'ce-card p-3';
  let host = s.url || '';
  try { const u = new URL(s.url); host = u.hostname + u.pathname; } catch { /* keep raw */ }
  const worthyPct = Math.min(100, Math.round((s.worthy_rate || 0) * 100));
  const approvePct = Math.min(100, Math.round((s.approve_rate || 0) * 100));
  const last = s.last_lead_at ? new Date(s.last_lead_at).toLocaleDateString() : '—';
  const cell = (v, l) => `<div><div class="text-sm font-semibold">${esc(String(v || 0))}</div><div class="text-[10px] text-gray-400">${l}</div></div>`;
  const bar = (label, pct, color) => `<div class="mt-1 flex items-center gap-2 text-xs text-gray-400">
      <span class="w-20">${label} ${pct}%</span>
      <div class="flex-1 h-2 bg-gray-800 rounded-full overflow-hidden"><div class="h-full ${color}" style="width:${pct}%"></div></div>
    </div>`;
  row.innerHTML = `
    <div class="flex items-center gap-2 flex-wrap">
      <span class="text-xs text-gray-500">#${i + 1}</span>
      <a href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer" class="text-sm underline truncate max-w-[60%]" style="color:var(--accent)">${esc(host)}</a>
      <span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-300">${esc(s.kind || '')}</span>
      <span class="text-xs text-gray-500 ml-auto">last lead ${esc(last)}</span>
    </div>
    <div class="mt-2 grid grid-cols-5 gap-2 text-center">
      ${cell(s.leads_total, 'leads')}${cell(s.worthy, 'worthy')}${cell(s.drafts, 'drafts')}${cell(s.approved, 'approved')}${cell(s.published, 'published')}
    </div>
    ${s.measured_posts ? `<div class="mt-2 text-xs text-gray-300">reach <b>${esc(String(s.reach || 0))}</b> · engagement <b>${esc(String(s.engagement || 0))}</b> <span class="text-gray-500">(${esc(String(s.measured_posts))} measured)</span></div>` : ''}
    ${bar('worthy', worthyPct, 'bg-purple-600')}
    ${bar('approved', approvePct, 'bg-green-600')}
  `;
  return row;
}

async function exportJournal(fmt) {
  const qs = journalQuery();
  const sep = qs ? '&' : '?';
  try {
    const res = await apiFetch(`${API}/api/business/journal/export${qs}${sep}format=${fmt}`);
    if (!res.ok) return toast('Export failed', 'error');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `journal.${fmt}`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch { toast('Export failed', 'error'); }
}

// Stream a draft (or digest) from the Business generation endpoint (user's key).
async function _streamBiz(url, body, onDone) {
  try {
    const res = await apiFetch(`${API}${url}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) { toast('Generation failed. Check your AI key in Account.', 'error'); return; }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '', ok = false;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === 'progress') toast(ev.message, 'info');
        else if (ev.type === 'error') toast(ev.message, 'error');
        else if (ev.type === 'complete') ok = true;
      }
    }
    if (ok && onDone) onDone();
  } catch { toast('Generation failed. Please try again.', 'error'); }
}

async function makePost(leadId, row, platform, xmode) {
  const net = platform === 'x' ? 'x' : 'instagram';
  const xm = (net === 'x' && xmode === 'thread') ? 'thread' : 'short';
  toast('Drafting a post…', 'info');
  await _streamBiz(`/api/business/leads/${encodeURIComponent(leadId)}/draft?platform=${net}&x_mode=${xm}`, {}, () => {
    toast('Draft created — see the Drafts tab.', 'success');
    if (row) row.remove();
    S.bizDigestSel.delete(leadId); updateDigestBtn();
  });
}

async function makeDigest() {
  const ids = Array.from(S.bizDigestSel);
  if (ids.length < 2) return;
  toast('Building a digest…', 'info');
  await _streamBiz('/api/business/digest', { lead_ids: ids }, () => {
    toast('Digest drafted — see the Drafts tab.', 'success');
    loadLeads();
  });
}

// One-time app bootstrap after a user is known (intervals registered once).
async function startApp() {
  if (S.appStarted) return;
  S.appStarted = true;
  refreshCost();
  setInterval(refreshCost, 60000);
  redeemTeamInvite();
  // What this person has already been shown. Deliberately not awaited: nothing
  // on the first screen depends on it, and the one thing that does — an offer
  // beside the caption box — is several clicks away. Awaiting it would put a
  // round trip in front of every login to decide a banner nobody can see yet.
  loadMilestones();
  // Business lands on Create, which for them is Leads, and skips the creator
  // composer loaders entirely. It used to land on 'biz-sources' -- a section id
  // that stopped existing in 3.8, when Sources became a Settings tab. That left
  // a Business account looking at an empty page after login, and nothing threw:
  // setSection simply hid every view, because none of them matched.
  if (S.user && S.user.account_type === 'business' && !S.user.is_local) {
    setSection('create');
    maybeStartOnboarding();   // an empty workspace needs a key and a source, same as a creator
    return;
  }
  setNetwork('instagram');   // default network tab → drives sections + platform
  // Cloud accounts: load the brand profile (pre-fills the composer) and, on first
  // login with no profile yet, offer the skippable onboarding step.
  if (S.user && !S.user.is_local) {
    await loadAccounts();    // agency multi-account switcher (Phase 7)
    await loadProfile();
    await loadXSettings();   // the composer gates "Long post" on this
    await loadPresets();     // saved composer settings, shown in step 2
    // Before onboarding: somebody who arrived with a post in hand should see it,
    // not a setup screen asking what they do.
    await carryHeroDraft();
    maybeStartOnboarding();
  }
}

// ===== MANAGED ACCOUNTS (Phase 7: agency multi-brand switcher) =====
S.accounts = [];
S.editingBrandId = null;

async function loadAccounts() {
  try {
    const res = await apiFetch(`${API}/api/accounts`);
    const d = res.ok ? await res.json() : { accounts: [], active_account_id: null };
    S.accounts = d.accounts || [];
    if (S.user) S.user.active_account_id = d.active_account_id || null;
  } catch { S.accounts = []; }
  renderAcctSwitcher();
  // The Team gate reads this list, and this is the only place it changes — so
  // this is where it gets re-asked. Leaving it to the milestone load alone was
  // a race: that fires first, when S.accounts is still empty.
  renderTeamTab();
}

function toggleAvatarMenu() {
  const m = document.getElementById('avatar-menu');
  if (!m) return;
  const open = m.classList.toggle('hidden') === false;
  document.getElementById('avatar-btn').setAttribute('aria-expanded', String(open));
}
function closeAvatarMenu() {
  const m = document.getElementById('avatar-menu');
  if (m) m.classList.add('hidden');
  const b = document.getElementById('avatar-btn');
  if (b) b.setAttribute('aria-expanded', 'false');
}
// Clicking anywhere else closes it — a menu that only closes by its own button
// is a menu people leave open over the screen they wanted to look at.
document.addEventListener('click', e => {
  const menu = document.getElementById('avatar-menu');
  if (!menu || menu.classList.contains('hidden')) return;
  if (!e.target.closest('#avatar-menu') && !e.target.closest('#avatar-btn')) closeAvatarMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAvatarMenu(); });

function renderAcctSwitcher() {
  const sel = document.getElementById('menu-acct-switcher');
  const row = document.getElementById('menu-brand-row');
  if (!sel) return;
  const show = S.user && !S.user.is_local && S.user.account_type !== 'business';
  if (row) row.classList.toggle('hidden', !show);
  if (!show) return;
  const active = (S.user && S.user.active_account_id) || '';
  // No hardcoded "Personal" row: since the profile rework every user owns a
  // profile and it is always in this list, primary first (the API orders it).
  // An empty value would now mean "switch to my primary", which is what the
  // list's first entry already does.
  sel.innerHTML = S.accounts.map(a =>
    `<option value="${esc(a.id)}"${a.id === active ? ' selected' : ''}>${esc(a.name || 'Brand')}</option>`).join('');
}

async function onAcctSwitch() {
  const sel = document.getElementById('acct-switcher');
  const id = sel.value || null;
  try {
    const res = await apiFetch(`${API}/api/accounts/switch`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_id: id }),
    });
    if (!res.ok) { toast('Couldn\'t switch brand', 'error'); return; }
    S.user.active_account_id = (await res.json()).active_account_id || null;
    toast('Switched brand', 'success');
    // refresh the current view + composer prefill for the new brand
    if (S.section) setSection(S.section);
    await prefillFromActiveBrand();
  } catch { toast('Switch failed', 'error'); }
}

async function onProductSwitch() {
  const sel = document.getElementById('product-switcher');
  const want = sel.value;
  const current = (S.user && S.user.account_type) || 'creator';
  if (!S.user || want === current) return;
  try {
    const res = await apiFetch(`${API}/api/auth/account-type`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_type: want }),
    });
    if (!res.ok) { toast('Couldn\'t switch product', 'error'); sel.value = current; return; }
    S.user.account_type = (await res.json()).account_type || 'creator';
    // Reboot into the new product's shell — the cleanest way to re-route the
    // whole app (Business vs Creators bootstrap differ). account_type persists
    // in the DB, so /me returns the right mode on reload.
    location.reload();
  } catch { toast('Switch failed', 'error'); sel.value = current; }
}

async function prefillFromActiveBrand() {
  const id = S.user && S.user.active_account_id;
  if (!id) { await loadProfile(); return; }   // Personal → the user's own profile
  try {
    const res = await apiFetch(`${API}/api/accounts/${encodeURIComponent(id)}`);
    if (!res.ok) return;
    const a = await res.json();
    S.profile = { niche: a.niche || '', target_audience: a.target_audience || '', brand_name: a.brand_name || '' };
    if (a.slide_accent_color) S.nicheBoxColor = a.slide_accent_color;
    if (typeof prefillComposerFromProfile === 'function') prefillComposerFromProfile();
  } catch { /* non-fatal */ }
}

function openBrandsModal() {
  document.getElementById('brands-modal').classList.remove('hidden');
  document.getElementById('brand-editor').classList.add('hidden');
  S.editingBrandId = null;
  renderBrandsList();
}
function closeBrandsModal() {
  document.getElementById('brands-modal').classList.add('hidden');
}

async function renderBrandsList() {
  await loadAccounts();
  const box = document.getElementById('brands-list');
  if (!S.accounts.length) { box.innerHTML = '<div class="text-sm text-gray-400">No brands yet. Add one above.</div>'; return; }
  box.innerHTML = '';
  S.accounts.forEach(a => {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 ce-card px-3 py-2';
    const tag = a.is_primary ? ' <span class="text-xs text-gray-500">· main</span>' : '';
    row.innerHTML = `<span class="text-sm flex-1">${esc(a.name || 'Brand')}${tag}</span><button class="ce-btn-ghost px-2 py-1 text-xs">Edit</button>`;
    row.querySelector('button').onclick = () => editBrand(a.id);
    box.appendChild(row);
  });
}

async function createBrand() {
  const input = document.getElementById('brand-new-name');
  const name = (input.value || '').trim();
  if (!name) return;
  try {
    const res = await apiFetch(`${API}/api/accounts`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) { toast('Couldn\'t create brand', 'error'); return; }
    input.value = '';
    await renderBrandsList();
    editBrand((await res.json()).id);
  } catch { toast('Create failed', 'error'); }
}

async function editBrand(id) {
  S.editingBrandId = id;
  try {
    const res = await apiFetch(`${API}/api/accounts/${encodeURIComponent(id)}`);
    if (!res.ok) return;
    const a = await res.json();
    document.getElementById('brand-editor').classList.remove('hidden');
    document.getElementById('brand-editor-name').textContent = a.name || '';
    document.getElementById('brand-f-name').value = a.name || '';
    document.getElementById('brand-f-brandname').value = a.brand_name || '';
    document.getElementById('brand-f-niche').value = a.niche || '';
    document.getElementById('brand-f-audience').value = a.target_audience || '';
    document.getElementById('brand-f-voice').value = a.brand_voice_preset || '';
    document.getElementById('brand-f-accent').value = a.slide_accent_color || '';
    document.getElementById('brand-logo-state').textContent = a.has_logo ? 'logo set' : 'no logo';
    // The main profile can't be deleted (the API returns 409). Hide the button
    // rather than offer an action that always fails.
    const primary = (S.accounts.find(x => x.id === id) || {}).is_primary;
    document.getElementById('brand-delete').classList.toggle('hidden', !!primary);
  } catch { /* non-fatal */ }
}

async function saveBrand() {
  if (!S.editingBrandId) return;
  const status = document.getElementById('brand-editor-status');
  const val = (id) => document.getElementById(id).value.trim();
  try {
    const res = await apiFetch(`${API}/api/accounts/${encodeURIComponent(S.editingBrandId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: val('brand-f-name'), brand_name: val('brand-f-brandname'),
        niche: val('brand-f-niche'), target_audience: val('brand-f-audience'),
        brand_voice_preset: val('brand-f-voice'), slide_accent_color: val('brand-f-accent'),
      }),
    });
    status.textContent = res.ok ? 'Saved.' : 'Couldn\'t save (check the colour is a hex like #ff751f).';
    status.classList.remove('hidden');
    if (res.ok) { await loadAccounts(); if (S.user.active_account_id === S.editingBrandId) prefillFromActiveBrand(); }
  } catch { status.textContent = 'Something went wrong.'; status.classList.remove('hidden'); }
}

async function uploadBrandLogo() {
  if (!S.editingBrandId) return;
  const file = document.getElementById('brand-f-logo').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await apiFetch(`${API}/api/accounts/${encodeURIComponent(S.editingBrandId)}/logo`, { method: 'POST', body: fd });
    document.getElementById('brand-logo-state').textContent = res.ok ? 'logo set' : 'upload failed';
  } catch { toast('Upload failed', 'error'); }
}

async function deleteBrand() {
  if (!S.editingBrandId || !confirm('Delete this brand? Its posts move to your main profile.')) return;
  try {
    const res = await apiFetch(`${API}/api/accounts/${encodeURIComponent(S.editingBrandId)}`, { method: 'DELETE' });
    if (!res.ok) { toast('Couldn\'t delete that brand', 'error'); return; }
    S.editingBrandId = null;
    document.getElementById('brand-editor').classList.add('hidden');
    await loadAccounts();
    await renderBrandsList();
  } catch { toast('Delete failed', 'error'); }
}

// ===== POST PRESETS =====
// A preset is the "how" of a post — the settings below — not the topic or photos.
S.presets = [];

function _currentPreset(name) {
  return {
    name,
    format: S.format,
    tone: document.getElementById('tone').value,
    length_tier: document.getElementById('length-tier').value,
    // 'upload' is per-post files, not a reusable setting — store stock instead.
    default_image_source: S.source === 'upload' ? 'stock' : S.source,
    platform: S.platform || 'instagram',
    template_style: S.templateStyle,
    niche_box_color: S.nicheBoxColor || null,
    apply_branding: document.getElementById('apply-brand').checked,
    show_logo: document.getElementById('show-logo').checked,
  };
}

function renderPresetSelect() {
  const row = document.getElementById('preset-row');
  const sel = document.getElementById('preset-select');
  if (!row || !sel) return;
  const cloud = S.user && !S.user.is_local;
  row.classList.toggle('hidden', !cloud);
  sel.innerHTML = S.presets.length
    ? S.presets.map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('')
    : '<option value="">No presets yet</option>';
}

async function loadPresets() {
  try {
    const res = await apiFetch(`${API}/api/settings/presets`);
    if (res.ok) S.presets = (await res.json()).presets || [];
  } catch { /* 401 handled by apiFetch */ }
  renderPresetSelect();
}

async function _savePresets() {
  const res = await apiFetch(`${API}/api/settings/presets`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ presets: S.presets }),
  });
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Save failed'); }
  S.presets = (await res.json()).presets || [];   // server deduped/validated
  renderPresetSelect();
}

function _clickToggle(selector, value) {
  const btn = document.querySelector(`${selector}[data-val="${value}"]`);
  if (btn) btn.click();            // updates both S and the visual state
}

function applySelectedPreset() {
  const name = document.getElementById('preset-select').value;
  const p = S.presets.find(x => x.name === name);
  if (!p) return;
  if ((p.platform || 'instagram') !== (S.platform || 'instagram')) setNetwork(p.platform);
  _clickToggle('.fmt-btn', p.format);
  _clickToggle('.src-btn', p.default_image_source);
  _clickToggle('.tpl-btn', p.template_style);
  document.getElementById('tone').value = p.tone;
  document.getElementById('length-tier').value = p.length_tier;
  document.getElementById('apply-brand').checked = !!p.apply_branding;
  document.getElementById('show-logo').checked = !!p.show_logo;
  // Click the matching swatch so both S and the selected-outline update; a custom
  // colour not in the palette just sets state (no swatch to highlight).
  const swatches = document.getElementById('niche-color-swatches');
  const swatch = swatches && swatches.querySelector(`button[data-color="${p.niche_box_color || ''}"]`);
  if (swatch) swatch.click(); else S.nicheBoxColor = p.niche_box_color || null;
  renderOwnPhotos();               // slide count may have changed
  toast(`Applied “${p.name}”`, 'info');
}

async function saveCurrentPreset() {
  const name = (prompt('Name this preset:') || '').trim();
  if (!name) return;
  const preset = _currentPreset(name);
  const i = S.presets.findIndex(p => p.name.toLowerCase() === name.toLowerCase());
  if (i >= 0) S.presets[i] = preset; else S.presets.push(preset);
  try { await _savePresets(); document.getElementById('preset-select').value = name; toast('✅ Preset saved', 'success'); }
  catch (e) { toast('❌ ' + e.message, 'error'); }
}

async function deleteSelectedPreset() {
  const name = document.getElementById('preset-select').value;
  if (!name || !S.presets.some(p => p.name === name)) return;
  S.presets = S.presets.filter(p => p.name !== name);
  try { await _savePresets(); toast('Preset deleted', 'info'); }
  catch (e) { toast('❌ ' + e.message, 'error'); }
}

function renderUserChrome() {
  const chip = document.getElementById('user-chip');
  const logout = document.getElementById('logout-btn');
  const cloud = S.user && !S.user.is_local;
  // Business is a cloud-only product (source polling has no meaning offline). Its
  // screens don't exist yet (Phase 2); expose the flag on <body> so future
  // Business-only sections can gate on it without re-reading S everywhere.
  const business = cloud && S.user.account_type === 'business';
  const agency = cloud && S.user.account_type === 'agency';
  // Three values now. An agency gets the creator shell plus the Team tab, which
  // is why this says 'agency' rather than folding it into 'creator': the CSS
  // needs something to match on, and only .agency-only keys off it.
  document.body.dataset.accountType = business ? 'business' : (agency ? 'agency' : 'creator');
  // Product switcher (Creators ↔ Business): one email = one login, so this
  // in-app toggle is the only way to move between the two products. Cloud only
  // (local desktop is creator-only and exempt from the business gate).
  const prod = document.getElementById('product-switcher');
  if (prod) {
    prod.classList.toggle('hidden', !cloud);
    prod.value = business ? 'business' : 'creator';
  }
  chip.textContent = cloud ? ('👤 ' + (S.user.email || '')) : '';
  chip.classList.toggle('hidden', !cloud);
  logout.classList.toggle('hidden', !cloud);
  // API-keys page: cloud only (local desktop uses .env). Backup: admin/local only.
  const keys = document.getElementById('keys-section');
  const backup = document.getElementById('backup-section');
  if (keys) keys.classList.toggle('hidden', !cloud);
  if (backup) backup.classList.toggle('hidden', !(S.user && (S.user.is_admin || S.user.is_local)));
  // Nudge cloud users to verify their email (publishing is gated on it in prod).
  const banner = document.getElementById('verify-banner');
  const showBanner = cloud && S.user.email_verified === false;
  if (banner) {
    banner.classList.toggle('hidden', !showBanner);
    banner.style.top = '0px';
  }
  layoutBanners();
  renderAcctSwitcher();   // agency brand switcher: shown for creator + cloud only
}

function showAuthScreen(tab, accountType) {
  hideLanding();
  document.getElementById('forgot-screen').classList.add('hidden');
  document.getElementById('reset-screen').classList.add('hidden');
  document.getElementById('auth-screen').classList.remove('hidden');
  // The door carries its type into the sign-up form; default follows the landing tab.
  setSignupAccountType(accountType || S.landingTab || 'creator');
  switchAuthTab(tab || 'login');
  document.getElementById('auth-email').focus();
}

function setSignupAccountType(t) {
  S.signupAccountType = t === 'business' ? 'business' : 'creator';
  const biz = S.signupAccountType === 'business';
  for (const [id, on] of [['atype-creator', !biz], ['atype-business', biz]]) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.classList.toggle('border-purple-500', on);
    b.classList.toggle('bg-purple-900', on);
    b.classList.toggle('text-white', on);
    b.classList.toggle('border-gray-700', !on);
    b.classList.toggle('bg-gray-800', !on);
    b.classList.toggle('text-gray-300', !on);
  }
}
function hideAuthScreen() {
  document.getElementById('auth-screen').classList.add('hidden');
}

function switchAuthTab(tab) {
  S.authTab = tab;
  const login = tab === 'login';
  for (const [id, on] of [['auth-tab-login', login], ['auth-tab-register', !login]]) {
    const b = document.getElementById(id);
    b.classList.toggle('border-purple-500', on);
    b.classList.toggle('bg-purple-900', on);
    b.classList.toggle('text-white', on);
    b.classList.toggle('border-gray-700', !on);
    b.classList.toggle('bg-gray-800', !on);
    b.classList.toggle('text-gray-300', !on);
  }
  document.getElementById('auth-heading').textContent = login ? 'Welcome back' : 'Create your account';
  document.getElementById('auth-subhead').textContent = login ? 'Log in to your workspace.' : 'Free to start — bring your own keys.';
  document.getElementById('auth-password').setAttribute(
    'autocomplete', login ? 'current-password' : 'new-password');
  document.getElementById('auth-submit').textContent = login ? 'Log in' : 'Create account';
  document.getElementById('auth-forgot-link').classList.toggle('hidden', !login);
  // Creator/Business chooser only matters when creating an account.
  const atype = document.getElementById('auth-accounttype-row');
  if (atype) atype.classList.toggle('hidden', login);
  document.getElementById('auth-error').classList.add('hidden');
}

async function submitAuth() {
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const errEl = document.getElementById('auth-error');
  const show = (m) => { errEl.textContent = m; errEl.classList.remove('hidden'); };
  if (!email || !password) return show('Email and password are required.');
  const path = S.authTab === 'register' ? 'register' : 'login';
  const payload = { email, password };
  if (path === 'register') payload.account_type = S.signupAccountType || 'creator';
  try {
    const res = await fetch(`${API}/api/auth/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail ? (typeof d.detail === 'string' ? d.detail : 'Check your input') : 'Failed');
    if (!d.access_token) {
      // Server accepted but returned no session token (e.g. "verify your email
      // first"). Don't store "undefined" — tell the user what to do next.
      document.getElementById('auth-password').value = '';
      show('✅ Account created. Check your email to verify, then log in.');
      return;
    }
    localStorage.setItem('api_token', d.access_token);
    document.getElementById('auth-password').value = '';
    await initAuth();
  } catch (e) { show('❌ ' + e.message); }
}

function logout() {
  localStorage.removeItem('api_token');
  S.user = null;
  S.creds = null;
  document.getElementById('user-chip').classList.add('hidden');
  document.getElementById('logout-btn').classList.add('hidden');
  showLanding();
}

// A 401 mid-session means the token expired/was revoked — back to the landing page.
function handleAuthExpired() {
  localStorage.removeItem('api_token');
  S.user = null;
  S.creds = null;
  showLanding();
}

// ---- email verification & password reset ----

function _hideAllAuthScreens() {
  for (const id of ['landing-screen', 'auth-screen', 'forgot-screen', 'reset-screen'])
    document.getElementById(id).classList.add('hidden');
}

function showForgotScreen() {
  _hideAllAuthScreens();
  document.getElementById('forgot-msg').classList.add('hidden');
  document.getElementById('forgot-screen').classList.remove('hidden');
  document.getElementById('forgot-email').focus();
}

async function submitForgot() {
  const email = document.getElementById('forgot-email').value.trim();
  const msg = document.getElementById('forgot-msg');
  const show = (m, cls) => { msg.textContent = m; msg.className = 'text-xs ' + cls; };
  if (!email) return show('Enter your email.', 'text-red-400');
  try {
    await fetch(`${API}/api/auth/forgot`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    // Always success-shaped — the server never reveals whether the email exists.
    show('✅ If that email is registered, a reset link is on its way.', 'text-green-400');
  } catch { show('Network error, try again.', 'text-red-400'); }
}

async function submitReset() {
  const password = document.getElementById('reset-password').value;
  const msg = document.getElementById('reset-msg');
  const show = (m, cls) => { msg.textContent = m; msg.className = 'text-xs ' + cls; };
  if (!password || password.length < 8) return show('Password must be at least 8 characters.', 'text-red-400');
  try {
    const res = await fetch(`${API}/api/auth/reset`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: S.resetToken, password }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Reset failed');
    show('✅ Password updated — you can log in now.', 'text-green-400');
    setTimeout(() => { _clearAuthQuery(); showAuthScreen('login'); }, 1200);
  } catch (e) { show('❌ ' + e.message, 'text-red-400'); }
}

async function resendVerification() {
  try {
    await apiFetch(`${API}/api/auth/resend-verification`, { method: 'POST' });
    toast('Verification email sent — check your inbox.', 'success');
  } catch (e) { toast(e.message, 'error'); }
}

// ===== TEAM (agency invitations) =====

async function loadTeam() {
  const box = document.getElementById('team-list');
  box.innerHTML = 'Loading…';
  try {
    const rows = await (await apiFetch(`${API}/api/team/invitations`)).json();
    if (!rows.length) {
      box.innerHTML = '<div class="text-sm text-gray-500">Nobody invited yet.</div>';
      return;
    }
    const dot = { pending: '⚪', accepted: '🟢', revoked: '⚫' };
    box.innerHTML = rows.map(r => `<div class="ce-card p-3 flex items-center gap-3 text-sm">
      <span>${dot[r.status] || '⚪'}</span>
      <div class="flex-1 min-w-0"><div class="text-gray-200 truncate">${esc(r.email)}</div>
        <div class="text-xs text-gray-500">${esc(r.status)}</div></div>
      ${r.status === 'pending'
        ? `<button data-action="revoke-invite" data-arg="${esc(r.id)}" class="ce-btn-ghost px-3 py-1 text-xs">Revoke</button>`
        : ''}
    </div>`).join('');
  } catch (e) { box.innerHTML = `<div class="text-sm text-red-400">${esc(e.message)}</div>`; }
}

async function inviteTeammate() {
  const input = document.getElementById('team-email');
  const status = document.getElementById('team-status');
  const email = (input.value || '').trim();
  status.classList.remove('hidden');
  // The server validates this too. Asking it first costs a round-trip to be told
  // what the field already knew, and the answer belongs next to the field.
  if (!email.includes('@')) { status.textContent = 'Enter a valid email address.'; return; }
  status.textContent = 'Sending…';
  try {
    const res = await apiFetch(`${API}/api/team/invitations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Could not send the invitation.');
    status.textContent = 'Invitation sent.';
    input.value = '';
    await loadTeam();
  } catch (e) { status.textContent = e.message; }
}

async function revokeInvite(id) {
  if (!confirm('Revoke this invitation? The link in the email stops working.')) return;
  try {
    await apiFetch(`${API}/api/team/invitations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadTeam();
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

/** Redeem an invitation the user arrived with.
 *
 *  Accepting is an authenticated call, but the link lands on a signed-out
 *  browser as often as not -- so the token is parked in sessionStorage and spent
 *  once the app has a user. sessionStorage rather than S, because the sign-in
 *  round trip reloads the page. */
async function redeemTeamInvite() {
  let token = null;
  try { token = sessionStorage.getItem('team_invite_token'); } catch { return; }
  if (!token) return;
  try { sessionStorage.removeItem('team_invite_token'); } catch {}
  try {
    const res = await apiFetch(`${API}/api/team/invitations/accept`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    const body = await res.json();
    toast(res.ok ? "✅ You're on the team. Shared access is coming — nothing has changed yet."
                 : '❌ ' + (body.detail || 'That invitation could not be accepted.'),
          res.ok ? 'success' : 'error');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

function _clearAuthQuery() {
  // Drop /verify?token / /reset?token from the address bar without a reload.
  try { history.replaceState(null, '', '/'); } catch {}
}

// Handle links that land on /verify?token=… or /reset?token=… before the app boots.
async function handleAuthLink() {
  const path = window.location.pathname;
  const token = new URLSearchParams(window.location.search).get('token');
  if (!token) return false;
  if (path === '/verify') {
    try {
      const res = await fetch(`${API}/api/auth/verify?token=${encodeURIComponent(token)}`);
      toast(res.ok ? '✅ Email verified — you can log in.' : '❌ Invalid or expired link.',
            res.ok ? 'success' : 'error');
    } catch { toast('Network error verifying email.', 'error'); }
    _clearAuthQuery();
    return false;   // continue normal boot (initAuth)
  }
  if (path === '/team/accept') {
    // Park it and carry on booting: accepting needs a signed-in user, and this
    // link routinely arrives in a browser that has never seen the app.
    try { sessionStorage.setItem('team_invite_token', token); } catch {}
    _clearAuthQuery();
    return false;
  }
  if (path === '/reset') {
    S.resetToken = token;
    _hideAllAuthScreens();
    document.getElementById('reset-screen').classList.remove('hidden');
    document.getElementById('reset-password').focus();
    return true;    // stop normal boot — user is resetting
  }
  return false;
}

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    ...opts,
    headers: { ...authHeader(), ...(opts.headers || {}) },
  });
  if (res.status === 401) {
    handleAuthExpired();
    throw new Error('Session expired — please sign in again.');
  }
  return res;
}

/** Whether the Reel's own visuals are usable, given the voiceover checkbox.
 *
 *  Called from the change handler AND once at start-up. It used to live in an
 *  inline attribute, which meant the initial state depended on the hardcoded
 *  `disabled` in the HTML still agreeing with the checkbox's `checked` — an
 *  invariant nothing enforced. Now there is one expression and it runs. */
function syncReelVisuals(el) {
  const box = el || document.getElementById('reel-voiceover');
  const visuals = document.getElementById('reel-visuals');
  if (box && visuals) visuals.disabled = !box.checked;
}

// ===== ACTIONS: one delegated click handler =====
//
// Markup carries an identifier, never code. `data-action` names an entry below
// and `data-arg` carries at most one primitive; anything richer than that is a
// closure instead (see the `data-act` sites, which assign `el.onclick` after
// rendering — a JS property, which CSP does not govern at all).
//
// Why one listener rather than per-element wiring: HTML built by innerHTML has
// no place to hang a closure without a second pass over the DOM, and a delegated
// listener works for markup that does not exist yet.
//
// Nesting is handled by `closest()` returning the NEAREST ancestor and this
// dispatching exactly once. That is why the converted sites dropped their
// `event.stopPropagation()`: by the time a document-level listener runs the
// event has finished bubbling, so stopping it there does nothing — the inner
// control wins because it is found first. Do not "improve" this into a loop
// over every [data-action] ancestor; that would fire the outer one too.
const ACTIONS = Object.freeze({
  'open-post':      (el) => openPost(el.dataset.arg),
  'expand-group':   (el) => el.closest('.ce-card')
                              .querySelectorAll('[data-group-row]')
                              .forEach(r => r.classList.toggle('hidden')),
  'revoke-invite':  (el) => revokeInvite(el.dataset.arg),
  'result-tab':     (el) => setResultTab(el.dataset.arg),
  'apply-overlay':  (el) => applyOverlay(Number(el.dataset.arg)),
  'reset-overlay':  (el) => resetOverlay(Number(el.dataset.arg)),
  'insert-emoji':   (el) => insertEmoji(el.dataset.arg),
  'remove-hashtag': (el) => removeHashtag(Number(el.dataset.arg)),
  'dismiss-toast':  () => document.getElementById('toast').classList.add('hidden'),
  'pick-voice':     (el) => pickVoice(el.dataset.arg),
  'ai-test':        (el) => testAI(el.dataset.arg),
  'onb-pick-color': (el) => onbPickColor(el.dataset.arg),

  // ── from the static markup (CSP phase 4) ────────────────────
  'add-hashtag':                         () => addHashtag(),
  'add-own-photos':                      (el, ev) => addOwnPhotos(ev),
  'add-plan-row':                        () => addPlanRow(),
  'add-source':                          () => addSource(),
  'apply-selected-preset':               () => applySelectedPreset(),
  'auth-register-business':              () => showAuthScreen('register', 'business'),
  'back-to-home':                        () => backToHome(),
  'cal-shift':                           (el) => calShift(Number(el.dataset.arg)),
  'clear-video-seed':                    () => clearVideoSeed(),
  'close-brands-modal':                  () => closeBrandsModal(),
  'close-delete-account':                () => closeDeleteAccount(),
  'close-edit-slide':                    () => closeEditSlide(),
  'close-edit-video-modal':              () => closeEditVideoModal(),
  'close-library-picker':                () => closeLibraryPicker(),
  'close-need-key':                      () => closeNeedKey(),
  'close-onboarding':                    (el) => closeOnboarding(el.dataset.arg),
  'close-publish-to-x-modal':            () => closePublishToXModal(),
  'close-variations':                    () => closeVariations(),
  'confirm-delete-account':              () => confirmDeleteAccount(),
  'create-brand':                        () => createBrand(),
  'delete-brand':                        () => deleteBrand(),
  'delete-selected-preset':              () => deleteSelectedPreset(),
  'dismiss-sources-offer':               () => document.getElementById('sources-offer').classList.add('hidden'),
  'dismiss-voice-hint':                  () => dismissVoiceHint(),
  'download-backup':                     () => downloadBackup(),
  'download-hero-image':                 () => downloadHeroImage(),
  'export-journal':                      (el) => exportJournal(el.dataset.arg),
  'export-my-data':                      () => exportMyData(),
  'export-post':                         () => exportPost(),
  'generate-library-image':              () => generateLibraryImage(),
  'generate-library-video':              () => generateLibraryVideo(),
  'generate-post':                       () => generatePost(),
  'go-home':                             () => goHome(),
  'go-step':                             (el) => goStep(Number(el.dataset.arg)),
  'goto-need-key':                       () => gotoNeedKey(),
  'invite-teammate':                     () => inviteTeammate(),
  'library-upload-image':                (el, ev) => uploadToLibrary(ev, 'image'),
  'load-journal':                        () => loadJournal(),
  'load-more-grid':                      () => loadMoreGrid(),
  'logout':                              () => logout(),
  'make-digest':                         () => makeDigest(),
  'make-reel':                           () => makeReel(),
  'menu-brands':                         () => { closeAvatarMenu(); openBrandsModal(); },
  'menu-settings':                       () => { closeAvatarMenu(); openSettings('profiles'); },
  'menu-setup-guide':                    () => { closeAvatarMenu(); startOnboarding({restart: true}); },
  'menu-theme':                          () => { closeAvatarMenu(); toggleTheme(); },
  'on-acct-switch':                      () => onAcctSwitch(),
  'on-product-switch':                   () => onProductSwitch(),
  'onb-copy-post':                       () => onbCopyPost(),
  'onb-extract':                         () => onbExtract(),
  'onb-finish':                          () => onbFinish(),
  'onb-no-site':                         () => onbNoSite(),
  'onb-pick-network':                    (el) => onbPickNetwork(el.dataset.arg),
  'onb-pick-type':                       (el) => onbPickType(el.dataset.arg),
  'onb-save-brand':                      () => onbSaveBrand(),
  'onb-skip-network':                    () => onbSkipNetwork(),
  'open-brand-voice':                    () => openBrandVoice(),
  'open-delete-account':                 () => openDeleteAccount(),
  'open-library-picker':                 (el) => openLibraryPicker(el.dataset.arg),
  'open-results':                        (el) => openResults(el.dataset.arg),
  'open-settings':                       (el) => openSettings(el.dataset.arg),
  'publish-post':                        () => publishPost(),
  'reel-voiceover-toggle':               (el) => syncReelVisuals(el),
  'refresh-insights':                    () => refreshInsights(),
  'remove-logo':                         () => removeLogo(),
  'remove-music':                        () => removeMusic(),
  'renew-instagram-token':               () => renewInstagramToken(),
  'resend-verification':                 () => resendVerification(),
  'reset-hero':                          () => resetHero(),
  'reset-slide-style':                   () => resetSlideStyle(),
  'restore-backup':                      (el, ev) => restoreBackup(ev),
  'run-batch':                           () => runBatch(),
  'run-demo':                            () => runDemo(),
  'run-fact-check':                      () => runFactCheck(),
  'run-hero-example':                    (el) => runHeroExample(el),
  'run-hero-post':                       () => runHeroPost(),
  'save-a-i-settings':                   () => saveAISettings(),
  'save-brand':                          () => saveBrand(),
  'save-brand-rules':                    () => saveBrandRules(),
  'save-brand-voice':                    () => saveBrandVoice(),
  'save-caption':                        () => saveCaption(),
  'save-credentials':                    () => saveCredentials(),
  'save-current-preset':                 () => saveCurrentPreset(),
  'save-limits':                         () => saveLimits(),
  'save-profile':                        () => saveProfile(),
  'save-slide-style':                    () => saveSlideStyle(),
  'save-x-settings':                     () => saveXSettings(),
  'schedule-post':                       () => schedulePost(),
  'set-calendar-mode':                   (el) => setCalendarMode(el.dataset.arg),
  'set-create-mode':                     (el) => setCreateMode(el.dataset.arg),
  'set-grid-mode':                       (el) => setGridMode(el.dataset.arg),
  'set-hero-mode':                       (el) => setHeroMode(el.dataset.arg),
  'set-landing-tab':                     (el) => setLandingTab(el.dataset.arg),
  'set-network':                         (el) => setNetwork(el.dataset.arg),
  'set-section':                         (el) => setSection(el.dataset.arg),
  'set-signup-account-type':             (el) => setSignupAccountType(el.dataset.arg),
  'show-all-features':                   () => showAllFeatures(),
  'show-auth-screen':                    (el) => showAuthScreen(el.dataset.arg),
  'show-cost-popover':                   () => showCostPopover(),
  'show-failed-posts':                   () => showFailedPosts(),
  'show-forgot-screen':                  () => showForgotScreen(),
  'show-step':                           (el) => showStep(Number(el.dataset.arg)),
  'show-variations':                     (el) => showVariations(el.dataset.arg),
  'split-into-thread':                   () => splitIntoThread(),
  'submit-auth':                         () => submitAuth(),
  'submit-edit-video':                   () => submitEditVideo(),
  'submit-forgot':                       () => submitForgot(),
  'submit-publish-to-x':                 () => submitPublishToX(),
  'submit-replace-slide':                () => submitReplaceSlide(),
  'submit-reset':                        () => submitReset(),
  'submit-upload-slide':                 (el, ev) => submitUploadSlide(ev),
  'suggest-plan':                        () => suggestPlan(),
  'suggest-video-idea':                  () => suggestVideoIdea(),
  'switch-auth-tab':                     (el) => switchAuthTab(el.dataset.arg),
  'sync-publish-x-counter':              () => syncPublishXCounter(),
  'take-sources-offer':                  () => takeSourcesOffer(),
  'test-publish-connection':             (el) => testPublishConnection(el.dataset.arg),
  'toggle-avatar-menu':                  () => toggleAvatarMenu(),
  'toggle-edit-video-voiceover-fields':  () => toggleEditVideoVoiceoverFields(),
  'toggle-fact-source':                  () => toggleFactSource(),
  'undo-edit':                           () => undoEdit(),
  'unschedule-post':                     () => unschedulePost(),
  'update-configure-summary':            () => updateConfigureSummary(),
  'update-video-cost-estimate':          () => updateVideoCostEstimate(),
  'upload-brand-logo':                   () => uploadBrandLogo(),
  'upload-logo':                         (el, ev) => uploadLogo(ev),
  'upload-music':                        (el, ev) => uploadMusic(ev),
});

document.addEventListener('click', ev => {
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const run = ACTIONS[el.dataset.action];
  if (run) run(el, ev);
});

// `change` and `input` resolve into the SAME registry through their own
// attributes. Both bubble, so one listener each is enough; `focus`/`blur` would
// not, and the file already answers that with focusin (see the modal focus
// trap) if either ever appears.
document.addEventListener('change', ev => {
  const el = ev.target.closest('[data-change]');
  if (!el) return;
  const run = ACTIONS[el.dataset.change];
  if (run) run(el, ev);
});

document.addEventListener('input', ev => {
  const el = ev.target.closest('[data-input]');
  if (!el) return;
  const run = ACTIONS[el.dataset.input];
  if (run) run(el, ev);
});

// Keeping focus where it was. A picker button that steals focus on mousedown
// makes the textarea it inserts into forget the caret, so the symbol lands at
// position zero — or nowhere.
// Enter in the landing's topic field runs it. A direct listener rather than a
// registry entry: keydown carries a condition, not an action, and there is
// exactly one of them — the same shape as the #auth-password listener below.
document.addEventListener('DOMContentLoaded', () => {
  const hero = document.getElementById('hero-input');
  if (hero) hero.addEventListener('keydown', e => {
    if (e.key === 'Enter') runHeroPost();
  });
  syncReelVisuals();     // the initial state, stated rather than implied
});

document.addEventListener('mousedown', ev => {
  if (ev.target.closest('[data-keep-focus]')) ev.preventDefault();
});

// ===== HTML ESCAPING =====
// Any server-provided string interpolated into innerHTML must go through esc();
// any href/src built from that data through safeUrl(). Without this, text like
// an <img> carrying an onerror attribute becomes stored XSS, and with the API
// token in localStorage that means token theft. (Written without the literal
// attribute so the inline-handler counter in tests/test_inline_handlers.py
// counts markup rather than prose.)
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function safeUrl(u) {
  u = String(u ?? '');
  return /^https?:\/\//i.test(u) ? u : '#';   // block javascript:, data:, etc.
}

// ===== STEP LOGIC =====
const STEP_LABELS = ['Topic', 'Settings', 'Generating', 'Preview'];

function renderSteps() {
  const el = document.getElementById('steps');
  el.innerHTML = '';
  STEP_LABELS.forEach((label, i) => {
    const n = i + 1;
    const active = n === S.step;
    const done = n < S.step;
    const div = document.createElement('div');
    div.className = 'flex items-center gap-1';
    div.innerHTML = `
      <div class="step-dot w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold border-2
        ${active ? 'bg-purple-600 border-purple-400 text-white' : done ? 'bg-green-700 border-green-500 text-white' : 'bg-gray-800 border-gray-600 text-gray-400'}">
        ${done ? '✓' : n}
      </div>
      <span class="text-xs hidden sm:inline ${active ? 'text-purple-300' : 'text-gray-500'}">${label}</span>
    `;
    el.appendChild(div);
    if (i < STEP_LABELS.length - 1) {
      const sep = document.createElement('div');
      sep.className = 'w-6 h-0.5 ' + (done ? 'bg-green-700' : 'bg-gray-700');
      el.appendChild(sep);
    }
  });
}

function showStep(n) {
  document.querySelectorAll('.step-section').forEach(s => s.classList.add('hidden'));
  const el = document.getElementById(`step-${n}`);
  if (el) { el.classList.remove('hidden'); el.scrollIntoView({behavior:'smooth', block:'start'}); }
  S.step = n;
  renderSteps();
}

function goStep(n) {
  if (n === 2) {
    const topic = document.getElementById('topic').value.trim();
    if (topic.length < 3) { toast('⚠️ Please enter a topic (at least 3 characters).', 'warn'); return; }
  }
  showStep(n);
}

// Header logo → back to the Create section (works from Account/keys/Calendar/etc.).
function goHome() {
  S.createMode = defaultCreateMode();
  setSection('create');
  showStep(1);
}

// ===== TOGGLE BUTTON GROUPS =====
function initToggleGroup(selector, stateKey, defaultVal, onChange) {
  document.querySelectorAll(selector).forEach(btn => {
    const val = btn.dataset.val;
    btn.className += ' px-3 py-2 rounded-xl text-sm font-medium border-2 transition ';
    if (val === defaultVal) btn.classList.add('border-purple-500','bg-purple-900','text-white');
    else btn.classList.add('border-gray-700','bg-gray-800','text-gray-300');

    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      document.querySelectorAll(selector).forEach(b => {
        b.className = b.className.replace(/border-purple-500|bg-purple-900|text-white|border-gray-700|bg-gray-800|text-gray-300/g,'');
        b.classList.add('border-gray-700','bg-gray-800','text-gray-300');
      });
      btn.className = btn.className.replace(/border-gray-700|bg-gray-800|text-gray-300/g,'');
      btn.classList.add('border-purple-500','bg-purple-900','text-white');
      S[stateKey] = val;
      if (onChange) onChange(val);
    });
  });
  S[stateKey] = defaultVal;
  if (onChange) onChange(defaultVal);
}

// ===== OWN PHOTOS =====

/** How many images a post of the current format needs — mirrors _num_slides. */
function slidesNeeded() {
  if (S.platform === 'x') return 1;         // X posts carry a single image
  return { carousel_3: 3, carousel_5: 5, carousel_10: 10 }[S.format] || 1;
}

/** Single source of truth for which image controls show, given the source
 *  (text-only hides all image controls) and the template style (plain photo hides
 *  the branding-only controls). The standalone "Apply branding" checkbox stays
 *  hidden — it's driven by the template-style choice. */
function updateComposerControls() {
  const textOnly = S.source === 'text_only';
  const plain = !!S.plainPhoto;
  const set = (id, hidden) => { const e = document.getElementById(id); if (e) e.classList.toggle('hidden', hidden); };
  set('template-group', textOnly);
  set('niche-group', textOnly || plain);
  set('show-logo-row', textOnly || plain);
  set('apply-brand-row', true);                       // always: derived from template style
  set('own-photos', textOnly || S.source !== 'upload');
}

function onSourceChange(source) {
  updateComposerControls();
  renderOwnPhotos();
}

/** Template style also decides branding: "Plain photo" = raw image, no card/overlay. */
function onTemplateChange(val) {
  S.plainPhoto = val === 'plain';
  const applyBrand = document.getElementById('apply-brand');
  if (applyBrand) applyBrand.checked = !S.plainPhoto;
  // 'plain' is not a server TemplateStyle enum; keep a valid value (ignored when
  // apply_branding is false) so the request validates.
  S.templateStyle = S.plainPhoto ? 'branded_card' : val;
  updateComposerControls();
}

function addOwnPhotos(ev) {
  const picked = Array.from(ev.target.files || []);
  const room = slidesNeeded() - S.ownPhotos.length;
  if (room <= 0) { toast('That is already enough photos for this format', 'warn'); }
  else S.ownPhotos.push(...picked.slice(0, room));
  ev.target.value = '';                     // so picking the same file again re-fires
  renderOwnPhotos();
}

function removeOwnPhoto(index) {
  S.ownPhotos.splice(index, 1);
  renderOwnPhotos();
}

function renderOwnPhotos() {
  const grid = document.getElementById('own-photos-grid');
  const count = document.getElementById('own-photos-count');
  if (!grid || !count) return;
  const need = slidesNeeded();

  // Thumbnails come straight off the local File — no round-trip, and nothing is
  // uploaded until the user actually hits Generate.
  grid.innerHTML = '';
  S.ownPhotos.forEach((file, i) => {
    const cell = document.createElement('div');
    cell.className = 'relative';
    // Drag to reorder: the array order is the slide order (see stageOwnPhotos).
    cell.draggable = true;
    cell.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', String(i));
      cell.classList.add('opacity-40');
    });
    cell.addEventListener('dragend', () => cell.classList.remove('opacity-40'));
    cell.addEventListener('dragover', e => { e.preventDefault(); cell.classList.add('ring-2', 'ring-purple-400'); });
    cell.addEventListener('dragleave', () => cell.classList.remove('ring-2', 'ring-purple-400'));
    cell.addEventListener('drop', e => {
      e.preventDefault();
      const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
      if (Number.isNaN(from) || from === i) return;
      const [moved] = S.ownPhotos.splice(from, 1);
      S.ownPhotos.splice(i, 0, moved);
      renderOwnPhotos();            // renumbers the badges to the new order
    });
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);
    img.className = 'w-16 h-20 object-cover rounded-lg border border-gray-700 cursor-move';
    img.alt = `Photo ${i + 1}`;
    cell.appendChild(img);
    const badge = document.createElement('span');
    badge.className = 'absolute top-1 left-1 bg-black/70 text-[10px] px-1.5 rounded-full';
    badge.textContent = i + 1;
    cell.appendChild(badge);
    const del = document.createElement('button');
    del.className = 'absolute -top-1 -right-1 bg-gray-800 border border-gray-600 rounded-full w-5 h-5 text-xs leading-none';
    del.textContent = '×';
    del.title = 'Remove';
    del.setAttribute('aria-label', `Remove photo ${i + 1}`);
    del.onclick = () => removeOwnPhoto(i);
    cell.appendChild(del);
    grid.appendChild(cell);
  });

  const have = S.ownPhotos.length;
  count.textContent = `${have} of ${need} photo${need > 1 ? 's' : ''}`;
  count.className = 'text-xs ' + (have >= need ? 'text-green-400' : 'text-yellow-300');

  const btn = document.getElementById('generate-btn');
  if (btn) {
    const short = S.source === 'upload' && have < need;
    btn.disabled = short;
    btn.classList.toggle('opacity-50', short);
    btn.classList.toggle('cursor-not-allowed', short);
    btn.title = short ? `Add ${need - have} more photo(s) first` : '';
  }
}

/** Park the chosen files server-side and return their ids, in order. */
async function stageOwnPhotos() {
  const form = new FormData();
  S.ownPhotos.forEach(f => form.append('files', f));
  const res = await apiFetch(`${API}/api/posts/uploads`, { method: 'POST', body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || 'Upload failed');
  }
  return (await res.json()).map(u => u.id);
}

// ===== GENERATE =====
/** One line per stage the server reports.
 *
 *  The newest line spins and the ones above it are ticked, so the list reads as
 *  "this is where we are" rather than "here is a log". `step`/`total` are
 *  optional: an older server (or the business draft stream) sends neither, and
 *  the header simply stays empty rather than the whole thing breaking. */
function addProgress(msg, step, total) {
  const list = document.getElementById('progress-list');
  // Whatever was spinning is now finished — the next message is proof of it.
  list.querySelectorAll('[data-spin]').forEach(el => {
    el.removeAttribute('data-spin');
    el.innerHTML = '<span class="text-green-400">✓</span>';
  });
  const li = document.createElement('li');
  li.className = 'flex items-center gap-2';
  li.innerHTML = `<span data-spin class="spinner inline-block w-3 h-3 border-2 border-gray-600 border-t-purple-400 rounded-full"></span> ${esc(msg)}`;
  list.appendChild(li);
  document.getElementById('loading-msg').textContent =
    (step && total) ? `Step ${step} of ${total} — ${msg}` : msg;
}

async function generatePost() {
  const topic = document.getElementById('topic').value.trim();
  if (!topic) { goStep(1); return; }
  if (S.generating) return;          // ignore a second click while one is in flight
  // Claimed before the first await, not after it. guardGenerateKeys() fetches the
  // AI settings, and two clicks inside that round-trip both found the flag still
  // false — two generations, two provider bills, and whichever reply came back
  // slower overwriting the other one's preview.
  S.generating = true;
  try {
    if (!await guardGenerateKeys()) return;   // cloud: needs an OpenRouter key first
    if (S.source === 'upload' && S.ownPhotos.length < slidesNeeded()) {
      toast(`Add ${slidesNeeded() - S.ownPhotos.length} more photo(s) first`, 'warn');
      return;
    }

    showStep(3);
    document.getElementById('loading-spinner').classList.remove('hidden');
    document.getElementById('loading-msg').textContent = 'Starting…';
    document.getElementById('progress-list').innerHTML = '';
    document.getElementById('gen-error').classList.add('hidden');

    const body = {
      topic,
      format: S.format,
      tone: document.getElementById('tone').value,
      niche: document.getElementById('niche').value || null,
      target_audience: document.getElementById('audience').value || null,
      additional_instructions: document.getElementById('instructions').value || null,
      apply_branding: document.getElementById('apply-brand').checked,
      // text_only carries no image; send a valid ImageSource enum (ignored server-side).
      text_only: S.source === 'text_only',
      default_image_source: S.source === 'text_only' ? 'stock' : S.source,
      platform: S.platform,
      length_tier: document.getElementById('length-tier').value,
      template_style: S.templateStyle,
      niche_box_color: S.nicheBoxColor,
      show_logo: document.getElementById('show-logo').checked,
    };
    if (S.source === 'upload') {
      // Files can't ride the SSE JSON body, so they are parked first and the
      // generation refers to them by id — in the order shown in step 2.
      try {
        document.getElementById('loading-msg').textContent = 'Uploading your photos…';
        body.upload_ids = await stageOwnPhotos();
      } catch (e) {
        showStep(2);
        toast('❌ ' + e.message, 'error');
        return;
      }
    }
    if (S.platform === 'x') {
      body.x_mode = S.xMode || 'short';
      body.x_style = S.xStyle || 'standard';
      if (body.x_mode === 'thread') {
        // Clamp here as well as server-side: a browser that ignores min/max on a
        // number input would otherwise turn a typo into a 422 after the user has
        // already waited through the generation spinner.
        const lo = Math.min(15, Math.max(2, parseInt(document.getElementById('thread-min').value, 10) || 3));
        const hi = Math.min(15, Math.max(lo, parseInt(document.getElementById('thread-max').value, 10) || 7));
        body.thread_min = lo;
        body.thread_max = hi;
      }
    }

    try {
      const post = await streamGenerate(body, addProgress);
      // The photos belong to this post now; the next one starts empty rather than
      // silently reusing them.
      S.ownPhotos = [];
      renderOwnPhotos();
      bindPost(post);
      showStep(4);
    } catch(e) {
      document.getElementById('loading-spinner').classList.add('hidden');
      const errEl = document.getElementById('gen-error');
      document.getElementById('gen-error-msg').textContent = '❌ ' + e.message;
      errEl.classList.remove('hidden');
    }
  } finally {
    S.generating = false;
  }
}

// One SSE implementation for both the composer and the batch loop. Resolves with
// the completed post, or throws on error / unexpected stream end.
async function streamGenerate(body, onProgress) {
  const res = await apiFetch(`${API}/api/posts/generate`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Server error ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '', post = null, sawTerminal = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      let ev;
      try { ev = JSON.parse(line.slice(6)); }
      catch { continue; }            // skip a malformed frame, don't kill the stream
      if (ev.type === 'progress' && onProgress) onProgress(ev.message, ev.step, ev.total);
      if (ev.type === 'complete') { sawTerminal = true; post = ev.post; }
      if (ev.type === 'error') { throw new Error(ev.message); }
    }
  }
  if (!sawTerminal) throw new Error('Generation stream ended unexpectedly. Please try again.');
  return post;
}

// ===== PREVIEW =====
/** The network the post on screen targets.
 *
 *  The post's own field wins. The composer's choice (S.platform) is only the
 *  fallback, for the moment before a post exists — while someone is still
 *  filling in step 1 there is nothing else to go on.
 *
 *  This used to read the active network tab, which is the same thing only for
 *  as long as you never leave the composer. Open an X post from the calendar
 *  with the tab on Instagram and every piece of chrome lied about it: the
 *  caption label, the character counter, and the Reel button — which then
 *  posted to the Instagram-only endpoint. */
function postPlatform() {
  return (S.post && S.post.platform) || S.platform || 'instagram';
}

/** Whether a given post targets Instagram — for gating Instagram-only preview
 *  surfaces (insights, reels, SEO keywords). */
function postIsInstagram(p) {
  return (((p && p.platform) || postPlatform()) === 'instagram');
}

//: The networks a result tab may exist for. Mirrors the server's
//: ADAPTABLE_PLATFORMS: LinkedIn generates but cannot be published, so a tab
//: for it would offer to spend money on a post that could never go out.
const RESULT_TABS = [
  { platform: 'instagram', label: '\ud83d\udcf8 Instagram' },
  { platform: 'x',         label: '\ud835\udd4f X' },
];

/** Draw the tab bar for the bound post's idea.
 *
 *  The list comes from `post.variants`, which ONLY the three endpoints that
 *  bind a post fill in — GET /{id}, the generate stream and /adapt. Everything
 *  else returns it empty because filling it costs a query nothing reads. So an
 *  empty list means "not asked", never "no siblings": the bar is keyed on
 *  variant_group_id and a response that does not change the group leaves it
 *  alone. Reading `variants.length` instead would collapse the whole bar every
 *  time somebody saved a caption. */
function renderResultTabs() {
  const box = document.getElementById('result-tabs');
  if (!box) return;
  const post = S.post;
  if (!post) { box.innerHTML = ''; return; }
  const group = post.variant_group_id || post.id;
  if (post.variants.length) {
    S.resultGroup = group;
    S.resultVariants = post.variants;
  } else if (S.resultGroup !== group) {
    // A different idea, and nobody told us its siblings: show the one tab we
    // can prove exists rather than the last idea's bar.
    S.resultGroup = group;
    S.resultVariants = [{ id: post.id, platform: post.platform, status: post.status }];
  }
  // Same idea, empty list -> keep what we have. That branch is the whole point:
  // most endpoints return `variants: []` because filling it costs a query
  // nothing reads, and reading its length as truth would collapse the bar every
  // time somebody saved a caption.
  const byNet = {};
  (S.resultVariants || []).forEach(v => { byNet[v.platform] = v; });
  const dot = { published: '\ud83d\udfe2', scheduled: '\ud83d\udfe3', failed: '\ud83d\udd34' };
  const here = postPlatform();
  box.innerHTML = RESULT_TABS.map(t => {
    const v = byNet[t.platform];
    const busy = S.adapting === t.platform;
    const cls = 'tab-btn px-3 py-1.5 rounded-lg text-sm whitespace-nowrap'
      + (v && t.platform === here ? ' tab-active' : '');
    const tail = busy ? ' \u2026' : (v ? (dot[v.status] ? ' ' + dot[v.status] : '') : ' \uff0b Adapt');
    return `<button data-result-tab="${t.platform}" ${busy ? 'disabled' : ''} `
      + `data-action="result-tab" data-arg="${t.platform}" class="${cls}">${t.label}${tail}</button>`;
  }).join('');
}

/** Show this idea written for another network.
 *
 *  Deliberately NOT setNetwork(): that one belongs to the composer — it rewrites
 *  S.platform, coerces the format and image source, edits the plan-a-week
 *  options and re-enters setSection. Using it here would change what the NEXT
 *  generation targets and re-render the section under the editor. The two share
 *  glyphs and nothing else. */
async function setResultTab(platform) {
  if (S.adapting) return;                       // one adaptation at a time
  if (platform === postPlatform()) return;

  // The editor autosaves to localStorage under the post's own key, so text
  // typed and then left behind by a switch would sit somewhere the user never
  // goes back to. If it cannot be saved, stay put: losing words silently is
  // worse than not switching.
  if (resultEditsPending() && !(await saveCaption())) return;

  const known = (S.resultVariants || []).find(v => v.platform === platform);
  try {
    if (known) {
      const res = await apiFetch(`${API}/api/posts/${encodeURIComponent(known.id)}`);
      if (!res.ok) throw new Error('Could not open that version.');
      bindPost(await res.json());
      return;
    }
    // Nothing there yet: this is the press that spends.
    S.adapting = platform;
    renderResultTabs();
    const res = await apiFetch(
      `${API}/api/posts/${encodeURIComponent(S.postId)}/adapt/${platform}`,
      { method: 'POST' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Could not adapt this post.');
    S.adapting = null;
    bindPost(body);
  } catch (e) {
    toast('\u274c ' + e.message, 'error');
  } finally {
    S.adapting = null;
    renderResultTabs();
  }
}

/** Is there text on screen the server has not been told about? */
function resultEditsPending() {
  const post = S.post;
  if (!post) return false;
  const thread = S.threadParts || [];
  const caption = thread.length
    ? thread.join('\n\n')
    : (document.getElementById('caption-edit') || {}).value;
  if (caption !== undefined && caption !== (post.caption || '')) return true;
  const same = (a, b) => JSON.stringify(a || []) === JSON.stringify(b || []);
  return !same(S.hashtags, post.hashtags) || !same(S.seoKeywords, post.seo_keywords);
}

/** Put a post in the editor, and let go of the last one.
 *
 *  Three pointers say which post is on screen — S.postId (read by roughly
 *  fourteen step-4 actions), S.post (the editor chrome's platform) and
 *  S.currentPost (schedule + fact check). They were assigned in two different
 *  functions, so nothing made them agree; that survived only because there was
 *  one way in. Phase 4's network tabs swap the bound post without leaving the
 *  screen, and then a stale pointer publishes the wrong network's post.
 *
 *  It also clears what renderPreview does not. S.slideOriginals is keyed by
 *  slide number, so a three-slide post followed by a one-slide post left
 *  entries 2 and 3 behind for the Reset button to read; S.editingSlide aimed
 *  the replace/upload calls at a slide the new post may not even have. */
function bindPost(post) {
  S.postId = post.id;
  S.post = post;
  S.currentPost = post;
  S.slideOriginals = {};
  if (S.editingSlide !== null) closeEditSlide();
  S.threadParts = post.thread_parts ? [...post.thread_parts] : [];
  const variations = document.getElementById('variations-modal');
  if (variations && !variations.classList.contains('hidden')) closeVariations();
  renderPreview(post);
  renderResultTabs();
}

function renderPreview(post) {
  // Keep the post itself: the editor chrome below needs its platform, and this
  // used to be the point where that information was thrown away.
  S.post = post;
  // Reset undo stack for this post (load any persisted one)
  try { S.undoStack = JSON.parse(localStorage.getItem('undo_' + post.id) || '[]'); } catch(e){ S.undoStack = []; }
  const undoBtn = document.getElementById('undo-btn');
  if (undoBtn) undoBtn.classList.toggle('hidden', !S.undoStack.length);

  // Slides
  const container = document.getElementById('slides-container');
  container.innerHTML = '';
  post.slides.forEach(slide => {
    const portrait = (slide.height || 1080) > (slide.width || 1080);
    const imgCls = portrait ? 'slide-img w-56 h-72 object-contain bg-black' : 'slide-img w-64 h-64 object-cover';
    const wrapper = document.createElement('div');
    wrapper.className = 'flex-shrink-0 snap-start space-y-2';
    const isFirst = slide.slide_number === 1;
    const overlayVal = (slide.overlay_text || '').replace(/"/g, '&quot;');
    const nicheVal = (slide.niche_text || '').replace(/"/g, '&quot;');
    const editable = slide.has_raw_image;     // only enabled if raw is stored
    wrapper.innerHTML = `
      <div class="relative group">
        <img data-slide-num="${slide.slide_number}" src="${API}${slide.image_url}" alt="Slide ${slide.slide_number}"
          class="${imgCls} rounded-xl border border-gray-700 shadow-lg" />
        <span class="absolute top-2 left-2 bg-black/60 text-xs text-white px-2 py-0.5 rounded-full">${slide.slide_number}/${post.slides.length}</span>
        <span class="absolute bottom-2 left-2 bg-black/60 text-xs text-gray-300 px-2 py-0.5 rounded-full">${slide.image_source}</span>
        <button data-act="replace-image"
          class="absolute top-2 right-2 bg-black/70 hover:bg-purple-700 transition text-white text-xs rounded-full px-2 py-1"
          title="Replace this image">
          Replace
        </button>
      </div>
      <div class="bg-gray-800 border border-gray-700 rounded-lg p-2 space-y-1 w-56 sm:w-64" data-slide-edit="${slide.slide_number}">
        ${isFirst ? `
          <label class="block text-[10px] text-gray-500 uppercase tracking-wider">Niche</label>
          <input type="text" data-slide-niche="${slide.slide_number}" value="${nicheVal}"
            class="overlay-input w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:ring-1 focus:ring-purple-500"
            placeholder="Your niche" ${editable ? '' : 'disabled'} />
        ` : ''}
        <label class="block text-[10px] text-gray-500 uppercase tracking-wider">Overlay</label>
        <textarea data-slide-overlay="${slide.slide_number}" rows="2"
          class="overlay-input w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:ring-1 focus:ring-purple-500 resize-none"
          placeholder="Short sentence." ${editable ? '' : 'disabled'}>${overlayVal}</textarea>
        <div class="flex gap-1 pt-1">
          <button data-action="apply-overlay" data-arg="${slide.slide_number}" ${editable ? '' : 'disabled'}
            class="flex-1 bg-purple-600 hover:bg-purple-500 disabled:bg-gray-700 disabled:cursor-not-allowed transition rounded px-2 py-1 text-[11px] font-semibold">
            ✓ Apply
          </button>
          <button data-action="reset-overlay" data-arg="${slide.slide_number}" ${editable ? '' : 'disabled'}
            title="Restore to LLM-generated text"
            class="bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed transition rounded px-2 py-1 text-[11px]">
            Reset
          </button>
        </div>
        ${editable ? '' : '<p class="text-[10px] text-gray-600">Replace this slide first to enable in-place editing.</p>'}
      </div>
    `;
    // Closure, not an attribute. This used to serialise a JSON string into an
    // inline click attribute and hand-escape the quotes; the slide object is
    // right here, so the escaping problem simply stops existing.
    wrapper.querySelector('[data-act="replace-image"]').onclick = () =>
      openEditSlide(slide.slide_number, slide.search_query || '', slide.image_source);
    // Cache the original LLM text so Reset works without an extra request.
    S.slideOriginals[slide.slide_number] = {
      overlay: slide.original_overlay_text || slide.overlay_text || '',
      niche:   slide.original_niche_text   || slide.niche_text   || '',
    };
    container.appendChild(wrapper);
  });

  // References panel (image attribution + text sources)
  renderReferences(post);
  renderClaims(post);

  // Caption (or, for a thread, one card per tweet)
  document.getElementById('caption-edit').value = post.caption;
  renderThread(post.thread_parts || []);

  // Hashtags
  S.hashtags = [...post.hashtags];
  renderHashtags();

  // SEO keywords
  S.seoKeywords = [...(post.seo_keywords || [])];
  renderSeoKeywords();

  // SEO keywords are an Instagram-search concept — no meaning on X.
  const seoGroup = document.getElementById('seo-group');
  if (seoGroup) seoGroup.classList.toggle('hidden', !postIsInstagram(post));

  // The preview follows the post, not the session that rendered it. It used to
  // be hidden unconditionally here, so a reload (or just opening another post
  // and coming back) lost the video and the publish button with it, and the
  // only way to get them back was to render the whole thing again.
  const reelPrev = document.getElementById('reel-preview');
  if (reelPrev) {
    const vid = post.video_url ? API + post.video_url : null;
    if (vid) {
      document.getElementById('reel-video').src = vid;
      document.getElementById('reel-download').href = vid;
    } else {
      // Clearing src matters: <video> keeps the previous post's frame otherwise,
      // and the next post to have no video would show the last one's.
      document.getElementById('reel-video').removeAttribute('src');
    }
    reelPrev.classList.toggle('hidden', !vid);
  }
  // Both networks take a video built from the post's slides — Instagram through
  // publish-reel, X through publish-video — so the platform is not what decides
  // this. What decides it is whether there is anything to show: slides to render
  // from, or a video already rendered. (The second half matters because
  // reel/from-library attaches a video without caring about slides, so a
  // slides-only test would hide a post's own video from it.) Hiding on X was
  // what made publishReelOrToX's whole X branch unreachable from the composer:
  // the button that dispatches to it lives inside this card.
  const reelCard = document.getElementById('reel-card');
  if (reelCard) reelCard.classList.toggle('hidden', !post.slides.length && !post.video_url);

  // Emoji picker (lazily built once)
  buildEmojiPicker();

  // Schedule + performance state
  S.currentPost = post;
  applyScheduleState(post);
  const perf = document.getElementById('performance-panel');
  // In-app insights exist for Instagram only (X has no metrics endpoint yet).
  if ((post.instagram_media_id || post.status === 'published') && postIsInstagram(post)) {
    perf.classList.remove('hidden');
    document.getElementById('insights-numbers').innerHTML =
      '<div class="col-span-full text-gray-500">Click Refresh to load metrics.</div>';
  } else {
    perf.classList.add('hidden');
  }

  // Offer to restore an unsaved draft, if any
  maybeOfferDraft(post);
}

// ===== EDIT SLIDE (replace / upload) =====
function openEditSlide(slideNum, originalQuery, currentSource) {
  S.editingSlide = slideNum;
  document.getElementById('edit-slide-label').textContent =
    `Slide ${slideNum}. Currently from: ${currentSource}.`;
  document.getElementById('edit-search-query').value = '';
  document.getElementById('edit-search-query').placeholder = originalQuery
    ? `Original: ${originalQuery}` : 'e.g. product photo, city skyline';
  document.getElementById('edit-gen-prompt').value = '';
  setEditSource(currentSource === 'ai_gen' ? 'ai_gen' : 'stock');
  document.getElementById('edit-slide-modal').classList.remove('hidden');
  onModalOpen('edit-slide-modal');
}

function closeEditSlide() {
  document.getElementById('edit-slide-modal').classList.add('hidden');
  S.editingSlide = null;
  onModalClose();
}

function setEditSource(src) {
  S.editSource = src;
  document.querySelectorAll('.edit-src-btn').forEach(b => {
    const active = b.dataset.editSrc === src;
    b.classList.toggle('border-purple-500', active);
    b.classList.toggle('bg-purple-900', active);
    b.classList.toggle('text-white', active);
    b.classList.toggle('border-gray-700', !active);
    b.classList.toggle('bg-gray-800', !active);
    b.classList.toggle('text-gray-300', !active);
  });
  document.getElementById('edit-stock-fields').classList.toggle('hidden', src !== 'stock');
  document.getElementById('edit-ai-fields').classList.toggle('hidden', src !== 'ai_gen');
}

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-edit-src]');
  if (btn) setEditSource(btn.dataset.editSrc);
});

async function submitReplaceSlide() {
  if (!S.postId || !S.editingSlide) return;
  const btn = document.getElementById('edit-replace-btn');
  btn.disabled = true; btn.textContent = '⏳ Replacing…';
  try {
    const body = { image_source: S.editSource || 'stock' };
    const q = document.getElementById('edit-search-query').value.trim();
    if (q) body.search_query = q;
    if (S.editSource === 'ai_gen') {
      body.gen_prompt = document.getElementById('edit-gen-prompt').value.trim() || null;
    }
    const res = await apiFetch(`${API}/api/posts/${S.postId}/slides/${S.editingSlide}/regenerate`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Replace failed');
    }
    const updated = await res.json();
    refreshSlideImage(updated);
    closeEditSlide();
    toast('✅ Slide replaced', 'success');
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Replace';
  }
}

async function submitUploadSlide(ev) {
  if (!S.postId || !S.editingSlide) return;
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  try {
    const form = new FormData();
    form.append('file', file);
    const res = await apiFetch(`${API}/api/posts/${S.postId}/slides/${S.editingSlide}/upload`, {
      method: 'POST',
      body: form,        // browser sets multipart Content-Type with boundary
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Upload failed');
    }
    const updated = await res.json();
    refreshSlideImage(updated);
    closeEditSlide();
    toast('✅ Custom image uploaded', 'success');
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  } finally {
    ev.target.value = '';
  }
}

function refreshSlideImage(updatedSlide) {
  const img = document.querySelector(`#slides-container img[data-slide-num="${updatedSlide.slide_number}"]`);
  if (img) img.src = API + updatedSlide.image_url;
}

// ===== EDITABLE OVERLAY TEXT =====
async function applyOverlay(slideNum) {
  if (!S.postId) return;
  const overlayEl = document.querySelector(`textarea[data-slide-overlay="${slideNum}"]`);
  const nicheEl   = document.querySelector(`input[data-slide-niche="${slideNum}"]`);
  const body = { overlay_text: overlayEl ? overlayEl.value : null };
  if (nicheEl) body.niche_text = nicheEl.value;
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/slides/${slideNum}/overlay`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Apply failed');
    }
    const updated = await res.json();
    refreshSlideImage(updated);
    toast('✅ Overlay updated', 'success');
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  }
}

function resetOverlay(slideNum) {
  const orig = S.slideOriginals[slideNum];
  if (!orig) return;
  const overlayEl = document.querySelector(`textarea[data-slide-overlay="${slideNum}"]`);
  const nicheEl   = document.querySelector(`input[data-slide-niche="${slideNum}"]`);
  if (overlayEl) overlayEl.value = orig.overlay;
  if (nicheEl)   nicheEl.value   = orig.niche;
  applyOverlay(slideNum);
}

// ===== EMOJI PICKER =====
const EMOJI_POOL = {
  'Smileys':     ['😀','😃','😄','😁','😊','🙂','😉','😍','🤩','😎','🤗','😇'],
  'Gestures':    ['👍','👏','🙌','🙏','👌','🤝','💪','✌️','🤞','👋','🫶','🤟'],
  'Symbols':     ['✨','🔥','💯','⭐','❤️','✅','⚡','💡','🎯','📌','❗','➡️'],
  'Objects':     ['📈','📊','📝','📅','⏰','📷','🎧','💻','📱','🛒','🎁','🔑'],
  'Nature':      ['🌱','🌸','🌿','🌊','☀️','🌙','🌟','🍃','🌈','🔆','🌍','🌷'],
  'Celebration': ['🎉','🎊','🥳','🏆','🥇','🎈','💫','✨','🎆','👑','🍾','🎇'],
};

let _lastFocusedField = null;
document.addEventListener('focusin', e => {
  if (e.target.matches('textarea, input[type=text]')) {
    _lastFocusedField = e.target;
  }
});

function insertEmoji(emoji) {
  const el = _lastFocusedField;
  if (!el) { toast('Click a text field first', 'warn'); return; }
  const start = el.selectionStart ?? el.value.length;
  const end   = el.selectionEnd   ?? start;
  if (typeof el.setRangeText === 'function') {
    el.setRangeText(emoji, start, end, 'end');
  } else {
    el.value = el.value.slice(0, start) + emoji + el.value.slice(end);
  }
  el.focus();
  el.dispatchEvent(new Event('input', {bubbles:true}));
}

function buildEmojiPicker() {
  const host = document.getElementById('emoji-picker-host');
  if (!host || host.dataset.built === '1') return;
  host.dataset.built = '1';
  let html = '<details class="bg-gray-800 border border-gray-700 rounded-xl">';
  html += '<summary class="cursor-pointer text-xs text-gray-400 px-3 py-2">😀 Emoji picker (click a field, then a symbol)</summary>';
  html += '<div class="p-2 space-y-1">';
  for (const [cat, list] of Object.entries(EMOJI_POOL)) {
    html += `<div class="flex flex-wrap gap-1 items-center"><span class="text-[10px] text-gray-500 w-20">${cat}</span>`;
    for (const e of list) {
      html += `<button type="button" data-keep-focus data-action="insert-emoji" data-arg="${e}"
        class="text-base hover:bg-gray-700 rounded px-1.5 py-0.5 transition" title="${e}">${e}</button>`;
    }
    html += '</div>';
  }
  html += '</div></details>';
  host.innerHTML = html;
}

function renderClaims(post) {
  const panel = document.getElementById('claims-panel');
  const list = document.getElementById('claims-list');
  const claims = (post && post.claims) || [];
  list.innerHTML = '';
  claims.forEach(c => {
    const li = document.createElement('li');
    li.className = 'text-yellow-100/90';
    li.innerHTML = `<span class="text-yellow-200">“${esc(c.text)}”</span>` +
      `<span class="text-yellow-200/50"> — ${esc(c.reason)}</span>`;
    list.appendChild(li);
  });
  // The panel also hosts the opt-in source check, so it stays available for a
  // saved post even when the regex found nothing — plenty of unflagged captions
  // still assert something. The wording drops the warning tone in that case.
  const canVerify = !!(post && post.id && S.user && !S.user.is_local);
  panel.classList.toggle('hidden', !claims.length && !canVerify);
  document.getElementById('claims-title').textContent =
    claims.length ? '⚠️ Check before posting' : 'Check before posting';
  document.getElementById('claims-intro').textContent = claims.length
    ? 'These lines state a number or cite research. Verify them against a source before you publish under your brand — AI can round facts in its own favour.'
    : 'Nothing here reads like a hard claim, but you can still check this post against a source.';
  const box = document.getElementById('factcheck-source');
  if (box) box.classList.add('hidden');
  renderFactCheck(post);
}

// ===== OPT-IN FACT CHECK (creators) =====

function toggleFactSource() {
  const el = document.getElementById('factcheck-source');
  el.classList.toggle('hidden');
  if (!el.classList.contains('hidden')) el.focus();
}

/** Ask the server to bind this post's claims to real source material. */
async function runFactCheck() {
  const post = S.currentPost;
  if (!post || !post.id) return;
  const btn = document.getElementById('factcheck-btn');
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Checking…';
  try {
    const res = await apiFetch(`${API}/api/posts/${encodeURIComponent(post.id)}/verify`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_text: document.getElementById('factcheck-source').value || '' }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Check failed'); }
    const updated = await res.json();
    S.currentPost = { ...post, ...updated };
    renderFactCheck(S.currentPost);
  } catch (e) {
    document.getElementById('factcheck-result').innerHTML =
      `<div class="text-red-400">❌ ${esc(e.message)}</div>`;
  } finally { btn.disabled = false; btn.textContent = prev; }
}

/** Show the verdict. "Nothing to check against" is a first-class outcome here,
 *  not an empty list — an author reading a blank panel would assume it passed. */
function renderFactCheck(post) {
  const host = document.getElementById('factcheck-result');
  if (!host) return;
  const fc = (post && post.fact_check) || {};
  const claims = (post && post.checked_claims) || [];
  if (!fc.status) { host.innerHTML = ''; return; }

  let html = '';
  if (fc.status === 'no_source') {
    html += `<div class="text-yellow-200">Nothing to check against yet — paste the source
      above, or generate with web sources on. We won't ask the AI to confirm its own numbers.</div>`;
  } else if (fc.status === 'no_key') {
    html += `<div class="text-yellow-200">Add an AI key under Connections to run the
      check — we read your source, but the check itself needs a model.</div>`;
  } else if (fc.status === 'error') {
    html += `<div class="text-red-400">The check could not finish${fc.error ? ': ' + esc(fc.error) : '.'}
      Nothing was verified.</div>`;
  } else if (!claims.length) {
    html += `<div class="text-yellow-200">Checked — the model found no factual claims to verify.</div>`;
  } else {
    claims.forEach(c => {
      const ok = c.status === 'confirmed';
      const tip = ok && c.evidence ? ` title="${esc(c.evidence)}"` : '';
      html += `<div class="${ok ? 'text-green-400' : 'text-yellow-300'}"${tip}>` +
        `${ok ? '✓' : '?'} ${esc(c.claim)}${ok ? '' : ' — not found in the source'}</div>`;
    });
  }
  const used = fc.sources_used || [];
  if (used.length) {
    html += '<div class="text-gray-400 mt-1">Read: ' + used.map(u =>
      `<span class="${u.ok ? '' : 'text-red-400'}">${esc(u.url)}${u.ok ? '' : ' (unreachable)'}</span>`
    ).join(', ') + '</div>';
  }
  if (fc.checked_at) {
    html += `<div class="text-gray-500">Checked ${new Date(fc.checked_at).toLocaleString()}</div>`;
  }
  host.innerHTML = html;
}

function renderReferences(post) {
  const panel = document.getElementById('references-panel');
  const sourcesBlock = document.getElementById('ref-sources');
  const sourcesList = document.getElementById('ref-sources-list');

  let any = false;

  // Text sources from web-grounded LLM (:online)
  const textBlock = document.getElementById('ref-text-sources');
  const textList = document.getElementById('ref-text-sources-list');
  if (post.sources && post.sources.length) {
    textList.innerHTML = '';
    post.sources.forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = s.url
        ? `<a href="${esc(safeUrl(s.url))}" target="_blank" class="text-purple-300 hover:text-purple-200 underline">${esc(s.title || s.url)}</a>`
        : `<span>${esc(s.title || '')}</span>`;
      textList.appendChild(li);
    });
    textBlock.classList.remove('hidden');
    any = true;
  } else {
    textBlock.classList.add('hidden'); textList.innerHTML = '';
  }

  // Per-slide image attribution
  const withAttrib = (post.slides || []).filter(s => s.attribution && s.attribution.author_name);
  if (withAttrib.length) {
    sourcesList.innerHTML = '';
    withAttrib.forEach(s => {
      const a = s.attribution;
      const authorHtml = a.author_profile_url
        ? `<a href="${esc(safeUrl(a.author_profile_url))}" target="_blank" class="text-purple-300 hover:text-purple-200 underline">${esc(a.author_name)}</a>`
        : `<span class="text-purple-300">${esc(a.author_name)}</span>`;
      const sourceHtml = a.source_link
        ? `<a href="${esc(safeUrl(a.source_link))}" target="_blank" class="text-gray-400 hover:text-gray-200 underline">${esc(a.source)}</a>`
        : `<span class="text-gray-400">${esc(a.source)}</span>`;
      const li = document.createElement('li');
      li.innerHTML = `<span class="text-gray-500">Slide ${s.slide_number}:</span> Photo by ${authorHtml} on ${sourceHtml}`;
      sourcesList.appendChild(li);
    });
    sourcesBlock.classList.remove('hidden');
    any = true;
  } else {
    sourcesBlock.classList.add('hidden'); sourcesList.innerHTML = '';
  }

  panel.classList.toggle('hidden', !any);
}

function renderSeoKeywords() {
  const c = document.getElementById('seo-container');
  if (!c) return;
  c.innerHTML = '';
  if (!S.seoKeywords.length) {
    c.innerHTML = '<span class="text-xs text-gray-600">No SEO keywords generated.</span>';
    return;
  }
  S.seoKeywords.forEach(kw => {
    const chip = document.createElement('span');
    chip.className = 'inline-flex items-center chip text-xs px-2 py-1 rounded-full';
    chip.textContent = kw;
    c.appendChild(chip);
  });
}

function renderHashtags() {
  const c = document.getElementById('hashtag-container');
  c.innerHTML = '';
  S.hashtags.forEach((tag, i) => {
    const chip = document.createElement('span');
    chip.className = 'inline-flex items-center gap-1 border text-xs px-2 py-1 rounded-full ' +
      'bg-purple-900 border-purple-700 text-purple-200';
    chip.innerHTML = `${tag} <button data-action="remove-hashtag" data-arg="${i}" class="hover:text-white ml-1 text-xs opacity-70">×</button>`;
    c.appendChild(chip);
  });
}

function addHashtag() {
  let val = document.getElementById('hashtag-input').value.trim();
  if (!val) return;
  if (!val.startsWith('#')) val = '#' + val;
  if (!S.hashtags.includes(val)) { S.hashtags.push(val); renderHashtags(); }
  document.getElementById('hashtag-input').value = '';
}

function removeHashtag(i) {
  S.hashtags.splice(i, 1);
  renderHashtags();
}

document.addEventListener('keydown', e => {
  if (e.target.id === 'hashtag-input' && e.key === 'Enter') addHashtag();
});

// ===== MODAL ACCESSIBILITY (focus trap + Esc-to-close + restore focus) =====
const _MODAL_CLOSERS = {
  'need-key-modal': () => closeNeedKey(),
  'delete-account-modal': () => closeDeleteAccount(),
  'variations-modal': () => closeVariations(),
  'edit-slide-modal': () => closeEditSlide(),
  'library-picker-modal': () => closeLibraryPicker(),
  'edit-video-modal': () => closeEditVideoModal(),
  'publish-x-modal': () => closePublishToXModal(),
};
let _modalReturnFocus = null;
function onModalOpen(id) {
  _modalReturnFocus = document.activeElement;
  const m = document.getElementById(id);
  const f = m.querySelector('input:not([disabled]),textarea,select,button:not([disabled]),a[href]');
  if (f) setTimeout(() => { try { f.focus(); } catch (e) {} }, 30);
}
function onModalClose() {
  if (_modalReturnFocus && _modalReturnFocus.focus) { try { _modalReturnFocus.focus(); } catch (e) {} }
  _modalReturnFocus = null;
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' && e.key !== 'Tab') return;
  const openId = Object.keys(_MODAL_CLOSERS).find(id => {
    const el = document.getElementById(id);
    return el && !el.classList.contains('hidden');
  });
  if (!openId) return;
  if (e.key === 'Escape') { _MODAL_CLOSERS[openId](); return; }   // closers call onModalClose
  const items = [...document.getElementById(openId).querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// ===== UNDO STACK (client-side, last 5 edits) =====
function snapshotEdits() {
  if (!S.postId) return;
  const snap = {
    caption: document.getElementById('caption-edit').value,
    hashtags: [...S.hashtags],
    seo: [...S.seoKeywords],
    thread: [...(S.threadParts || [])],
  };
  S.undoStack.push(snap);
  if (S.undoStack.length > 5) S.undoStack.shift();
  try { localStorage.setItem('undo_' + S.postId, JSON.stringify(S.undoStack)); }
  catch(e){ toast('⚠️ Local storage full — undo history not saved', 'warn'); }
  document.getElementById('undo-btn').classList.toggle('hidden', S.undoStack.length === 0);
}
function undoEdit() {
  if (!S.undoStack.length) return;
  const snap = S.undoStack.pop();
  document.getElementById('caption-edit').value = snap.caption;
  renderThread(snap.thread || []);
  S.hashtags = [...snap.hashtags]; renderHashtags();
  S.seoKeywords = [...snap.seo]; renderSeoKeywords();
  try { localStorage.setItem('undo_' + S.postId, JSON.stringify(S.undoStack)); } catch(e){}
  document.getElementById('undo-btn').classList.toggle('hidden', S.undoStack.length === 0);
  toast('Reverted last edit', 'info');
}

// ===== AUTOSAVE DRAFT (client-side) =====
let _draftTimer;
function scheduleDraftSave() {
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(saveDraft, 800);
}
function saveDraft() {
  if (!S.postId) return;
  try {
    localStorage.setItem('draft_' + S.postId, JSON.stringify({
      caption: document.getElementById('caption-edit').value,
      hashtags: S.hashtags, seo: S.seoKeywords,
      thread: S.threadParts || [], at: Date.now(),
    }));
  } catch(e){ toast('⚠️ Local storage full — draft not autosaved', 'warn'); }
}
function maybeOfferDraft(post) {
  let raw; try { raw = localStorage.getItem('draft_' + post.id); } catch(e){ return; }
  if (!raw) { document.getElementById('draft-banner').classList.add('hidden'); return; }
  let draft; try { draft = JSON.parse(raw); } catch(e){
    try { localStorage.removeItem('draft_' + post.id); } catch(e2){}
    return;   // corrupt draft — drop it rather than crash renderPreview
  }
  const banner = document.getElementById('draft-banner');
  // Surface the draft's age so the user can judge staleness (there's no server
  // updated_at to compare against, so we don't auto-decide).
  if (draft.at) {
    const mins = Math.round((Date.now() - draft.at) / 60000);
    const when = mins < 1 ? 'just now' : mins < 60 ? `${mins} min ago` : `${Math.round(mins/60)} h ago`;
    const lbl = banner.querySelector('#draft-age');
    if (lbl) lbl.textContent = ` (saved ${when})`;
  }
  banner.classList.remove('hidden');
  banner.querySelector('#draft-restore').onclick = () => {
    document.getElementById('caption-edit').value = draft.caption || '';
    renderThread(draft.thread || []);
    S.hashtags = [...(draft.hashtags||[])]; renderHashtags();
    S.seoKeywords = [...(draft.seo||[])]; renderSeoKeywords();
    banner.classList.add('hidden');
    toast('Draft restored', 'info');
  };
  banner.querySelector('#draft-discard').onclick = () => {
    try { localStorage.removeItem('draft_' + post.id); } catch(e){}
    banner.classList.add('hidden');
  };
}

// ===== THREAD EDITOR =====

/** One editable card per tweet, with a live character counter.
 *
 * The single caption box stays the editor for short/long posts and Instagram;
 * a thread needs per-tweet counters because the 250-char budget applies to each
 * tweet, not to the joined text.
 */
function renderThread(parts) {
  S.threadParts = [...parts];
  const wrap = document.getElementById('thread-editor');
  const capBox = document.getElementById('caption-editor');
  const isThread = S.threadParts.length > 0;
  if (wrap) wrap.classList.toggle('hidden', !isThread);
  if (capBox) capBox.classList.toggle('hidden', isThread);
  if (!isThread) { updatePostEditorChrome(); return; }

  const list = document.getElementById('thread-parts');
  list.innerHTML = '';
  S.threadParts.forEach((text, i) => {
    const card = document.createElement('div');
    card.className = 'bg-gray-800 border border-gray-700 rounded-xl p-3 space-y-1';
    card.innerHTML = `
      <div class="flex items-center justify-between text-xs">
        <span class="text-gray-500">${i + 1}/${S.threadParts.length}</span>
        <span class="tweet-count font-mono"></span>
      </div>
      <textarea rows="3" aria-label="Tweet ${i + 1}"
        class="w-full bg-transparent text-white text-sm resize-none focus:outline-none"></textarea>`;
    const ta = card.querySelector('textarea');
    const counter = card.querySelector('.tweet-count');
    ta.value = text;
    const sync = () => {
      S.threadParts[i] = ta.value;
      const n = tweetLength(ta.value);
      counter.textContent = `${n}/${X_TWEET_LIMIT}`;
      counter.className = 'tweet-count font-mono '
        + (n > X_TWEET_LIMIT ? 'text-red-400' : 'text-gray-500');
    };
    ta.addEventListener('input', () => { sync(); scheduleDraftSave(); });
    sync();
    list.appendChild(card);
  });
  document.getElementById('thread-count').textContent = `(${S.threadParts.length} tweets)`;
  updatePostEditorChrome();
}

// X posts trim to this at publish (fit_tweet, below X's 280 on purpose) — the
// single-post counter and the thread splitter both target it so preview == posted.
// Seeded from the server (XSettingsResponse.tweet_char_limit) so the two can't drift.
let X_TWEET_LIMIT = 250;

// X rewrites every link to a t.co of exactly this length, so a 120-character URL
// costs 23. Counting the raw string told users "340/250" on a post X accepts.
const X_URL_WEIGHT = 23;
function tweetLength(text) {
  return String(text || '').replace(/https?:\/\/\S+/g, '#'.repeat(X_URL_WEIGHT)).length;
}

/** Label + counter + split button reflect the network and whether it's a thread.
 *  For X the single-post box is the TWEET (not an Instagram "Caption"). */
function updatePostEditorChrome() {
  const isX = postPlatform() === 'x';
  const isThread = (S.threadParts || []).length > 0;
  const label = document.getElementById('caption-label');
  if (label) label.textContent = isX ? '🐦 Post' : 'Caption';
  const showXsingle = isX && !isThread;                 // counter/split only for a single tweet
  const count = document.getElementById('caption-count');
  const splitBtn = document.getElementById('split-thread-btn');
  if (count) count.classList.toggle('hidden', !showXsingle);
  if (splitBtn) splitBtn.classList.toggle('hidden', !showXsingle);
  updateCaptionCount();
  updateMakeReelButton();
  updateReelPublishButton();
}

// "Reel" is Instagram's word. The same render is just a video on X, so the
// button that starts it is labelled from the post's network — the card itself
// is shown on both. Skipped while a render is in flight: the label is the
// progress indicator then, and overwriting it would hide it.
function updateMakeReelButton() {
  const btn = document.getElementById('make-reel-btn');
  if (!btn || btn.disabled) return;
  btn.textContent = postPlatform() === 'x' ? 'Make video' : 'Make Reel';
}

// The Reel card's publish button is platform-aware (Instagram vs X) and, once
// a job is in flight for this post, shows its live status instead of being
// clickable again — clicking mid-upload would start a second job.
function updateReelPublishButton() {
  const btn = document.getElementById('reel-publish-btn');
  if (!btn) return;
  const isX = postPlatform() === 'x';
  const job = S.postId ? (S.publishJobs || {})[S.postId] : null;
  if (job && _PUB_JOB_ACTIVE.has(job.status)) {
    btn.disabled = true;
    const pct = job.status === 'uploading' && job.progress_pct != null ? ` ${job.progress_pct}%` : '';
    btn.textContent = (_PUB_JOB_LABELS[job.status] || 'Working…') + pct;
  } else if (job && job.status === 'published') {
    btn.disabled = false;
    btn.textContent = '✅ View on X ↗';
    btn.onclick = () => window.open(job.permalink, '_blank', 'noopener');
  } else {
    btn.disabled = false;
    btn.textContent = isX ? '𝕏 Publish video to X' : '📤 Publish Reel';
    btn.onclick = () => publishReelOrToX();
  }
}

function updateCaptionCount() {
  if (postPlatform() !== 'x') return;
  const ta = document.getElementById('caption-edit');
  const count = document.getElementById('caption-count');
  if (!ta || !count || count.classList.contains('hidden')) return;
  const n = tweetLength(ta.value);
  const over = n > X_TWEET_LIMIT;
  count.textContent = over ? `${n}/${X_TWEET_LIMIT} — will be trimmed`
                           : `${n}/${X_TWEET_LIMIT}`;
  count.className = 'text-xs font-mono ' + (over ? 'text-red-400' : 'text-gray-500');
}

/** Break the single post text into <=X_TWEET_LIMIT tweet cells — no regenerate.
 *  Packs on paragraph, then sentence, then word boundaries. */
function splitTextIntoTweets(text, limit) {
  // Every budget check is weighted (a link costs 23), so a post carrying a long URL
  // isn't split into more tweets than X would actually require.
  const units = [];
  for (const para of text.split(/\n\n+/)) {
    if (tweetLength(para) <= limit) { units.push(para); continue; }
    const sentences = para.match(/[^.!?]+[.!?]*\s*/g) || [para];
    let buf = '';
    for (const s of sentences) {
      if (tweetLength(buf + s) > limit && buf) { units.push(buf.trim()); buf = s; }
      else buf += s;
    }
    if (buf.trim()) units.push(buf.trim());
  }
  const tweets = [];
  let cur = '';
  for (const u of units) {
    const cand = cur ? cur + '\n\n' + u : u;
    if (tweetLength(cand) > limit && cur) { tweets.push(cur); cur = u; }
    else cur = cand;
  }
  if (cur) tweets.push(cur);
  // Last resort: a single unit still over the limit → slice on spaces.
  const out = [];
  for (let t of tweets) {
    while (tweetLength(t) > limit) {
      let cut = t.lastIndexOf(' ', limit);
      if (cut <= 0) cut = limit;
      out.push(t.slice(0, cut).trim());
      t = t.slice(cut).trim();
    }
    if (t) out.push(t);
  }
  return out;
}

function splitIntoThread() {
  const ta = document.getElementById('caption-edit');
  const text = (ta && ta.value || '').trim();
  if (!text) return;
  const parts = splitTextIntoTweets(text, X_TWEET_LIMIT);
  if (parts.length < 2) { toast('Fits in one tweet — nothing to split', 'warn'); return; }
  renderThread(parts);            // editable per-tweet cells; hides the single box
  scheduleDraftSave();
  toast(`Split into ${parts.length} tweets`, 'success');
}

// ===== SAVE CAPTION =====
async function saveCaption() {
  if (!S.postId) return;
  const thread = S.threadParts || [];
  if (thread.some(t => tweetLength(t) > X_TWEET_LIMIT)) {
    toast(`⚠️ A tweet is over ${X_TWEET_LIMIT} characters — shorten it first`, 'warn');
    return;   // before the snapshot: a rejected save must not enter the undo stack
  }
  snapshotEdits();   // push current state so Undo can revert this save
  // For a thread the tweets ARE the post; caption is their join, kept in sync so
  // export, pillars and the feed keep reading one field.
  const caption = thread.length
    ? thread.join('\n\n')
    : document.getElementById('caption-edit').value;
  const btn = document.getElementById('save-caption-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/caption`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        caption, hashtags: S.hashtags, seo_keywords: S.seoKeywords,
        thread_parts: thread.length ? thread : null,
      }),
    });
    if (!res.ok) {
      let detail = 'Save failed';
      try { detail = (await res.json()).detail || detail; } catch(e) {}
      throw new Error(detail);
    }
    // The response recomputes the claim flags from the saved text, so an edit that
    // removes a number clears its warning right away.
    try { renderClaims(await res.json()); } catch(e){ /* body already consumed */ }
    // Clear both per-post keys so they don't accumulate to the quota over time.
    try { localStorage.removeItem('draft_' + S.postId); } catch(e){}
    try { localStorage.removeItem('undo_' + S.postId); } catch(e){}
    S.undoStack = [];
    document.getElementById('undo-btn').classList.add('hidden');
    toast('✅ Caption saved!', 'success');
    // This save is the one action that can earn a milestone (8.1 records
    // edited_ai_text here), so it is the one place worth re-reading them: the
    // offer that hangs on it makes sense at the moment of the edit, not on the
    // next reload. Not awaited — the save is finished either way.
    if (!S.milestones['edited_ai_text']) loadMilestones();
    return true;
  } catch(e) {
    toast('❌ ' + e.message, 'error');
    // Reported, not swallowed: setResultTab abandons a switch whose save failed
    // rather than carrying the user away from text that never reached the server.
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

// ===== EXPORT (saves straight to ~/Downloads — visible path in toast) =====
async function exportPost() {
  if (!S.postId) return;
  toast('📦 Saving ZIP to Downloads…', 'info');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/export-to-disk`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Export failed (${res.status})`);
    }
    const data = await res.json();
    showExportToast(data.path);
  } catch(e) {
    toast('❌ ' + e.message, 'error');
  }
}

function showExportToast(path) {
  // Persistent toast with absolute path + Open-folder button.
  const el = document.getElementById('toast');
  clearTimeout(window._toastTimer);
  el.className = 'fixed bottom-6 right-6 bg-gray-800 border border-green-700 rounded-xl px-5 py-3 shadow-xl text-sm text-gray-100 max-w-md';
  el.innerHTML = `
    <div class="flex items-start gap-3">
      <span class="text-green-400 text-lg">✅</span>
      <div class="flex-1 min-w-0">
        <p class="font-semibold mb-1">Saved to Downloads</p>
        <p class="text-xs text-gray-400 break-all">${path}</p>
        <div class="mt-2 flex gap-2">
          <button data-act="open-folder"
            class="bg-purple-600 hover:bg-purple-500 transition rounded-lg px-3 py-1 text-xs font-semibold">
            📂 Open folder
          </button>
          <button data-action="dismiss-toast"
            class="bg-gray-700 hover:bg-gray-600 transition rounded-lg px-3 py-1 text-xs">Dismiss</button>
        </div>
      </div>
    </div>`;
  // Closure, so the path travels as a value. It used to be escaped for
  // backslashes and then for quotes on its way into an attribute — two
  // hand-rolled escapes on a Windows path, for a string the handler could
  // simply have closed over.
  el.querySelector('[data-act="open-folder"]').onclick = () => openExportedFolder(path);
  el.classList.remove('hidden');
}

async function openExportedFolder(path) {
  try {
    const res = await apiFetch(`${API}/api/posts/open-folder`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Open failed');
    }
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

// ===== PUBLISH =====

/** The same read as loadConnections, cached, so the composer can ask without
 *  the Settings screen having been opened. */
async function ensureConnections(force) {
  if (!S.user || S.user.is_local) return {};
  if (S.connections && !force) return S.connections;
  try {
    const res = await apiFetch(`${API}/api/settings/connections`);
    S.connections = res.ok ? ((await res.json()).connections || {}) : {};
  } catch { S.connections = {}; }
  return S.connections;
}

/** Ask before publishing, and say where it is going (UX phase 8.4).
 *
 *  The product used to name the NETWORK — which the person picked themselves —
 *  and never the account, which appears nowhere on this screen. For anybody
 *  running more than one brand that is the whole question, and the answer is
 *  not what the profile switcher implies: connections live on the account, not
 *  on the profile, so every brand publishes through the same Instagram and the
 *  same X. Said once, because said every time it is a lecture.
 *
 *  An unchecked connection has no handle, and no handle means no sentence. A
 *  guessed destination on a confirm dialog is worse than a missing one. */
async function confirmPublish(platform, question) {
  const conns = await ensureConnections();
  const handle = ((conns[platform] || {}).handle || '').replace(/^@/, '');
  const lines = [question];
  if (handle) lines.push(`Going to @${handle}.`);
  if ((S.accounts || []).length > 1 && !S.milestones['connections_are_shared']) {
    lines.push('Connections belong to your account: every brand profile publishes '
             + 'through the same ones.');
    noteConnectionsAreShared();
  }
  return confirm(lines.join('\n'));
}

function noteConnectionsAreShared() {
  noteMilestone('connections_are_shared');
}

async function publishPost() {
  if (!S.postId) return;
  if (S.publishing) return;          // block a double-click from publishing twice
  const names = { instagram: 'Instagram', x: 'X', linkedin: 'LinkedIn' };
  const platform = (S.currentPost && S.currentPost.platform) || 'instagram';
  const label = names[platform] || platform;
  if (!await guardPublishKeys(platform)) return;   // cloud: needs this network's keys
  if (!await confirmPublish(platform, `Publish this post to ${label} now?`)) return;
  S.publishing = true;
  const btn = document.getElementById('publish-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Publishing…'; }
  toast('📤 Publishing…', 'info');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/publish`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) throw new Error(data.detail || data.error || 'Publish failed');
    const link = data.published_url ? ` — ${data.published_url}` : (' — ' + (data.instagram_media_id || ''));
    toast(`✅ Published to ${label}!${link}`, 'success');
  } catch(e) {
    toast('❌ ' + e.message, 'error');
    // The toast is gone in 4 seconds; keep the reason on screen so the user can
    // still act on it after switching tabs or looking away.
    if (S.currentPost) {
      S.currentPost.schedule_error = e.message;
      applyScheduleState(S.currentPost);
    }
    refreshFailedBanner();
  } finally {
    S.publishing = false;
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

// ===== SCHEDULE =====
async function schedulePost() {
  if (!S.postId) return;
  const val = document.getElementById('schedule-dt').value;
  if (!val) { toast('Pick a date/time first', 'warn'); return; }
  const iso = new Date(val).toISOString();
  const btn = document.getElementById('schedule-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳…'; }
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/schedule`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ publish_at: iso }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Schedule failed'); }
    const post = await res.json();
    applyScheduleState(post);
    toast('✅ Scheduled for ' + new Date(post.scheduled_at).toLocaleString(), 'success');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

async function unschedulePost() {
  if (!S.postId) return;
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/schedule`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Cancel failed');
    const post = await res.json();
    applyScheduleState(post);
    toast('Schedule cancelled', 'info');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

/** Stack whichever banners are visible and push the page down by their total
 *  height. Both are fixed at the top over the sticky header, so hardcoding one
 *  banner's height would let the second one cover the logo / Log out. */
function layoutBanners() {
  let top = 0;
  for (const id of ['verify-banner', 'failed-banner', 'expiry-banner']) {
    const el = document.getElementById(id);
    if (!el || el.classList.contains('hidden')) continue;
    el.style.top = top + 'px';
    top += el.offsetHeight;
  }
  document.body.style.paddingTop = top ? top + 'px' : '';
}

/** Count posts that failed to publish and show the banner while any exist. */
async function refreshFailedBanner() {
  const banner = document.getElementById('failed-banner');
  if (!banner || !S.user || S.user.is_local) return;
  let failed = [];
  try {
    const res = await apiFetch(`${API}/api/posts?status=failed&limit=100`);
    if (res.ok) failed = await res.json();
  } catch { /* 401 handled by apiFetch */ }
  S.failedPosts = failed;
  const n = failed.length;
  banner.classList.toggle('hidden', n === 0);
  if (n) {
    document.getElementById('failed-banner-text').textContent =
      n === 1 ? '⚠️ 1 post failed to publish.' : `⚠️ ${n} posts failed to publish.`;
  }
  layoutBanners();
}

/** Open the most recent failure so the reason and a retry are one click away. */
function showFailedPosts() {
  const first = (S.failedPosts || [])[0];
  if (!first) return;
  openPost(first.id);
}

function applyScheduleState(post) {
  const status = document.getElementById('schedule-status');
  const cancelBtn = document.getElementById('unschedule-btn');
  if (post.scheduled_at) {
    status.textContent = 'Scheduled: ' + new Date(post.scheduled_at).toLocaleString();
    status.className = 'text-xs text-purple-300';
    cancelBtn.classList.remove('hidden');
  } else if (post.published_at) {
    status.textContent = 'Published: ' + new Date(post.published_at).toLocaleString();
    status.className = 'text-xs text-green-400';
    cancelBtn.classList.add('hidden');
  } else if (post.schedule_error) {
    // The reason was already served by the API and simply never rendered — a
    // failed publish left nothing on screen once its 4-second toast had gone.
    status.textContent = '⚠️ Publish failed: ' + post.schedule_error;
    status.className = 'text-xs text-red-400';
    cancelBtn.classList.add('hidden');
  } else {
    status.textContent = '';
    cancelBtn.classList.add('hidden');
  }
}

// ===== INSIGHTS =====
async function refreshInsights() {
  if (!S.postId) return;
  toast('Fetching insights…', 'info');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/insights/refresh`, { method: 'POST' });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Insights failed'); }
    renderInsights(await res.json());
    toast('✅ Insights updated', 'success');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

function renderInsights(ins) {
  const box = document.getElementById('insights-numbers');
  const cell = (label, val) => `<div class="bg-gray-900 rounded-lg py-2"><div class="text-base font-bold">${val ?? '—'}</div><div class="text-gray-500">${label}</div></div>`;
  box.innerHTML =
    cell('Reach', ins.reach) + cell('Likes', ins.likes) + cell('Comments', ins.comments) +
    cell('Saves', ins.saved) + cell('Shares', ins.shares) + cell('Views', ins.video_views ?? ins.impressions);
}

// ===== VARIATIONS =====
async function showVariations(field) {
  if (!S.postId) return;
  const modal = document.getElementById('variations-modal');
  const list = document.getElementById('variations-list');
  document.getElementById('variations-title').textContent = 'Variations — ' + field;
  list.innerHTML = '<p class="text-sm text-gray-500">⏳ Generating…</p>';
  modal.classList.remove('hidden');
  onModalOpen('variations-modal');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/regenerate-field`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ field, count: 4 }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Failed'); }
    const data = await res.json();
    list.innerHTML = '';
    (data.variants || []).forEach(v => {
      const isList = Array.isArray(v);
      const display = isList ? v.join(' ') : v;
      const btn = document.createElement('button');
      btn.className = 'w-full text-left bg-gray-800 hover:bg-purple-900 border border-gray-700 rounded-lg p-3 text-sm transition';
      btn.textContent = display;
      btn.onclick = () => { applyVariation(field, v); closeVariations(); };
      list.appendChild(btn);
    });
    if (!list.children.length) list.innerHTML = '<p class="text-sm text-gray-500">No variants returned.</p>';
  } catch(e) {
    list.innerHTML = `<p class="text-sm text-red-400">❌ ${e.message}</p>`;
  }
}

function applyVariation(field, value) {
  if (field === 'caption') {
    document.getElementById('caption-edit').value = value;
  } else if (field === 'hashtags') {
    S.hashtags = Array.isArray(value) ? [...value] : String(value).split(/\s+/).filter(Boolean);
    renderHashtags();
  } else if (field === 'seo_keywords') {
    S.seoKeywords = Array.isArray(value) ? [...value] : String(value).split(/,\s*/).filter(Boolean);
    renderSeoKeywords();
  }
  toast('Applied — remember to Save edits', 'info');
}

function closeVariations() {
  document.getElementById('variations-modal').classList.add('hidden');
  onModalClose();
}

// ===== CALENDAR =====
function calShift(delta) {
  const r = S.calRef || _todayRef();
  let m = r.month + delta, y = r.year;
  if (m < 0) { m = 11; y--; } if (m > 11) { m = 0; y++; }
  S.calRef = { year: y, month: m };
  renderCalendar();
}
function _todayRef() { const d = new Date(); return { year: d.getFullYear(), month: d.getMonth() }; }

// The network a post was written for, as a glyph. Calendar, Queue and Results
// show every network at once now, so each row has to say which one it is.
//
// This replaces postsForNetwork(), which filtered those screens on `p.platform`
// — a field /api/posts did not send. `undefined || 'instagram'` made the filter
// a no-op on Instagram and a match against nothing on X, so switching networks
// emptied the calendar, the feed grid and analytics. The field is sent now.
//: Worst first. A collapsed card that says "scheduled" while one network failed
//: is a lie the user acts on — they see it going out and never look again.
const GROUP_STATUS_ORDER = ['failed', 'scheduled', 'preview', 'draft', 'published'];

/** Sibling posts of one idea, newest group first.
 *
 *  Grouping is done here rather than on the server because `limit`/`offset`
 *  count rows, not ideas — a server-side group would break paging — and because
 *  two screens deliberately do NOT group (see loadGrid and loadAnalytics). */
function groupPosts(rows) {
  const byKey = new Map();
  (rows || []).forEach(p => {
    const key = p.variant_group_id || p.id;
    if (!byKey.has(key)) byKey.set(key, { key, posts: [], primary: p });
    byKey.get(key).posts.push(p);
  });
  return [...byKey.values()];
}

/** The status a whole group should report: the most actionable one. */
function groupStatus(posts) {
  for (const s of GROUP_STATUS_ORDER) {
    if (posts.some(p => p.status === s)) return s;
  }
  return (posts[0] || {}).status || 'draft';
}

/** The glyphs for a group of sibling posts, one per network, deduped.
 *  Used by the collapsed group cards in the Queue. */
function netBadges(posts) {
  const seen = new Set();
  return (posts || []).map(p => {
    const k = p.platform || 'instagram';
    if (seen.has(k)) return '';
    seen.add(k);
    return netBadge(p);
  }).join('');
}

function netBadge(p) {
  const x = (p.platform || 'instagram') === 'x';
  return `<span title="${x ? 'X' : 'Instagram'}" class="text-[10px]">${x ? '𝕏' : '📸'}</span>`;
}

async function renderCalendar() {
  if (!S.calRef) S.calRef = _todayRef();
  const startEl = document.getElementById('plan-start');
  if (startEl && !startEl.value) startEl.value = _tomorrowISO();
  const { year, month } = S.calRef;
  document.getElementById('cal-title').textContent =
    new Date(year, month, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
  renderPillars();
  // Exactly the month on screen. The server windows on the same
  // COALESCE(scheduled_at, published_at) this loop reads, so a published post
  // stays on the day it went out.
  const monthPosts = await fetchPosts({
    since: new Date(Date.UTC(year, month, 1)).toISOString(),
    until: new Date(Date.UTC(year, month + 1, 1)).toISOString(),
  });
  const byDay = {};
  monthPosts.forEach(p => {
    const when = p.scheduled_at || p.published_at;
    if (!when) return;
    const d = new Date(when);
    if (d.getFullYear() === year && d.getMonth() === month) {
      const k = d.getDate();
      (byDay[k] = byDay[k] || []).push(p);
    }
  });
  const grid = document.getElementById('cal-grid');
  grid.innerHTML = '';
  const first = new Date(year, month, 1);
  const startDow = (first.getDay() + 6) % 7;  // Mon=0
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  for (let i = 0; i < startDow; i++) grid.appendChild(document.createElement('div'));
  const dot = { scheduled: '🟣', published: '🟢', failed: '🔴', draft: '⚪', preview: '⚪' };
  for (let day = 1; day <= daysInMonth; day++) {
    const cell = document.createElement('div');
    cell.className = 'min-h-[64px] bg-gray-800 rounded-lg p-1 text-left';
    let html = `<div class="text-[10px] text-gray-500">${day}</div>`;
    // Grouped WITHIN the day, never across the month: siblings schedule
    // independently, so one idea can legitimately sit on two dates and a
    // group-level date would have to be invented — and be wrong on one of them.
    groupPosts(byDay[day] || []).forEach(g => {
      const st = groupStatus(g.posts);
      html += `<div data-cal-entry class="text-[10px] truncate cursor-pointer hover:text-purple-300" data-action="open-post" data-arg="${g.primary.id}">${dot[st] || '⚪'}${netBadges(g.posts)} ${esc(g.primary.topic.slice(0,14))}</div>`;
    });
    cell.innerHTML = html;
    grid.appendChild(cell);
  }
}

// ===== GRID =====
//: How many tiles a page of the profile grid holds. Three columns, so a
//: multiple of three keeps the last row full.
const GRID_PAGE = 60;

async function loadGrid() {
  S.gridOffset = 0;
  document.getElementById('grid-container').innerHTML = '';
  await loadMoreGrid();
}

async function loadMoreGrid() {
  const page = await fetchPosts({ limit: GRID_PAGE, offset: S.gridOffset || 0 });
  S.gridOffset = (S.gridOffset || 0) + page.length;
  // A short page means the end. Asking again to find out would be one wasted
  // request per visit, forever.
  document.getElementById('grid-more').classList.toggle('hidden', page.length < GRID_PAGE);
  renderGridPage(page);
}

function renderGridPage(posts_page) {
  // Instagram only, and deliberately so: this screen simulates an Instagram
  // profile page — a 4:5 ring grid. A tweet in it wouldn't be "unfiltered",
  // it would be wrong. So it is a property of the view, not of a global
  // active-network setting.
  //
  // It is also why this screen needs NO grouping in phase 4: a variant group
  // holds at most one Instagram post, so the filter already yields one tile per
  // idea. Adding grouping here would be wrong, and dropping the filter would put
  // a tweet in a simulated Instagram profile.
  const posts = posts_page.filter(p => (p.platform || 'instagram') === 'instagram');
  const c = document.getElementById('grid-container');
  // Empty only when nothing has been drawn at all — a later page that happens
  // to be all X posts must not blank a grid that already has tiles in it.
  document.getElementById('grid-empty').classList.toggle(
    'hidden', posts.length > 0 || c.childElementCount > 0);
  const ring = { scheduled: 'ring-purple-500', published: 'ring-green-500', failed: 'ring-red-500' };
  posts.forEach(p => {
    const div = document.createElement('div');
    div.className = 'relative aspect-[4/5] bg-gray-800 cursor-pointer';
    div.onclick = () => openPost(p.id);
    div.innerHTML = p.thumb_url
      ? `<img src="${API}${p.thumb_url}" alt="${esc(p.topic || 'post')}" class="w-full h-full object-cover ${ring[p.status] ? 'ring-2 ' + ring[p.status] : ''}" />`
      : `<div class="w-full h-full flex items-center justify-center text-gray-600 text-xs">${p.topic.slice(0,20)}</div>`;
    div.innerHTML += `<span class="absolute top-1 right-1 text-xs">${({scheduled:'🟣',published:'🟢',failed:'🔴'})[p.status] || ''}</span>`;
    c.appendChild(div);
  });
}

/** Fetch posts for one screen. Every caller says what it needs.
 *
 *  There used to be a single `ensurePosts()` caching the newest 500 for the
 *  Queue, the Calendar and the profile grid alike. It truncated in silence,
 *  which is the worst way to be wrong about a list: a scheduled post older than
 *  those 500 stayed out of the Queue and published anyway, and a month further
 *  back drew an empty calendar. Both read as "you have nothing".
 *
 *  Nothing is cached across screens either — the shared cache also meant the
 *  Queue rendered whatever the Calendar happened to have fetched first. */
async function fetchPosts(params = {}) {
  const qs = new URLSearchParams();
  (params.status || []).forEach(v => qs.append('status', v));
  if (params.since) qs.set('since', params.since);
  if (params.until) qs.set('until', params.until);
  qs.set('limit', params.limit || 500);
  if (params.offset) qs.set('offset', params.offset);
  try {
    const res = await apiFetch(`${API}/api/posts?${qs}`);
    return res.ok ? await res.json() : [];
  } catch(e) { return []; }
}

async function openPost(postId) {
  try {
    const res = await apiFetch(`${API}/api/posts/${postId}`);
    if (!res.ok) throw new Error('Load failed');
    const post = await res.json();
    S.createMode = defaultCreateMode();
    setView('create');
    bindPost(post);
    showStep(4);
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

// ===== CONTENT PILLARS =====
// ===== PLAN A WEEK (batch) =====
// Each row: {topic, pillar, pillar_label, date, status: '' | 'done' | 'failed'}
S.planRows = [];

function _tomorrowISO() {
  const d = new Date(); d.setDate(d.getDate() + 1);
  return d.toISOString().slice(0, 10);
}

function _batchFormat() { return document.getElementById('plan-format').value; }
function _batchSlides() {
  return { carousel_3: 3, carousel_5: 5 }[_batchFormat()] || 1;
}

async function suggestPlan() {
  if (!await guardGenerateKeys()) return;
  const btn = document.getElementById('plan-suggest-btn');
  const prev = btn.textContent; btn.disabled = true; btn.textContent = '⏳ Thinking…';
  try {
    const res = await apiFetch(`${API}/api/posts/plan`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        theme: document.getElementById('plan-theme').value || null,
        count: Math.min(14, Math.max(2, parseInt(document.getElementById('plan-count').value, 10) || 7)),
        start_date: document.getElementById('plan-start').value || _tomorrowISO(),
        cadence_days: parseInt(document.getElementById('plan-cadence').value, 10) || 1,
        platform: S.platform || 'instagram',
      }),
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Planning failed'); }
    S.planRows = (await res.json()).items.map(it => ({ ...it, status: '' }));
    renderPlan();
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = prev; }
}

function renderPlan() {
  const list = document.getElementById('plan-list');
  const actions = document.getElementById('plan-actions');
  const empty = S.planRows.length === 0;
  list.classList.toggle('hidden', empty);
  actions.classList.toggle('hidden', empty);
  list.innerHTML = '';
  S.planRows.forEach((row, i) => {
    const el = document.createElement('div');
    el.className = 'flex items-center gap-2 bg-gray-800 rounded-lg px-2 py-1.5';
    const mark = row.status === 'done' ? '✅' : row.status === 'failed' ? '⚠️' : '';
    el.innerHTML = `
      <span class="text-[10px] text-purple-300 whitespace-nowrap">${esc(row.pillar_label || row.pillar)}</span>
      <span class="text-[10px] text-gray-500 whitespace-nowrap">${esc(row.date)}</span>
      <input value="${esc(row.topic)}" aria-label="Topic ${i + 1}"
        class="flex-1 bg-transparent text-sm text-white focus:outline-none min-w-0" />
      <span class="text-xs w-4 text-center">${mark}</span>
      <button title="Remove" aria-label="Remove topic ${i + 1}" class="text-gray-500 hover:text-white text-sm">×</button>`;
    el.querySelector('input').addEventListener('input', e => { S.planRows[i].topic = e.target.value; });
    el.querySelector('button').onclick = () => { S.planRows.splice(i, 1); renderPlan(); };
    list.appendChild(el);
  });
  const n = S.planRows.length;
  // ~$0.007 per post is the measured order of magnitude for text + a few stock slides.
  const est = (n * _batchSlides() * 0.0015 + n * 0.004).toFixed(2);
  document.getElementById('plan-estimate').textContent =
    `${n} post${n === 1 ? '' : 's'} · ~$${est}`;
  document.getElementById('plan-generate-btn').textContent = `Generate ${n} post${n === 1 ? '' : 's'}`;
}

function addPlanRow() {
  const last = S.planRows[S.planRows.length - 1];
  const nextDate = last ? last.date : (document.getElementById('plan-start').value || _tomorrowISO());
  S.planRows.push({ topic: '', pillar: 'educational', pillar_label: 'Educational', date: nextDate, status: '' });
  renderPlan();
}

async function runBatch() {
  const rows = S.planRows.filter(r => (r.topic || '').trim());
  if (!rows.length) { toast('Add at least one topic', 'warn'); return; }
  if (S.batchRunning) return;
  S.batchRunning = true;
  const genBtn = document.getElementById('plan-generate-btn');
  genBtn.disabled = true;
  const prog = document.getElementById('plan-progress');
  prog.classList.remove('hidden');

  let done = 0, failed = 0;
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    prog.textContent = `Generating ${i + 1} of ${rows.length}: ${row.topic.slice(0, 40)}…`;
    try {
      await streamGenerate({
        topic: row.topic.trim(),
        format: _batchFormat(),
        tone: 'professional',
        apply_branding: true,
        default_image_source: 'stock',
        platform: S.platform || 'instagram',
        length_tier: 'sweet_spot',
        template_style: 'branded_card',
        show_logo: true,
        // Pin to the planned calendar day; server keeps it a preview draft.
        plan_date: row.date + 'T09:00:00',
      });
      row.status = 'done'; done++;
    } catch (e) {
      row.status = 'failed'; failed++;
    }
    renderPlan();
  }

  prog.textContent = failed
    ? `${done} of ${rows.length} generated, ${failed} failed. Fix or remove the ⚠️ topics and generate again.`
    : `✅ ${done} draft${done === 1 ? '' : 's'} added to the calendar for review.`;
  genBtn.disabled = false;
  S.batchRunning = false;
  renderCalendar();             // re-fetch so the new drafts show up
  if (done) toast(`✅ ${done} draft${done === 1 ? '' : 's'} on the calendar`, 'success');
}

async function renderPillars() {
  try {
    const res = await apiFetch(`${API}/api/posts/pillars/mix`);
    if (!res.ok) return;
    const data = await res.json();
    const sug = data.suggestion || {};
    document.getElementById('pillar-suggestion').innerHTML =
      `💡 <b>${sug.emoji || ''} Post today: ${sug.label || ''}</b> — ${sug.reason || ''}`;
    const bars = document.getElementById('pillar-bars');
    bars.innerHTML = '';
    (data.pillars || []).forEach(p => {
      const row = document.createElement('div');
      row.innerHTML = `
        <div class="flex justify-between text-xs text-gray-400 mb-0.5">
          <span>${p.emoji} ${p.label}</span>
          <span>${p.actual_pct}% <span class="text-gray-600">/ ${p.target_pct}% target · ${p.count} posts</span></span>
        </div>
        <div class="h-2 bg-gray-800 rounded-full overflow-hidden relative">
          <div class="h-full bg-purple-600" style="width:${Math.min(100, p.actual_pct)}%"></div>
          <div class="absolute top-0 h-full border-r-2 border-yellow-400" style="left:${Math.min(100, p.target_pct)}%"></div>
        </div>`;
      bars.appendChild(row);
    });
  } catch(e) { /* ignore */ }
}

// ===== REEL =====
async function makeReel() {
  if (!S.postId) return;
  const btn = document.getElementById('make-reel-btn');
  const voiceover = !!document.getElementById('reel-voiceover')?.checked;
  const voiceId = (document.getElementById('reel-voice-id')?.value || '').trim();
  const visuals = voiceover ? (document.getElementById('reel-visuals')?.value || 'slides') : 'slides';
  const music = voiceover && !!document.getElementById('reel-music')?.checked;
  const cover = voiceover && !!document.getElementById('reel-cover')?.checked;
  btn.disabled = true; btn.textContent = voiceover ? '⏳ Narrating…' : '⏳ Rendering…';
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/reel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voiceover, voice_id: voiceId || null, visuals, music, cover }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Render failed'); }
    const data = await res.json();
    const url = API + data.video_url;
    document.getElementById('reel-video').src = url;
    document.getElementById('reel-download').href = url;
    document.getElementById('reel-preview').classList.remove('hidden');
    const noun = postPlatform() === 'x' ? 'Video' : 'Reel';
    if (data.broll_fallbacks > 0) {
      const total = (data.broll_clips || 0) + data.broll_fallbacks;
      toast(`⚠ ${noun} ready — ${data.broll_fallbacks}/${total} scenes used slides (stock clips unavailable)`, 'warn');
    } else {
      toast(`✅ ${noun} rendered`, 'success');
    }
  } catch(e) { toast('❌ ' + e.message, 'error'); }
  finally { btn.disabled = false; updateMakeReelButton(); }
}

async function publishReel() {
  if (!S.postId) return;
  if (!await confirmPublish('instagram', 'Publish this Reel to Instagram now?')) return;
  toast('📤 Publishing Reel…', 'info');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/publish-reel`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) throw new Error(data.detail || data.error || 'Publish failed');
    toast('✅ Reel published! Media ID: ' + data.instagram_media_id, 'success');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

// The Reel card's button dispatches by network: Instagram publishes
// synchronously (publishReel, above); X is asynchronous (a background job —
// see services/x_video_publish.py), so it queues and polls instead.
function publishReelOrToX() {
  if (postPlatform() === 'x') return publishVideoToX();
  return publishReel();
}

async function publishVideoToX() {
  if (!S.postId) return;
  if (!await confirmPublish('x', 'Publish this video to X now?')) return;
  toast('📤 Queuing…', 'info');
  try {
    const res = await apiFetch(`${API}/api/posts/${S.postId}/publish-video`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = data.detail || 'Publish failed';
      if (res.status === 400 && /credential|X \(Twitter\)/i.test(msg)) {
        needKey('Connect your X account', msg, 'connections');
        return;
      }
      throw new Error(msg);
    }
    S.publishJobs[data.post_id] = data;
    toast(data.warning ? '⚠ ' + data.warning : '📤 Queued — uploading to X…', data.warning ? 'warn' : 'info');
    updatePostEditorChrome();
    startPublishJobPolling();
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

// ===== TOAST =====
let toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  const colors = { success: 'text-green-400', error: 'text-red-400', warn: 'text-yellow-400', info: 'text-blue-300' };
  el.className = `fixed bottom-6 right-6 bg-gray-800 border border-gray-700 rounded-xl px-5 py-3 shadow-xl text-sm ${colors[type] || ''}`;
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
}

// ===== NICHE COLOR SWATCHES =====
function initNicheSwatches() {
  const wrap = document.getElementById('niche-color-swatches');
  if (!wrap) return;
  const render = () => {
    wrap.querySelectorAll('button').forEach(b => {
      b.style.outline = (b.dataset.color === S.nicheBoxColor) ? '2px solid #fff' : 'none';
    });
  };
  // "Brand default" option
  const def = document.createElement('button');
  def.dataset.color = '';
  def.className = 'px-3 py-2 rounded-xl text-xs font-medium border-2 border-gray-700 bg-gray-800 text-gray-300';
  def.textContent = 'Brand default';
  def.onclick = () => { S.nicheBoxColor = null; render(); };
  wrap.appendChild(def);
  NICHE_BOX_PALETTE.forEach(hex => {
    const b = document.createElement('button');
    b.dataset.color = hex;
    b.title = hex;
    b.className = 'w-9 h-9 rounded-lg border-2 border-gray-700';
    b.style.background = hex;
    b.onclick = () => { S.nicheBoxColor = hex; render(); };
    wrap.appendChild(b);
  });
  // Free colour picker — the swatches above are shortcuts, not the only options.
  const custom = document.createElement('input');
  custom.type = 'color';
  custom.title = 'Custom colour';
  custom.setAttribute('aria-label', 'Custom niche box colour');
  custom.className = 'w-9 h-9 rounded-lg border-2 border-gray-700 bg-gray-800 cursor-pointer p-0';
  custom.value = S.nicheBoxColor || '#ff751f';
  custom.oninput = () => { S.nicheBoxColor = custom.value.toLowerCase(); render(); };
  wrap.appendChild(custom);
  render();
}

// ===== VIEW SWITCH =====
// ===== NETWORK TABS (Instagram / X) =====
function setNetwork(net) {
  S.platform = net;                 // what the next generation targets
  const isIG = net === 'instagram';
  // (Results names no single network any more — it lists every one, badged.)
  // The composer's own toggle — the only network control in the product now
  // that the rail is gone. It always was the only one in the Business shell.
  const igT = document.getElementById('net-toggle-ig');
  const xT = document.getElementById('net-toggle-x');
  if (igT) { igT.classList.toggle('bg-purple-600', isIG); igT.classList.toggle('text-white', isIG);
             igT.classList.toggle('bg-gray-800', !isIG); igT.classList.toggle('text-gray-300', !isIG); }
  if (xT)  { xT.classList.toggle('bg-purple-600', !isIG); xT.classList.toggle('text-white', !isIG);
             xT.classList.toggle('bg-gray-800', isIG); xT.classList.toggle('text-gray-300', isIG); }
  // X is a text network — hide the Instagram-only "image label" + caption-length fields.
  const nicheF = document.getElementById('niche-label-field');
  const lenF = document.getElementById('length-field');
  if (nicheF) nicheF.classList.toggle('hidden', !isIG);
  if (lenF) lenF.classList.toggle('hidden', !isIG);
  // sync the (now hidden) wizard platform toggle so generation uses this network
  document.querySelectorAll('.plt-btn').forEach(b => b.classList.toggle('active', b.dataset.val === net));
  // X has no carousels: swap the Instagram format picker for the X post types.
  const fmtGroup = document.getElementById('format-group');
  const xGroup = document.getElementById('xmode-group');
  if (fmtGroup) fmtGroup.classList.toggle('hidden', !isIG);
  if (xGroup) xGroup.classList.toggle('hidden', isIG);
  // Hiding the picker used to leave S.format alone, so a carousel chosen for
  // Instagram was still in the request after switching to X — a shape X has no
  // concept of, sent from an invisible control.
  if (!isIG && (S.format || '').startsWith('carousel')) {
    const single = document.querySelector('#format-group [data-val="single"]');
    if (single) single.click(); else S.format = 'single';
  }
  // Text-only is X-only (Instagram needs media). Show the button on X; if the user
  // switches to Instagram while it's picked, fall back to Stock photos.
  const textBtn = document.getElementById('src-text-only');
  if (textBtn) textBtn.classList.toggle('hidden', isIG);
  if (isIG && S.source === 'text_only') {
    const stockBtn = document.querySelector('#source-btns [data-val="stock"]');
    if (stockBtn) stockBtn.click();
  }
  if (!isIG) applyXPremiumGate();
  renderOwnPhotos();          // X takes one image, Instagram may take up to 10
  updatePostEditorChrome();   // "Post" vs "Caption", X char counter + split button
  // Plan-a-week: X has no carousels (one image per tweet), so offer Single only.
  const planFmt = document.getElementById('plan-format');
  if (planFmt) {
    planFmt.querySelectorAll('option').forEach(o => {
      const carousel = o.value.startsWith('carousel');
      o.hidden = carousel && !isIG;
      o.disabled = carousel && !isIG;
    });
    if (!isIG && planFmt.value.startsWith('carousel')) planFmt.value = 'single';
  }
  setSection(S.section || 'create');
  updateConfigureSummary();
}

// ===== X POST TYPES =====

/** Reflect the stored premium flag on the "Long post" button. */
function applyXPremiumGate() {
  const btn = document.getElementById('xmode-long');
  const note = document.getElementById('xmode-long-note');
  if (!btn) return;
  const allowed = !!S.xPremium;
  btn.disabled = !allowed;
  btn.classList.toggle('opacity-40', !allowed);
  btn.classList.toggle('cursor-not-allowed', !allowed);
  btn.title = allowed ? '' : 'Needs X Premium — enable it in Account';
  if (note) note.classList.toggle('hidden', allowed);
  // Someone who turns Premium off while "Long" is selected must not keep it.
  if (!allowed && S.xMode === 'long') {
    const short = document.querySelector('.xmode-btn[data-val="short"]');
    if (short) short.click();
  }
}

function onXModeChange(mode) {
  const range = document.getElementById('thread-range');
  if (range) range.classList.toggle('hidden', mode !== 'thread');
}

async function loadXSettings() {
  try {
    const res = await apiFetch(`${API}/api/settings/x`);
    if (res.ok) {
      const data = await res.json();
      S.xPremium = !!data.x_premium;
      // Take the limit from the server so the counter can't drift from what the
      // publisher actually enforces.
      if (data.tweet_char_limit > 0) X_TWEET_LIMIT = data.tweet_char_limit;
    }
  } catch { /* 401 handled by apiFetch */ }
  const cb = document.getElementById('x-premium');
  if (cb) cb.checked = !!S.xPremium;
  applyXPremiumGate();
}

async function saveXSettings() {
  const btn = document.getElementById('save-x-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/settings/x`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x_premium: document.getElementById('x-premium').checked }),
    });
    if (!res.ok) throw new Error('Save failed');
    await loadXSettings();
    toast('✅ Saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

/** Which of Create's three shapes is on screen. Set only through
 *  setCreateMode, so the buttons and the panels cannot disagree. */
function setCreateMode(mode) {
  S.createMode = mode || defaultCreateMode();
  applyCreateMode();
}

/** What Create means for this account when nothing else has been chosen. */
function defaultCreateMode() {
  return (S.user && S.user.account_type === 'business') ? 'leads' : 'post';
}

async function applyCreateMode() {
  // A mode this account cannot see is not a mode. The buttons are gated by the
  // same CSS as the nav, so availability is ASKED OF THE DOM — and it is a real
  // guard, not decoration: setCreateMode is reachable from anywhere and
  // S.createMode outlives an account switch, so without the fallback a creator
  // lands on the Business leads panel, or a Business account on a wizard whose
  // Generate button their product does not have — in both cases with no mode
  // button on screen to leave by.
  let mode = S.createMode || defaultCreateMode();
  const btn = document.querySelector(`#create-modes [data-create-mode="${mode}"]`);
  if (!btn || btn.offsetParent === null) mode = defaultCreateMode();
  S.createMode = mode;
  document.querySelectorAll('#create-modes .mode-btn').forEach(b =>
    b.classList.toggle('mode-active', b.dataset.createMode === mode));
  document.querySelectorAll('#view-create [data-create-panel]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.createPanel !== mode));
  if (mode === 'leads') await loadLeads();
  if (mode === 'photo') await loadLibrary('image');
  if (mode === 'video') {
    await loadVideoModels();
    await loadLibrary('video');
    startVideoPolling();
    startPublishJobPolling();
  }
}

/** Is the video grid actually on screen?
 *
 *  What the pollers used to ask was `S.section === 'library-video'`, which is
 *  the same question only while the video grid is its own section. Asking the
 *  DOM instead survives every move of it — and fixes a bug that was already
 *  there: opening the composer's video card while a render was in flight left
 *  S.section elsewhere and silently stopped the poll. */
function videoGridVisible() {
  const el = document.getElementById('create-video-panel');
  return !!el && !el.classList.contains('hidden');
}

// ===== SIDEBAR SECTIONS =====
function setSection(sec) {
  const map = { create:'view-create', calendar:'view-calendar', queue:'view-queue',
                results:'view-results', settings:'view-settings' };
  // An unknown name hides every view and leaves a blank page — the list below is
  // derived from the map, so nothing matches and everything gets `hidden`. That
  // has happened twice already (a section id removed while a caller kept asking
  // for it), and it fails silently: no throw, no log, just an empty screen. A
  // caller that asks for somewhere that no longer exists gets Create.
  if (!Object.hasOwn(map, sec)) sec = 'create';
  S.section = sec;
  // Every view id is a value in `map` above — deriving the list from it means a
  // section added only to the map (the easy mistake) can no longer end up
  // stuck visible forever, which used to require editing this list separately.
  for (const v of new Set(Object.values(map))) {
    const el = document.getElementById(v); if (el) el.classList.toggle('hidden', map[sec] !== v);
  }
  document.querySelectorAll('#section-nav .sec-btn').forEach(b => b.classList.toggle('sec-active', b.dataset.section === sec));
  if (sec === 'calendar') applyCalendarMode();
  else if (sec === 'results') loadResults();
  else if (sec === 'create') applyCreateMode();
  else if (sec === 'settings') loadAdmin();
  else if (sec === 'queue') loadQueue();
}

// Back-compat alias for any old callers (grid click etc.).
function setView(view) {
  if (view === 'grid') return setCalendarMode('profile'), setSection('calendar');
  setSection({ create:'create', calendar:'calendar' }[view] || 'create');
}

/** Switch the Calendar between the month and the profile grid. */
function setCalendarMode(mode) {
  S.calendarMode = (mode === 'profile') ? 'profile' : 'calendar';
  applyCalendarMode();
}

/** Show exactly one of the Calendar's two panels and load what it needs.
 *
 *  The panel list is read off the DOM, like the Settings tabs and setSection's
 *  view list before it: a panel added to the markup and forgotten in a
 *  hand-written list is a panel that stays on screen under the other mode. */
function applyCalendarMode() {
  const mode = S.calendarMode === 'profile' ? 'profile' : 'calendar';
  document.querySelectorAll('#calendar-modes .cal-mode-btn').forEach(b =>
    b.classList.toggle('sec-active', b.dataset.calendarMode === mode));
  document.querySelectorAll('#view-calendar [data-calendar-panel]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.calendarPanel !== mode));
  if (mode === 'profile') loadGrid(); else renderCalendar();
}

/** Open Results on a tab. The one entry point, like openSettings. */
function openResults(tab) {
  S.resultsTab = tab || 'posts';
  setSection('results');
}

/** Show the active tab's panel, and fetch for that tab only.
 *
 *  Two tabs belong to Business alone. The tab strip already says so in CSS, so
 *  availability is ASKED OF THE DOM rather than restated here — the two cannot
 *  disagree that way. It is a real guard and not decoration: openResults() is
 *  reachable from anywhere, and S.resultsTab outlives an account switch, so
 *  without the fallback a creator can land on an empty Business panel whose tab
 *  strip offers no way back. */
function loadResults() {
  let tab = S.resultsTab || 'posts';
  const btn = document.querySelector(`#results-tabs [data-results-tab="${tab}"]`);
  if (!btn || btn.offsetParent === null) tab = 'posts';
  S.resultsTab = tab;
  document.querySelectorAll('#results-tabs .set-btn').forEach(b =>
    b.classList.toggle('set-active', b.dataset.resultsTab === tab));
  document.querySelectorAll('#view-results > div[data-results-tab]').forEach(el =>
    el.classList.toggle('hidden', el.dataset.resultsTab !== tab));
  if (tab === 'sources') return loadSourceAnalytics();
  if (tab === 'journal') return loadJournal();
  return loadAnalytics();
}

async function loadAnalytics() {
  const box = document.getElementById('analytics-list');
  box.innerHTML = 'Loading…';
  try {
    const res = await apiFetch(`${API}/api/posts`);
    const posts = (await res.json()).filter(p => p.status === 'published');
    if (!posts.length) { box.innerHTML = '<span class="text-gray-500">Nothing published yet.</span>'; return; }
    box.innerHTML = posts.map(p => `<div class="ce-card p-3 flex items-center gap-3">
      <div class="flex-1 min-w-0"><div class="text-sm text-gray-200 truncate">${netBadge(p)} ${esc(p.topic || 'post')}</div>
      <div class="text-xs text-gray-500">${p.published_at ? new Date(p.published_at).toLocaleString() : ''}</div></div>
      ${p.published_url ? `<a href="${esc(safeUrl(p.published_url))}" target="_blank" class="ce-btn-ghost px-3 py-1 text-xs">View post ↗</a>` : ''}
    </div>`).join('');
  } catch (e) { box.innerHTML = '<span class="text-red-400">' + esc(e.message) + '</span>'; }
}

// ===== MEDIA LIBRARY (standalone assets — generate or upload, use in a post later) =====

async function loadLibrary(kind) {
  const box = document.getElementById(`library-${kind}-grid`);
  box.innerHTML = '<span class="text-gray-500 text-sm">Loading…</span>';
  try {
    const res = await apiFetch(`${API}/api/media?kind=${encodeURIComponent(kind)}`);
    const assets = res.ok ? await res.json() : [];
    renderLibraryGrid(kind, assets);
  } catch { box.innerHTML = '<span class="text-red-400 text-sm">Couldn\'t load your library.</span>'; }
}

function renderLibraryGrid(kind, assets) {
  const box = document.getElementById(`library-${kind}-grid`);
  if (!assets.length) {
    box.innerHTML = `<span class="text-gray-500 text-sm col-span-full">Nothing here yet — generate one above or upload your own.</span>`;
    return;
  }
  box.innerHTML = '';
  assets.forEach(a => box.appendChild(renderLibraryCard(a)));
}

function renderLibraryCard(asset) {
  const card = document.createElement('div');
  card.className = 'ce-card p-2 space-y-1';
  const media = asset.status === 'ready' && asset.url && asset.kind === 'video'
    ? `<video src="${API}${asset.url}" controls class="w-full aspect-square object-cover rounded-lg bg-black"></video>`
    : asset.status === 'ready' && asset.url
    ? `<img src="${API}${asset.url}" alt="${esc(asset.title || 'Generated image')}" class="w-full aspect-square object-cover rounded-lg bg-black" />`
    : asset.status === 'failed'
      ? `<div class="w-full aspect-square rounded-lg bg-gray-800 flex items-center justify-center text-xs text-red-400 p-2 text-center">${esc(asset.error || 'Failed')}</div>`
      : `<div class="w-full aspect-square rounded-lg bg-gray-800 flex items-center justify-center text-xs text-gray-400">Generating…</div>`;
  const editBtn = asset.kind === 'video' && asset.status === 'ready'
    ? `<button class="ce-btn-ghost w-full px-2 py-1 text-xs" data-act="edit">Edit</button>` : '';
  const publishBtn = renderPublishXAffordance(asset);
  card.innerHTML = `
    ${media}
    <div class="text-xs text-gray-400 truncate" title="${esc(asset.title || asset.prompt || '')}">${esc(asset.title || asset.prompt || 'Untitled')}</div>
    ${editBtn}
    ${publishBtn}
    <button class="ce-btn-ghost w-full px-2 py-1 text-xs" data-act="delete">Delete</button>
  `;
  const editEl = card.querySelector('[data-act="edit"]');
  if (editEl) editEl.onclick = () => openEditVideoModal(asset);
  const pubEl = card.querySelector('[data-act="publish-x"]');
  if (pubEl) pubEl.onclick = () => openPublishToXModal(asset);
  const retryEl = card.querySelector('[data-act="publish-x-retry"]');
  if (retryEl) retryEl.onclick = () => openPublishToXModal(asset);
  card.querySelector('[data-act="delete"]').onclick = () => deleteLibraryAsset(asset.id, asset.kind);
  return card;
}

// A ready video card is either "not published yet" (a button), mid-flight (a
// live status badge, no button — clicking again would start a second job), or
// terminal (a link to the tweet, or a Retry that reopens the same modal).
const _PUB_JOB_LABELS = {
  queued: 'Queued for X…', uploading: 'Uploading to X…',
  processing: 'X is processing the video…', tweeting: 'Posting…',
};
function renderPublishXAffordance(asset) {
  if (asset.kind !== 'video' || asset.status !== 'ready') return '';
  const job = (S.publishJobs || {})[asset.id];
  if (!job) {
    return `<button class="ce-btn-ghost w-full px-2 py-1 text-xs" data-act="publish-x">𝕏 Publish to X</button>`;
  }
  if (job.status === 'published') {
    return `<a href="${esc(safeUrl(job.permalink))}" target="_blank" rel="noopener"
              class="ce-btn-ghost w-full px-2 py-1 text-xs block text-center">✅ View on X ↗</a>`;
  }
  if (job.status === 'failed') {
    return `<button class="ce-btn-ghost w-full px-2 py-1 text-xs text-red-400" data-act="publish-x-retry"
                    title="${esc(job.error || '')}">❌ Failed — Retry</button>`;
  }
  const pct = job.status === 'uploading' && job.progress_pct != null ? ` ${job.progress_pct}%` : '';
  return `<div class="w-full px-2 py-1 text-xs text-center text-gray-400">${esc((_PUB_JOB_LABELS[job.status] || 'Working…') + pct)}</div>`;
}

async function generateLibraryImage() {
  const input = document.getElementById('lib-img-prompt');
  const btn = document.getElementById('lib-img-generate-btn');
  const status = document.getElementById('lib-img-status');
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  const prompt = (input.value || '').trim();
  if (prompt.length < 3) return setStatus('Describe the image in at least 3 characters.');

  btn.disabled = true; setStatus('Generating…');
  try {
    const res = await apiFetch(`${API}/api/media/images`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const msg = err.detail || `Server error ${res.status}`;
      // The two guard messages both point at the same fix; send the user there
      // instead of leaving them staring at an error with nowhere to go.
      if (res.status === 400 && /provider|model/i.test(msg)) {
        needKey('Set up image generation', msg, 'keys');
        status.classList.add('hidden');
        return;
      }
      setStatus('❌ ' + msg);
      return;
    }
    input.value = '';
    status.classList.add('hidden');
    await loadLibrary('image');
  } catch (e) { setStatus('❌ ' + e.message); }
  finally { btn.disabled = false; }
}

async function uploadToLibrary(event, kind) {
  const files = Array.from(event.target.files || []);
  event.target.value = '';    // same file can be picked again later
  if (!files.length) return;
  const form = new FormData();
  files.forEach(f => form.append('files', f));
  try {
    const res = await apiFetch(`${API}/api/media/uploads`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Upload failed');
    }
    await loadLibrary(kind);
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

async function deleteLibraryAsset(id, kind) {
  if (!confirm('Delete this from your library? This cannot be undone.')) return;
  try {
    const res = await apiFetch(`${API}/api/media/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!res.ok && res.status !== 204) throw new Error('Delete failed');
    await loadLibrary(kind);
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

// ===== VIDEO GENERATION (Kling) =====

async function loadVideoModels() {
  const sel = document.getElementById('lib-vid-model');
  if (sel.dataset.loaded) return;   // populate once; the catalogue doesn't change mid-session
  try {
    const res = await apiFetch(`${API}/api/models/providers`);
    const body = res.ok ? await res.json() : {};
    S.videoModels = (body.video && body.video[0] && body.video[0].models) || [];
    sel.innerHTML = S.videoModels.map(m =>
      `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
    sel.dataset.loaded = '1';
    updateVideoCostEstimate();
  } catch { /* the Generate call itself will surface a clearer error */ }
}

function updateVideoCostEstimate() {
  const el = document.getElementById('lib-vid-cost');
  if (!el) return;
  const modelId = document.getElementById('lib-vid-model').value;
  const seconds = Number(document.getElementById('lib-vid-duration').value) || 5;
  const model = (S.videoModels || []).find(m => m.id === modelId);
  el.textContent = model && model.price_per_sec
    ? `~$${(seconds * model.price_per_sec).toFixed(2)} (Kling, indicative)`
    : '';
}

async function suggestVideoIdea() {
  const btn = document.getElementById('lib-vid-idea-btn');
  const promptEl = document.getElementById('lib-vid-prompt');
  btn.disabled = true;
  try {
    const res = await apiFetch(`${API}/api/media/videos/suggest-idea`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Could not suggest an idea');
    }
    promptEl.value = (await res.json()).prompt;
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { btn.disabled = false; }
}

async function generateLibraryVideo() {
  const promptEl = document.getElementById('lib-vid-prompt');
  const btn = document.getElementById('lib-vid-generate-btn');
  const status = document.getElementById('lib-vid-status');
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  const prompt = (promptEl.value || '').trim();
  if (prompt.length < 3) return setStatus('Describe the video in at least 3 characters.');

  const body = {
    prompt,
    duration_sec: Number(document.getElementById('lib-vid-duration').value) || 5,
    aspect_ratio: document.getElementById('lib-vid-aspect').value,
  };
  const modelId = document.getElementById('lib-vid-model').value;
  if (modelId) body.model = modelId;
  if (S.videoSeedImageId) body.image_asset_id = S.videoSeedImageId;

  btn.disabled = true; setStatus('Starting…');
  try {
    const res = await apiFetch(`${API}/api/media/videos`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const msg = err.detail || `Server error ${res.status}`;
      if (res.status === 400 && /kling|key/i.test(msg)) {
        needKey('Set up video generation', msg, 'keys');
        status.classList.add('hidden');
        return;
      }
      setStatus('❌ ' + msg);
      return;
    }
    promptEl.value = '';
    clearVideoSeed();
    status.classList.add('hidden');
    await loadLibrary('video');
    startVideoPolling();
  } catch (e) { setStatus('❌ ' + e.message); }
  finally { btn.disabled = false; }
}

// The provider takes minutes, so the grid has to refresh itself rather than
// leave a "Generating…" card stuck until the user happens to reload the tab.
// Stops itself once nothing in the last-rendered list is still waiting.
let _videoPollTimer = null;
let _videoPollSignature = null;

function startVideoPolling() {
  if (_videoPollTimer) return;
  _videoPollTimer = setInterval(async () => {
    if (!videoGridVisible()) { stopVideoPolling(); return; }
    const res = await apiFetch(`${API}/api/media?kind=video`).catch(() => null);
    if (!res || !res.ok) return;
    const assets = await res.json();
    // Re-rendering unconditionally would tear down and restart any <video>
    // already playing in the grid every tick — only redraw when something
    // about the list actually changed.
    const signature = assets.map(a => `${a.id}:${a.status}`).join('|');
    if (signature !== _videoPollSignature) {
      _videoPollSignature = signature;
      renderLibraryGrid('video', assets);
    }
    if (!assets.some(a => a.status === 'pending' || a.status === 'running')) stopVideoPolling();
  }, 5000);
}

function stopVideoPolling() {
  if (_videoPollTimer) { clearInterval(_videoPollTimer); _videoPollTimer = null; }
  _videoPollSignature = null;
}

// ===== LIBRARY PICKER (used from the composer: replace a slide, or attach a Reel) =====

async function openLibraryPicker(mode) {
  S.libraryPickerMode = mode;   // 'slide' | 'reel' | 'video-seed' | 'edit-clip'
  const kind = (mode === 'reel' || mode === 'edit-clip') ? 'video' : 'image';
  document.getElementById('library-picker-title').textContent =
    mode === 'reel' ? 'Choose a video from your library'
    : mode === 'video-seed' ? 'Choose a photo to animate'
    : mode === 'edit-clip' ? 'Choose a clip to add'
    : 'Choose a photo from your library';
  const grid = document.getElementById('library-picker-grid');
  grid.innerHTML = 'Loading…';
  document.getElementById('library-picker-modal').classList.remove('hidden');
  onModalOpen('library-picker-modal');
  try {
    const res = await apiFetch(`${API}/api/media?kind=${kind}`);
    const assets = (res.ok ? await res.json() : []).filter(a => a.status === 'ready');
    if (!assets.length) {
      grid.innerHTML = `<span class="text-gray-500 text-sm col-span-full">Nothing in your ${kind === 'video' ? 'Video' : 'Photos'} library yet.</span>`;
      return;
    }
    grid.innerHTML = '';
    assets.forEach(a => {
      const btn = document.createElement('button');
      btn.className = 'rounded-lg overflow-hidden border-2 border-gray-700 hover:border-purple-500 transition';
      btn.title = a.title || a.prompt || '';
      btn.innerHTML = kind === 'video'
        ? `<div class="w-full aspect-square bg-black flex items-center justify-center text-2xl">▶</div>`
        : `<img src="${API}${a.url}" class="w-full aspect-square object-cover" />`;
      btn.onclick = () => pickLibraryAsset(a.id, a);
      grid.appendChild(btn);
    });
  } catch { grid.innerHTML = '<span class="text-red-400 text-sm">Couldn\'t load your library.</span>'; }
}

function closeLibraryPicker() {
  document.getElementById('library-picker-modal').classList.add('hidden');
  onModalClose();
}

async function pickLibraryAsset(assetId, asset) {
  const mode = S.libraryPickerMode;
  try {
    if (mode === 'video-seed') {
      // Just remembers the choice — no insert to make, nothing to attach yet.
      // generateLibraryVideo() reads S.videoSeedImageId when it actually posts.
      S.videoSeedImageId = assetId;
      const thumb = document.getElementById('lib-vid-seed-thumb');
      if (thumb && asset) thumb.src = API + asset.url;
      document.getElementById('lib-vid-seed-preview')?.classList.remove('hidden');
      return;
    }
    if (mode === 'edit-clip') {
      // Just appends to the editor's in-memory clip list — nothing to attach
      // yet, submitEditVideo() sends the whole list on save.
      S.editVideoClips.push({ asset_id: assetId, trim_start_sec: 0, trim_end_sec: null,
                              title: (asset && asset.title) || '', url: asset && asset.url });
      renderEditVideoClipsList();
      return;
    }
    if (mode === 'reel') {
      const res = await apiFetch(`${API}/api/posts/${S.postId}/reel/from-library`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_id: assetId }),
      });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed'); }
      const data = await res.json();
      const url = API + data.video_url;
      document.getElementById('reel-video').src = url;
      document.getElementById('reel-download').href = url;
      document.getElementById('reel-preview').classList.remove('hidden');
      toast('✅ Video attached', 'success');
    } else {
      const res = await apiFetch(`${API}/api/posts/${S.postId}/slides/${S.editingSlide}/from-library`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_id: assetId }),
      });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed'); }
      const updated = await res.json();
      refreshSlideImage(updated);
      closeEditSlide();
      toast('✅ Slide updated', 'success');
    }
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { closeLibraryPicker(); }
}

function clearVideoSeed() {
  S.videoSeedImageId = null;
  document.getElementById('lib-vid-seed-preview')?.classList.add('hidden');
}

// ===== EDIT VIDEO MODAL (Phase 6: trim/reframe/concat + voiceover/music/cover) =====

function openEditVideoModal(asset) {
  S.editVideoAnchorId = asset.id;   // the clip that was clicked — always the ownership anchor URL
  S.editVideoClips = [{ asset_id: asset.id, trim_start_sec: 0, trim_end_sec: null,
                        title: asset.title || '', url: asset.url }];
  document.getElementById('edit-video-music').checked = false;
  document.getElementById('edit-video-cover').checked = false;
  document.getElementById('edit-video-voiceover').checked = false;
  document.getElementById('edit-video-script').value = '';
  document.getElementById('edit-video-voice-id').value = '';
  toggleEditVideoVoiceoverFields();
  renderEditVideoClipsList();
  document.getElementById('edit-video-status').classList.add('hidden');
  document.getElementById('edit-video-modal').classList.remove('hidden');
  onModalOpen('edit-video-modal');
}

function closeEditVideoModal() {
  document.getElementById('edit-video-modal').classList.add('hidden');
  onModalClose();
}

function toggleEditVideoVoiceoverFields() {
  const on = document.getElementById('edit-video-voiceover').checked;
  document.getElementById('edit-video-voiceover-fields').classList.toggle('hidden', !on);
}

function renderEditVideoClipsList() {
  const box = document.getElementById('edit-video-clips');
  box.innerHTML = '';
  S.editVideoClips.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'ce-card p-2 flex items-center gap-2';
    row.innerHTML = `
      <video src="${API}${c.url}" muted preload="metadata" class="w-14 h-14 object-cover rounded bg-black flex-shrink-0"></video>
      <div class="flex-1 min-w-0">
        <div class="text-xs text-gray-400 truncate">${esc(c.title || 'Clip')}</div>
        <div class="flex gap-2 mt-1 text-xs items-center">
          <label class="text-gray-500">In<input type="number" min="0" step="0.1" value="${c.trim_start_sec}" class="ce-input w-16 px-1 py-0.5 ml-1" data-field="trim_start_sec" /></label>
          <label class="text-gray-500">Out<input type="number" min="0" step="0.1" value="${c.trim_end_sec ?? ''}" placeholder="end" class="ce-input w-16 px-1 py-0.5 ml-1" data-field="trim_end_sec" /></label>
        </div>
      </div>
      <div class="flex flex-col gap-1">
        <button data-act="up" class="ce-btn-ghost px-1.5 text-xs" ${i === 0 ? 'disabled' : ''}>▲</button>
        <button data-act="down" class="ce-btn-ghost px-1.5 text-xs" ${i === S.editVideoClips.length - 1 ? 'disabled' : ''}>▼</button>
        <button data-act="remove" class="ce-btn-ghost px-1.5 text-xs text-red-400">✕</button>
      </div>
    `;
    row.querySelector('[data-field="trim_start_sec"]').onchange = (e) => {
      c.trim_start_sec = Number(e.target.value) || 0;
    };
    row.querySelector('[data-field="trim_end_sec"]').onchange = (e) => {
      const v = e.target.value;
      c.trim_end_sec = v === '' ? null : Number(v);
    };
    row.querySelector('[data-act="up"]').onclick = () => moveEditVideoClip(i, -1);
    row.querySelector('[data-act="down"]').onclick = () => moveEditVideoClip(i, 1);
    row.querySelector('[data-act="remove"]').onclick = () => removeEditVideoClip(i);
    box.appendChild(row);
  });
  updateEditVideoTransitionsAvailability();
}

function moveEditVideoClip(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= S.editVideoClips.length) return;
  [S.editVideoClips[i], S.editVideoClips[j]] = [S.editVideoClips[j], S.editVideoClips[i]];
  renderEditVideoClipsList();
}

function removeEditVideoClip(i) {
  S.editVideoClips.splice(i, 1);
  renderEditVideoClipsList();
}

function updateEditVideoTransitionsAvailability() {
  const cb = document.getElementById('edit-video-transitions');
  const canUse = S.editVideoClips.length >= 2;
  cb.disabled = !canUse;
  if (!canUse) cb.checked = false;
}

async function submitEditVideo() {
  const btn = document.getElementById('edit-video-submit-btn');
  const status = document.getElementById('edit-video-status');
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  if (!S.editVideoClips.length) return setStatus('Add at least one clip.');

  const voiceover = document.getElementById('edit-video-voiceover').checked;
  const script = document.getElementById('edit-video-script').value.trim();
  if (voiceover && script.length < 1) return setStatus('Write a voiceover script first.');

  const body = {
    clips: S.editVideoClips.map(c => ({
      asset_id: c.asset_id, trim_start_sec: c.trim_start_sec, trim_end_sec: c.trim_end_sec,
    })),
    transitions: document.getElementById('edit-video-transitions').checked,
    voiceover,
    music: document.getElementById('edit-video-music').checked,
    cover: document.getElementById('edit-video-cover').checked,
  };
  if (voiceover) {
    body.voiceover_script = script;
    const voiceId = (document.getElementById('edit-video-voice-id').value || '').trim();
    if (voiceId) body.voice_id = voiceId;
  }

  btn.disabled = true; setStatus('Rendering…');
  try {
    const res = await apiFetch(`${API}/api/media/${encodeURIComponent(S.editVideoAnchorId)}/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const msg = err.detail || `Server error ${res.status}`;
      if (res.status === 400 && /elevenlabs/i.test(msg)) {
        needKey('Set up voiceover', msg, 'keys');
        status.classList.add('hidden');
        return;
      }
      setStatus('❌ ' + msg);
      return;
    }
    closeEditVideoModal();
    toast('✅ Video saved', 'success');
    await loadLibrary('video');
  } catch (e) { setStatus('❌ ' + e.message); }
  finally { btn.disabled = false; }
}

// ===== PUBLISH TO X (Phase 8: a standalone library clip, no post) =====

function openPublishToXModal(asset) {
  S.publishXAssetId = asset.id;
  document.getElementById('publish-x-text').value = '';
  document.getElementById('publish-x-alt').value = '';
  document.getElementById('publish-x-status').classList.add('hidden');
  syncPublishXCounter();
  document.getElementById('publish-x-modal').classList.remove('hidden');
  onModalOpen('publish-x-modal');
}

function closePublishToXModal() {
  document.getElementById('publish-x-modal').classList.add('hidden');
  onModalClose();
}

function syncPublishXCounter() {
  const n = tweetLength(document.getElementById('publish-x-text').value);
  const el = document.getElementById('publish-x-counter');
  el.textContent = `${n}/${X_TWEET_LIMIT}`;
  el.className = 'text-xs ' + (n > X_TWEET_LIMIT ? 'text-red-400' : 'text-gray-500');
}

async function submitPublishToX() {
  const btn = document.getElementById('publish-x-submit-btn');
  const status = document.getElementById('publish-x-status');
  const setStatus = (m) => { status.textContent = m; status.classList.remove('hidden'); };
  const text = document.getElementById('publish-x-text').value.trim();
  if (!text) return setStatus('Write something to post with the video.');
  const alt = document.getElementById('publish-x-alt').value.trim();

  btn.disabled = true; setStatus('Queuing…');
  try {
    const res = await apiFetch(`${API}/api/media/${encodeURIComponent(S.publishXAssetId)}/publish-x`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, alt_text: alt || null }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const msg = err.detail || `Server error ${res.status}`;
      if (res.status === 400 && /credential|X \(Twitter\)/i.test(msg)) {
        needKey('Connect your X account', msg, 'connections');
        status.classList.add('hidden');
        return;
      }
      setStatus('❌ ' + msg);
      return;
    }
    const data = await res.json();
    closePublishToXModal();
    if (data.warning) toast('⚠ ' + data.warning, 'warn');
    else toast('📤 Queued — uploading to X…', 'info');
    S.publishJobs[data.asset_id] = data;
    await loadLibrary('video');
    startPublishJobPolling();
  } catch (e) { setStatus('❌ ' + e.message); }
  finally { btn.disabled = false; }
}

// Job status is polled from one shared endpoint and indexed by whichever of
// asset_id/post_id the job carries, so both the library grid (asset_id) and
// the composer's Reel card (post_id) read from the same S.publishJobs map.
const _PUB_JOB_ACTIVE = new Set(['queued', 'uploading', 'processing', 'tweeting']);
let _pubJobTimer = null;
let _pubJobSignature = null;

function startPublishJobPolling() {
  if (_pubJobTimer) return;
  const tick = async () => {
    const res = await apiFetch(`${API}/api/publish-jobs`).catch(() => null);
    if (!res || !res.ok) return;
    const jobs = await res.json();
    const sig = jobs.map(j => `${j.id}:${j.status}:${j.progress_pct}`).join('|');
    S.publishJobs = {};
    jobs.forEach(j => { S.publishJobs[j.asset_id || j.post_id] = j; });
    if (sig !== _pubJobSignature) {
      _pubJobSignature = sig;
      // Only reload the grid when there's an actual job to reflect — an empty
      // list (the common case) has nothing new to show, and reloading anyway
      // races the grid's own independent load right after entering the tab.
      if (jobs.length && videoGridVisible()) loadLibrary('video');
      updatePostEditorChrome();
    }
    if (!jobs.some(j => _PUB_JOB_ACTIVE.has(j.status))) stopPublishJobPolling();
  };
  tick();   // don't make the user wait a full interval to see an already-running job
  _pubJobTimer = setInterval(tick, 5000);
}

function stopPublishJobPolling() {
  if (_pubJobTimer) { clearInterval(_pubJobTimer); _pubJobTimer = null; }
  _pubJobSignature = null;
}

// ===== GRID MODE (responsive preview) =====
function setGridMode(mode) {
  S.gridMode = mode;
  document.getElementById('grid-frame').style.maxWidth = mode === 'mobile' ? '430px' : '100%';
  document.querySelectorAll('.grid-mode-btn').forEach(b => {
    const active = b.dataset.gridMode === mode;
    b.classList.toggle('border-purple-500', active);
    b.classList.toggle('bg-purple-900', active);
    b.classList.toggle('text-white', active);
    b.classList.toggle('border-gray-700', !active);
    b.classList.toggle('bg-gray-800', !active);
    b.classList.toggle('text-gray-300', !active);
  });
}

// ===== COST BADGE =====
async function refreshCost() {
  try {
    const res = await apiFetch(`${API}/api/usage`);
    if (!res.ok) return;
    const d = await res.json();
    S.usage = d;
    const badge = document.getElementById('cost-badge');
    badge.textContent = `$${(d.today.cost || 0).toFixed(2)}`;
    badge.classList.remove('hidden');
    const limit = parseFloat(localStorage.getItem('cost_limit') || '5');
    badge.classList.toggle('text-red-300', d.today.cost > limit);
    renderFreeLeft();
  } catch(e) { /* ignore */ }
}

/** "3 of 5 free posts left", or nothing at all.
 *
 *  The server decides whether the subject applies (`free` is null when it does
 *  not) rather than the client inferring it from whether a key is set: the same
 *  answer has to serve the wall below and the counter here, and two readings of
 *  "do they have a key" would eventually disagree — showing a count next to a
 *  button the server refuses, or a wall in front of somebody it would serve. */
async function refreshFreeLeft() {
  await refreshCost();          // one endpoint carries both, so one call refreshes both
  return (S.usage && S.usage.free) || null;
}

function renderFreeLeft() {
  const el = document.getElementById('free-left');
  if (!el) return;
  const free = S.usage && S.usage.free;
  if (!free) { el.classList.add('hidden'); return; }
  el.textContent = free.remaining > 0
    ? `${free.remaining} of ${free.limit} free posts left — no API key needed yet`
    : 'Free posts used up — add your own AI key to keep going';
  el.classList.remove('hidden');
}
// ===== MILESTONES (UX phase 8) =====
//
// What the product has already shown this person. Read once at start-up and
// re-read after the one action that can create one mid-session, rather than
// polled: these are events, not a counter, and they only ever accumulate.
S.milestones = {};

async function loadMilestones() {
  try {
    const res = await apiFetch(`${API}/api/settings/milestones`);
    S.milestones = res.ok ? ((await res.json()).milestones || {}) : {};
  } catch { S.milestones = {}; }
  renderMilestoneGates();
}

/** Everything whose appearance hangs on a milestone, in one place.
 *
 *  A failed read leaves the gates shut. That is the safe direction: a tab that
 *  turns up next reload costs a shrug, and the alternative — defaulting open on
 *  a network blip — would make the whole phase look like it fires at random. */
function renderMilestoneGates() {
  renderVoiceHint();
  renderJournalTab();
  renderTeamTab();
}

/** Team is a screen about a second person, and it appears when there is a
 *  second anything: a second brand profile to hand over, or an invitation
 *  already sent. The milestone is what makes it stay — deleting the second
 *  profile must not take away a screen somebody has been using. */
function renderTeamTab() {
  const el = document.querySelector('#settings-tabs [data-settings-tab="team"]');
  if (!el) return;
  const many = (S.accounts || []).length > 1;
  // Agency too, not just the count. The milestone means "the Team screen
  // appeared", and it does not appear for a creator however many brands they
  // keep — recording it there would write a history that never happened, and it
  // would hand them the screen the day they ever became an agency.
  const agency = !!(S.user && S.user.account_type === 'agency');
  if (agency && many && !S.milestones['team_unlocked']) noteMilestone('team_unlocked');
  el.style.display = (many || S.milestones['team_unlocked']) ? '' : 'none';
}

/** Record one milestone, optimistically. The screen changes now; the write is
 *  what carries the change to the next machine. */
async function noteMilestone(name) {
  S.milestones[name] = new Date().toISOString();
  try {
    await apiFetch(`${API}/api/settings/milestones/${encodeURIComponent(name)}`,
                   {method: 'POST'});
  } catch {}
}

/** The price of hiding anything at all, paid up front.
 *
 *  Somebody who saw a feature on a colleague's screen and cannot find it goes
 *  to support rather than to the product. This reveals the gated FEATURES only
 *  — the server decides which those are, and deliberately does not record that
 *  this person rewrote a caption or was told something once. */
async function showAllFeatures() {
  closeAvatarMenu();
  try {
    const res = await apiFetch(`${API}/api/settings/milestones-all`, {method: 'POST'});
    if (res.ok) S.milestones = (await res.json()).milestones || S.milestones;
  } catch {}
  renderMilestoneGates();
  toast('Everything is showing now', 'success');
}

/** "Would you like topics to find themselves?" — once, to a creator who has
 *  made enough posts for keeping the queue full to have become work.
 *
 *  Sources are workspace-scoped and behind require_business, so this cannot be
 *  a tab quietly appearing: the capability lives in the other product. The
 *  offer says that plainly and leaves the switch to them.
 *
 *  Counted by asking for six posts rather than by storing a number — phase 8.0
 *  keeps counts out of the milestones for good reason, and a second count would
 *  eventually disagree with the posts table. Nobody who has already been asked
 *  pays for the request at all. */
async function maybeOfferSources() {
  const el = document.getElementById('sources-offer');
  if (!el) return;
  el.classList.add('hidden');
  if (!S.user || S.user.is_local) return;
  if (S.user.account_type === 'business') return;    // sources are their first screen
  if (S.milestones['sources_offered']) return;
  const rows = await fetchPosts({ limit: SOURCES_OFFER_AFTER + 1 });
  if (rows.length < SOURCES_OFFER_AFTER) return;
  el.classList.remove('hidden');
  noteMilestone('sources_offered');
}

const SOURCES_OFFER_AFTER = 5;

async function takeSourcesOffer() {
  if (!confirm('Business mode changes how the product works: posts start from '
             + 'links you watch, and each draft waits for a person to approve it. '
             + 'You can switch back at any time.\n\nSwitch now?')) return;
  const sel = document.getElementById('product-switcher');
  if (!sel) return;
  sel.value = 'business';
  await onProductSwitch();
}

/** The Journal is the approval record, and it appears once there is one.
 *
 *  The UX table says "after the tenth published post", but the journal is
 *  written at sign-off, not at publication — a workspace can approve nine posts
 *  and publish none. Counting publications would therefore hide a non-empty
 *  audit trail, together with the export button that turns it into the report a
 *  client gets. Following the feature's own content asks for nothing and hides
 *  nothing. */
function renderJournalTab() {
  const el = document.querySelector('#results-tabs [data-results-tab="journal"]');
  if (!el) return;
  el.style.display = S.milestones['journal_unlocked'] ? '' : 'none';
}

/** Offer the setting that steers how the AI writes, once its words have been
 *  rewritten and until the offer is waved away.
 *
 *  Voice, not Business's brand rules. Rules are forbidden phrases and required
 *  disclaimers, checked at approval against a workspace a creator has not got —
 *  and they change nothing about how a post gets written. Voice does. */
function renderVoiceHint() {
  const el = document.getElementById('voice-hint');
  if (!el) return;
  const show = !!S.milestones['edited_ai_text'] && !S.milestones['rules_hint_dismissed'];
  el.classList.toggle('hidden', !show);
}

function openBrandVoice() {
  openSettings('profiles');
  const el = document.getElementById('brand-voice-section');
  if (el) el.scrollIntoView({behavior: 'smooth', block: 'center'});
}

async function dismissVoiceHint() {
  // Optimistic: the hint goes now, and the record is what keeps it gone on the
  // next machine. A failed write costs one more sighting, not a stuck banner.
  noteMilestone('rules_hint_dismissed');
  renderVoiceHint();
}

function showCostPopover() {
  const pop = document.getElementById('cost-popover');
  if (!pop.classList.contains('hidden')) { pop.classList.add('hidden'); return; }
  const d = S.usage || {today:{},month:{},by_model:[]};
  let html = `<div class="bg-black/30 rounded-lg p-3 space-y-1">
    <div>Today: <b>$${(d.today.cost||0).toFixed(4)}</b> · ${d.today.calls||0} calls · ${d.today.tokens||0} tokens</div>
    <div>Month: <b>$${(d.month.cost||0).toFixed(4)}</b> · ${d.month.calls||0} calls</div>`;
  if ((d.by_model||[]).length) {
    html += '<div class="pt-1 text-gray-400">By model (month):</div>';
    d.by_model.forEach(m => { html += `<div>${m.model}: $${m.cost.toFixed(4)} (${m.calls})</div>`; });
  }
  html += '</div>';
  pop.innerHTML = html;
  pop.classList.remove('hidden');
}

// ===== ADMIN / BACKUP =====
// ===== ONBOARDING: missing-key guards =====
// Which stored keys each network needs before it can publish.
const NETWORK_KEYS = {
  instagram: ['instagram_access_token', 'instagram_user_id', 'imgbb_api_key'],
  x: ['x_api_key', 'x_api_secret', 'x_access_token', 'x_access_token_secret'],
};

// Cache the "is this key set?" map so generate/publish can check without a
// round-trip each time. Local desktop keys come from .env, so nothing to guard.
async function ensureCreds(force) {
  if (!S.user || S.user.is_local) return {};       // local → keys from .env
  if (S.creds && !force) return S.creds;
  try {
    const res = await apiFetch(`${API}/api/settings/credentials`);
    S.creds = res.ok ? await res.json() : {};
  } catch { S.creds = {}; }
  return S.creds;
}
function hasKey(key) {
  if (S.user && S.user.is_local) return true;      // .env-backed
  return !!(S.creds && S.creds[key] && S.creds[key].set);
}

//: A Settings TAB, not a section — which is the whole bug this name records.
//: `gotoNeedKey` used to hand it to setSection, whose map knows five section
//: names and none of them is 'keys'; every view hid and the page went blank one
//: click after the product had just explained what was missing.
let _needKeyTab = 'keys';
function needKey(title, msg, tab) {
  _needKeyTab = tab;
  document.getElementById('need-key-title').textContent = title;
  document.getElementById('need-key-msg').textContent = msg;
  document.getElementById('need-key-modal').classList.remove('hidden');
  onModalOpen('need-key-modal');
}
function closeNeedKey() {
  document.getElementById('need-key-modal').classList.add('hidden');
  onModalClose();
}
function gotoNeedKey() { closeNeedKey(); openSettings(_needKeyTab); }

// True if OK to generate; otherwise shows the onboarding modal and returns false.
// Generation needs a text provider + model + that provider's key. The image
// provider is only required when a slide is actually generated by AI.
//
// Until UX phase 6.4 this was an unconditional wall: no key, no generation,
// which made the product's answer to "show me what you do" a form. An account
// with free posts left is now let through, and the wall arrives when they run
// out — which is the moment the question is finally worth asking.
async function guardGenerateKeys() {
  if (!S.user || S.user.is_local) return true;   // desktop → keys from .env
  let ai = S.ai;
  try {
    const r = await apiFetch(`${API}/api/settings/ai`);
    if (r.ok) { ai = await r.json(); S.ai = ai; }
  } catch { /* 401 handled by apiFetch */ }

  const needs = (kind) => {
    const provider = ai[`${kind}_provider`], model = ai[`${kind}_model`];
    if (!provider || !model) return 'choose';
    return (ai.keys && ai.keys[provider] && ai.keys[provider].set) ? null : 'key';
  };

  // Freshly read, not from the last poll: the header refreshes on a timer, and
  // between two ticks somebody can spend their last post in another tab. Being
  // let through on a stale count means a 409 in place of the modal that says
  // what to do about it.
  const free = await refreshFreeLeft();

  const text = needs('text');
  if (text && !(free && free.remaining > 0)) {
    if (free) {
      needKey('Your free posts are used up',
        `You've used all ${free.limit}. Add your own AI key under Settings → `
        + 'Keys & spend to keep generating — you pay the model vendor directly.',
        'keys');
      return false;
    }
    needKey('Set up your AI model',
      text === 'choose'
        ? 'Pick who writes your posts — provider and model — under Settings → Keys & spend.'
        : 'Add the API key for your chosen text provider under Settings → Keys & spend.',
      'keys');
    return false;
  }
  if (S.source === 'ai_gen' && needs('image') && !(free && free.remaining > 0)) {
    needKey('Set up image generation',
      'AI images need an image provider, model and key under Settings → Keys & spend. '
      + 'Or switch the image source to Stock photos.',
      'keys');
    return false;
  }
  return true;
}

// True if OK to publish to `network`; otherwise shows the modal and returns false.
async function guardPublishKeys(network) {
  if (!S.user || S.user.is_local) return true;
  await ensureCreds();
  const missing = (NETWORK_KEYS[network] || []).filter(k => !hasKey(k));
  if (missing.length) {
    const label = network === 'x' ? 'X' : 'Instagram';
    needKey(`Connect ${label}`,
      `Publishing to ${label} needs your ${label} keys. Add them under ${label} keys, then try again.`
      // Somebody running two brands is about to wonder whether they have to
      // connect this network once per profile. They do not, and this is the
      // moment they are asking.
      + ((S.accounts || []).length > 1
         ? ' Connections belong to your account, so connecting once covers every brand profile.'
         : ''),
      'keys');
    return false;
  }
  return true;
}

/** Open Settings on a tab. The one entry point — nothing assigns S.settingsTab
 *  directly, so the panels and the tab strip cannot disagree. */
function openSettings(tab) {
  S.settingsTab = tab || 'profiles';
  setSection('settings');
}

/** Show exactly the panels belonging to the active tab.
 *
 *  The list is READ OFF THE DOM rather than kept here, for the same reason
 *  setSection derives its view list from its map: a panel added to the markup
 *  and forgotten in a hand-written hide list is a panel that stays on screen
 *  under every tab. A section may name more than one tab — the credentials
 *  block is on two, with only its scope differing. */
function renderSettingsTabs() {
  const tab = S.settingsTab || 'profiles';
  document.querySelectorAll('#settings-tabs .set-btn').forEach(b =>
    b.classList.toggle('set-active', b.dataset.settingsTab === tab));
  document.querySelectorAll('#view-settings section[data-settings-tab]').forEach(el =>
    el.classList.toggle('hidden', !el.dataset.settingsTab.split(' ').includes(tab)));
}

async function loadAdmin() {
  renderUserChrome();
  renderSettingsTabs();
  // Which tab is open decides what the credentials block means: "connections"
  // = the publishing keys for every network, "keys" = the account-wide ones
  // (OpenRouter, stock, ElevenLabs, Kling) plus spend, backup and GDPR.
  const tab = S.settingsTab || 'profiles';
  // Sources and Brand rules moved in from the top level in 3.8. They share
  // nothing with the credential blocks below, so they answer here instead of
  // falling through a preamble that is entirely about keys and brand identity.
  if (tab === 'sources') return loadSources();
  if (tab === 'team') return loadTeam();
  if (tab === 'rules') return loadBrandRules();
  const account = tab === 'keys';
  const keysH = document.getElementById('keys-title');
  if (keysH) keysH.textContent = account ? 'Account keys' : 'Connections';
  // The tab already hid everything that isn't its own; these only narrow it
  // further, for reasons about the ACCOUNT rather than about the tab.
  document.getElementById('backup-section').classList.toggle('hidden', !(account && S.user && (S.user.is_admin || S.user.is_local)));
  // Export and erasure are about a cloud tenancy; local mode's data is already
  // on the user's own disk and has no account to delete.
  document.getElementById('mydata-section').classList.toggle('hidden', !(account && S.user && !S.user.is_local));
  if (account) {
    await refreshCost();
    const d = S.usage || {today:{},month:{}};
    document.getElementById('admin-usage').innerHTML =
      `LLM spend — today <b>$${(d.today?.cost||0).toFixed(4)}</b>, this month <b>$${(d.month?.cost||0).toFixed(4)}</b> (${d.month?.calls||0} calls)`;
  }
  const keysSec = document.getElementById('keys-section');
  if (S.user && !S.user.is_local) {
    keysSec.classList.remove('hidden');
    // Connections shows every network at once. There is no active network to
    // scope by since the rail went, and scoping to one would hide half of
    // somebody's credentials with nothing on screen to say so.
    const scope = account ? 'account' : 'publishing';
    await loadCredentials(scope);
    // Health is about publishing, so the account-only tab has nothing to show.
    await loadConnections(account ? [] : ['instagram', 'x']);
  }
  else keysSec.classList.add('hidden');
  // One Test button per network, on Connections only. The single "Test
  // connection" button tested whichever network the rail pointed at, and there
  // is no such thing to point at any more.
  const canTest = !account && S.user && !S.user.is_local;
  const testBtn = document.getElementById('test-conn-btn');
  if (testBtn) testBtn.classList.add('hidden');
  const txBtn = document.getElementById('test-x-btn');
  if (txBtn) txBtn.classList.toggle('hidden', !canTest);
  const tigBtn = document.getElementById('test-ig-btn');
  if (tigBtn) tigBtn.classList.toggle('hidden', !canTest);
  const testRes = document.getElementById('test-conn-result');
  if (testRes) testRes.textContent = '';
  // Brand voice lives on the Account page for cloud users.
  // Brand identity is a cloud-tenancy notion — the desktop owner has no other
  // brand to keep theirs apart from. The TAB decides what is on screen; this
  // only decides what is worth fetching.
  if (!(S.user && !S.user.is_local)) {
    ['brand-voice-section', 'brand-profile-section', 'x-settings-section',
     'slide-style-section', 'ai-models-section'].forEach(id =>
      document.getElementById(id).classList.add('hidden'));
    return;
  }
  if (tab === 'profiles') {
    await loadBrandVoice();
    await loadProfile();
    await loadSlideStyle();
  } else if (tab === 'connections') {
    await loadXSettings();
  } else if (tab === 'keys') {
    await loadAISettings();
  }
}

// ===== PER-USER API KEYS ===== (scope: 'account' | 'instagram' | 'x')
const CRED_FIELDS = [
  // AI provider keys live in the "AI models" section, next to the provider they
  // belong to — listing them here too would be two places to set one thing.
  { key: 'unsplash_access_key',     label: 'Unsplash access key',     scope: 'account',   group: 'Stock photos (optional)' },
  { key: 'pexels_api_key',          label: 'Pexels API key',          scope: 'account',   group: 'Stock photos (optional)' },
  { key: 'elevenlabs_api_key',      label: 'ElevenLabs API key',      scope: 'account',   group: 'Voiceover Reels (optional)' },
  { key: 'kling_api_key',           label: 'Kling API key',           scope: 'account',   group: 'AI video (optional)', hint: 'Billed per second by Kling once video generation is live — a ~10s clip runs roughly $0.75.' },
  { key: 'instagram_access_token',  label: 'Instagram access token',  scope: 'instagram', group: 'Instagram publishing', hint: 'Instagram-Login token with content-publish permission' },
  { key: 'instagram_user_id',       label: 'Instagram user id',       scope: 'instagram', group: 'Instagram publishing', hint: 'the numeric IG user id' },
  { key: 'imgbb_api_key',           label: 'imgbb API key',           scope: 'instagram', group: 'imgbb — public image hosting for IG', hint: 'free from api.imgbb.com — hosts slide images so IG can fetch them' },
  { key: 'x_api_key',               label: 'X API key',               scope: 'x',         group: 'X / Twitter (OAuth 1.0a)', hint: 'X console: Consumer Key' },
  { key: 'x_api_secret',            label: 'X API secret',            scope: 'x',         group: 'X / Twitter (OAuth 1.0a)', hint: 'X console: Consumer Secret' },
  { key: 'x_access_token',          label: 'X access token',          scope: 'x',         group: 'X / Twitter (OAuth 1.0a)', hint: 'X console: Access Token (must be Read and write)' },
  { key: 'x_access_token_secret',   label: 'X access token secret',   scope: 'x',         group: 'X / Twitter (OAuth 1.0a)', hint: 'X console: Access Token Secret' },
];

async function loadCredentials(scope) {
  let data = {};
  try {
    const res = await apiFetch(`${API}/api/settings/credentials`);
    if (res.ok) data = await res.json();
  } catch { /* 401 handled by apiFetch */ }
  S.creds = data;   // keep the onboarding guard cache in sync with what we render
  // 'publishing' is every network's keys and NOTHING else: the account-wide
  // ones have their own tab, and a key that appears on two tabs is a key
  // somebody edits in one place and looks for in the other.
  const scopes = scope === 'publishing' ? ['instagram', 'x']
    : scope === 'all' ? ['account', 'instagram', 'x'] : [scope || 'account'];
  const fields = CRED_FIELDS.filter(f => scopes.includes(f.scope));
  const noneSet = fields.every(f => !(data[f.key] && data[f.key].set));
  const form = document.getElementById('keys-form');
  let html = '', lastGroup = null;
  if (noneSet) {
    const purpose = scope === 'account' ? 'content generation' : scope === 'all' ? 'posting and content' : 'publishing';
    html += `<div class="text-xs text-gray-400 bg-white/5 border border-white/10 rounded-lg px-3 py-2">
      👋 Add your keys here to unlock ${purpose}.
      Your keys are encrypted and only used for your own posts.</div>`;
  }
  for (const f of fields) {
    if (f.group !== lastGroup) { html += `<div class="text-xs font-semibold text-gray-400 pt-2">${esc(f.group)}</div>`; lastGroup = f.group; }
    const info = data[f.key] || {};
    const ph = info.set ? `set: ${esc(info.masked || '••••')}` : 'not set';
    html += `<input data-cred="${f.key}" type="password" autocomplete="off" aria-label="${esc(f.label)}" placeholder="${esc(f.label)} — ${ph}" class="ce-input w-full px-3 py-2 text-sm" />`;
    if (f.hint) html += `<div class="text-xs text-gray-500 -mt-2 pl-1">${esc(f.hint)}</div>`;
  }
  form.innerHTML = html;
}

// ===== CONNECTION HEALTH + TOKEN EXPIRY =====
const NETWORK_LABEL = { instagram: 'Instagram', x: '𝕏' };

/** Render last-known health per network on the Connections page.
 *  `scopes` mirrors the keys form, so a network-scoped page shows only its own. */
async function loadConnections(scopes) {
  const box = document.getElementById('conn-health');
  const renewBtn = document.getElementById('renew-ig-btn');
  if (!box || !S.user || S.user.is_local) return;
  let conns = {};
  try {
    const res = await apiFetch(`${API}/api/settings/connections`);
    if (res.ok) conns = (await res.json()).connections || {};
  } catch { /* 401 handled by apiFetch */ }
  S.connections = conns;

  const shown = Object.keys(conns).filter(p => scopes.includes(p)).sort();
  box.classList.toggle('hidden', shown.length === 0);
  box.innerHTML = shown.map(p => {
    const c = conns[p] || {};
    const label = esc(NETWORK_LABEL[p] || p);
    const head = c.ok
      ? `<span class="text-green-400">✅ ${label} connected</span>` +
        (c.handle ? ` <span class="text-gray-400">— ${esc(c.handle)}</span>` : '')
      : `<span class="text-red-400">❌ ${label} not working</span>` +
        (c.error ? `<div class="text-xs text-gray-400 mt-0.5">${esc(c.error)}</div>` : '');
    return `<div class="text-sm bg-white/5 border border-white/10 rounded-lg px-3 py-2">
      ${head}${expiryLine(c)}
      <div class="text-xs text-gray-500 mt-0.5">Checked ${c.checked_at ? new Date(c.checked_at).toLocaleString() : 'never'}</div>
    </div>`;
  }).join('');

  // Renewing is Instagram-only, and only worth offering once a token exists.
  if (renewBtn) {
    const igSet = !!(S.creds && S.creds.instagram_access_token && S.creds.instagram_access_token.set);
    renewBtn.classList.toggle('hidden', !(scopes.includes('instagram') && igSet));
  }
  refreshExpiryBanner();
}

/** One line about the token's remaining life — hedged when the date is a guess. */
function expiryLine(c) {
  const d = c.days_left;
  if (d === null || d === undefined) return '';
  const est = c.expires_estimated !== false;   // absent flag ⇒ assume estimate
  const when = d < 0 ? 'expired'
    : d === 0 ? 'expires today'
    : `expires in ${d} day${d === 1 ? '' : 's'}`;
  const tone = d <= 7 ? 'text-yellow-300' : 'text-gray-400';
  const hedge = est ? ' (estimated — press Renew for the exact date)' : '';
  return `<div class="text-xs mt-0.5 ${tone}">Token ${when}${esc(hedge)}</div>`;
}

/** Warn about a token that is about to take scheduled publishing down with it. */
function refreshExpiryBanner() {
  const banner = document.getElementById('expiry-banner');
  if (!banner) return;
  let worst = null;
  for (const [p, c] of Object.entries(S.connections || {})) {
    const d = c && c.days_left;
    if (d === null || d === undefined || d > 7) continue;
    if (!worst || d < worst.days) worst = { platform: p, days: d, est: c.expires_estimated !== false };
  }
  banner.classList.toggle('hidden', !worst);
  if (worst) {
    const label = NETWORK_LABEL[worst.platform] || worst.platform;
    const when = worst.days < 0 ? 'has expired' :
      worst.days === 0 ? 'expires today' : `expires in ${worst.days} days`;
    document.getElementById('expiry-banner-text').textContent =
      `Your ${label} token ${when}${worst.est ? ' (estimated)' : ''} — scheduled posts will stop going out.`;
  }
  layoutBanners();
}

/** Extend the Instagram token. Rotates it, so it stays an explicit user action. */
async function renewInstagramToken() {
  const btn = document.getElementById('renew-ig-btn');
  const out = document.getElementById('test-conn-result');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Renewing…'; }
  if (out) { out.textContent = ''; out.className = 'text-sm'; }
  try {
    const res = await apiFetch(`${API}/api/settings/instagram/refresh-token`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (out) {
      out.textContent = (data.ok ? '✅ ' : '❌ ') + (data.message || (data.ok ? 'Renewed.' : 'Failed.'));
      out.className = 'text-sm ' + (data.ok ? 'text-green-400' : 'text-red-400');
    }
    if (data.ok) await loadConnections(['instagram', 'x']);
  } catch (e) {
    if (out) { out.textContent = '❌ ' + e.message; out.className = 'text-sm text-red-400'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

async function testPublishConnection(network) {
  network = network || S.platform;               // 'x' | 'instagram'
  const btn = document.getElementById('test-conn-btn');
  const out = document.getElementById('test-conn-result');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Testing…'; }
  if (out) { out.textContent = ''; out.className = 'text-sm'; }
  try {
    const res = await apiFetch(`${API}/api/settings/publish/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: network }),
    });
    const data = await res.json().catch(() => ({}));
    if (out) {
      out.textContent = (data.ok ? '✅ ' : '❌ ') + (data.message || (data.ok ? 'Connected.' : 'Failed.'));
      out.className = 'text-sm ' + (data.ok ? 'text-green-400' : 'text-red-400');
    }
  } catch (e) {
    if (out) { out.textContent = '❌ ' + e.message; out.className = 'text-sm text-red-400'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

async function saveCredentials() {
  const body = {};
  document.querySelectorAll('#keys-form input[data-cred]').forEach(el => {
    const v = el.value.trim();
    if (v) body[el.dataset.cred] = v;   // only send changed (non-empty) fields
  });
  if (Object.keys(body).length === 0) { toast('Nothing to save — fields are blank', 'warn'); return; }
  const btn = document.getElementById('save-keys-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/settings/credentials`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Save failed'); }
    toast('✅ Keys saved', 'success');
    await ensureCreds(true);   // refresh the guard cache so just-added keys count
    await loadCredentials();   // refresh masks, clear inputs
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

// ===== BRAND VOICE =====
async function loadBrandVoice() {
  let data = { preset: 'balanced', custom: '', presets: [] };
  try {
    const res = await apiFetch(`${API}/api/settings/brand-voice`);
    if (res.ok) data = await res.json();
  } catch { /* 401 handled by apiFetch */ }
  S.brandVoice = { preset: data.preset, custom: data.custom || '', presets: data.presets || [] };
  document.getElementById('brand-voice-custom').value = S.brandVoice.custom;
  renderBrandVoice();
}

function renderBrandVoice() {
  const wrap = document.getElementById('brand-voice-list');
  const sel = S.brandVoice.preset;
  wrap.innerHTML = S.brandVoice.presets.map(p => {
    const active = p.key === sel;
    return `<button type="button" data-action="pick-voice" data-arg="${esc(p.key)}"
      class="text-left rounded-xl border p-3 transition ${active
        ? 'border-purple-500 bg-purple-900/40'
        : 'border-gray-700 bg-gray-800 hover:border-gray-600'}">
      <div class="text-sm font-semibold">${active ? '✓ ' : ''}${esc(p.label)}</div>
      <div class="text-xs text-gray-400 mt-0.5">${esc(p.description)}</div>
    </button>`;
  }).join('');
  document.getElementById('brand-voice-custom-wrap').classList.toggle('hidden', sel !== 'custom');
}

function pickVoice(key) {
  S.brandVoice.preset = key;
  renderBrandVoice();
  if (key === 'custom') document.getElementById('brand-voice-custom').focus();
}

async function saveBrandVoice() {
  const preset = S.brandVoice.preset;
  const custom = document.getElementById('brand-voice-custom').value;
  if (preset === 'custom' && !custom.trim()) { toast('Write your custom voice first', 'warn'); return; }
  const btn = document.getElementById('save-voice-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/settings/brand-voice`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset, custom }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Save failed'); }
    S.brandVoice.custom = custom;
    toast('✅ Brand voice saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

// ===== BRAND PROFILE =====
async function loadProfile() {
  let data = { niche: '', target_audience: '', brand_name: '' };
  try {
    const res = await apiFetch(`${API}/api/settings/profile`);
    if (res.ok) data = await res.json();
  } catch { /* 401 handled by apiFetch */ }
  S.profile = { niche: data.niche || '', target_audience: data.target_audience || '', brand_name: data.brand_name || '' };
  const n = document.getElementById('profile-niche');
  if (n) {
    n.value = S.profile.niche;
    document.getElementById('profile-audience').value = S.profile.target_audience;
    document.getElementById('profile-brand').value = S.profile.brand_name;
  }
  prefillComposerFromProfile();
}

// Save the profile from an arbitrary set of inputs; returns true on success.
async function _putProfile(niche, audience, brand, btn) {
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/settings/profile`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ niche, target_audience: audience, brand_name: brand }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Save failed'); }
    S.profile = { niche, target_audience: audience, brand_name: brand };
    prefillComposerFromProfile();
    return true;
  } catch (e) { toast('❌ ' + e.message, 'error'); return false; }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

async function saveProfile() {
  const ok = await _putProfile(
    document.getElementById('profile-niche').value.trim(),
    document.getElementById('profile-audience').value.trim(),
    document.getElementById('profile-brand').value.trim(),
    document.getElementById('save-profile-btn'),
  );
  if (ok) toast('✅ Brand profile saved', 'success');
}

// ===== AI MODELS (provider + model + that provider's key, per modality) =====
async function loadAISettings() {
  let cat = S.aiCatalog;
  if (!cat) {
    try {
      const r = await apiFetch(`${API}/api/models/providers`);
      cat = r.ok ? await r.json() : { text: [], image: [] };
    } catch { cat = { text: [], image: [] }; }
    S.aiCatalog = cat;
  }
  let cur = { text_provider: '', text_model: '', image_provider: '', image_model: '', keys: {} };
  try {
    const r = await apiFetch(`${API}/api/settings/ai`);
    if (r.ok) cur = await r.json();
  } catch { /* 401 handled by apiFetch */ }
  S.ai = cur;
  renderAISettings();
}

function _aiBlock(kind, title, note) {
  const cat = (S.aiCatalog && S.aiCatalog[kind]) || [];
  const provider = S.ai[`${kind}_provider`] || '';
  const model = S.ai[`${kind}_model`] || '';
  const meta = cat.find(p => p.key === provider);
  const models = meta ? meta.models : [];
  const known = models.some(m => m.id === model);
  const keyInfo = (S.ai.keys && S.ai.keys[provider]) || {};

  const provOpts = ['<option value="">— choose a provider —</option>'].concat(
    cat.map(p => `<option value="${esc(p.key)}" ${p.key === provider ? 'selected' : ''}>${esc(p.label)}</option>`)
  ).join('');
  const modelOpts = ['<option value="">— choose a model —</option>'].concat(
    models.map(m => `<option value="${esc(m.id)}" ${m.id === model ? 'selected' : ''}>${esc(m.label)} — $${m.price_in}/$${m.price_out} per M</option>`),
    [`<option value="__custom__" ${model && !known ? 'selected' : ''}>Custom model id…</option>`]
  ).join('');

  return `<div class="space-y-2 border border-gray-800 rounded-xl p-4">
    <div class="text-sm font-semibold text-gray-200">${esc(title)}</div>
    <div class="text-xs text-gray-500">${esc(note)}</div>
    <select data-ai="${kind}_provider" class="ce-input w-full px-3 py-2 text-sm">${provOpts}</select>
    <select data-ai="${kind}_model" class="ce-input w-full px-3 py-2 text-sm" ${provider ? '' : 'disabled'}>${modelOpts}</select>
    <input data-ai-custom="${kind}" type="text" maxlength="120" placeholder="e.g. vendor/model-name"
      value="${model && !known ? esc(model) : ''}"
      class="ce-input w-full px-3 py-2 text-xs font-mono ${model && !known ? '' : 'hidden'}" />
    ${meta ? `<div class="flex items-center gap-2">
      <input data-ai-key="${esc(meta.key_field)}" type="password" autocomplete="off"
        placeholder="${esc(meta.label)} API key — ${keyInfo.set ? 'set: ' + esc(keyInfo.masked || '••••') : 'not set'}"
        class="ce-input flex-1 px-3 py-2 text-sm" />
      <button type="button" data-action="ai-test" data-arg="${kind}" class="ce-btn-ghost px-3 py-2 text-xs whitespace-nowrap">Test</button>
    </div>
    <div class="text-xs text-gray-500">${esc(meta.hint)} <a href="${esc(safeUrl(meta.key_url))}" target="_blank" rel="noopener" class="underline">Get a key ↗</a></div>` : ''}
    <div data-ai-result="${kind}" class="text-xs"></div>
  </div>`;
}

function renderAISettings() {
  const form = document.getElementById('ai-models-form');
  if (!form) return;
  form.innerHTML =
    _aiBlock('text', 'Text — captions and hooks', 'Used for every post you generate.') +
    _aiBlock('image', 'Images — AI slide backgrounds', 'Only used when a slide’s source is AI. Stock photos do not need this. Anthropic is not listed: it cannot generate images.');
  // Wired after the markup exists, closure-style, like every data-act site. A
  // `change` listener rather than a data-action entry: this form re-renders
  // itself from inside these very handlers, so keeping the wiring next to the
  // render is what makes that loop readable.
  for (const kind of ['text', 'image']) {
    form.querySelector(`[data-ai="${kind}_provider"]`).onchange = () => onAIProviderChange(kind);
    form.querySelector(`[data-ai="${kind}_model"]`).onchange = () => onAIModelChange(kind);
  }
}

function onAIProviderChange(kind) {
  S.ai[`${kind}_provider`] = document.querySelector(`[data-ai="${kind}_provider"]`).value;
  S.ai[`${kind}_model`] = '';            // models differ per provider
  renderAISettings();
}

function onAIModelChange(kind) {
  const sel = document.querySelector(`[data-ai="${kind}_model"]`);
  const custom = document.querySelector(`[data-ai-custom="${kind}"]`);
  if (sel.value === '__custom__') {
    custom.classList.remove('hidden'); custom.focus();
  } else {
    custom.classList.add('hidden'); custom.value = '';
    S.ai[`${kind}_model`] = sel.value;
  }
}

function _aiModelValue(kind) {
  const sel = document.querySelector(`[data-ai="${kind}_model"]`);
  const custom = document.querySelector(`[data-ai-custom="${kind}"]`);
  if (!sel) return '';
  return sel.value === '__custom__' ? custom.value.trim() : sel.value;
}

// Save any keys typed into the AI blocks, then the provider/model choice.
async function _persistAI() {
  const keyBody = {};
  document.querySelectorAll('#ai-models-form input[data-ai-key]').forEach(el => {
    const v = el.value.trim();
    if (v) keyBody[el.dataset.aiKey] = v;
  });
  if (Object.keys(keyBody).length) {
    const r = await apiFetch(`${API}/api/settings/credentials`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(keyBody),
    });
    if (!r.ok) { const e = await r.json().catch(()=>({})); throw new Error(e.detail || 'Saving the key failed'); }
  }
  const body = {
    text_provider: document.querySelector('[data-ai="text_provider"]').value,
    text_model: _aiModelValue('text'),
    image_provider: document.querySelector('[data-ai="image_provider"]').value,
    image_model: _aiModelValue('image'),
  };
  const res = await apiFetch(`${API}/api/settings/ai`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Save failed'); }
  await ensureCreds(true);      // refresh the generate guard
  await loadAISettings();
}

async function saveAISettings() {
  const btn = document.getElementById('save-ai-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    await _persistAI();
    toast('✅ AI settings saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

// Save first, then ask the server to actually call the provider — a green Test
// means this provider+model+key really works, before you rely on it.
async function testAI(kind) {
  // Always re-query: _persistAI() re-renders the form, so a node captured before
  // it would be detached and the result would silently go nowhere.
  const write = (text, cls) => {
    const el = document.querySelector(`[data-ai-result="${kind}"]`);
    if (el) { el.textContent = text; el.className = 'text-xs ' + cls; }
  };
  write('⏳ Testing…', 'text-gray-400');
  try {
    await _persistAI();
    write('⏳ Testing…', 'text-gray-400');
    const res = await apiFetch(`${API}/api/settings/ai/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind }),
    });
    const body = await res.json();
    write((body.ok ? '✅ ' : '❌ ') + body.message, body.ok ? 'text-green-400' : 'text-red-400');
  } catch (e) {
    write('❌ ' + e.message, 'text-red-400');
  }
}

// ===== SLIDE COLOURS =====
function _syncColorPair(pickerId, hexId, value, fallback) {
  const picker = document.getElementById(pickerId);
  const hex = document.getElementById(hexId);
  if (!picker || !hex) return;
  picker.value = value || fallback;
  hex.value = value || '';
  picker.oninput = () => { hex.value = picker.value.toLowerCase(); };
  hex.oninput = () => {
    if (/^#[0-9a-fA-F]{6}$/.test(hex.value.trim())) picker.value = hex.value.trim();
  };
}

async function loadSlideStyle() {
  let data = { accent_color: '', text_box_color: '', default_accent_color: '#ff751f', palette: [] };
  try {
    const res = await apiFetch(`${API}/api/settings/slide-style`);
    if (res.ok) data = await res.json();
  } catch { /* 401 handled by apiFetch */ }
  S.slideStyle = data;
  _syncColorPair('style-accent', 'style-accent-hex', data.accent_color, data.default_accent_color);
  _syncColorPair('style-textbox', 'style-textbox-hex', data.text_box_color, '#ffffff');
  const wrap = document.getElementById('style-swatches');
  if (wrap) {
    wrap.innerHTML = '';
    (data.palette || []).forEach(hex => {
      const b = document.createElement('button');
      b.type = 'button';
      b.title = hex;
      b.setAttribute('aria-label', 'Use ' + hex);
      b.className = 'w-8 h-8 rounded-lg border-2 border-gray-700';
      b.style.background = hex;
      b.onclick = () => {
        document.getElementById('style-accent').value = hex;
        document.getElementById('style-accent-hex').value = hex;
      };
      wrap.appendChild(b);
    });
  }
  await loadLogo();
  await loadMusic();
}

// ===== BRAND LOGO =====
function _renderLogo(set) {
  const box = document.getElementById('logo-preview');
  const removeBtn = document.getElementById('logo-remove-btn');
  if (!box) return;
  if (set) {
    // Cache-bust so a just-uploaded logo replaces the old preview.
    box.innerHTML = `<img src="${API}/api/settings/logo/image?t=${Date.now()}" alt="Brand logo" class="max-w-full max-h-full object-contain" />`;
    removeBtn.classList.remove('hidden');
  } else {
    box.textContent = 'none';
    removeBtn.classList.add('hidden');
  }
}

async function loadLogo() {
  try {
    const res = await apiFetch(`${API}/api/settings/logo`);
    if (res.ok) _renderLogo((await res.json()).set);
  } catch { /* 401 handled by apiFetch */ }
}

async function uploadLogo(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const form = new FormData();
    form.append('file', file);
    const res = await apiFetch(`${API}/api/settings/logo`, { method: 'POST', body: form });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Upload failed'); }
    _renderLogo(true);
    toast('✅ Logo saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

async function removeLogo() {
  try {
    const res = await apiFetch(`${API}/api/settings/logo`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Remove failed');
    _renderLogo(false);
    toast('Logo removed', 'info');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

// ===== REEL BACKGROUND MUSIC (R3) =====
function _renderMusic(set) {
  const state = document.getElementById('music-state');
  const btn = document.getElementById('music-remove-btn');
  if (state) state.textContent = set ? '✅ track uploaded' : 'no track uploaded';
  if (btn) btn.classList.toggle('hidden', !set);
}

async function loadMusic() {
  try {
    const res = await apiFetch(`${API}/api/settings/music`);
    if (res.ok) _renderMusic((await res.json()).set);
  } catch { /* 401 handled by apiFetch */ }
}

async function uploadMusic(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const form = new FormData();
    form.append('file', file);
    const res = await apiFetch(`${API}/api/settings/music`, { method: 'POST', body: form });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Upload failed'); }
    _renderMusic(true);
    toast('✅ Music track saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

async function removeMusic() {
  try {
    const res = await apiFetch(`${API}/api/settings/music`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Remove failed');
    _renderMusic(false);
    toast('Music removed', 'info');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
}

async function _putSlideStyle(accent, textBox, btn) {
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const res = await apiFetch(`${API}/api/settings/slide-style`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accent_color: accent, text_box_color: textBox }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Save failed'); }
    await loadSlideStyle();
    toast('✅ Slide colours saved', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

async function saveSlideStyle() {
  const accent = document.getElementById('style-accent-hex').value.trim().toLowerCase();
  const textBox = document.getElementById('style-textbox-hex').value.trim().toLowerCase();
  for (const v of [accent, textBox]) {
    if (v && !/^#[0-9a-fA-F]{6}$/.test(v)) { toast('Use a hex colour like #ff751f', 'warn'); return; }
  }
  await _putSlideStyle(accent, textBox, document.getElementById('save-style-btn'));
}

async function resetSlideStyle() {
  await _putSlideStyle('', '', document.getElementById('save-style-btn'));
}

// Pre-fill the composer niche/audience from the saved profile, only when empty
// so a user's in-progress edits are never clobbered.
/** What the collapsed Configure row says it is holding.
 *
 *  A row labelled only "Configure" makes people open it to find out whether it
 *  matters; showing the values answers that without a click. Read off the
 *  controls rather than from S, so it cannot drift from what the form will
 *  actually send. */
function updateConfigureSummary() {
  const el = document.getElementById('configure-summary');
  if (!el) return;
  const val = id => (document.getElementById(id) || {}).value || '';
  const tone = document.getElementById('tone');
  const parts = [
    (S.platform === 'x') ? '\ud835\udd4f X' : '\ud83d\udcf8 Instagram',
    val('niche'),
    val('audience'),
    tone && tone.selectedIndex >= 0 ? tone.options[tone.selectedIndex].text : '',
  ].filter(Boolean);
  el.textContent = '\u2699 ' + parts.join(' \u00b7 ');
}

function prefillComposerFromProfile() {
  const p = S.profile || {};
  const niche = document.getElementById('niche');
  const aud = document.getElementById('audience');
  if (niche && !niche.value && p.niche) niche.value = p.niche;
  if (aud && !aud.value && p.target_audience) aud.value = p.target_audience;
  updateConfigureSummary();
}

// ===== FIRST-RUN SETUP =====
//
// Three questions and a post, on a real screen rather than a modal. The screen
// is deliberately NOT in setSection's map and NOT in _MODAL_CLOSERS: it is not
// a section (the four nav buttons stay untouched) and Escape must not dismiss
// it — a half-configured account dropped into the app with no sign anything was
// skipped is worse than one more click.

//: Where this account stopped. Namespaced by user id because the old global
//: flag meant two accounts in one browser shared a verdict: sign into a second
//: one and the app decided you had already done setup because somebody else had.
function onbKey() {
  return 'onboarding:' + ((S.user && S.user.id) || 'anon');
}

const ONB_SCREENS = ['1', '2', '3', '4'];

function onbState() {
  try { return localStorage.getItem(onbKey()); } catch (e) { return null; }
}

function onbRemember(state) {
  try { localStorage.setItem(onbKey(), state); } catch (e) { /* private mode */ }
}

/** Show one screen, and record that we got here.
 *
 *  Written on ENTRY rather than on success: a refresh or a crash should resume
 *  where the user actually was, not where they last completed something. */
function showOnboardingScreen(n) {
  const want = String(n);
  ONB_SCREENS.forEach(k => {
    const el = document.getElementById('onb-s' + k);
    if (el) el.classList.toggle('hidden', k !== want);
  });
  if (want === '4') onbGenerateFirstPost();
  const idx = ONB_SCREENS.indexOf(want);
  const prog = document.getElementById('onb-progress');
  if (prog) prog.textContent = idx >= 0 ? `Step ${idx + 1} of ${ONB_SCREENS.length}` : '';
  onbRemember(want);
}

function startOnboarding(opts) {
  const restart = !!(opts && opts.restart);
  if (restart) { S.onbAsked = false; S.onbPost = null; }
  document.getElementById('onboarding-screen').classList.remove('hidden');
  const at = restart ? '1' : (ONB_SCREENS.includes(onbState()) ? onbState() : '1');
  // Prefill from the server rather than from whatever was typed last time: a
  // second pass should show what is actually saved.
  const prof = S.profile || {};
  const set = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
  set('onb-niche', prof.niche);
  set('onb-audience', prof.target_audience);
  set('onb-brand', prof.brand_name);
  // A second pass starts at the website field again, but somebody who already
  // has a brand on file should see it rather than an empty form behind a link.
  if (prof.niche) onbShowFields();
  showOnboardingScreen(at);
}

/** Offer setup to somebody who has not finished it. */
async function maybeStartOnboarding() {
  if (!S.user || S.user.is_local) return;     // cloud accounts only
  const state = onbState();
  if (state === 'done') return;
  if (state === null) {
    // One-release compatibility shim: every account that finished the old
    // wizard carries a single global flag. Without honouring it once, this
    // release nags all of them. Delete after a release or two.
    let legacy = null;
    try { legacy = localStorage.getItem('onboarding_done'); } catch (e) {}
    if (legacy) { onbRemember('done'); return; }
  }
  startOnboarding({});
}

/** Leave setup. `reason` is 'done' either way — an explicit "not now" is a
 *  decision, and re-asking on the next load is how you teach people to distrust
 *  an app. The avatar menu keeps it reachable. */
function closeOnboarding(reason) {
  onbRemember('done');
  document.getElementById('onboarding-screen').classList.add('hidden');
  if (reason !== 'stay') setSection('create');
}

// ── screen 1: what do you run ───────────────────────────────────────────────

async function onbPickType(kind) {
  const current = (S.user && S.user.account_type) || 'creator';
  if (kind !== current) {
    try {
      const res = await apiFetch(`${API}/api/auth/account-type`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_type: kind }),
      });
      if (!res.ok) { toast("Couldn't save that — try again.", 'error'); return; }
      S.user.account_type = (await res.json()).account_type || 'creator';
    } catch (e) { toast('❌ ' + e.message, 'error'); return; }

    // Crossing the business boundary changes which shell the app bootstraps
    // into, so reboot — the same move onProductSwitch already makes. The resume
    // key is at '2' by now, so the reload lands back on the next question.
    const crossed = (kind === 'business') !== (current === 'business');
    if (crossed) { showOnboardingScreen('2'); location.reload(); return; }
    // creator ↔ agency needs no reboot: it is the same shell plus a Settings tab.
    renderUserChrome();
  }
  showOnboardingScreen('2');
}

// ── screen 2: your brand ────────────────────────────────────────────────────

//: What the site told us, kept until the user presses Continue. Nothing here is
//: saved on arrival: every field is a guess from markup nobody is obliged to get
//: right, so it is a proposal until somebody accepts it.
S.onbRead = null;

/** Reveal the brand form. One code path whether the site filled it or not. */
function onbShowFields() {
  document.getElementById('onb-fields').classList.remove('hidden');
  document.getElementById('onb-continue-brand').classList.remove('hidden');
}

function onbNoSite() {
  document.getElementById('onb-site-row').classList.add('hidden');
  onbShowFields();
}

async function onbExtract() {
  const url = document.getElementById('onb-site').value.trim();
  if (!url) { onbBrandSay('Paste your website address, or say you have none.', false); return; }
  const btn = document.getElementById('onb-read-site');
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Reading…';
  try {
    const res = await apiFetch(`${API}/api/brand/extract`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const body = await res.json();
    // The message is the server's: the refusal text for a blocked address is
    // deliberately uniform, and rewording it here would leak which kind of
    // address was refused.
    if (!res.ok) { onbBrandSay(body.detail || "We couldn't read that site.", false); return; }

    S.onbRead = body;
    const set = (id, v) => { if (v) document.getElementById(id).value = v; };
    set('onb-brand', body.name);
    set('onb-audience', body.target_audience);
    // A tenant with no text model gets an empty niche back — the guess is the one
    // part of extraction that needs a model, and that is the ordinary case for a
    // brand-new account. So it is a half-success rather than a failure: say what
    // is missing, and leave the field empty. Its placeholder already shows the
    // shape a niche has, which a truncated sentence does not.
    if (body.niche) set('onb-niche', body.niche);
    else onbBrandSay("We read your site but couldn't guess your niche — put it in a few words.", false);

    // The description is not thrown away: it is what we read, shown as that, so
    // the words are there to borrow from without pretending to be an answer.
    const read = document.getElementById('onb-read');
    if (body.description) {
      read.textContent = '“' + body.description + '”';
      read.classList.remove('hidden');
    } else {
      read.classList.add('hidden');
    }

    const colors = document.getElementById('onb-colors');
    colors.innerHTML = (body.colors || []).map((c, i) =>
      `<button type="button" data-onb-color="${esc(c)}" data-action="onb-pick-color" data-arg="${esc(c)}"
         class="w-8 h-8 rounded-full border-2 ${i === 0 ? 'border-white' : 'border-transparent'}"
         style="background:${esc(c)}" title="${esc(c)}"></button>`).join('');
    document.getElementById('onb-colors-row').classList.toggle('hidden', !(body.colors || []).length);
    S.onbColor = (body.colors || [])[0] || null;

    const logoRow = document.getElementById('onb-logo-row');
    if (body.logo_data_url) {
      document.getElementById('onb-logo').src = body.logo_data_url;
      logoRow.classList.remove('hidden');
    } else {
      logoRow.classList.add('hidden');
    }

    document.getElementById('onb-site-row').classList.add('hidden');
    onbShowFields();
  } catch (e) {
    onbBrandSay(e.message || "We couldn't read that site.", false);
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

function onbPickColor(c) {
  S.onbColor = c;
  document.querySelectorAll('#onb-colors [data-onb-color]').forEach(b =>
    b.classList.toggle('border-white', b.dataset.onbColor === c));
}

/** Save the colour, if one was chosen. Non-blocking on purpose — see onbSaveBrand. */
async function onbSaveColor() {
  if (!S.onbColor) return;
  try {
    await apiFetch(`${API}/api/settings/slide-style`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accent_color: S.onbColor }),
    });
  } catch (e) { /* reported below, never fatal */ }
}

/** Save the logo the site gave us, if the user kept the tick. Also non-blocking.
 *
 *  The data URL is turned back into bytes and posted as a file, which is what
 *  the logo endpoint takes — the same call the brand editor already makes. */
async function onbSaveLogo() {
  const use = document.getElementById('onb-logo-use');
  const read = S.onbRead;
  if (!read || !read.logo_data_url || !use || !use.checked) return;
  const acctId = S.user && S.user.active_account_id;
  if (!acctId) return;
  try {
    // Decoded here rather than fetched. `fetch()` on a data: URL is a network
    // request as far as CSP is concerned, and it was the only reason connect-src
    // had to allow `data:` at all — a directive left permanently open for five
    // lines of convenience.
    const blob = dataUrlToBlob(read.logo_data_url);
    if (!blob) return;
    const fd = new FormData();
    fd.append('file', blob, 'logo.png');
    await apiFetch(`${API}/api/accounts/${encodeURIComponent(acctId)}/logo`,
                   { method: 'POST', body: fd });
  } catch (e) { /* reported below, never fatal */ }
}

/** A data: URL to a Blob, without going through the network stack.
 *
 *  Returns null on anything that is not a base64 data URL: the caller's next
 *  step is a multipart upload, and posting `undefined` as a file is a worse
 *  failure than doing nothing. */
function dataUrlToBlob(url) {
  const m = /^data:([^;,]*);base64,(.*)$/.exec(String(url || ''));
  if (!m) return null;
  const bytes = atob(m[2]);
  const buf = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
  return new Blob([buf], { type: m[1] || 'application/octet-stream' });
}

function onbBrandSay(msg, ok) {
  const el = document.getElementById('onb-brand-status');
  if (!el) return;
  el.classList.remove('hidden');
  el.className = 'text-sm ' + (ok ? 'text-green-400' : 'text-yellow-300');
  el.textContent = msg;
}

async function onbSaveBrand() {
  const niche = document.getElementById('onb-niche').value.trim();
  if (!niche) {
    // The one rule the old wizard had that is worth keeping: the post at the end
    // is written from this, and a blank profile produces a blank post.
    onbBrandSay('Add your niche — a couple of words is enough.', false);
    return;
  }
  const btn = document.getElementById('onb-continue-brand');
  // The profile is BLOCKING: the first post is written from it, so advancing
  // without it would promise something we cannot deliver.
  const ok = await _putProfile(
    niche,
    document.getElementById('onb-audience').value.trim(),
    document.getElementById('onb-brand').value.trim(),
    btn);
  if (ok === false) { onbBrandSay("Couldn't save that — try again.", false); return; }
  // The colour and the logo are NOT blocking. They are decoration on a post that
  // will exist either way, and losing a favicon must not strand somebody in
  // setup with no way forward.
  await onbSaveColor();
  await onbSaveLogo();
  showOnboardingScreen('3');
}

// ── screen 3: one network ───────────────────────────────────────────────────

function onbPickNetwork(net) {
  setNetwork(net === 'x' ? 'x' : 'instagram');
  // setNetwork re-enters setSection, which is harmless underneath a full-screen
  // overlay — but it also means the composer is already aimed correctly when the
  // screen lifts.
  showOnboardingScreen('4');
}

function onbSkipNetwork() {
  // A skip that does nothing but move on. The composer already has a default,
  // and picking one here is a convenience rather than a requirement.
  showOnboardingScreen('4');
}

// ── screen 4: the first post ───────────────────────────────────────────────

//: The post we were given, kept so Copy and the composer prefill can read it.
S.onbPost = null;

/** Ask for the sample post, once.
 *
 *  Once matters: it costs real money on OUR key, and arriving at this screen
 *  twice — a resume, a back, a re-render — must not buy a second one. The
 *  server's own cap protects the row but not the spend. */
async function onbGenerateFirstPost() {
  if (S.onbAsked) return;
  S.onbAsked = true;
  const box = document.getElementById('onb-post');
  box.textContent = 'Writing your first post…';
  try {
    const res = await apiFetch(`${API}/api/onboarding/first-post`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: postPlatform() === 'x' ? 'x' : 'instagram' }),
    });
    if (!res.ok) {
      // 503 (no app key), 409 (allowance spent) and anything else all land here.
      // The message is the server's; the way forward stays open either way.
      const body = await res.json().catch(() => ({}));
      onbPostSay(body.detail || "We couldn't write a sample post — you can start anyway.");
      return;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let post = null;
    let failed = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === 'progress') box.textContent = ev.message;
        if (ev.type === 'complete') post = ev.post;
        if (ev.type === 'error') failed = ev.message;
      }
    }
    if (post) { S.onbPost = post; onbRenderPost(post); }
    else onbPostSay(failed || "We couldn't write a sample post — you can start anyway.");
  } catch (e) {
    onbPostSay(e.message || "We couldn't write a sample post — you can start anyway.");
  }
}

function onbPostSay(msg) {
  const box = document.getElementById('onb-post');
  box.textContent = msg;
  document.getElementById('onb-copy').classList.add('hidden');
}

function onbRenderPost(post) {
  const tags = (post.hashtags || []).join(' ');
  document.getElementById('onb-post').innerHTML =
    `<div class="ce-card p-4 space-y-2 text-left">
       <div class="font-semibold" style="color:var(--text)">${esc(post.hook || '')}</div>
       <div style="color:var(--text)">${esc(post.caption || '')}</div>
       ${post.cta ? `<div class="text-sm">${esc(post.cta)}</div>` : ''}
       ${tags ? `<div class="text-sm" style="color:var(--accent)">${esc(tags)}</div>` : ''}
     </div>
     <p class="text-xs">This one is just to show you the shape — it isn't saved. The next one will be.</p>`;
  document.getElementById('onb-copy').classList.remove('hidden');
}

function onbCopyPost() {
  const p = S.onbPost;
  if (!p) return;
  const text = [p.hook, p.caption, p.cta, (p.hashtags || []).join(' ')]
    .filter(Boolean).join('\n\n');
  try { navigator.clipboard.writeText(text); toast('Copied', 'success'); }
  catch (e) { toast("Couldn't copy", 'error'); }
}

/** Leave setup, carrying the topic across.
 *
 *  The whole point of the flow: the first thing after setup is a composer that
 *  already knows what it is about, rather than an empty box. */
function onbFinish() {
  const topic = S.onbPost && S.onbPost.topic;
  closeOnboarding('done');
  if (topic) {
    const el = document.getElementById('topic');
    if (el && !el.value) el.value = topic;
  }
}

// ===== YOUR DATA: export + erasure =====

/** Download everything we hold about this account as a ZIP. */
async function exportMyData() {
  const btn = document.getElementById('export-data-btn');
  const prev = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Building your archive…'; }
  try {
    const res = await apiFetch(`${API}/api/auth/export`);
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Export failed'); }
    const blob = await res.blob();
    const cd = res.headers.get('content-disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = m ? m[1] : 'content-engine-export.zip'; a.click();
    URL.revokeObjectURL(url);
    toast('✅ Your data is downloading', 'success');
  } catch (e) { toast('❌ ' + e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
}

function openDeleteAccount() {
  document.getElementById('del-acct-password').value = '';
  document.getElementById('del-acct-error').textContent = '';
  document.getElementById('delete-account-modal').classList.remove('hidden');
  onModalOpen('delete-account-modal');
  setTimeout(() => document.getElementById('del-acct-password').focus(), 50);
}

function closeDeleteAccount() {
  document.getElementById('del-acct-password').value = '';   // don't leave it in the DOM
  document.getElementById('delete-account-modal').classList.add('hidden');
  onModalClose();
}

async function confirmDeleteAccount() {
  const input = document.getElementById('del-acct-password');
  const err = document.getElementById('del-acct-error');
  const btn = document.getElementById('del-acct-btn');
  const password = input.value;
  if (!password) { err.textContent = 'Enter your password to confirm.'; return; }
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Deleting…';
  err.textContent = '';
  try {
    const res = await apiFetch(`${API}/api/auth/delete`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const e = await res.json().catch(()=>({}));
      throw new Error(e.detail || 'Could not delete the account.');
    }
    input.value = '';
    // Nothing left to be signed in to — drop the session before anything can
    // fire another request against an account that no longer exists.
    localStorage.removeItem('api_token');
    try { localStorage.removeItem(onbKey()); } catch (e) {}
    document.getElementById('delete-account-modal').classList.add('hidden');
    onModalClose();
    alert('Your account and its data have been deleted.');
    location.reload();
  } catch (e) {
    err.textContent = e.message;
  } finally { btn.disabled = false; btn.textContent = prev; }
}

async function downloadBackup() {
  toast('📦 Building backup…', 'info');
  try {
    const res = await apiFetch(`${API}/api/admin/backup`);
    if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'Backup failed'); }
    const blob = await res.blob();
    const cd = res.headers.get('content-disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = m ? m[1] : 'insta_backup.zip'; a.click();
    URL.revokeObjectURL(url);
    toast('✅ Backup downloaded', 'success');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
}

async function restoreBackup(ev) {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  if (!confirm('Restore will REPLACE current data. Continue?')) { ev.target.value=''; return; }
  toast('♻ Restoring…', 'info');
  try {
    const form = new FormData(); form.append('file', f);
    const res = await apiFetch(`${API}/api/admin/restore`, { method: 'POST', body: form });
    const d = await res.json();
    if (!res.ok || !d.ok) throw new Error(d.detail || 'Restore failed');
    toast('✅ ' + (d.detail || 'Restored'), 'success');
  } catch(e) { toast('❌ ' + e.message, 'error'); }
  finally { ev.target.value = ''; }
}

// ===== INIT =====
applyTheme();
initToggleGroup('.fmt-btn', 'format', 'single', renderOwnPhotos);
initToggleGroup('.xmode-btn', 'xMode', 'short', onXModeChange);
initToggleGroup('.xstyle-btn', 'xStyle', 'standard');
initToggleGroup('.src-btn', 'source', 'stock', onSourceChange);
initToggleGroup('.plt-btn', 'platform', 'instagram');
initToggleGroup('.tpl-btn', 'templateStyle', 'branded_card', onTemplateChange);
initNicheSwatches();
showStep(1);
switchAuthTab('login');

// Autosave draft as the user types the caption
document.getElementById('caption-edit').addEventListener('input', () => { updateCaptionCount(); scheduleDraftSave(); });
document.getElementById('auth-password').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth(); });

// Boot gate: local mode resolves silently and starts the app; cloud shows login.
// startApp() (called from initAuth on success) registers the status + cost polls.
// A /verify or /reset link is handled first; /reset stops here (user sets a new
// password), everything else falls through to the normal who-am-I boot.
(async () => { if (!await handleAuthLink()) initAuth(); })();
