/* Dewey web console - phase 2 (reads, dry runs, and real runs through the runner). Plain JS, no build step, no external requests. */
'use strict';

const isCompose = () => window.DEWEY_INSTALL === 'compose';
const cfgFile = () => isCompose() ? '.env' : 'web.env';
const runnerCmd = () => isCompose() ? 'docker compose --profile runner up -d' : '.\\dewey.ps1 runner';

// ---------------------------------------------------------------- helpers
// replaceChildren that accepts nested arrays, nulls and plain text (the page builders pass all three)
(function () {
  const orig = Element.prototype.replaceChildren;
  Element.prototype.replaceChildren = function (...kids) {
    const flat = [];
    const add = (c) => { if (c === null || c === undefined || c === false) return; if (Array.isArray(c)) c.forEach(add); else flat.push(c.nodeType ? c : document.createTextNode(String(c))); };
    kids.forEach(add);
    orig.apply(this, flat);
  };
})();
const $ = (s, r) => (r || document).querySelector(s);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    const v = attrs[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k === 'value') e.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'selected') e[k] = !!v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else e.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return e;
}
const api = async (path) => {
  const r = await fetch('/api/' + path, { cache: 'no-store' });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).error || m; } catch (e) {} throw new Error(m); }
  return r.json();
};
const post = async (path, body) => {
  const r = await fetch('/api/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-MD-Web': '1' }, body: JSON.stringify(body || {}) });
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const qs = (o) => Object.entries(o).filter(([, v]) => v !== '' && v !== null && v !== undefined).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
const human = (n) => { n = Number(n || 0); const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n.toFixed(0)) + ' ' + u[i]; };
const num = (n) => Number(n || 0).toLocaleString('en-CA');
const dur = (s) => { if (s === null || s === undefined) return '?'; s = Math.round(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const ago = (d) => d === null || d === undefined ? 'never' : d < 0.04 ? 'just now' : d < 1 ? Math.round(d * 24) + ' h ago' : d < 14 ? (Math.round(d * 10) / 10) + ' days ago' : Math.round(d) + ' days ago';
const split = (p) => { const i = p.lastIndexOf('/'); return i < 0 ? ['', p] : [p.slice(0, i + 1), p.slice(i + 1)]; };
function toast(msg, bad) { const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2500); }
const badge = (txt, cls) => h('span', { class: 'badge ' + (cls || '') }, txt);
const KIND_CLS = { exact: 'b-green', variant: 'b-blue', wide: 'b-amber', review: 'b-purple', similar: 'b-pink' };
const STATUS_CLS = { removable: 'b-red', blocked: 'b-amber', review: 'b-purple', keep: 'b-green' };
const ROLE_CLS = { keep: 'b-green', remove: 'b-red', blocked: 'b-amber', review: 'b-purple' };

function pager(res, go) {
  if (!res || res.pages <= 1) return h('div', { class: 'pager' }, res ? num(res.total) + ' item' + (res.total === 1 ? '' : 's') : '');
  return h('div', { class: 'pager' },
    h('button', { class: 'btn small', disabled: res.page <= 1, onclick: () => go(res.page - 1) }, '< prev'),
    'page ' + res.page + ' / ' + res.pages + '  (' + num(res.total) + ' items)',
    h('button', { class: 'btn small', disabled: res.page >= res.pages, onclick: () => go(res.page + 1) }, 'next >'));
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function chips(opts, cur, onpick, counts) {
  return h('div', { class: 'toolbar', style: 'margin:4px 0' }, opts.map(([v, label]) =>
    h('button', { class: 'chip' + (cur === v ? ' on' : ''), onclick: () => onpick(v) }, label, counts && counts[v] !== undefined ? h('span', { class: 'n' }, num(counts[v])) : null)));
}
function bars(parts) {   // parts: [[label, value, color]]
  const tot = parts.reduce((a, p) => a + p[1], 0) || 1;
  return h('div', null,
    h('div', { class: 'bar' }, parts.map(p => h('i', { style: 'width:' + (100 * p[1] / tot) + '%;background:' + p[2], title: p[0] + ': ' + num(p[1]) }))),
    h('div', { class: 'legend' }, parts.map(p => h('span', null, h('i', { style: 'background:' + p[2] }), p[0] + ' ', h('b', { class: 'bright' }, num(p[1]))))));
}
const empty = (msg, action) => h('div', { class: 'empty' }, msg, action ? [h('br'), h('br'), action] : null);

// ---------------------------------------------------------------- audio player
const player = { btn: null };
function play(root, rel, label, btn) {
  const a = $('#audio');
  if (player.btn === btn && !a.paused) { a.pause(); btn.classList.remove('on'); return; }
  if (player.btn) player.btn.classList.remove('on');
  a.src = '/api/audio?' + qs({ root, p: rel });
  a.play().catch(() => toast('This browser could not play that file', true));
  player.btn = btn; btn.classList.add('on');
  $('#playerLabel').textContent = label; $('#player').hidden = false;
}
function playBtn(root, rel, label) {
  const b = h('button', { class: 'play', title: 'preview' }, '▶');
  b.addEventListener('click', (e) => { e.stopPropagation(); play(root, rel, label, b); });
  return b;
}
$('#playerClose').addEventListener('click', () => { const a = $('#audio'); a.pause(); a.removeAttribute('src'); a.load(); $('#player').hidden = true; if (player.btn) player.btn.classList.remove('on'); });
$('#audio').addEventListener('pause', () => { if (player.btn) player.btn.classList.remove('on'); });
$('#audio').addEventListener('play', () => { if (player.btn) player.btn.classList.add('on'); });

// ---------------------------------------------------------------- state + routing
let STATUS = null;
const page = () => $('#page');
const ROUTES = [
  ['', 'Overview', pageOverview], ['run', 'Run a job', pageRun], ['dupes', 'Duplicates', pageDupes], ['new', 'New music', pageNew],
  ['layout', 'Folder layout', pageLayout], ['health', 'Tags, art & lyrics', pageHealth], ['quarantine', 'Quarantine & undo', pageQuarantine],
  ['rules', 'Rules', pageRules], ['logs', 'Logs', pageLogs], ['settings', 'Settings', pageSettings],
];
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [p, q] = raw.split('?');
  const params = {}; (q || '').split('&').filter(Boolean).forEach(kv => { const [k, v] = kv.split('='); params[decodeURIComponent(k)] = decodeURIComponent(v || ''); });
  return { path: p || '', params };
}
const go = (path, params) => { location.hash = '#/' + path + (params ? '?' + qs(params) : ''); };
let pollTimer = null;
async function route() {
  clearTimeout(pollTimer); pollTimer = null; stopRunPoll();
  const { path, params } = parseHash();
  const r = ROUTES.find(x => x[0] === path) || ROUTES[0];
  try { STATUS = await api('status'); } catch (e) { STATUS = STATUS || null; }
  drawNav(r[0]); drawSafety();
  $('#sidebar').classList.remove('open');
  page().replaceChildren(h('p', { class: 'dim' }, 'loading...'));
  try { await r[2](params); } catch (e) { page().replaceChildren(h('div', { class: 'notice bad' }, 'Something went wrong: ' + e.message)); }
}
window.addEventListener('hashchange', route);
$('#menuBtn').addEventListener('click', () => $('#sidebar').classList.add('open'));
$('#menuClose').addEventListener('click', () => $('#sidebar').classList.remove('open'));
$('#scrim').addEventListener('click', () => $('#sidebar').classList.remove('open'));
$('#jobPill').addEventListener('click', () => go('run', STATUS && STATUS.job ? { job: STATUS.job.id } : null));

function drawNav(cur) {
  const s = STATUS || {};
  const counts = {
    dupes: s.plan && s.plan.exists ? (s.plan.removable_open !== undefined ? s.plan.removable_open : (s.plan.status.removable || 0)) + (s.plan.status.blocked || 0) || null : null,
    new: s.incoming && s.incoming.files ? s.incoming.files : null,
    quarantine: s.quarantine && s.quarantine.runs ? s.quarantine.runs : null,
  };
  $('#nav').replaceChildren(
    ...ROUTES.map(([p, label]) => [
      p === 'run' ? h('div', { class: 'sep' }, 'tools') : p === 'dupes' ? h('div', { class: 'sep' }, 'review') : p === 'rules' ? h('div', { class: 'sep' }, 'reference') : p === 'settings' ? h('div', { class: 'sep' }, 'system') : null,
      h('a', { href: '#/' + p, class: p === cur ? 'on' : '' }, h('span', { class: 'dot' }), label, counts[p] ? h('span', { class: 'count' }, num(counts[p])) : null)]));
}
function drawSafety() {
  const s = STATUS; const box = $('#safety'); const pill = $('#jobPill');
  if (!s) { box.replaceChildren('server not answering'); return; }
  const ro = s.library.readonly_mount;
  box.replaceChildren(
    h('div', { class: ro ? 'ok' : 'bad' }, ro ? '● library mounted read-only' : ro === false ? '● LIBRARY IS WRITABLE: mount it :ro' : '● library mount unknown'),
    h('div', { class: location.protocol === 'https:' ? 'ok' : 'bad' }, location.protocol === 'https:' ? '● https (encrypted)' : '● plain http: see Settings > Security'),
    (() => { const rr = s.runner; return h('div', { class: rr && rr.armed ? 'bad' : rr && rr.available ? 'ok' : 'dim' },
      rr && rr.armed ? '● WRITE ACCESS ARMED (' + Math.ceil(rr.armed_left / 60) + ' min)' : rr && rr.available ? '● write access: ready, not armed' : '● write access: off (runner not running)'); })(),
    (window.DEWEY_INSTALL = s.install || 'launcher', null),
    h('div', null, 'tool v' + s.version + '  ·  web v' + String(s.web_version || '').split(' ')[0]));
  if (s.job) { pill.hidden = false; pill.replaceChildren(h('span', { class: 'spin' }), s.job.title + ' running'); } else pill.hidden = true;
}

// ---------------------------------------------------------------- overview
async function pageOverview() {
  const s = STATUS; if (!s) throw new Error('no status');
  const inv = s.inventory, p = s.plan, ta = s.tagaudit, ro = s.library.readonly_mount;
  const card = (cls, title, bigText, sub, rows, acts) => h('div', { class: 'card ' + (cls || '') }, h('h3', null, title), bigText ? h('div', { class: 'big' }, bigText) : null, sub ? h('div', { class: 'sub' }, sub) : null,
    rows ? h('div', { class: 'kv' }, rows.map(([k, v]) => [h('span', { class: 'k' }, k), h('span', null, v)])) : null, h('div', { class: 'row' }, acts || []));
  const cards = [];
  // duplicates
  if (p && p.exists) {
    const st = p.status || {};
    const gone = p.gone || 0;
    const open = p.removable_open !== undefined ? p.removable_open : (st.removable || 0);
    const stale = (p.stale_by && p.stale_by.length) || gone;
    cards.push(card(stale ? 'warn' : '', 'Duplicates', num(open) + ' removable groups', 'plan from ' + (p.generated || '?') + ' (' + ago(p.age_days) + ')' + (gone ? '' : ', would free ' + p.removable_h),
      [['blocked by playlists', num(st.blocked || 0)], ['need your review', num(st.review || 0)], ['groups in total', num(p.groups)]].concat(gone ? [['already removed', num(gone) + ' group(s): rescan']] : stale ? [['note', 'library changed since this scan: rescan']] : []),
      [h('a', { class: 'btn primary', href: '#/dupes' }, 'Review groups'), h('a', { class: 'btn', href: '#/run?cmd=scan' }, 'Rescan')]));
  } else cards.push(card('warn', 'Duplicates', 'no scan yet', 'Run a scan to find duplicates. It changes nothing.', null, [h('a', { class: 'btn primary', href: '#/run?cmd=scan' }, 'Scan now')]));
  // new music
  const inc = s.incoming;
  cards.push(card(s.place_pending ? 'warn' : inc.files ? 'good' : '', 'New music', inc.exists ? num(inc.files) + ' waiting' : 'no landing folder', inc.files ? 'in ' + inc.folders + ' folder(s) in _incoming, not filed yet' : 'nothing in _incoming',
    s.place_pending ? [['unfinished run', 'playlists still to write: run place again']] : (s.place_plan ? [['last plan', s.place_plan]] : null),
    [h('a', { class: 'btn primary', href: '#/new' }, 'See the plan'), h('a', { class: 'btn', href: '#/run?cmd=place' }, 'Dry run')]));
  // health
  if (ta) cards.push(card(ta.core_missing ? 'warn' : '', 'Tags, art & lyrics', num(ta.core_missing) + ' files missing core tags', 'audit ' + ago(ta.age_days),
    [['no cover art', num(ta.no_cover)], ['no lyrics', num(ta.no_lyrics)], ['files audited', num(ta.files)]], [h('a', { class: 'btn primary', href: '#/health' }, 'Open'), h('a', { class: 'btn', href: '#/run?cmd=tagaudit' }, 'Re-audit')]));
  else cards.push(card('', 'Tags, art & lyrics', 'not audited', 'Find files missing tags, cover art or lyrics.', null, [h('a', { class: 'btn primary', href: '#/run?cmd=tagaudit' }, 'Run audit')]));
  // layout
  if (s.reorg) { const sm = s.reorg.summary || {}; const rg = s.reorg;
    // the files on disk decide: changes the plan lists that are already in place do not count as off-layout
    const todo = rg.pending !== undefined ? rg.pending : (sm.move || 0) + (sm.rename || 0); const applied = rg.done || 0;
    cards.push(card(todo || applied ? 'warn' : 'good', 'Folder layout', todo ? num(todo) + ' files off-layout' : applied ? 'plan is out of date' : 'layout is clean',
      'plan ' + ago(rg.age_days) + (applied ? ': ' + num(applied) + ' of its ' + num((sm.move || 0) + (sm.rename || 0)) + ' changes are already in the library' : ''),
      [['already correct', num(sm.ok || 0)], ['still to do', num(todo)], ['already applied', num(applied)], ['held', num(sm.held || 0)]],
      [h('a', { class: applied && !todo ? 'btn' : 'btn primary', href: '#/layout' }, 'See changes'), h('a', { class: applied && !todo ? 'btn primary' : 'btn', href: '#/run?cmd=reorg' }, 'Re-plan')]));
  } else cards.push(card('', 'Folder layout', 'not planned', 'Plan the Artist/Album (YYYY)/NN - Title layout.', null, [h('a', { class: 'btn primary', href: '#/run?cmd=reorg' }, 'Plan')]));
  // quarantine
  const qn = s.quarantine;
  cards.push(card('', 'Quarantine', num(qn.runs) + ' run(s)', qn.runs ? 'oldest is ' + ago(qn.oldest_days) + '; nothing is deleted until you purge' : 'nothing set aside', [['duplicate / place runs', num(qn.place_or_apply)], ['reorg runs', num(qn.reorg)]],
    [h('a', { class: 'btn primary', href: '#/quarantine' }, 'Browse & undo')]));
  // library
  if (inv) cards.push(card('', 'Library', num(inv.files) + ' files', human(inv.bytes) + ' as of ' + ago(s.inventory_age), [['FLAC / lossless', num(inv.lossless)], ['MP3 etc. (lossy)', num(inv.lossy)], ['artists', num(inv.artists)], ['albums', num(inv.albums)], ['missing tags', num(inv.tags_missing)]]));

  const ro_note = ro === true ? h('div', { class: 'notice good' }, 'The library is mounted read-only in this container, so the console itself cannot change a file. Real runs go through the separate runner (arm it on the Run page with a code from your authenticator app); in this version the playlist fix, removing duplicates, filing new music, the folder layout, repointing Beets, undo and purge can all run for real from here.')
    : h('div', { class: 'notice bad' }, ro === false ? 'The library is mounted WRITABLE in this container. Change the volume to ":ro" in docker-compose.yml so the web console cannot change anything.' : 'Could not tell whether the library mount is read-only.');
  const jobs = s.recent_jobs || [];
  page().replaceChildren(
    h('h1', null, 'Overview', badge('reads + dry runs', 'b-green')),
    h('p', { class: 'lead' }, 'What needs attention in the music library, and the tools to look into it. Nothing on this page changes anything.'),
    ro_note, h('div', { class: 'grid' }, cards),
    h('h2', null, 'Recent jobs'),
    jobs.length ? jobTable(jobs) : empty('No jobs run from the web console yet.', h('a', { class: 'btn primary', href: '#/run' }, 'Open the tools')));
}

// ---------------------------------------------------------------- jobs
let runPoll = null; let runOffset = 0; let runJob = null; let runGen = 0;
// The Run page's job buttons and History box, kept here so they can be brought up to date the moment a job ends
// (they used to stay frozen at "running" until the browser was refreshed).
let RUNUI = null; let histSeq = 0;
function stopRunPoll() { clearTimeout(runPoll); runPoll = null; runGen++; }
function syncRunButtons() {
  if (!RUNUI) return;
  const busy = STATUS && STATUS.job;
  RUNUI.btns.forEach(([btn, c, hard]) => { btn.disabled = !!(c.blocked && c.blocked.length) || !!hard || !!busy; btn.title = busy ? 'another job is running' : hard ? hard : ''; });
}
async function refreshReals() {
  // a dry run just finished: fetch which dry runs a real run may be based on, and redraw each card's real-run block (not one being typed in)
  if (!RUNUI || !RUNUI.reals || !RUNUI.reals.length) return;
  const ui = RUNUI;
  try {
    const rn = await api('runner');
    if (ui !== RUNUI) return;
    RUNNER = rn;
    ui.reals.forEach(([holder, c]) => {
      if (holder.contains(document.activeElement) && document.activeElement.value) return;
      holder.replaceChildren(realBlock(c, rn));
    });
  } catch (e) {}
}
async function refreshHistory() {
  if (!RUNUI) return;
  const ui = RUNUI, my = ++histSeq;
  try {
    const jobs = await api('jobs');
    if (my !== histSeq || ui !== RUNUI) return;          // a newer refresh or another page has taken over
    ui.hist.replaceChildren(jobs.jobs.length ? jobTable(jobs.jobs) : empty('Nothing run yet.'));
  } catch (e) {}
}
async function jobEnded() {
  try { STATUS = await api('status'); } catch (e) {}
  drawSafety(); drawNav(parseHash().path); syncRunButtons();
  await Promise.all([refreshHistory(), refreshReals()]);      // History and each card's "Run for real" box now match the finished job
}
// how busy the whole NAS was (CPU, the busiest disk, load average): the answer to "can I raise the workers?"
const pct = (v) => (v === undefined || v === null) ? '?' : Math.round(v) + '%';
const liveLoad = (s) => 'CPU ' + pct(s.cpu) + '  ·  busiest disk ' + pct(s.disk) + '  ·  load ' + (s.load === undefined ? '?' : s.load);
const doneLoad = (l) => 'CPU avg ' + pct(l.cpu_avg) + ' (max ' + pct(l.cpu_max) + ')  ·  busiest disk avg ' + pct(l.disk_avg) + ' (max ' + pct(l.disk_max) + ')  ·  load max ' + (l.load_max === undefined ? '?' : l.load_max);
function jobTable(jobs) {
  const by = jobs.some(j => j.by);                       // the column only appears once more than one account has started jobs
  return h('div', { class: 'tablewrap' }, h('table', null,
    h('thead', null, h('tr', null, ['started'].concat(by ? ['by'] : [], ['tool', 'options', 'state', 'time', 'NAS busy (disk / cpu)', '']).map(x => h('th', null, x)))),
    h('tbody', null, jobs.map(j => h('tr', null,
      h('td', { class: 'mono' }, j.started), by ? h('td', { class: 'mono dim' }, j.by || '') : null, h('td', null, j.title), h('td', { class: 'mono dim wrap-any' }, (j.args || []).join(' ')),
      h('td', null, stateBadge(j.state)), h('td', { class: 'num' }, j.seconds ? j.seconds + ' s' : ''),
      h('td', { class: 'mono dim', title: j.load ? doneLoad(j.load) : 'too short to measure' }, j.load && j.load.disk_avg !== undefined ? pct(j.load.disk_avg) + ' / ' + pct(j.load.cpu_avg) : ''),
      h('td', null, h('a', { class: 'btn small', href: '#/run?job=' + j.id }, 'log')))))));
}
const stateBadge = (s) => badge(s, { running: 'b-amber', done: 'b-green', failed: 'b-red', cancelled: 'b-pink', interrupted: 'b-red' }[s] || '');
const AFTER = { scan: [['dupes', 'Review the duplicate groups']], tagaudit: [['health', 'Open the audit']], reorg: [['layout', 'See the planned changes']], place: [['new', 'See the new-music plan']], apply: [['dupes', 'Duplicates']] };

async function startJob(cmd, params) {
  try { const m = await post('jobs', { cmd, params }); go('run', { job: m.id }); }
  catch (e) { toast(e.message, true); }
}
function consoleBox(jobId) {
  const pre = h('pre', null, 'loading...'); const state = h('span'); const title = h('span', { class: 't' }); const cancel = h('button', { class: 'btn small danger', hidden: true }, 'cancel');
  const after = h('div', { class: 'after', hidden: true });
  const loadLine = h('div', { class: 'dim mono', style: 'padding:5px 14px;font-size:12px;border-bottom:1px solid var(--border)', hidden: true });
  const box = h('div', { class: 'console' }, h('div', { class: 'console-head' }, title, state, h('span', { class: 'dim mono', id: 'jobTime' }), h('span', { style: 'flex:1' }), cancel), loadLine, pre, after);
  runOffset = 0; runJob = jobId; let first = true; let atBottom = true; const gen = runGen; let lastHist = Date.now();
  pre.addEventListener('scroll', () => { atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30; });
  cancel.addEventListener('click', async () => { try { await post('jobs/' + jobId + '/cancel'); toast('cancelling...'); } catch (e) { toast(e.message, true); } });
  let text = '';
  const tick = async () => {
    let r;
    try { r = await api('jobs/' + jobId + '/log?offset=' + runOffset); } catch (e) { pre.textContent += '\n' + e.message; return; }
    if (gen !== runGen) return;                          // the page has been left: stop this poller
    if (first) { text = ''; first = false; }
    if (r.text) {
      text += r.text;
      // a carriage return rewrites the line (progress output)
      const lines = text.split('\n').map(l => l.indexOf('\r') >= 0 ? l.split('\r').filter(Boolean).pop() || '' : l);
      pre.textContent = lines.join('\n');
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    runOffset = r.offset;
    const m = r.meta; title.textContent = m.title + (m.args && m.args.length ? '  ' + m.args.join(' ') : '');
    state.replaceChildren(stateBadge(m.state)); cancel.hidden = m.state !== 'running';
    if (m.state === 'running' && m.live) { loadLine.hidden = false; loadLine.textContent = 'whole NAS right now:  ' + liveLoad(m.live); }
    else if (m.state !== 'running' && m.load) { loadLine.hidden = false; loadLine.textContent = 'whole NAS during this job:  ' + doneLoad(m.load); }
    else if (m.state !== 'running') loadLine.hidden = true;
    if (m.state === 'running') { $('#jobTime') && ($('#jobTime').textContent = Math.round((Date.now() - Date.parse(m.started.replace(' ', 'T'))) / 1000) + ' s'); }
    else if (m.seconds !== undefined && $('#jobTime')) $('#jobTime').textContent = m.seconds + ' s' + (m.rc !== null && m.rc !== 0 ? '  (exit ' + m.rc + ')' : '');
    if (m.state !== 'running' && !r.more) {
      if (!text) pre.textContent = '(no output)';
      const links = AFTER[m.cmd] || [];
      after.hidden = false;
      after.replaceChildren(...links.map(([p, l]) => h('a', { class: 'btn primary small', href: '#/' + p }, l)), h('a', { class: 'btn small', href: '#/run' }, 'Back to the tools'));
      if (gen === runGen) await jobEnded();              // buttons and History now match the finished job
      return;
    }
    if (m.state === 'running' && Date.now() - lastHist > 5000) { lastHist = Date.now(); refreshHistory(); }
    runPoll = setTimeout(tick, m.state === 'running' ? 800 : 200);
  };
  tick();
  return box;
}

let RUNNER = null;
async function pageRun(params) {
  const [cmds, rn] = await Promise.all([api('commands'), api('runner').catch(() => ({ available: false }))]);
  RUNNER = rn;
  const consoleHolder = h('div');
  RUNUI = { btns: [], reals: [], hist: h('div') };
  const gw = STATUS && STATUS.settings ? STATUS.settings.workers : null;
  const head = [h('h1', null, 'Run a job', badge(rn.available && rn.armed ? 'WRITE ACCESS ARMED' : 'reads + dry runs', rn.available && rn.armed ? 'b-red' : 'b-green')),
    h('p', { class: 'lead' }, 'Each tool below either only reads, or shows a dry run of what it would do. One job runs at a time. A real run that changes things is only possible for the tools marked below, after you arm write access; everything else is still done with dewey.ps1.'),
    runnerPanel(rn),
    gw ? h('div', { class: 'sub dim' }, 'Parallel workers for every job: ', h('b', { class: 'bright' }, gw), ' (set once on the ', h('a', { href: '#/settings' }, 'Settings page'), '; type a number on a card only to override it for that run).') : null];
  if (params.job) {
    consoleHolder.replaceChildren(consoleBox(params.job));
  } else if (STATUS && STATUS.job) {
    consoleHolder.replaceChildren(consoleBox(STATUS.job.id));
  }
  const groups = {}; cmds.forEach(c => (groups[c.group] = groups[c.group] || []).push(c));
  const body = Object.entries(groups).map(([g, list]) => [h('h2', null, g), h('div', { class: 'grid' }, list.map(c => cmdCard(c, params.cmd === c.id)))]);
  await refreshHistory();
  page().replaceChildren(...head, consoleHolder, ...body, h('h2', null, 'History'), RUNUI.hist);
  syncRunButtons();
  if (params.cmd) { const el = $('#cmd-' + params.cmd); if (el) el.scrollIntoView({ block: 'center' }); }
}

// the arming box: a 6-digit authenticator-app code (checked only inside the runner) switches write access on for a few minutes
function runnerPanel(rn) {
  const box = h('div', { class: 'armbox' });
  let timer = null;
  const reload = async () => { clearInterval(timer); try { await route(); } catch (e) { toast(e.message, true); } };
  if (!rn.available) {
    box.classList.add('off');
    box.replaceChildren(h('b', null, 'Write access is off.'), ' The runner container is not running, so this console can only read and do dry runs. To allow a few real runs from here, start it with ', h('code', null, runnerCmd()), ' (Settings > Security explains what it can and cannot do).');
    return box;
  }
  const lead = h('div', { class: 'armtxt' });
  const acts = h('div', { class: 'row armacts' });
  box.append(lead, acts);
  if (rn.armed) {
    box.classList.add('on');
    const t0 = Date.now(); const left0 = rn.armed_left;
    const show = () => { const left = Math.max(0, left0 - Math.round((Date.now() - t0) / 1000)); lead.replaceChildren(h('b', null, 'WRITE ACCESS IS ARMED'), ' for ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') + ' more. Real runs on a card below can start now. Disarm as soon as you are done.'); if (!left) reload(); };
    show(); timer = setInterval(() => { if (!document.body.contains(box)) { clearInterval(timer); return; } show(); }, 1000);
    acts.append(h('button', { class: 'btn', onclick: async () => { try { await post('runner/disarm'); toast('disarmed'); } catch (e) { toast(e.message, true); } reload(); } }, 'Disarm now'));
    return box;
  }
  const https = location.protocol === 'https:';
  box.classList.add('idle');
  lead.replaceChildren(h('b', null, 'Write access is off.'), ' Real runs are locked until you type the 6-digit code from your authenticator app (it switches on for ' + rn.arm_minutes + ' minutes, then locks again by itself). ',
    !rn.login ? h('span', { class: 'bad' }, 'It also needs a console login: set MD_WEB_USER and MD_WEB_PASS in ' + cfgFile() + '. ') : null,
    !https ? h('span', { class: 'bad' }, 'Open the console at its https address to arm it. ') : null,
    rn.locked_for ? h('span', { class: 'bad' }, 'Locked after wrong codes: try again in ' + Math.ceil(rn.locked_for / 60) + ' min. ') : null);
  const code = h('input', { type: 'text', inputmode: 'numeric', pattern: '[0-9]*', maxlength: '6', autocomplete: 'one-time-code', placeholder: '123456', class: 'armcode', disabled: !https || !rn.login || !!rn.locked_for });
  const go_ = async () => {
    if (!/^[0-9]{6}$/.test(code.value.trim())) { toast('type the 6 digits from your authenticator app', true); code.focus(); return; }
    btn.disabled = true;
    try { await post('runner/arm', { code: code.value.trim() }); toast('armed'); } catch (e) { toast(e.message, true); }
    code.value = ''; reload();
  };
  const btn = h('button', { class: 'btn primary', disabled: !https || !rn.login || !!rn.locked_for, onclick: go_ }, 'Arm for ' + rn.arm_minutes + ' minutes');
  code.addEventListener('keydown', (e) => { if (e.key === 'Enter') go_(); });
  acts.append(code, btn);
  return box;
}

// "Run for real" on a card the runner is allowed to run: tied to a finished dry run, so the options cannot differ from what was reviewed
function realBlock(c, rn) {
  const el = (rn.eligible || {})[c.id]; const need = ((rn.confirm || {})[c.id] || '').replace('{n}', el && el.n !== undefined ? el.n : '?');
  const busy = STATUS && STATUS.job;
  const conf = h('input', { type: 'text', placeholder: 'type ' + need, autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false', style: 'width:230px' });
  const needPw = !!(c.login && !rn.cred_password);
  const pw = needPw ? h('input', { type: 'password', autocomplete: 'new-password', placeholder: 'Navidrome password', style: 'width:220px' }) : null;
  const why = !rn.armed ? 'arm write access at the top of this page first' : !el ? 'do the dry run above first' : busy ? 'another job is running' : '';
  const btn = h('button', { class: 'btn danger', disabled: !!why, title: why, onclick: async () => {
    if (conf.value !== need) { toast('type ' + need + ' exactly to confirm', true); conf.focus(); return; }
    if (needPw && !pw.value) { toast('type the Navidrome password first', true); pw.focus(); return; }
    btn.disabled = true;
    try { const r = await post('runner/run', { cmd: c.id, dry_job: el.job, confirm: conf.value, nd_password: pw ? pw.value : '' }); if (pw) pw.value = ''; go('run', { job: r.job }); }
    catch (e) { toast(e.message, true); btn.disabled = false; }
  } }, 'Run for real');
  return h('div', { class: 'real' },
    h('div', { class: 'rh' }, h('b', null, 'Run for real'), badge('changes things', 'b-red')),
    el ? h('div', { class: 'd' }, 'Ready. It is based on your dry run from ' + el.finished + ' (' + el.age_minutes + ' min ago) and uses exactly its options plus --execute: ', h('span', { class: 'mono wrap-any' }, el.args.join(' ') || '(no options)'),
        c.id === 'beets-remap' && el.n !== undefined ? [h('br'), h('b', null, 'It will repoint ' + num(el.items) + ' Beets item' + (el.items === 1 ? '' : 's') + (el.art ? ' and ' + num(el.art) + ' album art path' + (el.art === 1 ? '' : 's') : '') + ' at the files’ new locations'), '. Only the paths in Beets’ library.db change: no music file is touched, ids and everything else in the database stay as they are. The database is backed up first, next to itself (library.db.bak-reorg-…), and the run stops with nothing changed if Beets has it locked. ',
          el.missing ? el.missing + ' item' + (el.missing === 1 ? '' : 's') + ' whose file is nowhere (stale rows, for example duplicates that went to the quarantine) are left as they are. ' : '',
          el.outside ? h('b', { class: 'bad' }, el.outside + ' item' + (el.outside === 1 ? ' is' : 's are') + ' outside the library root Beets is expected to use and will not be touched. ') : '',
          h('b', null, 'Pause the Beets task first'), ' (DSM Task Scheduler) and make sure no Beets command is running. The number is part of the phrase you type, and the run refuses to start if anything changed since this dry run (plan fingerprint ' + el.fp.slice(0, 8) + '). To go back, stop Beets and copy the backup over library.db.']
        : c.id === 'undo' && el.n !== undefined ? [h('br'), h('b', null, 'It will put back ' + num(el.n) + ' file' + (el.n === 1 ? '' : 's') + ' from the ' + el.kind + ' run ' + el.run + (el.playlists ? ' and restore ' + el.playlists + ' playlist file' + (el.playlists === 1 ? ' from its backup' : 's from their backups') : '')),
          el.skipped ? ' (' + el.skipped + ' entr' + (el.skipped === 1 ? 'y is' : 'ies are') + ' skipped: already back, gone, or the original place is taken, see the dry run)' : '',
          '. Nothing is deleted. ', el.kind === 'place' ? 'The set-aside files go back to the _incoming folder; run “File new music” again for them.' : el.kind === 'reorg' ? 'Every file is renamed back to where it was before the layout run. Afterwards let Navidrome rescan, and if Beets was already repointed, run “Repoint Beets after a reorg” again (it follows the move history backwards).' : 'Every file goes back to its old place in the library and the playlists that were rewritten get their backups back. Afterwards let Navidrome rescan.',
          ' The number is part of the phrase you type, and the run refuses to start if anything changed since this dry run (plan fingerprint ' + el.fp.slice(0, 8) + ').']
        : c.id === 'purge' && el.n !== undefined ? [h('br'), h('b', { class: 'bad' }, 'It will permanently delete ' + el.n + ' quarantine run' + (el.n === 1 ? '' : 's') + ' (' + el.size + ', ' + num(el.files) + ' file' + (el.files === 1 ? '' : 's') + ') older than ' + el.days + ' days. This cannot be undone.'),
          ' The files in them are gone for good, and so are their manifests, so those runs can no longer be undone. ' + el.keeping + ' younger run' + (el.keeping === 1 ? ' stays' : 's stay') + '. Only the exact runs listed as DELETE in the dry run are removed: the number is part of the phrase you type, and the run deletes nothing if a run has aged past the limit or changed since (plan fingerprint ' + el.fp.slice(0, 8) + ').']
        : c.id === 'place' && el.fp ? [h('br'), h('b', null, 'It will file ' + el.place + ' new song' + (el.place === 1 ? '' : 's') + ' into the library and set ' + el.dup + ' duplicate' + (el.dup === 1 ? '' : 's') + ' aside in the quarantine'), (el.held ? ' (' + el.held + ' stay in _incoming, not placed)' : ''), '. Nothing is deleted; Undo puts the set-aside files back in _incoming. New playlists are written for new folders; an existing playlist is left alone unless you named it under “Add new tracks to existing playlists”. The number is part of the phrase you type, and the run refuses to start if anything arrived, changed or went missing since this dry run (plan fingerprint ' + el.fp.slice(0, 8) + ').']
        : c.id === 'reorg-apply' && el.n !== undefined ? [h('br'), h('b', null, 'It will move or rename ' + el.n + ' file' + (el.n === 1 ? '' : 's') + ' (' + el.size + ') into the Artist/Album layout'), (el.skipped ? ' (' + el.skipped + ' more in the plan are skipped: changed, gone or target taken, see the dry run)' : ''), '. Navidrome’s database is backed up first (the run stops if it cannot be), .m3u playlists that point at these files are rewritten with a backup, lyrics files travel with their song, and cover art goes with its album. Nothing is deleted; Undo puts every file back. Afterwards let Navidrome rescan (playlists, stars and play counts follow the files). Beets keeps the old paths until you repoint it, so leave Beets alone until the whole layout is done. The number is part of the phrase you type, and the run refuses to start if it would move a different number.']
        : el.n !== undefined ? [h('br'), h('b', null, 'It will move ' + el.n + ' file' + (el.n === 1 ? '' : 's') + ' (' + el.size + ', ' + el.groups + ' group' + (el.groups === 1 ? '' : 's') + ') into the quarantine. '), 'Nothing is deleted; Undo puts them back. Playlist files (.m3u) that point at them are rewritten, a backup is kept. The number is part of the phrase you type, and the run refuses to start if it would move a different number.'] : '. Playlists are backed up first.')
       : h('div', { class: 'd dim' }, 'A real run starts only from a dry run of this card that finished OK on the current plan within the last ' + rn.dry_max_hours + ' hours, and each dry run can be used once. Read the dry run’s output, then come back here.' + (c.id === 'purge' ? ' A real purge needs an age limit of at least 7 days and at least one run to delete.' : c.id === 'undo' ? ' A run that was already undone has nothing left to restore.' : '')),
    h('div', { class: 'row' }, conf, pw, btn));
}

function cmdCard(c, hl) {
  const vals = {};
  const busy = STATUS && STATUS.job;
  const form = h('form', { onsubmit: (e) => e.preventDefault() });
  c.opts.forEach(o => {
    let inp;
    if (o.type === 'bool') { inp = h('input', { type: 'checkbox', onchange: (e) => { vals[o.name] = e.target.checked; } }); form.appendChild(h('label', { class: 'chk' }, inp, o.label, o.help ? h('span', { class: 'dim' }, ' ' + o.help) : null)); return; }
    if (o.type === 'set') {                       // a group of tick boxes: the ticked ones are sent
      const sel = new Set(o.default || []); vals[o.name] = Array.from(sel);
      form.appendChild(h('div', { class: 'f', style: 'width:100%;gap:4px' }, o.label,
        o.choices.map(ch => h('label', { class: 'chk', style: 'align-items:flex-start' },
          h('input', { type: 'checkbox', checked: sel.has(ch.value), onchange: (e) => { if (e.target.checked) sel.add(ch.value); else sel.delete(ch.value); vals[o.name] = Array.from(sel); } }),
          h('span', null, ch.label, ch.help ? h('span', { class: 'dim', style: 'display:block;font-size:11.5px' }, ch.help) : null)))));
      return;
    }
    // a global option is left blank: the server then uses the value from the Settings page (or rules.txt)
    const ph = o.global && o.global_value !== undefined ? (o.global_from === 'settings' ? 'global' : o.global_from) + ': ' + o.global_value : (o.placeholder || '');
    inp = h('input', { type: o.type === 'str' ? 'text' : 'number', step: o.type === 'float' ? 'any' : null, value: o.default === null || o.default === undefined ? '' : o.default, placeholder: ph, style: 'width:' + (o.type === 'str' ? '210px' : '110px'), oninput: (e) => { vals[o.name] = e.target.value; } });
    if (o.default !== null && o.default !== undefined) vals[o.name] = o.default;
    form.appendChild(h('label', { class: 'f' }, o.label, inp, o.global ? h('span', { class: 'dim', style: 'font-size:11px' }, 'blank = ', h('a', { href: '#/settings' }, 'global setting')) : null));
  });
  if (c.positional) {
    ['file_a', 'file_b'].forEach((k, i) => {
      const dl = h('datalist', { id: 'dl-' + k });
      const inp = h('input', { type: 'text', list: 'dl-' + k, placeholder: 'type artist or song to search the library', style: 'width:min(520px,80vw)' });
      inp.addEventListener('input', debounce(async () => { vals[k] = inp.value; if (inp.value.length < 2) return; try { const r = await api('search?q=' + encodeURIComponent(inp.value)); dl.replaceChildren(...r.items.map(p => h('option', { value: p }))); } catch (e) {} }, 250));
      form.appendChild(h('label', { class: 'f', style: 'width:100%' }, i ? 'File B' : 'File A', inp, dl));
    });
  }
  if (c.special === 'manifest') {
    const sel = h('select', { onchange: (e) => { vals.manifest = e.target.value; } }, h('option', { value: '' }, 'loading runs...'));
    api('quarantine').then(r => { const rs = r.runs.filter(x => x.manifest); sel.replaceChildren(h('option', { value: '' }, rs.length ? 'choose a run' : 'no runs with a manifest'), ...rs.map(x => h('option', { value: x.container_manifest }, x.run + '  (' + x.kind + ', ' + (x.files === null ? '?' : x.files) + ' files)'))); if (hl_manifest) { sel.value = hl_manifest; vals.manifest = hl_manifest; } });
    form.appendChild(h('label', { class: 'f', style: 'width:100%' }, 'Run to restore', sel));
  }
  const hl_manifest = parseHash().params.manifest;
  const hasForm = c.opts.length || c.positional || c.special;
  // a card that logs in to Navidrome asks for the password itself when navidrome.cred does not store one
  const needPw = !!(c.login && !c.login.stored);
  // the password is only typed on an https page: over plain http it would cross the network readable
  const plainPage = needPw && location.protocol !== 'https:' && !c.login.plain_ok;
  const hard = plainPage ? 'open the console over https to type the Navidrome password' : '';
  const pwInp = needPw ? h('input', { type: 'password', autocomplete: 'new-password', placeholder: 'Navidrome password', style: 'width:220px', disabled: plainPage, oninput: (e) => { vals.nd_password = e.target.value; } }) : null;
  const runBtn = h('button', { class: 'btn primary', disabled: !!(c.blocked && c.blocked.length) || !!hard || !!busy, title: busy ? 'another job is running' : hard,
    onclick: () => { if (needPw && !vals.nd_password) { toast('Type the Navidrome password first', true); pwInp.focus(); return; } startJob(c.id, vals); } }, c.kind === 'read' ? 'Run' : 'Run dry run');
  if (RUNUI) RUNUI.btns.push([runBtn, c, hard]);
  // the values this card will use if its options are left alone, so nothing important hides behind the Options button
  const eff = [];
  c.opts.forEach(o => {
    if (o.type === 'bool' || o.type === 'set' && !o.default) return;
    const lbl = o.label.toLowerCase();
    if (o.type === 'set') eff.push(lbl + ': ' + o.default.join(', '));
    else if (o.global && o.global_value !== undefined) eff.push(lbl + ': ' + o.global_value + ' (' + (o.global_from === 'settings' ? 'Settings page' : o.global_from) + ')');
    else if (o.default !== null && o.default !== undefined) eff.push(lbl + ': ' + o.default);
  });
  const optBtn = hasForm ? h('button', { class: 'btn', onclick: (e) => { card.classList.toggle('open'); e.target.textContent = card.classList.contains('open') ? 'Hide options' : 'Options'; } }, (hl || (hl_manifest && c.special)) ? 'Hide options' : 'Options') : null;
  const usesLine = eff.length ? h('div', { class: 'dim', style: 'font-size:11.5px' }, 'Uses ' + eff.join('  ·  ') + '  ',
    h('a', { href: '#/run', onclick: (e) => { e.preventDefault(); card.classList.add('open'); optBtn.textContent = 'Hide options'; } }, 'change')) : null;
  const card = h('div', { class: 'cmd' + (hl ? ' hl open' : '') + (hl_manifest && c.special ? ' open' : ''), id: 'cmd-' + c.id },
    h('h3', null, c.title, badge(c.kind === 'read' ? 'read-only' : 'dry run', c.kind === 'read' ? 'b-green' : 'b-blue'), h('span', { class: 'dim mono', style: 'font-size:11px;font-weight:400;margin-left:auto', title: 'the command name used in dewey.ps1 and in the logs' }, c.id)),
    h('div', { class: 'd' }, c.desc),
    c.blocked && c.blocked.length ? h('div', { class: 'notice warn', style: 'margin:0' }, 'Not available yet: ' + c.blocked.join('; ')) : null,
    usesLine,
    needPw ? h('label', { class: 'f' }, 'Navidrome password', pwInp, h('span', { class: 'dim', style: 'font-size:11px' }, 'asked for each run; used for that run only, never saved or logged')) : null,
    plainPage ? h('div', { class: 'notice warn', style: 'margin:0' }, 'This page is on plain http, so the password box is switched off: typed here it would cross your network readable. Open the console at its https address (see ', h('a', { href: '#/settings' }, 'Settings > Security'), '), or store password= in navidrome.cred.') : null,
    c.login && c.login.stored ? h('div', { class: 'dim', style: 'font-size:11px' }, 'logs in with the password stored in navidrome.cred') : null,
    c.login && c.login.missing && c.login.missing.length ? h('div', { class: 'notice warn', style: 'margin:0' }, 'navidrome.cred has no ' + c.login.missing.join(' or ') + '= line: add it, then run dewey.ps1 web again.') : null,
    hasForm ? form : null,
    h('div', { class: 'acts' },
      runBtn, optBtn),
    (() => {
      if (!(RUNNER && RUNNER.available && (RUNNER.commands || []).indexOf(c.id) >= 0)) return null;
      const holder = h('div', null, realBlock(c, RUNNER));      // redrawn when a dry run finishes, so its "Run for real" box appears without a page reload
      if (RUNUI) RUNUI.reals.push([holder, c]);
      return holder;
    })());
  return card;
}

// ---------------------------------------------------------------- duplicates
const D = { kind: '', status: '', flag: '', q: '', sort: 'artist', page: 1 };
let SUG = {};                       // group id -> suggested rules for it (from the last scan)
const PICK = {};                    // group id -> index of the option ticked in the bulk panel
function copyRules(lines, what) {
  const txt = lines.join('\n') + '\n';
  const done = () => toast(what + ' copied. Paste into rules.txt, deploy, then rescan.');
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done, () => toast('Copy was blocked by the browser; select the text and copy it by hand.', true));
  else toast('Copy is not available here; select the text and copy it by hand.', true);
}
function sugOption(o, extra) {
  return h('div', { class: 'sug-opt' },
    h('div', { class: 'sug-label' }, extra, o.label),
    h('div', { class: 'dim' }, o.effect),
    o.lines.length ? h('pre', { class: 'raw sug-lines' }, o.lines.join('\n')) : h('div', { class: 'dim' }, '(already set in rules.txt)'));
}
function groupSuggest(x) {
  const box = h('div', { class: 'sug', hidden: true },
    h('div', { class: 'dim' }, 'Why it is held: ' + x.why),
    x.note ? h('div', { class: 'note' }, x.note) : null,
    h('div', { class: 'dim' }, 'Pick one option. Copy its lines into rules.txt (on the PC), deploy, then rescan. Nothing changes until then.'),
    x.options.map(o => h('div', { class: 'sug-row' }, sugOption(o), h('button', { class: 'btn small', disabled: !o.lines.length, onclick: () => copyRules(o.lines, 'Rule lines') }, 'copy lines'))));
  const btn = h('button', { class: 'btn small', onclick: () => { box.hidden = !box.hidden; } }, 'Suggested rules');
  return [btn, box];
}
function suggestPanel(sg) {
  if (!sg.exists || !sg.groups.length) return null;
  const F = { q: '', kind: '' };
  const body = h('div', { hidden: true });
  const count = h('span', { class: 'dim' });
  const copyBtn = h('button', { class: 'btn small primary', onclick: () => {
    const lines = []; sg.groups.forEach(x => { const i = PICK[x.group]; if (i !== undefined && x.options[i]) x.options[i].lines.forEach(l => { if (!lines.includes(l)) lines.push(l); }); });
    if (!lines.length) return toast('Nothing ticked yet.', true);
    copyRules(lines, lines.length + ' rule line(s)');
  } }, 'Copy ticked lines');
  const list = h('div');
  const refresh = () => { const n = Object.keys(PICK).filter(k => PICK[k] !== undefined).length; count.textContent = n + ' group(s) ticked'; };
  const draw = () => {
    const q = F.q.toLowerCase();
    const rows = sg.groups.filter(x => (!F.kind || x.kind === F.kind) && (!q || (x.artist + ' ' + x.title).toLowerCase().includes(q)));
    list.replaceChildren(...rows.slice(0, 100).map(x => h('div', { class: 'sug-group' },
      h('div', null, badge(x.kind, KIND_CLS[x.kind]), ' ', h('b', null, x.title), ' ', h('span', { class: 'dim' }, x.artist + '  [' + x.group + ']')),
      h('div', { class: 'dim' }, x.why),
      h('label', { class: 'sug-pick' }, h('input', { type: 'radio', name: 'sg-' + x.group, checked: PICK[x.group] === undefined, onchange: () => { delete PICK[x.group]; refresh(); } }), ' leave undecided'),
      x.options.map((o, i) => h('label', { class: 'sug-pick' }, h('input', { type: 'radio', name: 'sg-' + x.group, checked: PICK[x.group] === i, disabled: !o.lines.length, onchange: () => { PICK[x.group] = i; refresh(); } }), ' ', o.label, o.lines.length ? '' : '  (already set)')))),
      rows.length > 100 ? h('div', { class: 'dim' }, 'Showing the first 100 of ' + rows.length + '; narrow the list with the search box.') : null, rows.length ? null : empty('Nothing matches.'));
    refresh();
  };
  body.append(
    h('p', { class: 'dim' }, 'For each song pick what you want. Copy ticked lines gives one block to paste into rules.txt (on the PC), then deploy and rescan. Options for the same song exclude each other; this page never edits rules.txt.'),
    h('div', { class: 'toolbar' }, h('input', { type: 'search', placeholder: 'search artist or title', style: 'width:260px', oninput: debounce((e) => { F.q = e.target.value; draw(); }, 200) }),
      h('select', { onchange: (e) => { F.kind = e.target.value; draw(); } }, [['', 'all kinds'], ['exact', 'exact'], ['variant', 'variant'], ['wide', 'wide gap'], ['review', 'review']].map(([v, l]) => h('option', { value: v }, l))),
      copyBtn, h('button', { class: 'btn small', onclick: () => { Object.keys(PICK).forEach(k => delete PICK[k]); draw(); } }, 'clear ticks'), count),
    list);
  draw();
  const head = h('button', { class: 'btn', onclick: () => { body.hidden = !body.hidden; } }, 'Suggested rules (' + sg.groups.length + ' groups waiting for a decision)');
  return h('div', { class: 'sug-panel' }, head, '  ', h('a', { href: '/out/suggested-rules.txt', download: 'suggested-rules.txt', class: 'dim' }, 'suggested-rules.txt'),
    sg.rules_changed ? h('div', { class: 'notice warn' }, 'rules.txt changed after this scan, so some suggestions may already be done or out of date. Rescan to refresh them.') : null, body);
}
async function pageDupes() {
  const info = await api('plan');
  let sg = { exists: false, groups: [] };
  try { sg = await api('suggestions'); } catch (e) { /* older scan: no suggestions yet */ }
  SUG = {}; sg.groups.forEach(x => { SUG[x.group] = x; });
  if (!info.exists) { page().replaceChildren(h('h1', null, 'Duplicates'), empty('No scan has been run yet.', h('a', { class: 'btn primary', href: '#/run?cmd=scan' }, 'Run the scan'))); return; }
  const list = h('div'); const bar = h('div');
  const load = async () => {
    const r = await api('groups?' + qs(Object.assign({}, D, { size: 30 })));
    list.replaceChildren(r.items.length ? r.items.map(groupCard) : empty('No groups match these filters.'), pager(r, (p) => { D.page = p; load(); window.scrollTo(0, 0); }));
  };
  const drawBar = () => {
    const search = h('input', { type: 'search', placeholder: 'search artist, title or file path', value: D.q, style: 'width:300px', oninput: debounce((e) => { D.q = e.target.value; D.page = 1; load(); }, 250) });
    const sort = h('select', { onchange: (e) => { D.sort = e.target.value; D.page = 1; load(); } }, [['artist', 'sort: artist'], ['title', 'sort: title'], ['size', 'sort: space freed'], ['plays', 'sort: plays']].map(([v, l]) => h('option', { value: v, selected: D.sort === v }, l)));
    const flag = h('select', { onchange: (e) => { D.flag = e.target.value; D.page = 1; load(); } }, [['', 'any'], ['plays', 'has plays'], ['starred', 'starred'], ['playlists', 'in playlists'], ['label', 'clean/explicit label differs']].map(([v, l]) => h('option', { value: v, selected: D.flag === v }, l)));
    bar.replaceChildren(
      chips([['', 'all kinds'], ['exact', 'exact'], ['variant', 'variant'], ['wide', 'wide gap'], ['review', 'review'], ['similar', 'similar']], D.kind, (v) => { D.kind = v; D.page = 1; drawBar(); load(); }, Object.assign({ '': info.groups }, info.kinds)),
      chips([['', 'any status'], ['removable', 'removable'], ['blocked', 'blocked'], ['review', 'needs review']], D.status, (v) => { D.status = v; D.page = 1; drawBar(); load(); }, info.status),
      h('div', { class: 'toolbar' }, search, sort, h('label', { class: 'f', style: 'flex-direction:row;align-items:center;gap:6px' }, 'show', flag)));
  };
  drawBar();
  const stale = info.stale_by && info.stale_by.length;
  page().replaceChildren(
    h('h1', null, 'Duplicates', badge(num(info.groups) + ' groups', 'b-blue')),
    h('p', { class: 'lead' }, 'Groups of files that are the same song. Green is the copy that would be kept, red would go to the quarantine, amber is held back because a playlist still points at it, purple needs a human decision. Press play on each copy to listen. This page only shows the plan; nothing here changes a file.'),
    info.gone ? h('div', { class: 'notice warn' }, num(info.gone) + ' of the ' + num(info.status.removable || 0) + ' removable groups below have already been removed from the library since this scan (their files are no longer there). Rescan to refresh the list.  ', h('a', { class: 'btn small', href: '#/run?cmd=scan' }, 'Rescan')) : null,
    stale ? h('div', { class: 'notice warn' }, 'The library was changed after this scan (' + info.stale_by.join(', ') + '). Rescan before trusting the groups.  ', h('a', { class: 'btn small', href: '#/run?cmd=scan' }, 'Rescan')) : null,
    h('div', { class: 'sub dim mono' }, 'scan v' + info.version + ' from ' + info.generated + ', tolerance ' + info.tolerance + ' s / wide ' + info.wide_tolerance + ' s, would free ' + info.removable_h + '  ·  ', h('a', { href: '/out/report.html', target: '_blank' }, 'original report.html'), '  ·  ', h('a', { href: '/out/dupes.csv', download: 'dupes.csv' }, 'dupes.csv')),
    suggestPanel(sg), bar, list);
  load();
}

function groupCard(g) {
  const body = h('div', { class: 'group-body', hidden: true });
  const card = h('div', { class: 'group' });
  let loaded = false;
  const head = h('div', { class: 'group-head' },
    badge(g.kind, KIND_CLS[g.kind]), g.gone ? badge('already removed', 'b-green') : g.status !== g.kind ? badge(g.status, STATUS_CLS[g.status]) : null, h('span', { class: 't' }, g.title), h('span', { class: 'a' }, g.artist), h('span', { class: 'spacer' }),
    h('span', { class: 'meta' }, [g.n + ' copies', g.reclaim && !g.gone ? human(g.reclaim) + ' to free' : null, g.plays ? g.plays + ' plays' : null, g.starred ? '\u2605' : null, g.playlists.length ? g.playlists.length + ' playlist' + (g.playlists.length > 1 ? 's' : '') : null, g.label ? 'label differs' : null].filter(Boolean).map(t => h('span', null, t))));
  head.addEventListener('click', async () => {
    const open = body.hidden; body.hidden = !open; card.classList.toggle('open', open);
    if (open && !loaded) { loaded = true; try { const full = await api('groups/' + g.id); body.replaceChildren(...groupBody(full)); } catch (e) { body.replaceChildren(h('div', { class: 'notice bad' }, e.message)); } }
  });
  card.append(head, body);
  return card;
}
function groupBody(g) {
  const out = [];
  if (g.note) out.push(h('div', { class: 'note' }, g.note));
  const winner = g.members.find(m => m.role === 'keep');
  g.members.forEach(m => {
    const [dir, file] = split(m.path);
    const reason = h('div', { class: 'reason clip', title: 'click to expand' }, m.reason);
    reason.addEventListener('click', () => reason.classList.toggle('clip'));
    const img = h('img', { loading: 'lazy', src: '/api/art?' + qs({ root: 'music', p: m.path }), alt: '', onerror: (e) => { e.target.remove(); } });
    out.push(h('div', { class: 'member role-' + m.role },
      h('div', { class: 'cover-wrap' }, '\u266A', m.exists ? img : null),
      h('div', null,
        h('div', { class: 'badges' }, badge(m.role, ROLE_CLS[m.role]), m.exists ? null : badge('not on disk', 'b-red')),
        h('div', { class: 'path' }, dir, h('b', null, file)),
        h('div', { class: 'facts' }, h('span', null, m.fmt), h('span', null, dur(m.duration)), h('span', null, m.size_h), h('span', null, m.album + (m.year ? ' (' + m.year + ')' : '')),
          m.plays ? h('span', { style: 'color:var(--green)' }, m.plays + ' plays') : null, m.starred ? h('span', { style: 'color:var(--amber)' }, '\u2605 starred') : null,
          m.edit ? h('span', null, 'edit: ' + m.edit) : null, m.tags_missing ? h('span', { style: 'color:var(--red)' }, 'tags missing') : null),
        m.reason ? reason : null,
        (m.pl_nd.length || m.pl_m3u.length) ? h('div', { class: 'plchips' }, [...new Set(m.pl_nd.concat(m.pl_m3u))].map(p => h('span', { class: 'plchip' }, p))) : null,
        m.blocked_by && m.blocked_by.length ? h('div', { class: 'note' }, 'held back by: ' + m.blocked_by.join(', ')) : null),
      m.exists ? playBtn('music', m.path, g.artist + ' \u2013 ' + g.title + '  [' + m.role + ', ' + m.fmt + ']') : h('span')));
  });
  const acts = [];
  if (g.members.length >= 2) {
    const a = winner || g.members[0]; const b = g.members.find(m => m !== a);
    acts.push(h('button', { class: 'btn small', title: 'decodes both files and compares the audio', onclick: () => startJob('compare', { file_a: a.path, file_b: b.path }) }, 'Compare the audio of the first two'));
  }
  acts.push(h('button', { class: 'btn small', onclick: () => { navigator.clipboard && navigator.clipboard.writeText(g.id).then(() => toast('group id ' + g.id + ' copied')); } }, 'copy group id ' + g.id));
  const sx = SUG[g.id];
  const sugParts = sx ? groupSuggest(sx) : null;
  if (sugParts) acts.push(sugParts[0]);
  out.push(h('div', { class: 'toolbar' }, acts));
  if (sugParts) out.push(sugParts[1]);
  return out;
}

// ---------------------------------------------------------------- new music
const N = { status: '', playlist: '', q: '', page: 1 };
async function pageNew() {
  const r = await api('place?' + qs(Object.assign({}, N, { size: 50 })));
  const inc = r.incoming;
  const parts = [h('h1', null, 'New music', badge('plan only', 'b-green')),
    h('p', { class: 'lead' }, 'What is waiting in the _incoming landing folder, and where the last place dry run would file it. Copying new folders over from the PC is still done with sync-music.ps1.')];
  if (r.pending) parts.push(h('div', { class: 'notice warn' }, 'A previous place run was interrupted: ' + (r.pending.playlists.length ? 'playlists still to write: ' + r.pending.playlists.join(', ') + '. ' : '') + 'Run place --execute (or sync-music.ps1) to finish them.'));
  parts.push(h('div', { class: 'grid' },
    h('div', { class: 'card ' + (inc.files ? 'good' : '') }, h('h3', null, 'Landing folder'), h('div', { class: 'big' }, num(inc.files) + ' files'),
      h('div', { class: 'sub' }, inc.exists ? (inc.folders.length ? inc.folders.length + ' folder(s) waiting' : 'empty') : '_incoming not found'),
      inc.folders.length ? h('div', { class: 'kv' }, inc.folders.slice(0, 12).map(f => [h('span', { class: 'k' }, f.files), h('span', { class: 'wrap-any' }, f.folder)])) : null,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', disabled: STATUS && STATUS.job, onclick: () => startJob('place', {}) }, 'Run the place dry run'))),
    h('div', { class: 'card' }, h('h3', null, 'Already filed'), h('div', { class: 'big' }, num(r.map_lines)), h('div', { class: 'sub' }, 'files recorded in incoming-map.tsv, so sync-music.ps1 does not send them again'))));
  if (!r.exists) { parts.push(empty('No place plan yet. Run the dry run to see where the waiting files would go.')); page().replaceChildren(...parts); return; }
  const bs = r.by_status;
  parts.push(h('h2', null, 'Last plan (' + (r.plan_mtime || '?') + ')'),
    chips([['', 'all'], ['place', 'to place'], ['duplicate', 'duplicates'], ['upgrade', 'upgrades'], ['held', 'held (no tags)']], N.status, (v) => { N.status = v; N.page = 1; pageNew(); },
      { '': Object.values(bs).reduce((a, b) => a + b, 0), place: bs.place || 0, duplicate: bs.duplicate || 0, upgrade: r.upgrades, held: bs.held || 0 }),
    h('div', { class: 'toolbar' }, h('select', { onchange: (e) => { N.playlist = e.target.value; N.page = 1; pageNew(); } }, h('option', { value: '' }, 'all playlists'), ...r.playlists.map(p => h('option', { value: p.playlist, selected: N.playlist === p.playlist }, p.playlist + '  (' + p.place + ' new, ' + p.duplicate + ' dup)'))),
      h('input', { type: 'search', placeholder: 'search file or library path', value: N.q, style: 'width:280px', onchange: (e) => { N.q = e.target.value; N.page = 1; pageNew(); } }),
      h('a', { class: 'btn small', href: '/out/place-plan.csv', download: 'place-plan.csv' }, 'place-plan.csv')),
    r.items.length ? h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['playlist', 'status', 'incoming file', 'library path', 'note'].map(x => h('th', null, x)))),
      h('tbody', null, r.items.map(x => h('tr', null, h('td', null, x.playlist), h('td', null, badge(x.note && x.note.indexOf('upgrade:') >= 0 ? 'upgrade' : x.status, { place: 'b-green', duplicate: 'b-amber', held: 'b-red', upgrade: 'b-cyan' }[x.note && x.note.indexOf('upgrade:') >= 0 ? 'upgrade' : x.status])),
        h('td', { class: 'mono wrap-any' }, x.incoming.replace(/^_incoming\//, '')), h('td', { class: 'mono wrap-any' }, x.library), h('td', { class: 'dim' }, x.note)))))) : empty('Nothing matches.'),
    pager(r, (p) => { N.page = p; pageNew(); }));
  page().replaceChildren(...parts);
}

// ---------------------------------------------------------------- layout
const L = { action: '', state: '', q: '', page: 1 };
const LSTATE = { pending: ['to do', 'b-blue'], done: ['already applied', 'b-green'], both: ['target taken', 'b-amber'], gone: ['file missing', 'b-red'] };
async function pageLayout() {
  const r = await api('reorg?' + qs(Object.assign({}, L, { size: 60 })));
  if (!r.exists) { page().replaceChildren(h('h1', null, 'Folder layout'), empty('No layout plan yet.', h('a', { class: 'btn primary', href: '#/run?cmd=reorg' }, 'Plan the layout'))); return; }
  const c = r.counts; const sc = r.state_counts || {};
  const labels = [['', 'planned changes'], ['move', 'move'], ['rename', 'rename'], ['held', 'held'], ['skip', 'skipped'], ['ok', 'already correct']];
  const changes = (c.move || 0) + (c.rename || 0); const done = sc.done || 0; const pend = sc.pending || 0; const odd = (sc.both || 0) + (sc.gone || 0);
  const banner = done ? h('div', { class: 'notice warn' },
    done >= changes ? 'All ' + num(changes) + ' changes in this plan are already in the library: the layout was applied after the plan was written (' + r.generated + '), so there is nothing left to do from it.  '
      : num(done) + ' of the ' + num(changes) + ' changes in this plan are already in the library (' + num(pend) + ' still to do' + (odd ? ', ' + num(odd) + ' need a look' : '') + '). The plan is older than the last layout run.  ',
    h('a', { class: 'btn small primary', href: '#/run?cmd=reorg' }, 'Re-plan')) : null;
  const stateChips = (done || odd) ? chips([['', 'any state'], ['pending', 'still to do'], ['done', 'already applied'], ['both', 'target taken'], ['gone', 'file missing']].filter(([v]) => !v || sc[v]), L.state, (v) => { L.state = v; L.page = 1; pageLayout(); }, Object.assign({ '': changes }, sc)) : null;
  page().replaceChildren(h('h1', null, 'Folder layout', badge('plan only', 'b-green')),
    h('p', { class: 'lead' }, 'The plan for putting every file at Artist/Album (YYYY)/NN - Title.ext, written ' + r.generated + '. Each change says why, and the state column checks the files on disk right now, so a plan that has already been applied is flagged. Tags are never changed by a reorg.'),
    banner,
    h('div', { class: 'sub dim mono' }, Object.entries(r.summary || {}).filter(([k, v]) => typeof v === 'number').map(([k, v]) => k.replace(/_/g, ' ') + ' ' + num(v)).join('  ·  '), '  ·  ', h('a', { href: '/out/reorg-report.html', target: '_blank' }, 'original reorg-report.html'), '  ·  ', h('a', { href: '/out/reorg-plan.csv', download: 'reorg-plan.csv' }, 'reorg-plan.csv')),
    chips(labels, L.action, (v) => { L.action = v; L.page = 1; pageLayout(); }, Object.assign({ '': (c.move || 0) + (c.rename || 0) + (c.held || 0) + (c.skip || 0) + (c.ext_fix || 0) + (c.loose || 0) }, c)),
    stateChips,
    h('div', { class: 'toolbar' }, h('input', { type: 'search', placeholder: 'search path or reason', value: L.q, style: 'width:320px', onchange: (e) => { L.q = e.target.value; L.page = 1; pageLayout(); } })),
    r.items.length ? h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['action', 'state', 'from', 'to', 'why'].map(x => h('th', null, x)))),
      h('tbody', null, r.items.map(x => h('tr', null, h('td', null, badge(x.action, { move: 'b-blue', rename: 'b-cyan', held: 'b-amber', skip: 'b-pink', ok: 'b-green' }[x.action])),
        h('td', null, LSTATE[x.state] ? badge(LSTATE[x.state][0], LSTATE[x.state][1]) : null),
        h('td', { class: 'mono wrap-any' }, x.path), h('td', { class: 'mono wrap-any' }, x.to && x.to !== x.path ? x.to : ''), h('td', { class: 'dim' }, x.reason)))))) : empty('Nothing to show.'),
    pager(r, (p) => { L.page = p; pageLayout(); }));
}

// ---------------------------------------------------------------- tags, art, lyrics
const T = { list: '', q: '', page: 1 };
async function pageHealth() {
  const r = await api('tagaudit?' + qs(Object.assign({}, T, { size: 50 })));
  if (!r.exists) { page().replaceChildren(h('h1', null, 'Tags, art & lyrics'), empty('No audit has been run yet.', h('a', { class: 'btn primary', href: '#/run?cmd=tagaudit' }, 'Run the audit'))); return; }
  const cv = r.cover, ly = r.lyrics;
  const tile = (title, sub, parts) => h('div', { class: 'card' }, h('h3', null, title), h('div', { class: 'sub' }, sub), bars(parts));
  const tabs = [['', 'overview'], ['core', 'missing core tags'], ['nocover', 'no cover art'], ['folderonly', 'folder art only'], ['embeddedonly', 'embedded art only'], ['nolyrics', 'no lyrics'], ['nogenre', 'no genre'], ['errors', 'unreadable']];
  const parts = [h('h1', null, 'Tags, art & lyrics', badge('audit ' + ago(r.age_days), 'b-blue')),
    h('p', { class: 'lead' }, 'What the library is missing. Lists can be downloaded from the original CSVs. Fix tags with Picard or Mp3tag, then run the reorg and the Beets remap (and check stars and playlists first, because a changed tag changes Navidrome’s track id).'),
    h('div', { class: 'sub dim mono' }, num(r.files) + ' files audited · ' + r.mtime + '  ·  ', h('a', { href: '/out/tagaudit.csv', download: 'tagaudit.csv' }, 'tagaudit.csv'), '  ·  ', h('a', { href: '/out/tagaudit-albums.csv', download: 'tagaudit-albums.csv' }, 'tagaudit-albums.csv'), '  ·  ', h('a', { href: '#/run?cmd=tagaudit' }, 're-run audit')),
    chips(tabs, T.list, (v) => { T.list = v; T.page = 1; pageHealth(); })];
  if (!T.list) {
    parts.push(h('div', { class: 'grid', style: 'margin-top:12px' },
      tile('Cover art', num(r.no_cover) + ' files have none', [['embedded + folder', cv['embedded+folder'] || 0, 'var(--green)'], ['embedded only', cv.embedded || 0, 'var(--blue)'], ['folder image only', cv.folder || 0, 'var(--amber)'], ['none', cv.none || 0, 'var(--red)']]),
      tile('Lyrics', num(r.no_lyrics) + ' files have none (instrumentals included)', Object.entries(ly).map(([k, v]) => [k, v, { none: 'var(--red)', embedded: 'var(--green)', sidecar: 'var(--cyan)' }[k] || 'var(--blue)'])),
      tile('Core tags', num(r.core_missing) + ' files miss title / artist / album / track / year', [['complete', r.files - r.core_missing, 'var(--green)'], ['missing', r.core_missing, 'var(--red)']]),
      h('div', { class: 'card' }, h('h3', null, 'Missing tags by kind'), h('div', { class: 'kv' }, Object.entries(r.missing_tags).sort((a, b) => b[1] - a[1]).map(([k, v]) => [h('span', { class: 'k' }, k), h('span', null, num(v))])))));
    parts.push(h('h2', null, 'Artists with the most tracks lacking lyrics'),
      h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, [h('th', null, 'artist'), h('th', { class: 'num' }, 'no lyrics'), h('th', { class: 'num' }, 'tracks'), h('th', { class: 'num' }, 'share')])),
        h('tbody', null, r.artists_no_lyrics.slice(0, 30).map(a => h('tr', { style: 'cursor:pointer', onclick: () => { T.list = 'nolyrics'; T.q = a.artist + '/'; T.page = 1; pageHealth(); } }, h('td', null, a.artist), h('td', { class: 'num' }, a.no_lyrics), h('td', { class: 'num' }, a.tracks), h('td', { class: 'num' }, a.share + '%')))))));
  } else {
    parts.push(h('div', { class: 'toolbar' }, h('input', { type: 'search', placeholder: 'filter by path', value: T.q, style: 'width:300px', onchange: (e) => { T.q = e.target.value; T.page = 1; pageHealth(); } })),
      r.items && r.items.length ? h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['file', 'fmt', 'missing', 'cover', 'lyrics', ''].map(x => h('th', null, x)))),
        h('tbody', null, r.items.map(x => h('tr', null, h('td', { class: 'mono wrap-any' }, x.path), h('td', null, x.format), h('td', { class: 'dim' }, x.missing || x.error), h('td', null, x.cover), h('td', null, x.lyrics), h('td', null, /\.(flac|mp3|m4a|ogg|opus|wav)$/i.test(x.path) ? playBtn('music', x.path, x.path) : null)))))) : empty('Nothing in this list.'),
      pager(r, (p) => { T.page = p; pageHealth(); }));
  }
  page().replaceChildren(...parts);
}

// ---------------------------------------------------------------- quarantine
let QV = { run: null, root: null, q: '', page: 1 };
async function pageQuarantine() {
  const r = await api('quarantine');
  const parts = [h('h1', null, 'Quarantine & undo', badge('nothing is deleted', 'b-green')),
    h('p', { class: 'lead' }, 'Everything the tool removes or sets aside goes to a run folder with a manifest, so it can be put back. Browse a run to listen to what is in it. The dry-run buttons show what an undo or a purge would do; the real thing runs from the Run page (arm it first), or with dewey.ps1.')];
  if (!r.exists) { parts.push(empty('No quarantine or reorg folders found.')); page().replaceChildren(...parts); return; }
  parts.push(h('div', { class: 'toolbar' }, h('button', { class: 'btn small', onclick: () => startJob('purge', { 'older-than': 30 }) }, 'Purge dry run (30 days)')));
  const sizeCells = [];
  parts.push(h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['run', 'kind', 'files', 'age', 'size on disk', ''].map(x => h('th', null, x)))),
    h('tbody', null, r.runs.map(x => h('tr', null, h('td', { class: 'mono' }, x.run), h('td', null, badge(x.kind, { apply: 'b-red', place: 'b-cyan', reorg: 'b-blue' }[x.kind])), h('td', { class: 'num' }, x.files === null ? '?' : num(x.files)),
      h('td', { class: 'num' }, ago(x.age_days)), (() => { const td = h('td', { class: 'num dim', title: x.kind === 'reorg' ? 'A reorg run only moves files inside the library, so it holds no copies: just a manifest and a journal.' : 'Adds up the size of every file stored in this run folder.' }, '...'); sizeCells.push([x, td]); return td; })(),
      h('td', { style: 'white-space:nowrap' }, x.manifest ? [h('button', { class: 'btn small', onclick: () => { QV.run = x.run; QV.root = x.root; QV.q = ''; QV.page = 1; pageQuarantine(); } }, 'browse'), ' ',
        h('a', { class: 'btn small', href: '#/run?cmd=undo&manifest=' + encodeURIComponent(x.container_manifest) }, 'undo dry run')] : h('span', { class: 'dim' }, 'no manifest'))))))));
  if (QV.run) {
    const d = await api('quarantine/' + encodeURIComponent(QV.root) + '/' + encodeURIComponent(QV.run) + '?' + qs({ q: QV.q, page: QV.page, size: 50 }));
    parts.push(h('h2', null, 'Run ' + d.run + ' (' + d.kind + ', ' + num(d.all_files) + ' files' + (d.playlists.length ? ', ' + d.playlists.length + ' playlist(s) rewritten' : '') + ')'),
      h('div', { class: 'toolbar' }, h('input', { type: 'search', placeholder: 'filter files', value: QV.q, style: 'width:300px', onchange: (e) => { QV.q = e.target.value; QV.page = 1; pageQuarantine(); } }), h('button', { class: 'btn small', onclick: () => { QV.run = null; pageQuarantine(); } }, 'close')),
      d.items.length ? h('div', { class: 'tablewrap' }, h('table', null, h('tbody', null, d.items.map(f => h('tr', null, h('td', { style: 'width:44px' }, f.play ? playBtn('quarantine', f.play, f.label) : null),
        h('td', { class: 'mono wrap-any' }, f.label, f.to ? h('div', { class: 'dim' }, (d.kind === 'apply' ? 'kept instead: ' : '→ ') + f.to) : null), h('td', { class: 'num dim' }, f.size ? human(f.size) : '')))))) : empty('No files match.'),
      pager(d, (p) => { QV.page = p; pageQuarantine(); }));
  }
  page().replaceChildren(...parts);
  // sizes fill in one run at a time, so the page never waits for them
  (async () => {
    for (const [x, td] of sizeCells) {
      if (!document.body.contains(td)) return;
      try { const z = await api('quarantine/size/' + encodeURIComponent(x.root) + '/' + encodeURIComponent(x.run)); td.textContent = z.reorg && !z.bytes ? 'none (moved, not copied)' : z.reorg ? z.size_h + ' (journal)' : z.size_h; }
      catch (e) { td.textContent = '?'; }
    }
  })();
}

// ---------------------------------------------------------------- rules + logs
async function pageRules() {
  const [r, stg, pl] = await Promise.all([api('rules'), api('settings'), api('plan')]);
  const act = r.rules.filter(x => x.active), off = r.rules.filter(x => !x.active);
  const eff = (k) => stg.settings[k] !== null ? [stg.settings[k], 'the Settings page'] : stg.rules[k] !== null ? [stg.rules[k], 'rules.txt'] : [stg.tool_defaults[k], 'the tool default'];
  const [t1, w1] = [eff('tolerance'), eff('wide_tolerance')];
  const tolCard = h('div', { class: 'card', style: 'margin:10px 0' }, h('h3', null, 'Duplicate matching tolerance'),
    h('div', { class: 'kv' },
      h('span', { class: 'k' }, 'strict'), h('span', null, t1[0] + ' s  ', h('span', { class: 'dim' }, 'from ' + t1[1])),
      h('span', { class: 'k' }, 'wide'), h('span', null, w1[0] + ' s  ', h('span', { class: 'dim' }, 'from ' + w1[1])),
      pl.exists ? [h('span', { class: 'k' }, 'last scan used'), h('span', null, pl.tolerance + ' s / ' + pl.wide_tolerance + ' s')] : null),
    h('div', { class: 'sub' }, 'Two copies whose lengths differ by less than the strict tolerance count as the same recording and can be removed automatically; copies up to the wide tolerance apart become WIDE-GAP groups that wait for your OK. For scans started from this console, change it on the ',
      h('a', { href: '#/settings' }, 'Settings page'), '. For every scan, including dewey.ps1, set ', h('code', null, 'tolerance:'), ' and ', h('code', null, 'wide-tolerance:'), ' in rules.txt on the PC and deploy. A one-off value can also be typed on the scan card under Run a job.'));
  const tbl = (rows) => h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['rule', 'value', 'what it does'].map(x => h('th', null, x)))),
    h('tbody', null, rows.map(x => h('tr', null, h('td', { class: 'mono' }, x.key), h('td', { class: 'mono wrap-any' }, x.value), h('td', { class: 'dim' }, x.help))))));
  page().replaceChildren(h('h1', null, 'Rules', badge('read-only view', 'b-green')),
    h('p', { class: 'lead' }, 'The exceptions in rules.txt that the tool follows. To change one, edit rules.txt on the PC and deploy; editing rules from this page comes in a later phase (the settings that are not rules, like workers and tolerance, are on the Settings page).'),
    tolCard,
    !r.exists ? empty('rules.txt not found.') : [h('h2', null, 'Active (' + act.length + ')'), act.length ? tbl(act) : empty('No active rules.'),
      off.length ? [h('h2', null, 'Available but switched off (' + off.length + ')'), tbl(off)] : null,
      h('h2', null, 'rules.txt as the tool sees it'), h('pre', { class: 'raw' }, r.text)]);
}
const LG = { src: '', real: false };
async function pageLogs() {
  const r = await api('logs'); const view = h('pre', { class: 'raw', hidden: true }); const viewTitle = h('h2', { hidden: true }, 'Log');
  const items = r.items.filter(l => (!LG.src || l.source === LG.src) && (!LG.real || l.real));
  const open = async (l) => {
    try {
      let t;
      if (l.source === 'tool') t = await (await fetch('/api/logs/' + encodeURIComponent(l.id))).text();
      else { const j = await api('jobs/' + encodeURIComponent(l.id) + '/log?offset=0'); t = j.text + (j.more ? '\n... (the first part only; the full log is under Run a job > History)' : ''); }
      viewTitle.hidden = false; viewTitle.textContent = l.title + (l.source === 'console' ? '  ' + (l.args || '') : ''); view.hidden = false; view.textContent = t; viewTitle.scrollIntoView();
    } catch (e) { toast(e.message, true); }
  };
  const src = (l) => l.source === 'tool' ? badge('run log', 'b-blue') : badge('console job', 'b-cyan');
  const mode = (l) => l.source === 'tool' ? (l.real ? badge('real run', 'b-red') : badge('dry run', 'b-green')) : stateBadge(l.state);
  const lead = h('p', { class: 'lead' }, 'Every log in one place. ', h('b', null, 'Run logs'), ' are the tool’s own audit trail: each real and dry run of apply, place, reorg-apply, playlistfix, beets-remap, undo and purge, whether it was started from dewey.ps1 or from here. ',
    h('b', null, 'Console jobs'), ' are everything started from this console, including the read-only scans and audits (which do not keep a run log of their own). A console dry run that the tool also logged is listed once, as a run log.');
  const note = !r.dir_exists || !r.tool_count ? h('div', { class: 'notice warn' },
    'No run logs were found in ' + r.dir + (r.dir_exists ? ' (the folder is empty)' : ' (the folder does not exist)') + '. Real and dry runs started from the command line or this console write theirs there, so if you have run any, check the Diagnostics on the ',
    h('a', { href: '#/settings' }, 'Settings page'), ' to see what this container can see.') : null;
  page().replaceChildren(h('h1', null, 'Logs'), lead, note,
    chips([['', 'all'], ['tool', 'run logs'], ['console', 'console jobs']], LG.src, (v) => { LG.src = v; pageLogs(); }, { '': r.items.length, tool: r.tool_count, console: r.console_count }),
    h('div', { class: 'toolbar' }, h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: LG.real, onchange: (e) => { LG.real = e.target.checked; pageLogs(); } }), 'real runs only')),
    items.length ? h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, h('tr', null, ['when', 'kind', 'tool', 'log', 'size'].map(x => h('th', null, x)))),
      h('tbody', null, items.map(l => h('tr', { style: 'cursor:pointer', onclick: () => open(l) },
        h('td', { class: 'mono' }, l.when), h('td', null, src(l), ' ', mode(l)), h('td', null, l.cmd), h('td', { class: 'mono wrap-any dim' }, l.source === 'tool' ? l.id : l.title + (l.args ? '  ' + l.args : '')), h('td', { class: 'num' }, human(l.size))))))) : empty('No logs match.'),
    viewTitle, view);
}

// ---------------------------------------------------------------- settings + diagnostics
const WIDGET_FIELDS = [
  ['duplicate_groups_open', 'duplicate groups still to deal with', 'number'],
  ['space_to_free_bytes', 'space the removals would free', 'bytes'],
  ['waiting_in_incoming', 'files waiting in _incoming', 'number'],
  ['layout_changes_pending', 'folder-layout changes not applied yet', 'number'],
  ['tags_missing', 'files missing core tags', 'number'],
  ['no_cover_art', 'files with no cover art', 'number'],
  ['no_lyrics', 'files with no lyrics', 'number'],
  ['quarantine_runs', 'quarantine run folders on disk', 'number'],
  ['scan_age_days', 'days since the last scan', 'number'],
  ['job', 'idle, or "<command> running"', 'text'],
  ['library_read_only', 'true when the library is mounted read-only', 'text'],
  ['files', 'files in the library (last scan)', 'number'],
  ['bytes', 'library size in bytes (last scan)', 'bytes'],
  ['version', 'tool version', 'text'],
];
const WIDGET_YAML = `- Dewey:
    href: ${location.origin}
    description: Music Library Tool
    icon: mdi-bookshelf
    server: my-docker
    container: dewey-web
    widget:
      type: customapi
      url: http://dewey-web:8099/api/widget
      refreshInterval: 300000
      headers:
        Authorization: "Bearer {{HOMEPAGE_VAR_DEWEY_WIDGET_TOKEN}}"
      mappings:
        - field: duplicate_groups_open
          label: Duplicates
          format: number
        - field: space_to_free_bytes
          label: Reclaimable
          format: bytes
        - field: waiting_in_incoming
          label: New music
          format: number
        - field: tags_missing
          label: Tags missing
          format: number`;
function widgetTable(wd) {
  const shown = (k) => wd === null ? '?' : (wd[k] === null || wd[k] === undefined) ? 'none yet' : (typeof wd[k] === 'number' && /bytes/.test(k)) ? human(wd[k]) : String(wd[k]);
  return h('div', { class: 'tablewrap' }, h('table', null,
    h('thead', null, h('tr', null, ['field', 'what it is', 'Homepage format', 'now'].map(x => h('th', null, x)))),
    h('tbody', null, WIDGET_FIELDS.map(f => h('tr', null, h('td', { class: 'mono' }, f[0]), h('td', null, f[1]), h('td', { class: 'mono dim' }, f[2]), h('td', { class: 'mono' }, shown(f[0])))))));
}
async function pageSettings() {
  const [st, dg, sec] = await Promise.all([api('settings'), api('diagnostics'), api('security')]);
  let wd = null; try { wd = await api('widget'); } catch (e) { wd = null; }
  const s = st.settings;
  const w = h('input', { type: 'number', min: 1, max: 16, value: s.workers, style: 'width:90px' });
  const t = h('input', { type: 'number', step: 'any', min: 0, value: s.tolerance === null ? '' : s.tolerance, placeholder: (st.rules.tolerance !== null ? 'rules.txt: ' + st.rules.tolerance : 'default: ' + st.tool_defaults.tolerance), style: 'width:120px' });
  const wt = h('input', { type: 'number', step: 'any', min: 0, value: s.wide_tolerance === null ? '' : s.wide_tolerance, placeholder: (st.rules.wide_tolerance !== null ? 'rules.txt: ' + st.rules.wide_tolerance : 'default: ' + st.tool_defaults.wide_tolerance), style: 'width:120px' });
  const save = async (body) => { try { await post('settings', body); STATUS = await api('status'); toast('Settings saved'); pageSettings(); } catch (e) { toast(e.message, true); } };
  const lvl = { ok: ['ok', 'b-green'], warn: ['warning', 'b-amber'], bad: ['problem', 'b-red'], info: ['info', ''] };
  const groups = {}; dg.checks.forEach(c => (groups[c.group] = groups[c.group] || []).push(c));
  const diag = Object.entries(groups).map(([g, list]) => [h('h2', null, g), h('div', { class: 'tablewrap' }, h('table', null, h('tbody', null, list.map(c => h('tr', null,
    h('td', { style: 'width:80px' }, badge(lvl[c.level][0], lvl[c.level][1])), h('td', null, c.name), h('td', { class: 'dim' }, c.detail, c.path ? h('div', { class: 'mono wrap-any', style: 'font-size:11.5px' }, c.path) : null))))))]);
  // security: the first row is judged by the browser itself (what address this page was opened at)
  const isHttps = location.protocol === 'https:';
  const secRows = [{ name: 'This page', level: isHttps ? 'ok' : 'bad',
    detail: isHttps ? 'opened over https: encrypted from this browser to the NAS' : 'opened over plain http: your console login crosses the network readable, and the Navidrome password box is switched off',
    fix: isHttps ? '' : 'Open the console at its https address instead (set up once with Tailscale Serve; see the README, "Security").' }].concat(sec.rows);
  const secTable = h('div', { class: 'tablewrap' }, h('table', null, h('tbody', null, secRows.map(c => h('tr', null,
    h('td', { style: 'width:80px' }, badge(lvl[c.level][0], lvl[c.level][1])), h('td', null, c.name),
    h('td', { class: 'dim' }, c.detail, c.fix ? h('div', { style: 'margin-top:3px;color:var(--text)' }, 'Fix: ', h('span', { class: 'mono wrap-any', style: 'font-size:11.5px' }, c.fix)) : null))))));
  page().replaceChildren(h('h1', null, 'Settings', badge('console only', 'b-green')),
    h('p', { class: 'lead' }, 'Set once, used by every job. These are the console’s own settings: they are saved in out/web-settings.json and never touch the library, rules.txt or the tool. A card on Run a job can still override a value for a single run.'),
    h('div', { class: 'card', style: 'max-width:760px' }, h('h3', null, 'Jobs'),
      h('label', { class: 'f' }, 'Parallel workers', w),
      h('div', { class: 'sub' }, 'How many files a scan, tag audit, layout plan or new-music check reads at the same time. Reading tags is mostly waiting for the NAS disks, so a few at once is faster than one. 4 suits a small NAS when nothing else is busy; use 2 or 3 if your media server or other services should stay responsive, 1 to keep the NAS as quiet as possible. More than 4 rarely helps on spinning disks. To tune it by evidence: every finished job shows how busy the whole NAS was (CPU and the busiest disk); if the busiest disk already averages around 85% or more at your setting, more workers will not make it faster, only slower for everything else.'),
      h('h3', { style: 'margin-top:10px' }, 'Duplicate matching'),
      h('div', { class: 'f-row', style: 'display:flex;gap:16px;flex-wrap:wrap' }, h('label', { class: 'f' }, 'Tolerance (s)', t), h('label', { class: 'f' }, 'Wide tolerance (s)', wt)),
      h('div', { class: 'sub' }, 'Leave blank to use rules.txt (' + (st.rules.tolerance !== null ? st.rules.tolerance : st.tool_defaults.tolerance) + ' s strict, ' + (st.rules.wide_tolerance !== null ? st.rules.wide_tolerance : st.tool_defaults.wide_tolerance) + ' s wide). A value here applies to scans started from this console only; dewey.ps1 keeps following rules.txt, so set the same value there if you want both to agree. A longer tolerance removes more automatically, so raise it only after looking at the WIDE-GAP groups it would absorb.'),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', disabled: !st.writable, onclick: () => save({ workers: w.value, tolerance: t.value, wide_tolerance: wt.value }) }, 'Save'),
        h('button', { class: 'btn', disabled: !st.writable, onclick: () => save({ workers: '', tolerance: '', wide_tolerance: '' }) }, 'Reset to defaults'),
        st.writable ? null : h('span', { class: 'dim' }, 'the out folder is not writable from this container'))),
    h('h2', { style: 'margin-top:22px' }, 'Security'),
    h('p', { class: 'sub dim' }, 'How exposed this console is right now, and the exact line that fixes each weak spot. Lines starting MD_ go in ' + cfgFile() + (isCompose() ? '; then run docker compose up -d.' : ' on the NAS; then run .\\dewey.ps1 web.')),
    secTable,
    h('h2', { style: 'margin-top:22px' }, 'Dashboard widget (Homepage)'),
    h('p', { class: 'sub dim' }, 'The one endpoint meant for other programs: a handful of counts, no names or paths. Inside Docker, Homepage calls ', h('code', null, 'http://dewey-web:8099/api/widget'), ' with the header ', h('code', null, 'Authorization: Bearer <MD_WIDGET_TOKEN>'),
      ' (token and network are set in web.env). In this browser the same data is at ', h('code', null, location.origin + '/api/widget'), ' with your normal login. Everything else under /api/ is this console’s own plumbing and can change between versions.'),
    widgetTable(wd),
    h('details', { style: 'margin:10px 0 0' }, h('summary', { class: 'dim', style: 'cursor:pointer' }, 'Homepage tile example (services.yaml)'),
      h('pre', { class: 'raw' }, WIDGET_YAML),
      h('button', { class: 'btn small', onclick: () => { try { navigator.clipboard.writeText(WIDGET_YAML); toast('Copied'); } catch (e) { toast('Select the text and copy it by hand', true); } } }, 'copy')),
    h('div', { class: 'toolbar', style: 'margin-top:22px' }, h('h2', { style: 'margin:0' }, 'What this console can see'), h('button', { class: 'btn small', onclick: () => pageSettings() }, 'check again')),
    h('p', { class: 'sub dim' }, 'A quick health check of the folders, databases and connections the console depends on. If a page looks empty or out of date, the reason is usually shown here.'),
    diag);
}

route();
setInterval(async () => { try { const s = await api('status'); const was = STATUS && STATUS.job; STATUS = s; drawSafety(); drawNav(parseHash().path); if (was && !s.job) { const p = parseHash().path; if (p === '') route(); else if (p === 'run') { syncRunButtons(); refreshReals(); refreshHistory(); } } } catch (e) {} }, 15000);
