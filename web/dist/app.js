/* WOOF 介面：對話、串流、工具卡片、模型選單、插件、設定、任務與額度。 */
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
// 後端位址與 token 由 prefs.js 準備好（桌面版來自 preload，埠每次隨機；瀏覽器版同主機＋網址上的 token）
const CONN = window.WOOF_CONN || { base: '', token: new URLSearchParams(location.search).get('token') || '' };
const REMOTE_KEY = 'woof.remoteBase';
const LOGIN_KEY = 'woof.login';
let TOKEN = CONN.token;
let BASE = CONN.base;
const D = window.woofDesktop || null;
const ASKDONE_TTL = 8000;   // 已回答的卡片留在畫面上多久（毫秒）
const NL = String.fromCharCode(10);
const ico = (name, cls = '') => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

const EFFORT_LABEL = { none: '關', off: '關', on: '開', minimal: '最小', low: '低', medium: '中', high: '高', xhigh: '極高', max: '最高', ultra: '超高' };
const EFFORT_SCALE = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
const EFFORT_HINT = { on: '開啟思考，先推理再回答', off: '不思考，最快', none: '不思考，最快', minimal: '幾乎不思考', low: '快速回答', medium: '一般難度', high: '仔細推理', xhigh: '更深入推理', max: '想得更久', ultra: '想最久，慢但最完整' };
const TOOL_ICON = { delegate_task: '🤖', terminal: '⌨️', read_file: '📄', write_file: '✏️', patch: '🩹', search_files: '🔎', list_tree: '🗂️', skill_view: '📘',
  skills_list: '📚', web_search: '🌐', web_extract: '🌐', vision_analyze: '👁️', Bash: '⌨️', Read: '📄', Write: '✏️',
  Edit: '🩹', Grep: '🔎', Glob: '🔎', WebSearch: '🌐', WebFetch: '🌐', Task: '🤖', TodoWrite: '📝' };
const BACKEND_LABEL = { openai_compat: 'OpenAI 相容 API', anthropic: 'Anthropic', claude_cli: 'Claude Code CLI' };
const TOOL_POOL_LABEL = { firecrawl: 'Firecrawl', tavily: 'Tavily' };
const TOOL_POOL_DESC = { firecrawl: '網頁搜尋與擷取', tavily: '網頁搜尋' };

localStorage.removeItem('woof.perm'); localStorage.removeItem('woof.effortWant'); localStorage.removeItem('woof.model');   // 舊版把權限、強度、模型記成全域，會帶到別的對話
const S = {
  draft: {}, follow: true,
  models: [], vendors: [], backends: [], plugins: [], keys: {}, sessions: [], sid: null, msgs: [],
  running: new Set(), live: {}, tools: {}, errors: {}, usage: {}, attach: [], plugFilter: '全部', ws: null, approvals: {}, asks: {}, askDone: {}, todos: {}, planOn: {}, gate: {}, subOpen: new Set(), subShut: new Set(), act: {}, groups: [], drag: null,
  modelId: null, workdir: '', tasks: [], queue: {}, srvQ: {}, resumeHide: {}, interrupting: {}, askDraft: {},
  pins: new Set(JSON.parse(localStorage.getItem('woof.pins') || '[]')),
  profile: null,
  cfg: null, dirty: false, setSec: localStorage.getItem('woof.setSec') || 'vendors', openVendors: new Set(),
  openPlugins: new Set(),
};

// ---- 分頁：一個視窗可以同時開幾個對話。每個分頁各自記住自己的訊息、捲動位置、草稿與附件；
//      S.sid／S.msgs／S.follow／S.attach／S.draft／S.liveThink 都指向「目前這個分頁」，
//      所以右側面板、TODO、額度、終端機一律跟著目前的分頁走，不會指錯對話。
const TAB_MAX = 4;
S.tabs = []; S.tab = 0;
const tabNew = (sid) => ({ sid: sid || null, msgs: [], follow: true, attach: [], files: [], draft: {}, liveThink: null, top: 0, loaded: false, text: '' });
const tab = () => S.tabs[S.tab] || (S.tabs[S.tab] = tabNew(null));
const tabOf = (sid) => (sid ? S.tabs.find((t) => t.sid === sid) : null);
for (const k of ['sid', 'msgs', 'follow', 'attach', 'files', 'draft', 'liveThink']) {
  Object.defineProperty(S, k, { configurable: true, get: () => tab()[k], set: (v) => { tab()[k] = v; } });
}
function saveTabs() {
  localStorage.setItem('woof.tabs', JSON.stringify(S.tabs.map((t) => t.sid || null)));
  localStorage.setItem('woof.tab', String(S.tab));
}
// 重開程式：把上次開的分頁放回來（對話已經被刪掉的丟掉），作用中的分頁不一定是第一個
function restoreTabs() {
  let arr = [];
  try { const raw = JSON.parse(localStorage.getItem('woof.tabs') || '[]'); if (Array.isArray(raw)) arr = raw; } catch { arr = []; }
  const ids = new Set(S.sessions.map((s) => s.id));
  const tabs = arr.slice(0, TAB_MAX).map((sid) => tabNew(typeof sid === 'string' && ids.has(sid) ? sid : null));
  S.tabs = tabs.length ? tabs : [tabNew(null)];
  const i = Number(localStorage.getItem('woof.tab'));
  S.tab = Number.isInteger(i) && i >= 0 && i < S.tabs.length ? i : 0;
}
// ── 並排兩個對話：右半邊是同一支畫面的 iframe（網址帶 ?pane=2&sid=…），
//    它只顯示對話區，後端位址與 token 由母視窗帶過去（iframe 拿不到桌面版 preload）。
const PANE2 = new URLSearchParams(location.search).get('pane') === '2';
if (PANE2) document.body.classList.add('pane2');

function ensurePanes() {
  let panes = $('#panes');
  if (panes) return panes;
  const chatEl = $('#chat'), compEl = $('.composer'), view = $('#chatView');
  panes = document.createElement('div'); panes.id = 'panes'; panes.className = 'panes';
  const a = document.createElement('div'); a.id = 'paneA'; a.className = 'pane';
  view.insertBefore(panes, chatEl);
  panes.appendChild(a);
  a.appendChild(chatEl);
  a.appendChild(compEl);
  return panes;
}

function closeSplit() {
  const b = $('#paneB');
  if (b) b.remove();
  S.split = null;
  renderTabbar();
}

function openSplit(sid) {
  if (!sid) return;
  const panes = ensurePanes();
  let b = $('#paneB');
  if (!b) { b = document.createElement('div'); b.id = 'paneB'; b.className = 'pane'; panes.appendChild(b); }
  const s = S.sessions.find((x) => x.id === sid);
  const conn = window.WOOF_CONN || {};
  const q = new URLSearchParams({ pane: '2', sid, token: conn.token || '', base: conn.base || '' });
  b.innerHTML = `<div class="phead">${ico('chat')}<span class="ptitle"></span>`
    + `<span class="pclose" data-splitclose title="關閉並排">${ico('x')}</span></div>`
    + `<iframe src="index.html?${esc(q.toString())}" title="並排對話"></iframe>`;
  $('.ptitle', b).textContent = (s && s.title) || '新對話';
  S.split = sid;
  renderTabbar();
}

function toggleSplit() {
  if (S.split) { closeSplit(); return; }
  // 用「不是目前這個」的分頁來並排；只有一個分頁就並排自己（例如邊看邊問）
  const other = S.tabs.filter((t) => t.sid && t.sid !== S.sid).pop();
  const sid = other ? other.sid : S.sid;
  if (!sid) { notify('info', '還沒有對話', '先開一個有訊息的對話，再按並排。'); return; }
  openSplit(sid);
}

function renderTabbar() {
  const bar = $('#tabbar'); if (!bar) return;
  bar.innerHTML = S.tabs.map((t, i) => {
    const s = t.sid ? S.sessions.find((x) => x.id === t.sid) : null;
    const title = (t.sid && s && s.title) || '新對話';
    const run = !!(t.sid && S.running.has(t.sid));
    const wait = !!(t.sid && (Object.values(S.asks).some((a) => a.session_id === t.sid) || Object.values(S.approvals).some((a) => a.session_id === t.sid)));
    return `<div class="tab${i === S.tab ? ' on' : ''}" data-tab="${i}" title="${esc(title)}">`
      + (run ? '<i class="run"></i>' : wait ? '<i class="wait"></i>' : '')
      + `<span class="tt">${esc(title)}</span><span class="tx" data-tabx="${i}" title="關閉分頁">${ico('x')}</span></div>`;
  }).join('') + '<span class="tabnew" data-tabnew title="新分頁">＋</span>'
    + `<span class="tabsp${S.split ? ' on' : ''}" data-split title="並排另一個對話">${ico('split')}</span>`;
}
// 切到某個分頁：第一次或換過對話才抓訊息，之後整組重畫
async function showTab(i, fresh) {
  S.tab = i;
  const t = tab();
  S.progTop = null; S.lastTop = null;
  if (fresh) { t.loaded = false; t.top = 0; t.follow = true; }
  if (t.sid && !t.loaded) await loadTab(t);
  localStorage.setItem('woof.sid', t.sid || '');
  showView('chat');
  renderAll();
  input.value = t.text || '';
  autosize();
  const top = t.top || 0;
  requestAnimationFrame(() => {
    const c = $('#chat'); if (!c) return;
    if (t.follow) stickBottom(); else if (top) c.scrollTop = top;
    updateToBottom();
  });
}
async function switchTab(i) {
  if (!S.tabs[i]) return;
  if (i === S.tab) { const t = tab(); if (!t.loaded) { await loadTab(t); renderAll(); } return; }
  tab().text = input.value;
  await showTab(i);
  $('#input').focus();
}
function closeTab(i) {
  if (!S.tabs[i]) return;
  S.tabs.splice(i, 1);
  if (!S.tabs.length) { S.tabs = [tabNew(null)]; S.tab = 0; }
  else if (S.tab > i) S.tab--;
  else if (S.tab >= S.tabs.length) S.tab = S.tabs.length - 1;
  S.progTop = null; S.lastTop = null;
  const t = tab();
  localStorage.setItem('woof.sid', t.sid || '');
  if (t.sid && !t.loaded) loadTab(t).then(renderAll); else renderAll();
}
// ＋：開一個新的空白分頁；分頁已滿就沿用目前這個分頁開新對話（舊對話留在側邊欄）
function newTab() {
  if (S.tabs.length < TAB_MAX) {
    const prev = tab();
    if (prev.sid) prev.top = $('#chat').scrollTop;
    S.tabs.push(tabNew(null));
    showTab(S.tabs.length - 1);
  } else {
    const i = S.tabs.length - 1;
    showTab(i);
    newSession();
  }
  $('#input').focus();
}
// 刪掉的對話如果開著，把它從分頁列移除
function dropTabsOf(gone) {
  const keep = S.tabs.filter((t) => !(t.sid && gone.has(t.sid)));
  if (keep.length === S.tabs.length) return;
  S.tabs = keep.length ? keep : [tabNew(null)];
  S.tab = Math.min(S.tab, S.tabs.length - 1);
  saveTabs();
}
// 側邊欄按 Ctrl／Cmd 點對話：開成一個新分頁
function openInTab(sid) {
  const open = tabOf(sid);
  if (open) { switchTab(S.tabs.indexOf(open)); return; }
  if (S.tabs.length >= TAB_MAX) { notify('warn', `最多只能開 ${TAB_MAX} 個分頁`, '先關掉一個分頁，再按一下這個對話。'); return; }
  S.tabs.push(tabNew(sid));
  showTab(S.tabs.length - 1, true);
}
async function loadTab(t) {
  const sid = t.sid;
  api('GET', '/api/sessions/' + sid + '/tasks')
    .then((x) => { S.todos[sid] = x.todos || []; S.planOn[sid] = !!x.plan; S.gate[sid] = x.gate || 'off'; if (tab() === t) renderTaskBar(); })
    .catch(() => {});
  t.msgs = sid ? await api('GET', `/api/sessions/${sid}/messages`) : [];
  t.loaded = true;
  const s = S.sessions.find((x) => x.id === sid);
  if (s && s.engine_state && s.engine_state.usage && !S.usage[sid]) S.usage[sid] = s.engine_state.usage;
}
function renderAll() {
  renderSessions(); renderTitle(); renderModelSelect(); renderChat(); renderComposer(); renderMeter(); renderFiles(); saveTabs();
}
$('#tabbar').addEventListener('click', (e) => {
  const x = e.target.closest('[data-tabx]');
  if (x) { closeTab(Number(x.dataset.tabx)); return; }
  if (e.target.closest('[data-tabnew]')) { newTab(); return; }
  if (e.target.closest('[data-split]')) { toggleSplit(); return; }
  if (e.target.closest('[data-splitclose]')) { closeSplit(); return; }
  const t = e.target.closest('[data-tab]');
  if (t) switchTab(Number(t.dataset.tab));
});

// ------------------------------------------------------------------ 共用
async function api(method, path, body, retry = true) {
  const r = await fetch(BASE + path, {
    method, headers: { 'x-woof-token': TOKEN, ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && retry && path !== '/api/login') {
    const tok = await askLogin('這個主機要密碼，登入後會繼續。');
    if (tok) return api(method, path, body, false);
  }
  if (!r.ok) {
    const raw = await r.text();
    let msg = raw;
    try { const j = JSON.parse(raw); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || j); } catch {}
    throw new Error(`${msg.slice(0, 400)}（HTTP ${r.status}）`);
  }
  return r.json();
}
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ---- 後端連線：登入憑證、改連到別台電腦
// 憑證存在這台裝置（localStorage），換裝置或過期會再登入；後端只留雜湊。
function loginGet() {
  try { return JSON.parse(localStorage.getItem(LOGIN_KEY) || 'null'); } catch { return null; }
}
function loginSet(token, exp, base) {
  if (!token) { localStorage.removeItem(LOGIN_KEY); return; }
  localStorage.setItem(LOGIN_KEY, JSON.stringify({ token, exp: exp || 0, base: base || '' }));
}
function loginRestore() {
  const s = loginGet();
  if (!s || !s.token) return;
  if (s.base && s.base !== BASE) return;                      // 換了主機，憑證不能沿用
  if (s.exp && s.exp * 1000 <= Date.now()) { loginSet(''); return; }
  TOKEN = s.token;
}
async function askLogin(desc) {
  let token = '';
  const ok = await modal({
    title: '登入 WOOF',
    desc: desc || '這個主機要密碼才能使用。',
    body: '<label class="fld"><span>密碼</span><input type="password" data-lp autocomplete="current-password">'
      + '<small>登入後這台裝置會記住，直到憑證過期；很久沒用或換裝置會再問一次。</small></label>',
    ok: '登入', cancel: '',
    onOk: async (m) => {
      const pw = $('[data-lp]', m).value;
      if (!pw) { notify('warn', '請輸入密碼'); return false; }
      const btn = $('[data-m="ok"]', m);
      btn.classList.add('busy');
      try {
        const r = await fetch(BASE + '/api/login', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password: pw }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          notify('error', '登入失敗', typeof j.detail === 'string' ? j.detail : `HTTP ${r.status}`);
          return false;
        }
        if (!j.token) { notify('warn', '這台主機沒有設密碼'); return false; }
        token = j.token;
        TOKEN = token;
        loginSet(token, j.expires, BASE);
        notify('ok', '已登入');
        return true;
      } catch (e) {
        notify('error', '連不上後端', e.message);
        return false;
      } finally {
        btn.classList.remove('busy');
      }
    },
  });
  return ok ? token : '';
}
function remoteBase() {
  if (window.WOOF_CONN && window.WOOF_CONN.remote) return window.WOOF_CONN.base || '';
  try { return (localStorage.getItem(REMOTE_KEY) || '').trim().replace(/\/+$/, ''); } catch { return ''; }
}
async function connectRemote() {
  const now = remoteBase();
  const v = await promptBox('連線到…', '後端位址', now || (BASE ? BASE : ''), {
    desc: '別台電腦上的 WOOF，例如 http://192.168.1.10:8000 或 https://woof.example.com。'
      + '留空＝用本機後端。改了會重新載入畫面。',
    placeholder: 'http://192.168.1.10:8000', mono: true,
  });
  if (v === null) return;
  const url = (v || '').trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\/[^\s]+$/i.test(url)) { notify('warn', '位址格式不對，要像 http://192.168.1.10:8000'); return; }
  // 桌面版寫進 data/desktop-conn.json（主程序據此決定要不要啟動本機後端）；瀏覽器版存這台裝置
  if (D && D.remoteSet) { await D.remoteSet(url); }
  else if (url) localStorage.setItem(REMOTE_KEY, url); else localStorage.removeItem(REMOTE_KEY);
  loginSet('');
  location.reload();
}
// ---- 通知：右上角堆疊。錯誤不會自己消失，可展開詳細、可複製。
function notify(type, title, detail = '', ms) {
  if (PANE2) return;   // 並排的第二個畫面不再跳通知，母視窗已經跳過一次
  const box = $('#notes');
  const el = document.createElement('div');
  el.className = 'note ' + type;
  const icon = { error: 'alert', warn: 'alert', ok: 'check', info: 'info' }[type] || 'info';
  detail = String(detail || '');
  el.innerHTML = `<span class="ni">${ico(icon)}</span><b></b><span class="nx" title="關閉">${ico('x')}</span>`
    + (detail ? '<p></p>' : '')
    + (detail && type === 'error' ? '<div class="nacts"><a data-n="more">顯示詳細</a><a data-n="copy">複製</a></div>' : '');
  $('b', el).textContent = title;
  if (detail) $('p', el).textContent = detail;
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 200); };
  el.addEventListener('click', (e) => {
    if (e.target.closest('.nx')) return close();
    const a = e.target.closest('[data-n]'); if (!a) return;
    if (a.dataset.n === 'more') { el.classList.toggle('open'); a.textContent = el.classList.contains('open') ? '收合' : '顯示詳細'; }
    if (a.dataset.n === 'copy') { navigator.clipboard.writeText(title + NL + detail); a.textContent = '已複製'; }
  });
  box.appendChild(el);
  while (box.children.length > 5) box.firstElementChild.remove();
  const life = ms != null ? ms : type === 'error' ? 0 : type === 'warn' ? 6000 : 2600;
  if (life) {
    let t = setTimeout(close, life);
    el.onmouseenter = () => clearTimeout(t);
    el.onmouseleave = () => { t = setTimeout(close, 1800); };
  }
  return el;
}
// 舊呼叫點：依文字判斷類型，「標題：內容」拆成兩段
function toast(t, ms) {
  t = String(t || '');
  const type = /失敗|錯誤|無法|中斷|意外|讀不到|拒絕/.test(t) ? 'error' : /^(已|成功)|成功|完成/.test(t) ? 'ok' : /注意|需要|先/.test(t) ? 'warn' : 'info';
  const i = t.indexOf('：');
  if (type === 'error' && i > 0) return notify(type, t.slice(0, i), t.slice(i + 1), ms);
  return notify(type, t, '', ms);
}

// ---- 對話框：取代瀏覽器的 confirm／prompt
function modal({ title, desc = '', body = '', ok = '確定', cancel = '取消', danger = false, wide = false, onOpen, onOk, extra = '' }) {
  return new Promise((resolve) => {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal${wide ? ' wide' : ''}"><div class="mhd"></div>${desc ? '<div class="mdesc"></div>' : ''}<div class="mbd">${body}</div>`
      + `<div class="mft">${extra}<span class="grow"></span>${cancel ? `<span class="btn" data-m="cancel">${esc(cancel)}</span>` : ''}<span class="btn ${danger ? 'danger-fill' : 'primary'}" data-m="ok">${esc(ok)}</span></div></div>`;
    $('.mhd', bg).textContent = title;
    if (desc) $('.mdesc', bg).textContent = desc;
    const m = $('.modal', bg);
    let done = false;
    const finish = (v) => { if (done) return; done = true; document.removeEventListener('keydown', key, true); bg.remove(); resolve(v); };
    const doOk = async () => {
      const okBtn = $('[data-m="ok"]', bg);
      if (!onOk) return finish(true);
      okBtn.classList.add('busy');
      try { const v = await onOk(m); if (v !== false) finish(v === undefined ? true : v); }
      catch (e) { notify('error', '操作失敗', e.message); }
      finally { okBtn.classList.remove('busy'); }
    };
    const key = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); finish(false); }
      if (e.key === 'Enter' && !e.isComposing && e.target.tagName !== 'TEXTAREA') { e.preventDefault(); e.stopPropagation(); doOk(); }
    };
    document.addEventListener('keydown', key, true);
    bg.addEventListener('mousedown', (e) => { if (e.target === bg) finish(false); });
    bg.addEventListener('click', (e) => {
      const b = e.target.closest('[data-m]'); if (!b) return;
      if (b.dataset.m === 'cancel') finish(false); else if (b.dataset.m === 'ok') doOk();
    });
    document.body.appendChild(bg);
    if (onOpen) onOpen(m, finish);
    const f = $('input:not([type=checkbox]),textarea', m); if (f) { f.focus(); if (f.select) f.select(); }
  });
}
const confirmBox = (title, desc, opts = {}) => modal({ title, desc, ok: opts.ok || '確定', cancel: opts.cancel === undefined ? '取消' : opts.cancel, danger: !!opts.danger });
async function promptBox(title, label, value = '', opts = {}) {
  let out = null;
  const ok = await modal({
    title, desc: opts.desc || '',
    body: `<label class="fld"><span>${esc(label)}</span><input data-p value="${esc(value)}" placeholder="${esc(opts.placeholder || '')}"${opts.mono ? ' class="mono"' : ''}></label>`,
    onOk: (m) => { out = $('[data-p]', m).value.trim(); if (!out) { notify('warn', '請先填寫' + label); return false; } },
  });
  return ok ? out : null;
}
const fileUrl = (p) => `${BASE}/api/file?path=${encodeURIComponent(p)}&token=${TOKEN}`;
const modelOf = (id) => S.models.find((m) => m.id === id);
const cur = () => S.sessions.find((s) => s.id === S.sid);
function fmtMs(ms) { ms = Math.round(ms); return ms < 1000 ? `${ms}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.floor(ms / 60000)}分${Math.round(ms % 60000 / 1000)}秒`; }
function fmtCtx(n) { n = Number(n) || 0; if (!n) return ''; return n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}K`; }
function hueOf(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatar(name, cls = 'av') {
  const ch = String(name || '?').replace(/^[^A-Za-z0-9\u4e00-\u9fa5]+/, '').slice(0, 1).toUpperCase() || '?';
  return `<span class="${cls}" style="--h:${hueOf(name)}">${esc(ch)}</span>`;
}

marked.setOptions({ gfm: true, breaks: true });
// 思考框展開狀態：renderChat 每次都會把整段對話重建，details 的 open 不記住就會「點開→模型下一步→自己收起來」。
// 用 message.id + 區塊序號當 key；toggle 不冒泡，所以用 capture 攔。
const THINK_OPEN = new Set();
document.addEventListener('toggle', (e) => {
  const d = e.target;
  if (!d || d.tagName !== 'DETAILS' || !d.dataset.tk) return;
  if (d.open) THINK_OPEN.add(d.dataset.tk); else THINK_OPEN.delete(d.dataset.tk);
  // 展開的當下才把文字塞進去（見 renderChat 的 _th）
  const tx = d.querySelector('.tx');
  if (tx && d._th != null && !tx.textContent && d.open) tx.textContent = d._th;
}, true);
const MEDIA_RE = /(?:(?<![\w:/.])MEDIA:|(?<![\w:/.]))(?:[A-Za-z]:[\\/]|\/)[^\s`"'<>|*?]+?\.(png|jpe?g|gif|webp|bmp|mp4|webm|mov|mp3|wav|m4a|ogg)(?![\w])/gi;
const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp)$/i, VID_EXT = /\.(mp4|webm|mov)$/i;
// 訊息文字裡的本機圖片／影片／音訊路徑 → 直接預覽（檔案不存在或不在允許清單時自動隱藏）
function attachMedia(box, text) {
  const seen = new Set(); const found = [];
  for (const m of String(text || '').matchAll(MEDIA_RE)) {
    const p = m[0].replace(/^MEDIA:/, ''); if (seen.has(p)) continue; seen.add(p); found.push(p);
    if (found.length >= 6) break;
  }
  if (!found.length) return;
  const wrap = document.createElement('div'); wrap.className = 'mprev';
  for (const p of found) {
    const item = document.createElement('div'); item.className = 'mitem';
    const src = fileUrl(p);
    let el;
    if (IMG_EXT.test(p)) { el = document.createElement('img'); el.src = src; el.loading = 'lazy'; el.alt = p.split(/[\\/]/).pop(); }
    else if (VID_EXT.test(p)) { el = document.createElement('video'); el.src = src; el.controls = true; el.preload = 'metadata'; }
    else { el = document.createElement('audio'); el.src = src; el.controls = true; el.preload = 'metadata'; }
    el.onerror = () => item.remove();
    item.appendChild(el);
    const cap = document.createElement('small'); cap.textContent = p.split(/[\\/]/).pop(); item.appendChild(cap);
    wrap.appendChild(item);
  }
  box.appendChild(wrap);
}
// 回覆裡提到的本機檔案路徑（C:\...、C:/...、/c/...）變成可點的連結：點了在 WOOF 裡預覽，右鍵可用預設程式開、開資料夾
const PATH_RE = /(?<![\w/\\])(?:[A-Za-z]:[\\/]|\/[a-z]\/)[^\s`"'<>|*?()（），。、；：「」]+/g;
const REL_FILE = /^(?:\.{0,2}[\\/])?(?:[^\s<>|*?"':`\\/]+[\\/])*[^\s<>|*?"':`\\/]+\.(?:html?|md|txt|json|csv|tsv|xlsx?|docx?|pptx?|pdf|png|jpe?g|gif|webp|svg|mp4|mp3|wav|py|js|ts|css|toml|ya?ml|log|zip)$/i;
function flinkPath(a) {
  if (a.dataset.path) return a.dataset.path;
  const base = (S.workdir || '').replace(/[\\/]+$/, '');
  const rel = (a.dataset.rel || '').replace(/^\.[\\/]/, '');
  return base ? `${base}${base.includes('/') ? '/' : '\\'}${rel}` : rel;
}
function cleanPath(p) {
  p = p.replace(/[.,;:!?。，、；：！？)\]}」』]+$/, '');
  const m = /^\/([a-z])\//.exec(p);
  return m ? `${m[1].toUpperCase()}:/${p.slice(3)}` : p;
}
function linkPaths(box) {
  // 行內程式碼整段就是一個路徑：直接整段可點
  $$('code', box).forEach((c) => {
    if (c.closest('pre, a')) return;
    const t = c.textContent.trim();
    if (/^(?:[A-Za-z]:[\\/]|\/[a-z]\/)\S+$/.test(t) && !/[<>|*?"]/.test(t)) {
      const a = document.createElement('a'); a.className = 'flink'; a.dataset.path = cleanPath(t); a.title = '點一下預覽；右鍵有更多';
      c.replaceWith(a); a.appendChild(c);
    } else if (REL_FILE.test(t)) {
      // 相對路徑（`out\report.md`、`floor_engine.py`）：點的時候才對這個對話的工作目錄解開
      const a = document.createElement('a'); a.className = 'flink'; a.dataset.rel = t.replace(/^\.[\\/]/, ''); a.title = '點一下預覽（相對於工作目錄）；右鍵有更多';
      c.replaceWith(a); a.appendChild(c);
    }
  });
  const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest('a, pre, code, .mprev') || !/[A-Za-z]:[\\/]|\/[a-z]\//.test(n.nodeValue)
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = []; while (walk.nextNode()) nodes.push(walk.currentNode);
  for (const n of nodes) {
    const s = n.nodeValue; let last = 0; const frag = document.createDocumentFragment(); let hit = false;
    for (const m of s.matchAll(PATH_RE)) {
      const raw = m[0].replace(/[.,;:!?。，、；：！？)\]}」』]+$/, '');
      const p = cleanPath(raw);
      if (raw.length < 4) continue;
      hit = true;
      frag.append(s.slice(last, m.index));
      const a = document.createElement('a'); a.className = 'flink'; a.dataset.path = p; a.textContent = raw; a.title = '點一下預覽；右鍵有更多';
      frag.append(a);
      last = m.index + raw.length;
    }
    if (!hit) continue;
    frag.append(s.slice(last));
    n.replaceWith(frag);
  }
}
// 重畫整段對話時，同一段文字會一而再地被 marked 解析、消毒、上色；長對話這樣很吃主執行緒（打字、按鈕都跟著卡）。
// 用「原文 → 處理好的 HTML」快取，第二次以後直接貼回去；上色結果也另外記一份。
const MD_HTML = new Map();
const MD_HL = new Map();
function md(text, live) {
  // 單獨一行的 MEDIA:<路徑> 是「顯示這個檔」的指令：下面直接預覽，原文不顯示（行內提到的路徑照常保留）
  const shown = String(text || '').replace(/^[ \t]*MEDIA:\S+[ \t]*$/gm, '').replace(/\n{3,}/g, '\n\n').trim();
  let html = live ? null : MD_HTML.get(shown);
  if (html == null) {
    html = DOMPurify.sanitize(marked.parse(shown), { ADD_ATTR: ['target'] });
    if (!live) { if (MD_HTML.size > 400) MD_HTML.clear(); MD_HTML.set(shown, html); }
  }
  const box = document.createElement('div');
  box.className = 'md';
  box.innerHTML = html;
  $$('a', box).forEach((a) => a.setAttribute('target', '_blank'));
  linkPaths(box);
  attachMedia(box, text);
  // live＝串流中每一格都會重畫，程式碼上色很貴又看不出來，等定稿那次才做
  if (!live) $$('pre code', box).forEach((c) => {
    const src = c.textContent;
    const hit = MD_HL.get(src);
    if (hit != null) { c.innerHTML = hit; c.classList.add('hljs'); }
    else {
      try { hljs.highlightElement(c); } catch {}
      if (MD_HL.size > 300) MD_HL.clear();
      MD_HL.set(src, c.innerHTML);
    }
    const b = document.createElement('span');
    b.className = 'copy'; b.textContent = '複製';
    b.onclick = () => { navigator.clipboard.writeText(c.innerText); b.textContent = '已複製'; setTimeout(() => (b.textContent = '複製'), 1200); };
    c.parentElement.appendChild(b);
  });
  return box;
}

// ------------------------------------------------------------------ WebSocket
function connect() {
  const ws = new WebSocket(`${BASE ? BASE.replace(/^http/, 'ws') : 'ws://' + location.host}/ws?token=${TOKEN}`);
  S.ws = ws;
  ws.onopen = () => {
    connect.wait = 500;
    if (connect.lost) { connect.lost = false; notify('ok', '已重新連上後端'); loadSessions(); if (S.sid) openSession(S.sid); }
    refreshState(); loadCron();
  };
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
  ws.onclose = () => {
    S.ws = null;
    if (!connect.lost) { connect.lost = true; notify('warn', '和後端的連線中斷', '正在自動重連…'); }
    setTimeout(connect, connect.wait = Math.min((connect.wait || 500) * 2, 8000));
  };
}
function wsSend(obj) {
  if (!S.ws || S.ws.readyState !== 1) { notify('error', '和後端的連線中斷', '正在重連，請稍後再送一次。'); return false; }
  S.ws.send(JSON.stringify(obj)); return true;
}

// 每個對話最近一次有動靜的時間與內容：背景子代理、審核在跑但畫面沒在閃時，用它告訴使用者「還在動」
function actText(ev) {
  const a = ev.args || {};
  const brief = String(a.command || a.path || a.query || a.url || a.goal || '').replace(/\s+/g, ' ').slice(0, 60);
  switch (ev.type) {
    case 'run_start': return '開始';
    case 'delta': return ev.kind === 'thinking' || ev.thinking ? '思考中' : '寫回覆中';
    case 'tool_start': return `${ev.name || '工具'} ${brief}`.trim();
    case 'tool_output': return '工具輸出中';
    case 'tool_end': return `${ev.name || '工具'} 完成`;
    case 'notice': return String(ev.text || '').slice(0, 60);
    case 'message': return '';
    default: return '';
  }
}
function noteAct(ev) {
  const sid = ev.session_id;
  if (!sid || ev.type === 'pet' || ev.type === 'usage' || ev.type === 'session_updated') return;
  const t = actText(ev);
  const now = Date.now();
  S.act[sid] = { at: now, text: t || (S.act[sid] || {}).text || '' };
  const s = S.sessions.find((x) => x.id === sid);
  if (s && s.parent) S.act[s.parent] = { at: now, text: t || (S.act[s.parent] || {}).text || '', child: sid };
  if (S.sid && (sid === S.sid || (s && s.parent === S.sid))) renderBgRun();
}
// 輸入框上方一行：主對話沒在串流、但它自己或底下的子代理還在跑時顯示（附「幾秒前有動靜」）
function bgRunInfo() {
  const s = cur(); if (!s) return null;
  const kids = S.sessions.filter((k) => k.parent === s.id && (S.running.has(k.id) || k.busy));
  const self = S.running.has(s.id) || s.busy;
  if (!kids.length && !self) return null;
  const a = S.act[s.id] || {};
  const lv = S.live[s.id];
  // 主對話正在串流字的時候不用這行（畫面本來就在動）
  if (!kids.length && lv && (Date.now() - (a.at || 0) < 4000)) return null;
  const nm = (k) => (k.title || '子代理').replace(/^(子代理|審核模型|拓展模型)[:：]\s*/, (m0, k0) => (k0 === '子代理' ? '' : '審核：'));
  const who = kids.length ? kids.map(nm).slice(0, 2).join('、') + (kids.length > 2 ? ` 等 ${kids.length} 個` : '') : '';
  return { who, text: a.text || '', at: a.at || 0 };
}
function renderBgRun() {
  const el = $('#bgRun'); if (!el) return;
  const i = bgRunInfo();
  if (!i) { el.hidden = true; el._k = ''; return; }
  const k = `${i.who}|${i.text}`;
  if (el._k !== k) {
    el._k = k;
    el.innerHTML = `<i class="run"></i><span class="bg-who">${esc(i.who ? `還在跑：${i.who}` : '還在跑')}</span>`
      + (i.text ? `<span class="bg-tx" title="${esc(i.text)}">${esc(i.text)}</span>` : '') + '<span class="bg-age"></span>';
  }
  el.hidden = false;
  const age = $('.bg-age', el);
  if (age) { const sec = i.at ? Math.floor((Date.now() - i.at) / 1000) : -1; age.textContent = sec < 0 ? '' : sec < 2 ? '剛剛有動靜' : `${sec} 秒前有動靜`; }
}
setInterval(renderBgRun, 1000);
function onEvent(ev) {
  const sid = ev.session_id;
  noteAct(ev);
  if (typeof ev.busy === 'boolean' && sid) {
    const s = S.sessions.find((x) => x.id === sid);
    if (s) { s.busy = ev.busy; s.flow = !!ev.flow; }
    if (sid === S.sid) { renderTaskBar(); renderComposer(); }
  }
  switch (ev.type) {
    case 'pet':
      petEvent(ev.pet, ev.history);
      break;
    case 'run_start':
      S.running.add(sid); S.live[sid] = { thinking: '', text: '' }; delete S.errors[sid];
      renderSessions(); if (sid === S.sid) { renderChat(); renderComposer(); renderTaskBar(); }
      break;
    case 'run_end':
      S.running.delete(sid); delete S.live[sid];
      if (ev.status === 'stopped') (S.errors[sid] = S.errors[sid] || []).push({ stopped: true });
      for (const t of Object.values(S.tools)) if (t.sid === sid && t.running) { t.running = false; t.stopped = ev.status === 'stopped'; }
      delete S.srvQ[sid];
      renderSessions(); if (sid === S.sid) { renderChat(); renderComposer(); renderTaskBar(); }
      if (ev.status !== 'stopped' || S.interrupting[sid]) { delete S.interrupting[sid]; drainQueue(sid); }
      refreshKeys(); refreshTasks(); refreshCtxPanel();
      break;
    case 'delta': {
      const l = S.live[sid] || (S.live[sid] = { thinking: '', text: '' });
      l[ev.kind] += ev.text;
      if (sid === S.sid) scheduleLive();
      break;
    }
    case 'message':
      // 思考過程中是展開的，commit 成訊息後沿用：不然模型一動，剛才看到一半的思考就自己收起來
      if (sid === S.sid && ev.message.role === 'assistant' && typeof ev.message.id === 'number'
        && S.liveThink && S.liveThink.open !== false) S.thinkCarry = ev.message.id;
      if (S.live[sid]) S.live[sid] = { thinking: '', text: '' };
      if (sid === S.sid) S.liveThink = null;
      if (sid === S.sid) {
        if (ev.message.role === 'user') {
          const text = (ev.message.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('');
          S.msgs = S.msgs.filter((m) => !m.pending || (m.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('') !== text);
        }
        S.msgs.push(ev.message); renderChat(); renderFiles();
      } else {
        // 背景分頁：訊息照樣存進它自己的清單，切回去就看得到，不必重新抓
        const t = tabOf(sid);
        if (t) {
          if (ev.message.role === 'user') {
            const text = (ev.message.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('');
            t.msgs = t.msgs.filter((m) => !m.pending || (m.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('') !== text);
          }
          t.msgs.push(ev.message);
        }
      }
      renderTabbar();
      break;
    case 'tool_start':
      S.tools[ev.id] = { sid, name: ev.name, args: ev.args, out: '', running: true, t0: Date.now() };
      if (sid === S.sid) renderChat();
      break;
    case 'tool_output': {
      const t = S.tools[ev.id]; if (!t) break;
      t.out += ev.chunk;
      if (sid === S.sid) { const pre = $(`.tool[data-id="${CSS.escape(ev.id)}"] pre`); if (pre) { const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 8; pre.textContent = t.out.slice(-20000); if (atEnd) pre.scrollTop = pre.scrollHeight; } }
      break;
    }
    case 'tool_end': {
      const t = S.tools[ev.id] || (S.tools[ev.id] = { sid, name: ev.name });
      Object.assign(t, { running: false, ms: ev.ms, is_error: ev.is_error, output: ev.output, images: ev.images });
      if (sid === S.sid) renderChat();
      refreshTasks();
      break;
    }
    case 'usage':
      S.usage[sid] = ev; if (sid === S.sid) renderMeter();
      break;
    case 'error': {
      (S.errors[sid] = S.errors[sid] || []).push(ev.text);
      if (sid === S.sid) renderChat();
      const s = S.sessions.find((x) => x.id === sid);
      notify('error', sid === S.sid ? '模型回覆失敗' : `「${s ? s.title : '背景對話'}」出錯`, ev.text);
      break;
    }
    case 'notice':
      notify('info', ev.text || '');
      break;
    case 'truncated':
      if (sid === S.sid) { S.msgs = S.msgs.filter((m) => !(typeof m.id === 'number' && m.id >= ev.from_id)); delete S.errors[sid]; renderChat(); }
      else { const t = tabOf(sid); if (t) t.msgs = t.msgs.filter((m) => !(typeof m.id === 'number' && m.id >= ev.from_id)); }
      break;
    case 'approval':
      S.approvals[ev.id] = ev;
      if (sid === S.sid) renderChat();
      if (document.hidden && D && D.notify) D.notify('WOOF 需要你確認', `${ev.name}：${toolArgSummary(ev.name, ev.args)}`);
      if (sid !== S.sid) notify('warn', '有工具等你確認', (S.sessions.find((x) => x.id === sid) || {}).title || '');
      break;
    case 'ask':
      S.asks[ev.id] = ev;
      if (sid === S.sid) { S.follow = true; renderChat(); }   // 卡片在對話串尾端，捲到底才看得到
      renderTaskBar();
      if (sid !== S.sid) notify('warn', '有問題等你回答', (S.sessions.find((x) => x.id === sid) || {}).title || '');
      break;
    case 'ask_done': {
      const a = S.asks[ev.id];
      if (!a && !ev.answer) { delete S.asks[ev.id]; renderTaskBar(); break; }
      (S.askDone[sid] = S.askDone[sid] || []).push({
        question: (a && a.question) || ev.question || '',
        answer: ev.answer || '',
        skipped: !!ev.skipped || /略過/.test(ev.answer || ''),
        timedOut: !!ev.timed_out, waited: ev.waited || 0,
        at: Date.now(),
      });
      if (S.askDone[sid].length > 20) S.askDone[sid].shift();
      delete S.asks[ev.id];
      // 已回答的卡片只留一下下（看一下自己回了什麼），之後就讓它消失，不要一直黏在對話尾巴
      setTimeout(() => { if (S.sid === sid) renderChat(); }, ASKDONE_TTL + 100);
      if (sid === S.sid) renderChat();
      renderTaskBar();
      break;
    }
    case 'todos':
      S.todos[sid] = ev.items || []; if (sid === S.sid) renderTaskBar();
      break;
    case 'queue':
      // 後端排隊的內部通知（例如背景指令結束），這一步結束後送進對話
      S.srvQ[sid] = (ev.items || []).filter((it) => it.internal);
      if (sid === S.sid) renderQueue();
      break;
    case 'approval_done':
      delete S.approvals[ev.id];
      if (sid === S.sid) renderChat();
      break;
    case 'steer':
      if (ev.session_id === S.sid) {
        S.msgs.push({ id: 'pending-' + Date.now(), role: 'user', content: ev.content || [], pending: true });
        renderChat();
      } else {
        const t = tabOf(ev.session_id);
        if (t) t.msgs.push({ id: 'pending-' + Date.now(), role: 'user', content: ev.content || [], pending: true });
      }
      break;
    case 'session_updated': {
      const i = S.sessions.findIndex((s) => s.id === ev.session.id);
      const prevSum = i >= 0 ? JSON.stringify(((S.sessions[i].engine_state || {}).summary) || null) : '';
      const prevWd = i >= 0 ? ((S.sessions[i].engine_state || {}).workdir || '') : '';
      if (ev.session.id === S.sid && prevWd !== ((ev.session.engine_state || {}).workdir || '')) loadWorkdir();
      if (i >= 0) S.sessions[i] = ev.session; else S.sessions.unshift(ev.session);
      if (ev.session.id === S.sid && prevSum !== JSON.stringify(((ev.session.engine_state || {}).summary) || null)) renderChat();
      if (ev.session.parent && !S.sessions.some((x) => x.id === ev.session.parent)) refreshState();
      renderSessions(); if (ev.session.id === S.sid) { renderTitle(); renderTaskBar(); }
      break;
    }
    case 'state_changed':
      refreshState();
      break;
    case 'styles_changed':
      loadStyles().then(() => { if (S.setSec === 'styles' && $('#setView') && $('#setView').classList.contains('on')) renderSettings(); });
      break;
    case 'snapshots_changed':
      if (sid === S.sid) loadSnapshots();
      break;
    case 'cron_changed':
      if ($('#cronView').classList.contains('on') && !document.querySelector('.modal-bg')) renderCronView(); else loadCron();
      break;
    case 'task_end': {
      const ok = ev.exit_code === 0;
      const title = ok ? '背景任務完成' : `背景任務結束（結束碼 ${ev.exit_code}）`;
      const body = `${ev.command.slice(0, 100)} · ${(ev.ms / 1000).toFixed(1)} 秒`;
      notify(ok ? 'ok' : 'error', title, body);
      if (document.hidden || !document.hasFocus()) sysNotify(title, body);
      refreshTasks();
      break;
    }
  }
}

// ------------------------------------------------------------------ 資料載入
async function refreshState() {
  const st = await api('GET', '/api/state');
  S.models = st.models; S.vendors = st.vendors || []; S.backends = st.backends || [];
  S.plugins = st.plugins; S.keys = st.keys;
  S.running = new Set(st.running);
  S.approvals = {}; for (const a of st.approvals || []) S.approvals[a.id] = a;
  if (st.profile) { S.profile = st.profile; if (!S.profList || !S.profList.length) refreshProf(); else renderProf(); }
  renderModelSelect(); renderPlugins(); renderKeys(); renderSessions(); renderComposer();
}
async function refreshKeys() { try { S.keys = (await api('GET', '/api/state')).keys; renderKeys(); } catch {} }
async function loadSessions() {
  S.sessions = await api('GET', '/api/sessions');
  // 分頁開著的對話已經不在（在別處被刪掉）：關掉那個分頁，不然標題、上下文面板會一直讀到 404
  const ids = new Set(S.sessions.map((x) => x.id));
  const gone = new Set((S.tabs || []).map((t) => t.sid).filter((id) => id && !ids.has(id)));
  if (gone.size) { dropTabsOf(gone); renderAll(); }
  await loadGroups();
}
async function loadGroups() {
  try { S.groups = await api('GET', '/api/groups'); } catch { S.groups = S.groups || []; }
  renderSessions();
}
async function loadWorkdir() {
  const sid = S.sid || '';
  try { S.wd = await api('GET', `/api/workspace?session_id=${encodeURIComponent(sid)}`); S.workdir = S.wd.workdir || ''; } catch {}
  if ((S.sid || '') !== sid) return;
  $('#sbDirTxt').textContent = S.workdir ? S.workdir.split(/[\\/]/).filter(Boolean).pop() : '未設定工作目錄';
  $('#sbDir').classList.toggle('own', !!(S.wd && S.wd.own));
  $('#sbDir').title = '這個對話的工作目錄：' + (S.workdir || '未設定') + (S.wd && S.wd.own ? '' : '（預設）') + '\n按一下可以換資料夾、開啟或在終端開啟';
  if (FT.root !== S.workdir) { FT.root = S.workdir; FT.open = new Set(); FT.cache.clear(); if (paneOn('paneFiles')) loadTree(); }
}

async function openSession(sid) {
  // 側邊欄按一下：這個對話已經開在某個分頁就切過去，不然就用目前這個分頁開
  const open = tabOf(sid);
  if (open && open !== tab()) { await switchTab(S.tabs.indexOf(open)); return; }
  const t = tab();
  t.sid = sid; t.msgs = []; t.loaded = false; t.top = 0; t.follow = true;
  if (sid && !cur()) await loadSessions().catch(() => {});
  localStorage.setItem('woof.sid', sid || '');
  if (sid) await loadTab(t); else t.loaded = true;
  showView('chat');
  renderAll();
  if (S.follow) requestAnimationFrame(() => { const c = $('#chat'); if (c) stickBottom(); });
  $('#input').focus();
}

function newSession() {
  // 分頁已滿：新對話直接取代目前分頁（原本那個對話仍然存在側邊欄，不會被刪掉）
  const t = tab();
  t.sid = null; t.msgs = []; t.loaded = true; t.top = 0; t.follow = true; t.attach = []; t.files = []; t.draft = {}; t.liveThink = null;
  localStorage.setItem('woof.sid', '');
  showView('chat');
  renderAll();
  renderChat(true);
  $('#input').focus();
}

// ------------------------------------------------------------------ 側邊欄
function dayGroup(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return '今天';
  if (d >= start - 864e5) return '昨天';
  if (d >= start - 6 * 864e5) return '本週';
  if (d >= start - 29 * 864e5) return '本月';
  return '更早';
}
// 底下所有子代理對話（含更深一層）的 id
function descIds(sid) {
  const out = [], todo = [sid];
  while (todo.length) {
    const cur = todo.pop();
    for (const s of S.sessions) if (s.parent === cur && !out.includes(s.id)) { out.push(s.id); todo.push(s.id); }
  }
  return out;
}
function grpById(gid) { return (S.groups || []).find((g) => g.id === gid); }
// 子代理的種類：審核／拓展模型歸一疊，其他是一般子代理（舊資料沒有標記，看標題）
function subKind(s) {
  const k = ((s.engine_state || {}).sub || {}).kind;
  if (k) return k;
  return /^(審核模型|拓展模型|對抗檢查|方案審核)/.test(s.title || '') ? 'review' : 'task';
}
// 同一次 delegate_task 派出的算一批；舊資料沒有批次 id，就把 20 秒內接連建立的算同一批
function subBatches(list) {
  const arr = list.slice().sort((a, b) => (a.created || 0) - (b.created || 0));
  const out = [];
  for (const s of arr) {
    const b = ((s.engine_state || {}).sub || {}).batch || '';
    const last = out[out.length - 1];
    if (last && (b ? last.key === b : (!last.tagged && (s.created || 0) - last.end < 20))) {
      last.items.push(s); last.end = s.created || last.end;
    } else out.push({ key: b || 't' + Math.round(s.created || 0), tagged: !!b, items: [s], at: s.created || 0, end: s.created || 0 });
  }
  return out;
}

function renderSessions() {
  renderTabbar();
  const q = $('#search').value.trim().toLowerCase();
  const ids = new Set(S.sessions.map((s) => s.id));
  const kids = {};
  for (const s of S.sessions) if (s.parent && ids.has(s.parent)) (kids[s.parent] = kids[s.parent] || []).push(s);
  const match = (s) => !q || (s.title || '').toLowerCase().includes(q);
  const cur = S.sessions.find((x) => x.id === S.sid);
  const groups = S.groups || [];

  // 一則對話連同它的子代理，可能長出好幾列：
  // 子代理分兩疊——一般子代理（依「同一次派出」分批）、審核／拓展模型；兩疊各自收合、可整疊刪
  const nodesOf = (s) => {
    const out = [];
    const all = kids[s.id] || [];
    const ks = all.filter(match);
    if (!(match(s) || ks.length)) return out;
    out.push(s);
    if (!all.length) return out;
    const viewing = cur && cur.parent === s.id ? cur : null;
    const tasks = all.filter((k) => subKind(k) !== 'review');
    const revs = all.filter((k) => subKind(k) === 'review');
    if (tasks.length) {
      const key = s.id;
      const open = !!q || S.subOpen.has(key) || (viewing && subKind(viewing) !== 'review');
      out.push({ _tog: true, key, parent: s.id, kind: 'task', label: '子代理', n: tasks.length, open, ids: tasks.map((k) => k.id) });
      if (open) {
        const batches = subBatches(tasks);
        if (batches.length === 1) for (const k of batches[0].items.filter(match)) out.push(k);
        else batches.forEach((b, i) => {
          const bk = `${s.id}|${b.key}`;
          const bopen = !!q || S.subOpen.has(bk) || (!S.subShut.has(bk) && i === batches.length - 1)
            || (viewing && b.items.includes(viewing));
          out.push({ _batch: true, key: bk, no: i + 1, n: b.items.length, at: b.at, open: bopen, ids: b.items.map((k) => k.id) });
          if (bopen) for (const k of b.items.filter(match)) out.push(Object.assign(k, { _deep: true }));
        });
      }
    }
    if (revs.length) {
      const key = 'rev:' + s.id;
      const open = !!q || S.subOpen.has(key) || (viewing && subKind(viewing) === 'review');
      out.push({ _tog: true, key, parent: s.id, kind: 'review', label: '審核', n: revs.length, open, ids: revs.map((k) => k.id) });
      if (open) for (const k of revs.filter(match).sort((a, b) => (a.created || 0) - (b.created || 0))) out.push(k);
    }
    return out;
  };
  const row = (s) => {
    const child = s.parent && ids.has(s.parent);
    const m = modelOf(s.model);
    const wait = Object.values(S.approvals).some((a) => a.session_id === s.id);
    const deep = child && s._deep;
    delete s._deep;
    return `<div class="item${s.id === S.sid ? ' on' : ''}${child ? ' child' : ''}${deep ? ' deep' : ''}" data-sid="${s.id}" draggable="true"${child ? ' title="拖到群組或日期標題上＝拆出來變成獨立對話"' : ''}>`
      + `<span class="t"${child ? ` title="${esc(s.title || '')}"` : ''}>${S.running.has(s.id) || s.busy || (kids[s.id] || []).some((k) => S.running.has(k.id) || k.busy) ? '<i class="run"></i>' : ''}${s.pinned ? `<span class="pin">${ico('pin')}</span>` : ''}${esc(child ? (s.title || '子代理').replace(/^(子代理|審核模型|拓展模型)[:：]\s*/, '') : (s.title || '新對話'))}</span>`
      + `<small>${wait ? '等你確認 · ' : ''}${(s.engine_state || {}).forked_from ? '分支 · ' : ''}${esc(m ? m.label : s.model || '')}</small><span class="del" title="刪除">${ico('x')}</span></div>`;
  };
  const live = (idList) => idList.filter((id) => S.running.has(id) || (S.sessions.find((x) => x.id === id) || {}).busy).length;
  const flat = (arr) => arr.flatMap(nodesOf).map((s) => {
    if (s._tog) {
      const run = live(s.ids);
      return `<div class="subtog${s.kind === 'review' ? ' rev' : ''}" data-subtog="${esc(s.key)}"><span class="car">${s.open ? '▾' : '▸'}</span>`
        + `<span class="lab">${s.label} ${s.n} 個</span>${run ? `<span class="srun"><i class="run"></i>${run} 個在跑</span>` : ''}`
        + `<span class="sdel" data-delsubs="${esc(s.ids.join(','))}" data-dlab="${esc(s.label)}" title="整疊刪除">${ico('x')}</span></div>`;
    }
    if (s._batch) {
      const run = live(s.ids);
      return `<div class="subbatch" data-subbatch="${esc(s.key)}"><span class="car">${s.open ? '▾' : '▸'}</span>`
        + `<span class="lab">第 ${s.no} 批 · ${s.n} 個</span><span class="when">${esc(fmtTime(s.at))}</span>${run ? '<i class="run"></i>' : ''}`
        + `<span class="sdel" data-delsubs="${esc(s.ids.join(','))}" data-dlab="第 ${s.no} 批子代理" title="刪除這一批">${ico('x')}</span></div>`;
    }
    return row(s);
  }).join('');

  const roots = S.sessions.filter((s) => !(s.parent && ids.has(s.parent)));
  const pinned = roots.filter((s) => !s.archived && s.pinned);
  const archived = roots.filter((s) => s.archived);
  const arcOpen = !!q || S.showArchived;
  const gids = new Set(groups.map((g) => g.id));
  const inGrp = (s) => !s.archived && !s.pinned && s.grp && gids.has(s.grp);
  const loose = roots.filter((s) => !s.archived && !s.pinned && !inGrp(s));

  let html = '';
  if (pinned.length) html += '<div class="grp">已釘選</div>' + flat(pinned);
  for (const g of groups) {
    const items = roots.filter((s) => inGrp(s) && s.grp === g.id);
    const body = flat(items);
    if (q && !body && !g.name.toLowerCase().includes(q)) continue;
    const n = S.sessions.filter((s) => s.grp === g.id && !s.archived).length;
    html += `<div class="ghead${g.collapsed ? ' closed' : ''}" data-gid="${esc(g.id)}" data-drop="${esc(g.id)}">`
      + `<span class="gcar">${ico('down')}</span><span class="gname">${esc(g.name)}</span><span class="gnum">${n}</span>`
      + `<span class="gmenu" data-gmenu="${esc(g.id)}" title="群組選項">${ico('more')}</span></div>`;
    if (!g.collapsed || q) html += `<div class="gbody">${body}</div>`;
  }
  if (groups.length) {
    // 有群組時，剩下的對話集中成「未分組」；拖到這裡就是移出群組
    if (loose.length) html += `<div class="ghead plain" data-drop=""><span class="gcar">${ico('down')}</span><span class="gname">未分組</span><span class="gnum">${loose.length}</span></div><div class="gbody">${flat(loose)}</div>`;
  } else {
    let last = '';
    for (const s of loose) {
      const g = dayGroup(s.updated || s.created);
      if (g !== last) { html += `<div class="grp" data-drop="">${g}</div>`; last = g; }
      html += flat([s]);
    }
  }
  if (archived.length) {
    html += `<div class="arctog" data-arctog>${arcOpen ? '▾' : '▸'} 已封存 ${archived.length} 個</div>`;
    if (arcOpen) html += flat(archived);
  }
  if (q) html += `<div class="grp">對話內容</div><div id="ftsBox">${S.fts && S.fts.q === q ? ftsHtml(S.fts) : '<div class="sempty">搜尋中…</div>'}</div>`;
  $('#sessionList').innerHTML = html || '<div class="sempty">還沒有對話</div>';
  if ($('#grpMenu') && $('#grpMenu').classList.contains('on')) syncGrpMenu();
  if (q && (!S.fts || S.fts.q !== q)) ftsQueue(q);
}
// 側邊欄搜尋：標題直接比對；對話內容走後端全文檢索（/api/search），停手 250ms 再查
function ftsQueue(q) {
  clearTimeout(S.ftsT);
  S.ftsT = setTimeout(async () => {
    try {
      const hits = await api('GET', '/api/search?q=' + encodeURIComponent(q) + '&limit=40');
      if ($('#search').value.trim().toLowerCase() !== q) return;
      S.fts = { q, hits }; const b = $('#ftsBox'); if (b) b.innerHTML = ftsHtml(S.fts);
    } catch (e) { const b = $('#ftsBox'); if (b) b.innerHTML = `<div class="sempty">${esc(e.message)}</div>`; }
  }, 250);
}
function ftsHtml(f) {
  if (!f.hits.length) return '<div class="sempty">沒有對話提到這個</div>';
  const terms = f.q.split(/\s+/).filter(Boolean);
  const mark = (s) => { let h = esc(s); for (const t of terms) h = h.replace(new RegExp(esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), (x) => `<mark>${x}</mark>`); return h; };
  return f.hits.map((h) => `<div class="fts" data-fsid="${h.session_id}" data-fmid="${h.mid}"><b>${esc(h.title)}</b><span>${mark(h.snippet)}</span>`
    + `<small>${h.role === 'user' ? '你' : h.role === 'tool' ? '工具結果' : '助理'} · ${fmtTime(h.created)}${h.archived ? ' · 已封存' : ''}</small></div>`).join('');
}
async function gotoHit(sid, mid, q) {
  if (S.sid !== sid) await openSession(sid);
  const chat = $('#chat');
  const users = [...chat.querySelectorAll('.msg.user[data-mid], details.internal[data-mid]')];
  let el = users.find((x) => +x.dataset.mid === mid);
  if (!el) { const before = users.filter((x) => +x.dataset.mid < mid).pop(); el = before ? (before.nextElementSibling || before) : chat.querySelector('.msg.bot'); }
  if (!el) return;
  el.scrollIntoView({ block: 'center' }); el.classList.add('hit'); setTimeout(() => el.classList.remove('hit'), 1600);
  const t = (q || '').split(/\s+/).filter(Boolean)[0];
  if (t && window.find) { getSelection().removeAllRanges(); const r = document.createRange(); r.setStart(el, 0); r.collapse(true); getSelection().addRange(r); window.find(t, false, false, false); }
}
$('#sessionList').addEventListener('click', async (e) => {
  const at = e.target.closest('[data-arctog]');
  if (at) { S.showArchived = !S.showArchived; renderSessions(); return; }
  const fh = e.target.closest('.fts');
  if (fh) { gotoHit(fh.dataset.fsid, +fh.dataset.fmid, $('#search').value.trim()); return; }
  const ds = e.target.closest('[data-delsubs]');
  if (ds) { e.stopPropagation(); deleteSubs(ds.dataset.delsubs.split(',').filter(Boolean), ds.dataset.dlab || '子代理'); return; }
  const tg = e.target.closest('.subtog');
  if (tg) { const id = tg.dataset.subtog; S.subOpen.has(id) ? S.subOpen.delete(id) : S.subOpen.add(id); renderSessions(); return; }
  const sb = e.target.closest('.subbatch');
  if (sb) {
    const id = sb.dataset.subbatch;
    const open = sb.querySelector('.car').textContent === '▾';
    if (open) { S.subOpen.delete(id); S.subShut.add(id); } else { S.subOpen.add(id); S.subShut.delete(id); }
    renderSessions(); return;
  }
  const it = e.target.closest('.item'); if (!it) return;
  const sid = it.dataset.sid;
  if (e.target.closest('.del')) return deleteSession(sid);
  if (e.ctrlKey || e.metaKey) return openInTab(sid);
  openSession(sid);
});
// 整疊／整批刪子代理：一次確認、一次刪完（後端會連它們底下的一起清）
async function deleteSubs(idList, label) {
  if (!idList.length) return;
  if (!await confirmBox('刪除子代理', `${label}共 ${idList.length} 個對話會永久刪除，無法復原。主對話不受影響。`, { ok: '刪除', danger: true })) return;
  let bad = 0;
  for (const id of idList) { try { await api('DELETE', `/api/sessions/${id}`); } catch (e) { bad++; } }
  const gone = new Set(idList);
  S.sessions = S.sessions.filter((x) => !gone.has(x.id));
  for (const id of gone) { delete S.todos[id]; delete S.live[id]; delete S.usage[id]; delete S.errors[id]; delete S.queue[id]; S.running.delete(id); }
  dropTabsOf(gone);
  if (bad) notify('error', '有幾個沒刪掉', `${bad} 個刪除失敗`);
  const t = tab();
  if (t.sid && !t.loaded) loadTab(t).then(renderAll); else renderAll();
}
async function deleteSession(sid) {
  const s = S.sessions.find((x) => x.id === sid);
  const kids = descIds(sid);
  const extra = kids.length ? `\n連同 ${kids.length} 個子代理對話一起刪除。` : '';
  if (!await confirmBox('刪除對話', `「${s ? s.title : sid}」會永久刪除，無法復原。${extra}`, { ok: '刪除', danger: true })) return;
  try { await api('DELETE', `/api/sessions/${sid}`); } catch (e) { notify('error', '刪除失敗', e.message); return; }
  const gone = new Set([sid, ...kids]);
  S.sessions = S.sessions.filter((x) => !gone.has(x.id));
  S.subOpen.delete(sid);
  for (const id of gone) { delete S.todos[id]; delete S.planOn[id]; delete S.gate[id]; delete S.live[id]; delete S.usage[id]; delete S.errors[id]; delete S.queue[id]; S.running.delete(id); }
  for (const [k, a] of Object.entries(S.asks)) if (gone.has(a.session_id)) delete S.asks[k];
  for (const [k, a] of Object.entries(S.approvals)) if (gone.has(a.session_id)) delete S.approvals[k];
  dropTabsOf(gone);
  refreshTasks();
  // 一律整組重畫：以前只在「目前這個分頁也被刪掉」時才重畫，刪掉的剛好是開著的分頁時
  // 分頁變成新的空白頁、但對話區與標題還是舊的（看起來像沒刪掉）
  const t = tab();
  if (t.sid && !t.loaded) loadTab(t).then(renderAll); else renderAll();
}
async function exportSession(sid) {
  try {
    const s = S.sessions.find((x) => x.id === sid);
    const r = await api('GET', `/api/sessions/${sid}/export`);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([r.markdown], { type: 'text/markdown' }));
    a.download = ((s && s.title) || 'woof').replace(/[\\/:*?"<>|]/g, '_') + '.md';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { notify('error', '匯出失敗', e.message); }
}
async function togglePin(sid) {
  const s = S.sessions.find((x) => x.id === sid); if (!s) return;
  try { Object.assign(s, await api('PATCH', `/api/sessions/${sid}`, { pinned: !s.pinned })); await loadSessions(); }
  catch (e) { notify('error', '釘選失敗', e.message); }
}
async function copyText(text, msg) {
  try { await navigator.clipboard.writeText(text); notify('ok', msg || '已複製', text.length > 60 ? '' : text); }
  catch (e) { notify('error', '複製失敗', e.message); }
}
async function forkSession(sid, upto) {
  try {
    const s = await api('POST', `/api/sessions/${sid}/fork`, { upto });
    S.sessions = [s, ...S.sessions.filter((x) => x.id !== s.id)]; await openSession(s.id); notify('ok', '已分支成新對話', '原對話不受影響');
  } catch (e) { notify('error', '分支失敗', e.message); }
}
async function toggleArchive(sid) {
  const s = S.sessions.find((x) => x.id === sid); if (!s) return;
  try {
    Object.assign(s, await api('PATCH', `/api/sessions/${sid}`, { archived: !s.archived }));
    if (s.archived) { notify('ok', '已封存', '在側邊欄最下面的「已封存」可以找回來'); if (sid === S.sid) newSession(); }
    renderSessions();
  } catch (e) { notify('error', '封存失敗', e.message); }
}
// 右鍵選單
function ctxMenu(x, y, items) {
  $$('.pop.ctx').forEach((p) => p.remove());
  const m = document.createElement('div');
  m.className = 'pop ctx on';
  m.innerHTML = items.map((it, i) => it === '-' ? '<div class="sep"></div>' : `<div class="mi${it.danger ? ' danger' : ''}${it.disabled ? ' dis' : ''}" data-ci="${i}">${it.icon ? ico(it.icon) : ''}${esc(it.label)}${it.hint ? `<small>${esc(it.hint)}</small>` : ''}</div>`).join('');
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 8) + 'px';
  m.style.top = Math.min(y, innerHeight - r.height - 8) + 'px';
  m.onclick = (e) => { const d = e.target.closest('[data-ci]'); if (!d || d.classList.contains('dis')) return; m.remove(); items[+d.dataset.ci].run(); };
}
document.addEventListener('mousedown', (e) => { if (!e.target.closest('.pop.ctx')) $$('.pop.ctx').forEach((p) => p.remove()); });
$('#sessionList').addEventListener('contextmenu', (e) => {
  const it = e.target.closest('.item'); if (!it) return;
  e.preventDefault();
  const sid = it.dataset.sid, s = S.sessions.find((x) => x.id === sid);
  ctxMenu(e.clientX, e.clientY, [
    { icon: 'edit', label: '重新命名', run: () => startRename(it) },
    { icon: 'pin', label: s && s.pinned ? '取消釘選' : '釘選到最上面', run: () => togglePin(sid) },
    { icon: 'copy', label: '複製 ID', hint: sid, run: () => copyText(sid, '已複製對話 ID') },
    '-',
    { icon: 'branch', label: '分支成新對話', run: () => forkSession(sid, 0) },
    { icon: 'download', label: '匯出 Markdown', run: () => exportSession(sid) },
    '-',
    { icon: 'archive', label: s && s.archived ? '取消封存' : '封存', run: () => toggleArchive(sid) },
    { icon: 'trash', label: '刪除', danger: true, run: () => deleteSession(sid) },
  ]);
});
$('#search').addEventListener('input', renderSessions);
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#search').focus(); $('#search').select(); }
});
$('#sessionList').addEventListener('dblclick', (e) => {
  const it = e.target.closest('.item');
  if (!it || e.target.closest('.del')) return;
  startRename(it);
});
function startRename(it) {
  const sid = it.dataset.sid;
  const s = S.sessions.find((x) => x.id === sid);
  if (!s) return;
  const box = document.createElement('input');
  box.className = 'rename';
  box.value = s.title || '';
  it.replaceChildren(box);
  box.focus();
  box.select();
  let done = false;
  const save = async () => {
    if (done) return;
    done = true;
    const title = box.value.trim();
    if (title && title !== s.title) {
      try { Object.assign(s, await api('PATCH', '/api/sessions/' + sid, { title })); }
      catch (err) { toast('改名失敗：' + err.message); }
    }
    renderSessions();
    if (sid === S.sid) renderTitle();
  };
  box.addEventListener('keydown', (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Enter') { ev.preventDefault(); save(); }
    if (ev.key === 'Escape') { done = true; renderSessions(); }
  });
  box.addEventListener('blur', save);
}

$('#newBtn').onclick = newSession;

function renderTitle() {
  const s = cur();
  $('#tbTitle').textContent = s ? s.title || '新對話' : '新對話';
  document.title = s ? `${s.title} · WOOF` : 'WOOF';
}

// ------------------------------------------------------------------ 浮動選單
function closePops() { $$('.pop.on').forEach((p) => p.classList.remove('on')); $$('.pill.on,.ic.on').forEach((b) => b.classList.remove('on')); closeSpick(); }
function openPop(menu, anchor) {
  const was = menu.classList.contains('on');
  closePops();
  if (was) return false;
  menu.classList.add('on');
  anchor.classList.add('on');
  const wrap = $('.cwrap').getBoundingClientRect();
  const a = anchor.getBoundingClientRect();
  const w = menu.offsetWidth;
  let left = a.left - wrap.left;
  if (left + w > wrap.width) left = Math.max(0, a.right - wrap.left - w);
  menu.style.left = left + 'px';
  popAbove(menu, anchor);
  return true;
}
// 選單貼在按鈕正上方（不要被上面的待辦面板推得老遠）
function popAbove(menu, anchor) {
  const wrap = $('.cwrap').getBoundingClientRect(), a = anchor.getBoundingClientRect();
  menu.style.bottom = Math.round(wrap.bottom - a.top + 8) + 'px';
}
document.addEventListener('mousedown', (e) => { if (!e.target.closest('.pop,.pill,#plusBtn,[data-spick]')) closePops(); });

// ------------------------------------------------------------------ 模型 / 思考強度
function selectedModel() { return modelOf(S.modelId); }
function renderModelSelect() {
  const s = cur();
  const firstOk = (S.models.find((m) => m.available) || S.models[0] || {}).id;
  const dflt = (S.models.find((m) => m.is_default && m.available) || {}).id;
  // 有對話 → 用對話自己的模型；新對話 → 這個新對話自己選的 → 設定裡的主要模型 → 第一個可用（不沿用上一個對話的模型）
  let want = (s && s.model) || (s ? null : S.draft.model) || dflt || firstOk;
  // 對話的模型被刪或改名：不偷換成別的，明白告訴使用者要重選
  const gone = !!(s && s.model && S.models.length && !modelOf(s.model));
  if (!gone && !modelOf(want)) want = firstOk;
  S.modelId = gone ? null : (want || null);
  const m = selectedModel();
  $('#modelPillTxt').textContent = m ? m.label : (gone ? '原模型已移除，請重選' : '選擇模型');
  $('#modelPill').classList.toggle('gone', gone);
  $('#modelPill').title = m ? `${m.vendor_label} · ${m.label}` : (gone ? `這個對話原本用「${s.model}」，它已經不在模型清單裡` : '切換模型');
  renderEffort();
  // 生成型模型：提示這則訊息會當成生成的描述
  const gk = m && (m.kind === 'image' || m.kind === 'video') ? m.kind : '';
  $('#input').placeholder = gk === 'image' ? '描述要生成的圖片（可附參考圖），Enter 送出' : gk === 'video' ? '描述要生成的影片（畫面、動作、鏡頭），Enter 送出' : I18N[uiLang()].ph;
  if ($('#modelMenu').classList.contains('on')) renderModelMenu();
}
// 思考強度：記的是「想要的強度」（每個對話各自的 effort；還沒送出的新對話記在 S.draft），送出時依模型檔位向下兼容（後端 woof/effort.py 同規則）
function effortIsToggle(m) { return !!m && m.efforts.length > 0 && m.efforts.map(effNorm).every((x) => x === 'off' || x === 'on'); }
// 檔位一律用小寫比對（送 API 的值就是這組小寫）；介面顯示才用 EFFORT_LABEL 的中文標籤
const effNorm = (v) => (typeof v === 'string' ? v.trim().toLowerCase() : v);
function effortWant() {
  const s = cur();
  return effNorm(s ? (s.effort || 'high') : (S.draft.effort || 'high'));   // 每個對話各自記；新對話預設高
}
function resolveEffort(m, want) {
  const es = ((m && m.efforts) || []).map(effNorm);
  if (!es.length) return null;
  want = effNorm(want);
  const dflt = es.includes(effNorm(m.default_effort)) ? effNorm(m.default_effort) : es[es.length - 1];
  if (!want) return dflt;
  if (es.includes(want)) return want;
  if (effortIsToggle(m)) return (want === 'off' || want === 'none') ? (es.includes('off') ? 'off' : 'on') : (es.includes('on') ? 'on' : 'off');
  const graded = es.filter((e) => EFFORT_SCALE.includes(e)).sort((a, b) => EFFORT_SCALE.indexOf(a) - EFFORT_SCALE.indexOf(b));
  if (want === 'off' || want === 'none') return es.includes('off') ? 'off' : (graded[0] || es[0]);
  if (!EFFORT_SCALE.includes(want)) return dflt;
  const below = graded.filter((e) => EFFORT_SCALE.indexOf(e) <= EFFORT_SCALE.indexOf(want));
  return below.length ? below[below.length - 1] : (graded[0] || dflt);
}
function currentEffort() { return resolveEffort(selectedModel(), effortWant()); }
// 標籤：沒被降級 →「高」；被降級 →「極高→高」；開關型 →「開／關」
function effortTag(m, want) {
  const got = resolveEffort(m, want);
  if (!got) return null;
  if (effortIsToggle(m)) return { text: EFFORT_LABEL[got], down: false, got };
  const w = want === 'none' ? 'off' : want;
  if (!w || w === got || !(EFFORT_SCALE.includes(w) || w === 'off')) return { text: EFFORT_LABEL[got] || got, down: false, got };
  return { text: `${EFFORT_LABEL[w]}→${EFFORT_LABEL[got]}`, down: true, got, want: w };
}
function renderEffort() {
  const m = selectedModel(), pill = $('#effortPill');
  if (!m || !m.efforts.length || m.kind === 'image' || m.kind === 'video') { pill.hidden = true; return; }
  pill.hidden = false;
  const t = effortTag(m, effortWant());
  $('#effortPillTxt').textContent = t.text;
  pill.classList.toggle('down', t.down);
  pill.title = t.down
    ? `思考：選了「${EFFORT_LABEL[t.want]}」，${m.label} 沒有這一檔，下兼容到「${EFFORT_LABEL[t.got]}」`
    : effortIsToggle(m) ? `思考：${EFFORT_LABEL[t.got]}（${m.label} 只能開／關）` : `思考強度：${EFFORT_LABEL[t.got]}`;
}
function modelMeta(m, withVendor) {
  const bits = [];
  if (withVendor) bits.push(m.vendor_label);
  if (fmtCtx(m.context_window)) bits.push(fmtCtx(m.context_window) + ' 上下文');
  if (m.kind === 'image' || m.kind === 'video') bits.push(m.kind === 'image' ? '生成圖片（訊息當提示詞，可附參考圖）' : '生成影片（訊息當提示詞，約 1–3 分鐘）');
  else { if (m.vision) bits.push('看圖'); bits.push(m.efforts.length ? '可調思考' : '不支援思考'); }
  if (!m.available) bits.push('插件未載入');
  else if (m.no_key) bits.push('還沒設 API key');
  else if (m.down) bits.push('服務商暫停中：' + m.down);
  return bits.join(' · ');
}
const STAR_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3.6l2.6 5.5 6 .8-4.4 4.1 1.1 5.9L12 17.1l-5.3 2.8 1.1-5.9-4.4-4.1 6-.8z"/></svg>';
const STAR_ON = STAR_SVG.replace('fill="none"', 'fill="currentColor"');
// 生成型模型（生圖、影片）不放進聊天的模型選單：在「設定 → 模型用途 → 生圖・生影片」選，給工具與子代理用
function isGenModel(m) { return m.kind === 'image' || m.kind === 'video'; }
function visibleModels() { return S.models.filter((m) => m.enabled !== false && !isGenModel(m)); }
function vendorName(id) { const v = S.vendors.find((x) => x.id === id); return v ? v.label || v.id : (id || '其他'); }
function mgrpOpenSet() {
  try { const v = JSON.parse(localStorage.getItem('woof.mgrpOpen') || 'null'); if (Array.isArray(v)) return new Set(v); } catch (e) { /* 壞值當沒設 */ }
  const m = selectedModel(); return new Set(m ? [m.vendor || ''] : []);
}
function modelRowHtml(m) {
  const pinned = S.pins.has(m.id), on = m.id === S.modelId;
  const gen = m.kind === 'image' ? '生圖' : m.kind === 'video' ? '影片' : '';
  const t = m.efforts.length && !gen ? effortTag(m, effortWant()) : null;
  const warn = !m.available ? '插件未載入' : m.no_key ? '未設 key' : m.down ? '服務商暫停' : '';
  return `<div class="mrow${on ? ' on' : ''}${m.available && !m.no_key ? '' : ' na'}" data-id="${esc(m.id)}" title="${esc(modelMeta(m, true))}">`
    + `<span class="mstar${pinned ? ' on' : ''}" data-pin="${esc(m.id)}" title="${pinned ? '取消我的最愛' : '加入我的最愛'}">${pinned ? STAR_ON : STAR_SVG}</span>`
    + `<span class="mn">${esc(m.label)}</span>`
    + (warn ? `<span class="mwarn">${warn}</span>` : '')
    + (gen ? `<span class="echip" title="生成型模型：訊息內容當提示詞，回覆是生成的${gen === '生圖' ? '圖片' : '影片'}">${gen}</span>` : '')
    + (t ? `<span class="echip${t.down ? ' down' : ''}" title="${t.down ? `選「${EFFORT_LABEL[t.want]}」，這個模型下兼容到「${EFFORT_LABEL[t.got]}」` : '這個模型目前的思考強度，點右邊箭頭可以改'}">思考 ${esc(t.text)}</span>` : '')
    + `<span class="mck">${on ? ico('check') : ''}</span>`
    + (m.efforts.length && !gen ? `<span class="mmore" data-eff="${esc(m.id)}" title="思考選項">${ico('right')}</span>` : '<span class="mmore none"></span>')
    + '</div>';
}
function renderModelMenu() {
  const q = $('#modelSearch').value.trim().toLowerCase();
  const match = (m) => !q || `${m.label} ${m.vendor_label} ${m.id}`.toLowerCase().includes(q);
  const open = mgrpOpenSet();
  let html = '';
  const favs = visibleModels().filter((m) => S.pins.has(m.id) && match(m));
  const favSet = new Set(favs.map((m) => m.id));
  if (favs.length) {
    html += '<div class="mgrp fav">我的最愛</div>';
    let lastV = null;
    for (const m of favs) {
      if (m.vendor !== lastV) { html += `<div class="mfv">${esc(vendorName(m.vendor))}</div>`; lastV = m.vendor; }
      html += modelRowHtml(m);
    }
  }
  const groups = new Map();
  for (const m of visibleModels()) {
    if (!match(m) || favSet.has(m.id)) continue;   // 已經在「我的最愛」的不再重複列
    const k = m.vendor || '';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(m);
  }
  const order = S.vendors.map((v) => v.id);
  const keys = [...groups.keys()].sort((a, b) => (order.indexOf(a) + 1 || 999) - (order.indexOf(b) + 1 || 999));
  for (const k of keys) {
    const isOpen = !!q || open.has(k);
    const ms = groups.get(k);
    html += `<div class="mgrp vg${isOpen ? ' open' : ''}" data-vg="${esc(k)}"><span>${esc(vendorName(k))}</span>${ico('down')}<small>${ms.length}</small></div>`;
    if (isOpen) html += ms.map(modelRowHtml).join('');
  }
  $('#modelList').innerHTML = html || '<div class="mempty">找不到模型。到「編輯模型」可以勾選顯示。</div>';
}
async function reloadModels(quiet) {
  const st = await api('GET', '/api/state');
  S.models = st.models; S.vendors = st.vendors || []; S.backends = st.backends || [];
  renderModelSelect();
  if (!quiet) notify('ok', '已重新整理模型');
}
function openModelEdit() {
  closePops();
  let ov = $('#mEdit');
  if (!ov) {
    ov = document.createElement('div'); ov.id = 'mEdit'; ov.className = 'medit';
    ov.innerHTML = `<div class="medit-box"><div class="medit-hd"><span>模型</span><span class="cp-x" data-medit="close" title="關閉">${ico('x')}</span></div>`
      + '<label class="msearch"><svg><use href="#i-search"/></svg><input id="mEditQ" placeholder="搜尋模型"></label>'
      + '<div class="medit-list" id="mEditList"></div>'
      + '<div class="medit-ft"><span data-medit="add">新增提供方…</span><span data-medit="reset">恢復預設</span></div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', onModelEditClick);
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) ov.classList.remove('on'); });
    $('#mEditQ', ov).addEventListener('input', renderModelEdit);
    ov.addEventListener('keydown', (e) => { if (e.key === 'Escape') ov.classList.remove('on'); });
  }
  S.meditOpen = S.meditOpen || new Set();
  ov.classList.add('on');
  $('#mEditQ', ov).value = '';
  renderModelEdit();
  $('#mEditQ', ov).focus();
}
function renderModelEdit() {
  const ov = $('#mEdit'); if (!ov) return;
  const q = $('#mEditQ', ov).value.trim().toLowerCase();
  const match = (m) => !q || `${m.label} ${m.vendor_label} ${m.id}`.toLowerCase().includes(q);
  const groups = new Map();
  for (const m of S.models) {
    if (!match(m) || isGenModel(m)) continue;
    const k = m.vendor || '';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(m);
  }
  const order = S.vendors.map((v) => v.id);
  const keys = [...groups.keys()].sort((a, b) => (order.indexOf(a) + 1 || 999) - (order.indexOf(b) + 1 || 999));
  let html = '';
  for (const k of keys) {
    const ms = groups.get(k);
    const on = ms.filter((m) => m.enabled !== false).length;
    const st = on === 0 ? 'none' : on === ms.length ? 'all' : 'some';
    const isOpen = !!q || S.meditOpen.has(k);
    html += `<div class="mv${isOpen ? ' open' : ''}"><span class="mvn" data-vexp="${esc(k)}">${esc(vendorName(k))}${ico('right')}<small>${on}/${ms.length}</small></span>`
      + `<span class="tri ${st === 'all' ? 'on' : st === 'some' ? 'some' : ''}" data-vtog="${esc(k)}" data-st="${st}" title="${st === 'all' ? '全部隱藏' : '全部顯示'}"></span></div>`;
    if (isOpen) html += ms.map((m) => `<div class="mm"><span class="mt">${esc(m.label)}</span>`
      + `<span class="sw sm${m.enabled !== false ? ' on' : ''}" data-mtog="${esc(m.id)}" title="${m.enabled !== false ? '在選單裡隱藏' : '在選單裡顯示'}"></span></div>`).join('');
  }
  $('#mEditList', ov).innerHTML = html || '<div class="mempty">找不到模型</div>';
}
async function saveModelsEnabled(map) {
  try {
    const c = await api('GET', '/api/settings');
    const models = c.models.map((m) => {
      const x = { ...m };
      if (m.id in map) { if (map[m.id]) delete x.enabled; else x.enabled = false; }
      return x;
    });
    await api('POST', '/api/settings', { models });
    await reloadModels(true);
    renderModelEdit();
  } catch (err) { notify('error', '儲存失敗', err.message); }
}
async function onModelEditClick(e) {
  const ov = $('#mEdit');
  if (e.target.closest('[data-medit="close"]')) { ov.classList.remove('on'); return; }
  if (e.target.closest('[data-medit="add"]')) { ov.classList.remove('on'); openSettings('vendors'); return; }
  if (e.target.closest('[data-medit="reset"]')) { await saveModelsEnabled(Object.fromEntries(S.models.map((m) => [m.id, true]))); return; }
  const ex = e.target.closest('[data-vexp]');
  if (ex) { const k = ex.dataset.vexp; S.meditOpen.has(k) ? S.meditOpen.delete(k) : S.meditOpen.add(k); renderModelEdit(); return; }
  const vt = e.target.closest('[data-vtog]');
  if (vt) {
    const turnOn = vt.dataset.st !== 'all', k = vt.dataset.vtog;
    await saveModelsEnabled(Object.fromEntries(S.models.filter((m) => (m.vendor || '') === k && !isGenModel(m)).map((m) => [m.id, turnOn])));
    return;
  }
  const mt = e.target.closest('[data-mtog]');
  if (mt) {
    const id = mt.dataset.mtog, m = S.models.find((x) => x.id === id);
    await saveModelsEnabled({ [id]: m.enabled === false });
  }
}
async function chooseModel(id) {
  const m = modelOf(id); if (!m) return;
  if (!m.available) { notify('error', `「${m.label}」不能用`, '它需要的插件沒有載入。'); return; }
  S.modelId = m.id;
  const s = cur();
  if (!s) S.draft.model = m.id;   // 只記在這個還沒送出的新對話
  if (s) {
    Object.assign(s, await api('PATCH', `/api/sessions/${s.id}`, { model: m.id, effort: effortWant() }));
    renderSessions();
  }
  closePops(); renderModelSelect(); renderMeter();
  if (!S.msgs.length) renderChat();
}
$('#modelPill').onclick = () => {
  if (openPop($('#modelMenu'), $('#modelPill'))) {
    $('#modelSearch').value = ''; renderModelMenu(); $('#modelSearch').focus();
    $('#modelList').scrollTop = 0;  // 從最上面開始：我的最愛在最上面
  }
};
$('#modelSearch').addEventListener('input', renderModelMenu);
$('#modelSearch').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { const r = $('#modelList .mrow:not(.na)'); if (r) chooseModel(r.dataset.id); }
  if (e.key === 'Escape') { e.stopPropagation(); closePops(); }
});
$('#modelList').addEventListener('click', (e) => {
  const pin = e.target.closest('[data-pin]');
  if (pin) {
    const id = pin.dataset.pin;
    S.pins.has(id) ? S.pins.delete(id) : S.pins.add(id);
    localStorage.setItem('woof.pins', JSON.stringify([...S.pins]));
    renderModelMenu();
    return;
  }
  const vg = e.target.closest('[data-vg]');
  if (vg) {
    const set = mgrpOpenSet(), k = vg.dataset.vg;
    set.has(k) ? set.delete(k) : set.add(k);
    localStorage.setItem('woof.mgrpOpen', JSON.stringify([...set]));
    renderModelMenu();
    return;
  }
  const more = e.target.closest('[data-eff]');
  if (more) { openEffortMenu(modelOf(more.dataset.eff), more.closest('.mrow')); return; }
  const r = e.target.closest('.mrow'); if (r) chooseModel(r.dataset.id);
});
$('#modelReload').onclick = async () => { closePops(); await reloadModels(false); };
$('#modelAdd').onclick = () => { closePops(); openSettings('vendors'); };
$('#modelEdit').onclick = () => openModelEdit();
// 思考選單：上面「思考」開關，下面「推理強度」整條檔位；模型沒有的檔位標「下兼容到 X」
function effortMenuHtml(m) {
  const want = effortWant(), got = resolveEffort(m, want);
  const effs = (m.efforts || []).map(effNorm);
  const thinkOn = want !== 'off' && want !== 'none';
  const canOff = effs.includes('off');
  let h = `<div class="phd">${esc(m.label)}</div>`
    + `<div class="mi etog" data-etog="${thinkOn ? 'off' : 'on'}">思考<small>${!thinkOn && !canOff ? `不能關，下兼容到 ${EFFORT_LABEL[got]}` : ''}</small><span class="sw sm${thinkOn ? ' on' : ''}"></span></div>`;
  if (effortIsToggle(m)) return h + '<div class="ehint">這個模型只有開／關，不分強度。</div>';
  h += '<div class="sep"></div><div class="phd">推理強度</div>';
  for (const lv of EFFORT_SCALE) {
    const r = resolveEffort(m, lv), sel = thinkOn && lv === want;
    h += `<div class="mi${sel ? ' on' : ''}${r !== lv ? ' down' : ''}" data-e="${lv}"><span class="mck">${sel ? ico('check') : ''}</span>${EFFORT_LABEL[lv]}`
      + `<small>${r !== lv ? `下兼容到 ${EFFORT_LABEL[r]}` : ''}</small></div>`;
  }
  return h;
}
function openEffortMenu(m, anchorRow) {
  if (!m || !m.efforts.length) return;
  const menu = $('#effortMenu');
  if (!anchorRow) { if (!openPop(menu, $('#effortPill'))) return; }
  menu.dataset.mid = m.id;
  menu.innerHTML = effortMenuHtml(m);
  if (anchorRow) {
    // 從模型選單某一列的「›」打開：貼在模型選單旁邊
    menu.classList.add('on');
    const wrap = $('.cwrap').getBoundingClientRect(), mm = $('#modelMenu').getBoundingClientRect();
    let left = mm.right - wrap.left + 6;
    if (left + menu.offsetWidth > wrap.width) left = Math.max(0, mm.left - wrap.left - menu.offsetWidth - 6);
    menu.style.left = left + 'px';
  } else {
    const wrap = $('.cwrap').getBoundingClientRect(), a = $('#effortPill').getBoundingClientRect();
    let left = a.left - wrap.left;
    if (left + menu.offsetWidth > wrap.width) left = Math.max(0, a.right - wrap.left - menu.offsetWidth);
    menu.style.left = left + 'px';
    menu.style.bottom = $('#modelMenu').style.bottom;
  }
}
$('#effortPill').onclick = () => openEffortMenu(selectedModel(), null);
async function setEffort(val, mid) {
  if (val === 'on') val = localStorage.getItem('woof.effortLastOn') || 'high';
  if (val !== 'off') localStorage.setItem('woof.effortLastOn', val);
  const s = cur();
  if (s) { Object.assign(s, await api('PATCH', `/api/sessions/${s.id}`, { effort: val })); renderSessions(); }
  else S.draft.effort = val;   // 還沒送出的新對話：只記在這個草稿
  if (mid && mid !== S.modelId) return chooseModel(mid);
  closePops(); renderEffort();
}
$('#effortMenu').onclick = async (e) => {
  const b = e.target.closest('[data-e],[data-etog]'); if (!b) return;
  await setEffort(b.dataset.e ?? b.dataset.etog, $('#effortMenu').dataset.mid);
};

// 上下文用量：模型回報的輸入（已含快取）＋輸出；舊資料沒有 context_used 時自己算
const ctxUsed = (u) => !u ? 0 : u.context_used != null ? Number(u.context_used) || 0 : (u.input_tokens || 0) + (u.output_tokens || 0);
function renderMeter() {
  const u = S.usage[S.sid], m = selectedModel();
  const comp = Number(((cur() || {}).engine_state || {}).compress_count || 0);
  const win = winOf(u, m);
  const ring = $('#meterFill').parentElement;
  const compTxt = comp ? ` · 壓縮 ${comp} 次` : '';
  if (!u) { ring.style.setProperty('--p', 0); $('#meterTxt').textContent = '上下文 —' + compTxt; $('#meterTip').textContent = '上下文用量：送出第一則訊息後才會有數字'; return; }
  if (!win) {
    // 模型沒填上下文窗口：只顯示用了多少
    const n = ctxUsed(u);
    ring.style.setProperty('--p', 0);
    $('#meterTxt').textContent = '上下文 ' + (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n) + compTxt;
    $('#meterTip').textContent = `上下文：${n.toLocaleString()} tokens（這個模型沒有設定上下文窗口，在「設定 → 模型商」的模型或「設定 → 壓縮 → 上下文大小」填上就會顯示百分比）`;
    return;
  }
  const used = ctxUsed(u);
  const pct = Math.min(100, used / win * 100);
  const est = u.estimated ? '約 ' : '';
  ring.style.setProperty('--p', pct.toFixed(1));
  ring.classList.toggle('hot', pct >= 70);
  $('#meterTxt').textContent = '上下文 ' + est + (pct < 1 ? '<1%' : Math.round(pct) + '%') + compTxt;
  const cache = u.cache_read ? `，其中 ${Number(u.cache_read).toLocaleString()} 走快取` : '';
  $('#meterTip').textContent = `上下文：${est}${used.toLocaleString()} / ${win.toLocaleString()} tokens（${pct.toFixed(1)}%）${cache}${u.estimated ? '。壓縮或回溯後的估計值，下一次回覆後換成模型回報的數字' : ''}${S.ctxCap ? `。上下文大小用壓縮設定的 ${Number(S.ctxCap).toLocaleString()}` : ''}`;
}

// ------------------------------------------------------------------ 對話內容
// `cd <工作目錄> && 指令` 是每一條都一樣的樣板，卡片上不顯示；
// 真的換到別的目錄才在前面標一個 [資料夾] 提醒
function splitCd(cmd) {
  const raw = String(cmd || '');
  const m = raw.match(/^\s*cd\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*(?:&&|;|\n)\s*([\s\S]*)$/);
  if (!m) return { body: raw, dir: '' };
  const dir = m[1] || m[2] || m[3] || '';
  return { body: m[4], dir };
}
function normPath(p) {
  return String(p || '').replace(/\\/g, '/').replace(/^\/([A-Za-z])\//, '$1:/').replace(/^\/?([A-Za-z]):\//, (x, d) => d.toLowerCase() + ':/')
    .replace(/\/+$/, '').toLowerCase();
}
function toolArgSummary(name, a) {
  if (!a) return '';
  // 工具延遲載入的橋接：直接顯示真正要用的工具
  if (name === 'tool_call' && Array.isArray(a.calls)) return '→ ' + a.calls.map((c) => (c && c.name) || '?').join('、');
  if (name === 'tool_search' && Array.isArray(a.queries)) return a.queries.join('、').slice(0, 160);
  if (name === 'tool_describe' && Array.isArray(a.names)) return a.names.join('、').slice(0, 160);
  if (name === 'process') {
    const { body, dir } = splitCd(a.command || '');
    const same = !dir || !S.workdir || normPath(dir) === normPath(S.workdir);
    const bits = [a.action, a.task_id, a.timeout ? `最多 ${a.timeout} 秒` : ''].filter(Boolean);
    if (a.command) bits.push((same ? '' : `[${dir.split(/[\\/]/).filter(Boolean).pop() || dir}] `) + body.split(NL)[0].slice(0, 140));
    return bits.join(' ');
  }
  if (name === 'terminal' && a.command) {
    const { body, dir } = splitCd(a.command);
    const same = !dir || !S.workdir || normPath(dir) === normPath(S.workdir);
    const tag = same ? '' : `[${dir.split(/[\\/]/).filter(Boolean).pop() || dir}] `;
    return tag + body.trim().split(NL).join(' ').slice(0, 300);
  }
  const v = a.command || a.path || a.file_path || a.pattern || a.query || a.prompt || a.goal || a.name || a.url || a.description || '';
  return typeof v === 'string' ? v.split(NL)[0].slice(0, 160) : JSON.stringify(a).slice(0, 160);
}
// 工具參數照人看的方式排：字串裡的換行真的換行（不顯示 \n），多行內容縮排在欄位名底下
function fmtArgs(a) {
  if (!a || typeof a !== 'object') return String(a ?? '');
  const lines = [];
  for (const [k, v] of Object.entries(a)) {
    if (typeof v === 'string') {
      if (v.includes(NL) || v.length > 120) lines.push(`${k}:`, ...v.split(NL).map((l) => '  ' + l));
      else lines.push(`${k}: ${v}`);
    } else lines.push(`${k}: ${JSON.stringify(v)}`);
  }
  return lines.join(NL);
}
// 失敗的工具要一眼看到原因，不然只有「失敗 · 7ms」，得點開才知道為什麼
function errPreview(name, out) {
  const t = String(out || '').trim();
  if (!t) return '（沒有錯誤訊息）';
  const lines = t.split(NL).map((s) => s.trim()).filter(Boolean);
  // terminal／process 的錯誤在結尾（traceback 最後一行、curl 的失敗原因），前面都是命令回顯
  // 先挑看起來像錯誤的那幾行（KeyError、curl: (6)、Permission denied…），沒有就取結尾
  const ERR_RE = /error|exception|traceback|refused|denied|not found|no such|cannot|failed|fatal|exit [1-9]|退出碼 [1-9]/i;
  const bad = lines.filter((l) => ERR_RE.test(l));
  const pick = bad.length ? bad.slice(-2) : ((name === 'terminal' || name === 'process') ? lines.slice(-2) : lines.slice(0, 3));
  return pick.join(' · ').slice(0, 300);
}

function toolCard(call, result) {
  const live = S.tools[call.id] || {};
  const running = !result && live.running;
  const out = result ? result.output : live.output != null ? live.output : live.out || '';
  const isErr = result ? result.is_error : live.is_error;
  let st;
  if (!result && S.approvals[call.id]) st = '<span class="st-run">等你確認</span>';
  else if (running) st = `<span class="st-run"><span class="spin"></span>執行中<span class="el" data-t0="${live.t0 || Date.now()}"></span></span>`;
  else if (!result && !live.output && live.stopped) st = '<span class="st-dim">已停止</span>';
  else if (isErr) st = `<span class="st-bad">失敗${live.ms != null ? ' · ' + fmtMs(live.ms) : ''}</span>`;
  else if (result || live.output != null) st = `<span class="st-ok">${ico('check')}${live.ms != null ? fmtMs(live.ms) : ''}</span>`;
  else st = `<span class="st-dim">${S.running.has(S.sid) ? '—' : '中斷，沒有結果'}</span>`;
  const el = document.createElement('div');
  el.className = `tool${running ? ' open' : ''}${isErr ? ' err' : ''}`;
  el.dataset.id = call.id;
  const args = fmtArgs(call.args);
  el.innerHTML = `<div class="th"><span class="ti">${TOOL_ICON[call.name] || '🔧'}</span><code>${esc(call.name)}</code><span class="arg" title="${esc(toolArgSummary(call.name, call.args))}">${esc(toolArgSummary(call.name, call.args))}</span><span class="st">${st}</span>${ico('right', 'chev')}</div><pre></pre>`;
  if (isErr && !running) {
    const eb = document.createElement('div');
    eb.className = 'terr';
    eb.textContent = errPreview(call.name, out);
    eb.title = '點開看完整輸出';
    eb.onclick = (ev) => { ev.stopPropagation(); el.classList.toggle('open'); };
    el.insertBefore(eb, $('pre', el));
  }
  // 執行中還沒有輸出：先顯示參數，不要一片空白
  const raw = running ? (out || args + NL + '────────' + NL + '（等待輸出…）') : args + NL + '────────' + NL + (out || '');
  const parts = raw.split(NL);
  const clip = parts.length > 14 && !(S.fullTools && S.fullTools.has(call.id));
  $('pre', el).textContent = (clip ? parts.slice(-8).join(NL) : raw).slice(-30000);
  if (clip) {
    const more = document.createElement('div');
    more.className = 'more';
    more.textContent = '顯示全部（還有 ' + (parts.length - 8) + ' 行）';
    more.onclick = (ev) => {
      ev.stopPropagation();
      S.fullTools = S.fullTools || new Set();
      S.fullTools.add(call.id);
      el.classList.add('open');
      $('pre', el).textContent = raw.slice(-30000);
      more.remove();
    };
    el.appendChild(more);
  }
  if (S.openTools && S.openTools.has(call.id)) el.classList.add('open');
  // 工具交給模型自己看的圖（vision_analyze）：卡片上直接顯示，點一下用看圖器開
  const imgs = (result && result.images) || live.images || [];
  if (imgs.length) {
    const box = document.createElement('div');
    box.className = 'timgs';
    for (const p of imgs) {
      const im = document.createElement('img');
      im.src = fileUrl(p.path); im.loading = 'lazy'; im.title = p.path;
      im.onclick = (ev) => { ev.stopPropagation(); openPreview(p.path); };
      box.appendChild(im);
    }
    el.insertBefore(box, $('pre', el));
  }
  if (call.name === 'delegate_task') {
    const sids = [...String(out || '').matchAll(/對話 ([0-9a-f]{12})）/g)].map((x) => x[1]);
    // 執行中還沒有輸出：只列「這次派出的」一般子代理（審核另外一疊，不混進來）
    const t0 = Number(call.started || 0) || 0;
    const kids = S.sessions.filter((s) => s.parent === S.sid && subKind(s) !== 'review'
      && (sids.includes(s.id) || (running && (!t0 || (s.created || 0) >= t0 - 2))));
    const list = sids.length ? sids.map((id) => S.sessions.find((s) => s.id === id) || { id, title: '子代理' }) : kids;
    if (list.length) {
      const bar = document.createElement('details');
      bar.className = 'subfold';
      bar.dataset.tk = 'subfold-' + (call.id || S.sid || '');
      bar.open = THINK_OPEN.has(bar.dataset.tk);
      const rows = list.map((s) => {
        const live = S.running.has(s.id) || s.busy;
        return `<div class="subrow"><span class="sl" data-opensub="${esc(s.id)}">${ico('agent')}${esc(s.title || '子代理')}</span>`
          + (live ? `<span class="sb stop" data-stopsub="${esc(s.id)}">停止</span>` : '')
          + `<span class="sb" data-closesub="${esc(s.id)}">關閉</span></div>`;
      }).join('');
      bar.innerHTML = `<summary>${ico('agent')}子代理 ${list.length} 個（點開看）</summary><div class="sublinks">${rows}</div>`;
      el.appendChild(bar);
    }
  }
  const ap = S.approvals[call.id];
  if (ap) {
    el.classList.add('ask', 'open');
    const bar = document.createElement('div');
    bar.className = 'approve';
    bar.innerHTML = `<span class="q"><b>需要你確認</b> · 允許執行 <code>${esc(call.name)}</code>？${ap.reason ? `<small class="ap-why${/^危險/.test(ap.reason) ? ' bad' : ''}">${esc(ap.reason)}</small>` : ''}</span>`
      + `<span class="btn sm" data-approve="deny" data-id="${esc(call.id)}">拒絕</span>`
      + `<span class="btn sm" data-approve="always" data-id="${esc(call.id)}">這個對話都允許</span>`
      + `<span class="btn sm primary" data-approve="allow" data-id="${esc(call.id)}">允許</span>`;
    el.appendChild(bar);
  }
  return el;
}
$('#chat').addEventListener('click', (e) => {
  const os = e.target.closest('[data-opensub]');
  if (os) { e.stopPropagation(); openSession(os.dataset.opensub); return; }
  const ss = e.target.closest('[data-stopsub]');
  if (ss) { e.stopPropagation(); stopSub(ss.dataset.stopsub); return; }
  const cs = e.target.closest('[data-closesub]');
  if (cs) { e.stopPropagation(); closeSub(cs.dataset.closesub); return; }
  if (e.target.closest('[data-backparent]')) { const s = cur(); if (s && s.parent) openSession(s.parent); return; }
  const ap = e.target.closest('[data-approve]');
  if (ap) {
    if (wsSend({ type: 'approve', id: ap.dataset.id, decision: ap.dataset.approve })) { delete S.approvals[ap.dataset.id]; renderChat(); }
    return;
  }
  const act = e.target.closest('[data-act]');
  if (act && act.closest('.acts')) return msgAction(act.dataset.act, act.closest('.msg'));
  const th = e.target.closest('.tool .th');
  if (th) {
    const t = th.parentElement; t.classList.toggle('open');
    S.openTools = S.openTools || new Set();
    t.classList.contains('open') ? S.openTools.add(t.dataset.id) : S.openTools.delete(t.dataset.id);
  }
  const img = e.target.closest('.thumbs img, .md img, .bubble img');
  if (img) { e.preventDefault(); e.stopPropagation(); openViewer(img); return; }
  // 回覆裡指向圖片的連結（本機檔或網路圖片）也在 WOOF 裡看，不開瀏覽器
  const fl = e.target.closest('a.flink');
  if (fl) { e.preventDefault(); e.stopPropagation(); openFileLink(flinkPath(fl)); return; }
  const lk = e.target.closest('.md a[href]');
  if (lk && IMG_EXT.test((lk.getAttribute('href') || '').split(/[?#]/)[0])) { e.preventDefault(); openViewer({ src: lk.href }); return; }
  const sug = e.target.closest('[data-suggest]');
  if (sug) { input.value = sug.dataset.suggest; autosize(); input.focus(); }
});
// ── 看圖（在 WOOF 裡開，不丟瀏覽器）────────────────────────────
const V = { list: [], i: 0, s: 1, x: 0, y: 0, rot: 0, fit: 1, moved: false };
const vImg = $('#viewerImg'), vStage = $('#vStage');
function openViewer(img) {
  const all = [...$('#chat').querySelectorAll('.thumbs img, .md img, .bubble img')].filter((x) => x.src);
  V.list = [...new Set(all.map((x) => x.src))];
  V.i = Math.max(0, V.list.indexOf(img.src));
  if (!V.list.length) V.list = [img.src];
  $('#viewer').classList.add('on');
  $('#viewer').classList.toggle('multi', V.list.length > 1);
  showViewerImg();
}
function closeViewer() { $('#viewer').classList.remove('on'); vImg.removeAttribute('src'); }
function viewerPath(src) { try { return new URL(src).searchParams.get('path') || ''; } catch { return ''; } }
function showViewerImg() {
  const src = V.list[V.i]; const p = viewerPath(src);
  $('#vName').textContent = (p ? p.split(/[\\/]/).pop() : '圖片') + (V.list.length > 1 ? `　${V.i + 1} / ${V.list.length}` : '');
  $('#vFolder').hidden = !(p && D && D.showItem);
  V.rot = 0;
  vImg.onload = () => { viewerFit(false); };
  vImg.src = src;
  if (vImg.complete && vImg.naturalWidth) viewerFit(false);
}
function viewerFitScale() {
  const r = vStage.getBoundingClientRect(); const side = V.rot % 180 !== 0;
  const w = side ? vImg.naturalHeight : vImg.naturalWidth, h = side ? vImg.naturalWidth : vImg.naturalHeight;
  if (!w || !h) return 1;
  return Math.min((r.width - 120) / w, (r.height - 150) / h, 1);   // 下方留給工具列
}
function viewerApply(anim) {
  vImg.classList.toggle('anim', !!anim);
  vImg.style.transform = `translate(-50%,-50%) translate(${V.x}px,${V.y}px) rotate(${V.rot}deg) scale(${V.s})`;
  $('#vPct').textContent = Math.round(V.s * 100) + '%';
}
function viewerFit(anim = true) { V.fit = viewerFitScale(); V.s = V.fit; V.x = 0; V.y = -26; viewerApply(anim); }
// 以畫面上某一點為中心縮放（滾輪、雙擊用游標位置；按鈕用畫面中心）
function viewerZoom(to, cx, cy, anim) {
  const r = vStage.getBoundingClientRect();
  const s2 = Math.min(16, Math.max(0.05, to));
  const ox = (cx ?? r.left + r.width / 2) - (r.left + r.width / 2), oy = (cy ?? r.top + r.height / 2) - (r.top + r.height / 2);
  V.x = ox - (ox - V.x) * (s2 / V.s); V.y = oy - (oy - V.y) * (s2 / V.s); V.s = s2;
  viewerApply(anim);
}
function viewerStep(d) { if (V.list.length < 2) return; V.i = (V.i + d + V.list.length) % V.list.length; showViewerImg(); }
function viewerAct(a) {
  if (a === 'in') viewerZoom(V.s * 1.25, null, null, true);
  else if (a === 'out') viewerZoom(V.s / 1.25, null, null, true);
  else if (a === 'fit') viewerFit();
  else if (a === 'one') { V.x = 0; V.y = 0; V.s = 1; viewerApply(true); }
  else if (a === 'rot') { V.rot = (V.rot + 90) % 360; viewerFit(); }
  else if (a === 'prev') viewerStep(-1);
  else if (a === 'next') viewerStep(1);
  else if (a === 'close') closeViewer();
  else if (a === 'folder') { const p = viewerPath(V.list[V.i]); if (p && D && D.showItem) D.showItem(p); }
}
$('#viewer').addEventListener('click', (e) => {
  const b = e.target.closest('[data-v]');
  if (b) { viewerAct(b.dataset.v); return; }
  if (V.moved) { V.moved = false; return; }
  if (e.target === vStage) closeViewer();   // 點圖片外的空白處關閉
});
vStage.addEventListener('wheel', (e) => {
  e.preventDefault();
  const k = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018));
  viewerZoom(V.s * k, e.clientX, e.clientY, false);
}, { passive: false });
vStage.addEventListener('dblclick', (e) => {
  if (Math.abs(V.s - V.fit) < 0.01 && V.fit < 1) viewerZoom(1, e.clientX, e.clientY, true);
  else if (Math.abs(V.s - V.fit) < 0.01) viewerZoom(V.s * 2, e.clientX, e.clientY, true);
  else viewerFit();
});
vStage.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  const sx = e.clientX, sy = e.clientY, bx = V.x, by = V.y;
  V.moved = false; vStage.setPointerCapture(e.pointerId); vStage.classList.add('drag');
  const mv = (ev) => {
    if (!V.moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 4) return;
    V.moved = true; V.x = bx + ev.clientX - sx; V.y = by + ev.clientY - sy; viewerApply(false);
  };
  const up = () => { vStage.classList.remove('drag'); vStage.removeEventListener('pointermove', mv); vStage.removeEventListener('pointerup', up); vStage.removeEventListener('pointercancel', up); };
  vStage.addEventListener('pointermove', mv); vStage.addEventListener('pointerup', up); vStage.addEventListener('pointercancel', up);
});
window.addEventListener('resize', () => { if ($('#viewer').classList.contains('on') && Math.abs(V.s - V.fit) < 0.01) viewerFit(false); });
document.addEventListener('keydown', (e) => {
  if (!$('#viewer').classList.contains('on')) return;
  const k = e.key;
  const map = { '+': 'in', '=': 'in', '-': 'out', '_': 'out', '0': 'fit', '1': 'one', r: 'rot', R: 'rot', ArrowLeft: 'prev', ArrowRight: 'next' };
  if (map[k]) { e.preventDefault(); e.stopPropagation(); viewerAct(map[k]); }
}, true);

function whoLine(meta) {
  const m = modelOf(meta && meta.model);
  return m ? m.label : (meta && meta.model) || 'WOOF';
}

function emptyState() {
  const m = selectedModel();
  const tips = ['整理這個資料夾裡最近修改的檔案', '幫我查今天的科技新聞重點', '看一下這張截圖哪裡有問題'];
  return '<div class="empty"><div class="big">WOOF</div>'
    + `<div class="sub">${m ? `目前使用 <b>${esc(m.label)}</b> · ${esc(m.vendor_label)}` : '先到設定加一個模型商'}</div>`
    + `<div class="sugs">${tips.map((t) => `<span data-suggest="${esc(t)}">${esc(t)}</span>`).join('')}</div></div>`;
}

// 上一回合沒做完：後端記的 interrupted（出錯、停止、步數上限），或最後一則是工具結果／使用者訊息卻沒有回覆（程式被關掉）
function resumeInfo() {
  // 回合、方案／目標、對抗審核子代理、待答提問都算「還在跑」：那時顯示「沒做完」會讓人以為卡死
  if (!S.sid || S.running.has(S.sid) || (cur() || {}).busy) return null;
  const es = ((cur() || {}).engine_state) || {};
  let r = null;
  if (es.interrupted) r = { reason: es.interrupted.reason || '中途停下', kind: es.interrupted.kind || '', key: 'i' + es.interrupted.at };
  else {
    const last = [...S.msgs].reverse().find((m) => typeof m.id === 'number');
    const lastCalls = last && last.role === 'assistant' ? last.content.filter((b) => b.type === 'tool_call') : [];
    if (last && last.role === 'tool') r = { reason: '工具做完後模型沒有接著回覆（程式關閉或連線中斷）', kind: 'lost', key: 'm' + last.id };
    else if (lastCalls.length && !(last.meta || {}).stopped) r = { reason: '工具執行到一半程式被關閉，沒有拿到結果', kind: 'lost', key: 'm' + last.id };
    else if (last && last.role === 'user' && Date.now() / 1000 - (last.created || 0) > 20) r = { reason: '送出後沒有收到回覆（程式關閉或連線中斷）', kind: 'lost', key: 'm' + last.id };
  }
  if (!r || S.resumeHide[S.sid] === r.key) return null;
  return r;
}
function continueRun() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  if (S.running.has(S.sid)) { notify('warn', '還在執行中'); return; }
  const r = resumeInfo();
  wsSend({ type: 'continue', session_id: S.sid, reason: r ? r.reason : '' });
}
function renderTaskBar() {
  const bar = $('#taskBar'); if (!bar) return;
  renderModePill();
  const rs = resumeInfo();
  const ICON = { pending: '○', doing: '◐', done: '●', skipped: '–', failed: '✕' };
  const todos = S.todos[S.sid] || [];
  // ask_user 的卡片不放這裡：它是「對話的一部分」，放在對話串尾端跟著捲（見 renderChat），
  // 放輸入區上方會固定佔版面，把對話區壓到看不見上下文。
  let h = '';
  if (todos.length) {
    const done = todos.filter((t) => t.status === 'done').length;
    // 收合狀態每個對話各自記住，進度更新重畫時照舊，不會自己彈開
    h += `<details class="todos"${todoFolded(S.sid) ? '' : ' open'}><summary>TODO ${done}/${todos.length}</summary>`
      + todos.map((t) => `<div class="todo ${esc(t.status)}"><span class="ti">${ICON[t.status] || '○'}</span>${esc(t.text)}</div>`).join('')
      + '</details>';
  }
  if (rs) {
    h += `<div class="resumecard ${rs.kind === 'stopped' ? 'soft' : ''}"><div class="rc-tx"><b>${rs.kind === 'stopped' ? '已停止，還沒做完' : '上一回合沒做完'}</b>`
      + `<span title="${esc(rs.reason)}">${esc(rs.reason)}</span></div>`
      + `<button class="btn" data-resume-x>略過</button><button class="btn primary" data-resume>繼續</button></div>`;
  }
  if (bar._h === h) return;   // 內容沒變就不要重建：重建會讓使用者選好的字被清掉、也一直在閃
  bar._h = h; bar.innerHTML = h; bar.hidden = !h;
}
function todoOpenSet() { try { return new Set(JSON.parse(localStorage.getItem('woof.todoOpen') || '[]')); } catch { return new Set(); } }
// 預設收合：TODO 一大串會把輸入區整個往下推，看不到對話。使用者自己點開才展開，狀態照對話記住
function todoFolded(sid) { return !todoOpenSet().has(sid); }
$('#taskBar').addEventListener('toggle', (e) => {
  if (!e.target.matches('details.todos') || !S.sid) return;
  const s = todoOpenSet();
  if (e.target.open) s.add(S.sid); else s.delete(S.sid);
  localStorage.setItem('woof.todoOpen', JSON.stringify([...s].slice(-200)));
}, true);
$('#taskBar').addEventListener('click', (e) => {
  if (e.target.closest('[data-resume]')) continueRun();
  else if (e.target.closest('[data-resume-x]')) { const r = resumeInfo(); if (r) S.resumeHide[S.sid] = r.key; renderTaskBar(); }
});
const ASK_SKIP = '（略過這題，你自己判斷決定）';
// 問題卡片的倒數：時間到後端會自動略過；在卡片上打字會重新計時
// 生成速度：每一步的 out_tok／gen_s（woof/agent/loop.py _speed）。一次整段吐出來的（gen_s 太短）不算速度
function stepRate(s) { return s.gen_s >= 0.3 ? s.out_tok / s.gen_s : null; }
function spdText(steps) {
  steps = steps || []; if (!steps.length) return '';
  const ok = steps.filter((s) => stepRate(s) != null);
  const tok = ok.reduce((a, s) => a + s.out_tok, 0), sec = ok.reduce((a, s) => a + s.gen_s, 0);
  // 填充：送出到第一個字（模型讀完上下文、開始吐字前）；多步加總
  const fill = steps.reduce((a, s) => a + (s.ttft_s || 0), 0);
  return [ok.length ? `${Math.round(tok / sec)} tok/s` : '', `填充 ${fill < 10 ? fill.toFixed(1) : Math.round(fill)}s`].filter(Boolean).join(' · ');
}
function spdTitle(steps) {
  if (!(steps || []).length) return '';
  const rows = steps.map((s, i) => `第 ${i + 1} 步：${stepRate(s) != null ? Math.round(stepRate(s)) + ' tok/s' : '一次回完'}，${s.tok_est ? '約 ' : ''}${s.out_tok} tokens，生成 ${s.gen_s} 秒，第一個字等了 ${s.ttft_s} 秒`);
  return ` title="${esc((steps.length > 1 ? 'tok/s 是整則平均（只算生成時間），填充是每步等第一個字的時間加總\n' : '填充＝送出到第一個字的時間（模型讀上下文）\n') + rows.join('\n'))}"`;
}
function askSpan(sec) { const s = Math.max(0, Math.round(sec)); return s >= 60 ? `${Math.floor(s / 60)} 分${s % 60 ? ` ${s % 60} 秒` : '鐘'}` : `${s} 秒`; }
function askLeft(a) {
  const s = Math.max(0, Math.ceil(a.deadline - Date.now() / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} 內沒回就自動略過，讓模型自己判斷（在這裡打字會重新計時）`;
}
setInterval(() => {
  for (const el of document.querySelectorAll('[data-askcd]')) {
    const a = S.asks[el.dataset.askcd]; if (!a || !a.deadline) continue;
    el.textContent = askLeft(a);
    el.classList.toggle('soon', a.deadline - Date.now() / 1000 < 30);
  }
}, 1000);
const ASK_TOUCHED = {};
function askTouch(id) {
  const a = S.asks[id]; if (!a || !a.deadline || Date.now() - (ASK_TOUCHED[id] || 0) < 5000) return;
  ASK_TOUCHED[id] = Date.now();
  a.deadline = Date.now() / 1000 + (a.timeout || 0);   // 先在畫面上重設，後端回來再對齊
  api('POST', `/api/asks/${id}/touch`).then((r) => { if (r && r.deadline && S.asks[id]) S.asks[id].deadline = r.deadline; }).catch(() => {});
}
async function answerAsk(id, text) {
  try { await api('POST', `/api/asks/${id}`, { answer: text }); } catch (e) { notify('error', '回答送出失敗', e.message); return; }
  // 先標記已送出、讓卡片收起來，但不要刪掉那個 ask：後端回的 ask_done 要靠它才知道題目是什麼
  const a = S.asks[id];
  if (a) a._sent = text;
  renderChat(); renderTaskBar();
}
function goalPrompt() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  promptBox('設定目標', '要達成什麼？', '', { desc: '會先拆成 1～5 步，照做完之後由審核模型驗一次。輸入 stop 可以停止目前的目標。' }).then(async (raw) => {
    const obj = (raw || '').trim(); if (!obj) return;
    if (obj.toLowerCase() === 'stop') {
      const r = await api('POST', `/api/sessions/${S.sid}/goal/stop`);
      notify(r.ok ? 'ok' : 'info', r.ok ? '已停止目標' : '目前沒有執行中的目標'); return;
    }
    try { await api('POST', `/api/sessions/${S.sid}/goal`, { objective: obj }); notify('ok', '目標開始執行', '步驟會出現在聊天區上方的 TODO。'); }
    catch (e) { notify('error', '無法開始目標', e.message); }
  });
}
const INTERNAL_LABEL = {
  goal_step: '目標・執行步驟', review_feedback: '審核回饋', review_request: '審核請求', expand_request: '思維拓展',
  plan_request: '方案請求', plan_feedback: '方案回饋', subagent_task: '子代理任務',
  steps_low: '步數快用完', nudge: '空回覆補問', truncated: '截斷接續', stall: '停下補推', todo_left: '待辦未完成',
  resume: '中斷後接續', bg_done: '背景指令結束',
};
const GATE_LABEL = { off: '一般', human: '方案・人工核准', model: '方案・對抗審核', expand: '方案・思維拓展' };
const GATE_HINT = {
  off: '照常對話，工具直接執行。',
  human: '先給你一份短方案（1～5 步），你按「照做」才動手；做完由審核模型（子代理）驗一次，它的意見會貼在對話裡。',
  model: '先提短方案，審核模型（子代理）只擋「漏步驟、順序錯、不可逆損害」；做完再由它驗一次，有缺口就補。打回和補做都有次數上限（設定 → 方案模式）。',
  expand: '先由拓展模型（子代理）把你的一句話補成完整的一套（A→A/B/C/D，並點名哪些該先查、不要瞎試），再照做；做完一樣驗收。',
};
function gateNow() { return S.sid ? (S.gate[S.sid] || 'off') : (localStorage.getItem('woof.gate') || 'off'); }
function renderModePill() {
  const g = gateNow(), pill = $('#modePill');
  if (!pill) return;
  $('#modePillTxt').textContent = { off: '一般', human: '方案・人工', model: '方案・對抗', expand: '方案・拓展' }[g] || '一般';
  pill.classList.toggle('hot', g !== 'off');
  pill.title = '對話模式：' + (GATE_LABEL[g] || '一般');
}
async function setGate(mode) {
  if (!S.sid) {
    localStorage.setItem('woof.gate', mode); renderModePill();
    notify('ok', '對話模式：' + GATE_LABEL[mode], mode === 'off' ? '' : '送出第一則訊息時，新對話會套用這個模式。');
    return;
  }
  try {
    await api('POST', `/api/sessions/${S.sid}/gate`, { mode });
    S.gate[S.sid] = mode; renderTaskBar(); renderModePill();
    notify('ok', '對話模式：' + GATE_LABEL[mode], mode === 'off' ? '' : GATE_HINT[mode]);
  } catch (e) { notify('error', '切換失敗', e.message); }
}
const MODE_ITEMS = [
  { v: 'off', label: GATE_LABEL.off, sub: GATE_HINT.off },
  { v: 'human', label: GATE_LABEL.human, sub: GATE_HINT.human },
  { v: 'model', label: GATE_LABEL.model, sub: GATE_HINT.model },
  { v: 'expand', label: GATE_LABEL.expand, sub: GATE_HINT.expand },
];
$('#modePill').onclick = () => openSpick($('#modePill'), MODE_ITEMS, gateNow(), (v) => setGate(v));
async function togglePlan() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  const on = !S.planOn[S.sid];
  await api('POST', `/api/sessions/${S.sid}/plan`, { on });
  S.planOn[S.sid] = on;
  notify('ok', on ? '預案模式已開' : '預案模式已關', on ? '模型只會出預案，不改檔、不跑指令。要照預案執行時，再按一次 /plan 關掉。' : '');
}
async function reviewNow(scope = 'last') {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  const n = notify('info', scope === 'changes' ? '審核本對話改過的檔案…' : '子代理審核中…', '', 0);
  try { const r = await api('POST', `/api/sessions/${S.sid}/review`, { scope }); n.remove(); notify(r.ok ? 'ok' : 'warn', r.ok ? '審核通過' : '審核未通過', r.reason || ''); }
  catch (e) { n.remove(); notify('error', '審核失敗', e.message); }
}
function renderChat(scrollBottom) {
  renderTaskBar();
  const chat = $('#chat');
  if (scrollBottom) S.follow = true;
  // ask 卡片在對話串裡，重畫時把還沒送出的答案跟焦點留著，不要打一半被清掉
  const askFocus = document.activeElement && document.activeElement.closest && document.activeElement.closest('[data-askin]');
  const askFocusId = askFocus ? askFocus.dataset.askin : null;
  const askCaret = askFocus ? askFocus.selectionStart : null;
  for (const el of chat.querySelectorAll('[data-askin]')) if (el.value) S.askDraft[el.dataset.askin] = el.value;
  const keep = chat.scrollTop;
  const anchor = S.follow === false ? chatAnchor(chat) : null;
  const inner = innerScrolls(chat);
  chat.innerHTML = '';
  const results = {};
  for (const m of S.msgs) if (m.role === 'tool') for (const b of m.content) if (b.type === 'tool_result') results[b.id] = b;
  const running = S.running.has(S.sid);
  const asksHere = Object.values(S.asks).filter((a) => a.session_id === S.sid);
  const doneHere = (S.askDone[S.sid] || []).filter((x) => Date.now() - (x.at || 0) < ASKDONE_TTL);
  if (!S.msgs.length && !running && !(S.errors[S.sid] || []).length && !asksHere.length && !doneHere.length) {
    chat.innerHTML = emptyState();
    renderTerm();
    return;
  }
  const curS = cur();
  if (curS && curS.parent) {
    const par = S.sessions.find((x) => x.id === curS.parent);
    chat.insertAdjacentHTML('beforeend', `<div class="subbar"><div>${ico('agent')}<span>這是子代理的對話${par ? `，由「${esc(par.title)}」派出` : ''}。子代理做完會把最後一則回覆交回主對話。</span>${par ? '<a data-backparent>回到主對話</a>' : ''}</div></div>`);
  }
  let group = null, lastUser = null;
  // 剛從「思考中」變成正式訊息的那一則：沿用使用者原本的展開狀態，不要一 commit 就收起來
  const carry = S.thinkCarry; S.thinkCarry = null;
  const groups = [];
  // 壓縮分隔線：畫在「被摘要的最後一則」之後；點開看摘要內容（原始訊息照樣顯示，只是送模型時改用摘要）
  const sum = ((curS && curS.engine_state) || {}).summary;
  const sumThrough = sum && sum.text ? Number(sum.through) || 0 : 0;
  let sumDone = !sumThrough;
  const putSummary = () => {
    sumDone = true; group = null;
    const det = document.createElement('details'); det.className = 'cmpmark';
    det.dataset.tk = 'cmp-' + (S.sid || '');
    const n = ((curS.engine_state || {}).compress_count) || 1;
    det.open = THINK_OPEN.has(det.dataset.tk);
    det.innerHTML = `<summary><span class="cm-line"></span><span class="cm-lab">已壓縮上面的對話${n > 1 ? `（第 ${n} 次）` : ''} · 摘要 ${sum.text.length} 字 · ${fmtTime(Number(sum.at) || 0)}</span><span class="cm-line"></span></summary>`
      + `<div class="cm-body"><div class="cm-note">模型之後看到的是這段摘要＋分隔線以下的訊息；上面的原文仍保留在這裡和資料庫。</div></div>`;
    $('.cm-body', det).appendChild(md(sum.text));
    chat.appendChild(det);
  };
  const newGroup = (meta) => {
    group = document.createElement('div'); group.className = 'msg bot';
    group.innerHTML = `<div class="who">${esc(whoLine(meta))}</div>`;
    group._info = { userId: lastUser, lastId: null, texts: [], created: null, model: meta && meta.model };
    groups.push(group);
    chat.appendChild(group);
  };
  for (const m of S.msgs) {
    if (!sumDone && typeof m.id === 'number' && m.id > sumThrough) putSummary();
    if (m.role === 'tool' && group && typeof m.id === 'number') group._info.lastId = m.id;
    if (m.role === 'user' && m.meta && m.meta.internal) {
      group = null;
      const det = document.createElement('details'); det.className = 'internal msg';
      det.dataset.mid = m.id;
      // 手動打開的內部指令不要在重畫時被收起來（跟思考框同一套 open 記錄）
      const itk = 'internal-' + m.id;
      det.dataset.tk = itk;
      det.open = THINK_OPEN.has(itk);
      const text = m.content.filter((b) => b.type === 'text').map((b) => b.text).join(NL);
      const first = (text.split(NL).find((l) => l.trim()) || '').replace(/^【[^】]*】/, '').slice(0, 60);
      det.innerHTML = `<summary><span class="itag">內部指令</span><span class="ilabel">${esc(INTERNAL_LABEL[m.meta.internal] || m.meta.internal)}</span>`
        + ` <span class="ifirst">${esc(first)}</span></summary><pre class="ibody">${esc(text)}</pre>`;
      chat.appendChild(det);
    } else if (m.role === 'user') {
      group = null;
      if (!m.pending && typeof m.id === 'number') lastUser = m.id;
      const div = document.createElement('div'); div.className = 'msg user' + (m.pending ? ' pending' : '');
      div.dataset.mid = m.id;
      const imgs = m.content.filter((b) => b.type === 'image');
      if (imgs.length) div.innerHTML = `<div class="thumbs">${imgs.map((b) => `<img data-img="${esc(b.path)}" src="${fileUrl(b.path)}">`).join('')}</div>`;
      const atts = m.content.filter((b) => b.type === 'file');
      if (atts.length) {
        const box = document.createElement('div'); box.className = 'thumbs files';
        box.innerHTML = atts.map((b) => `<span class="fchip" title="${esc(b.path)}" data-path="${esc(b.path)}">${ico('folder')}${esc(b.name)} <small>${fmtBytes(b.bytes || 0)}</small></span>`).join('');
        div.appendChild(box);
      }
      const text = m.content.filter((b) => b.type === 'text').map((b) => b.text).join(NL);
      if (text) { const bb = document.createElement('div'); bb.className = 'bubble'; bb.textContent = text; div.appendChild(bb); }
      if (m.meta && m.meta.note) div.insertAdjacentHTML('beforeend', `<div class="unote" title="${esc(m.meta.note)}">已附上設定變更${esc((m.meta.note.match(/#\d+/) || [''])[0])}給模型：${esc(m.meta.note.replace(/^[^\n]*\n/, '').replace(/(^|\n)-\s*/g, ' ').trim().slice(0, 90))}</div>`);
      if (!m.pending && typeof m.id === 'number') {
        div.insertAdjacentHTML('beforeend', `<div class="acts"><span class="meta">${fmtTime(m.created)}</span>`
          + `<span class="ib" data-act="copy" title="複製">${ico('copy')}</span><span class="ib" data-act="edit" title="編輯後重送">${ico('edit')}</span><span class="ib" data-act="rewind" title="回溯到這則（之後的訊息刪掉，只回溯對話，不還原檔案）">${ico('refresh')}</span></div>`);
      }
      chat.appendChild(div);
    } else if (m.role === 'assistant') {
      if (!group) newGroup(m.meta);
      const gi = group._info;
      if (typeof m.id === 'number') gi.lastId = m.id;
      gi.created = m.created || gi.created;
      if (m.meta && m.meta.model) gi.model = m.meta.model;
      if (m.meta && m.meta.out_tok && m.meta.gen_s != null) (gi.steps = gi.steps || []).push(m.meta);
      for (const b of m.content) if (b.type === 'text' && b.text) gi.texts.push(b.text);
      if (m.meta && m.meta.hidden_reasoning) group.insertAdjacentHTML('beforeend', `<div class="think hidden" title="服務商有算思考 token，但沒有把思考內容傳回來，所以看不到過程">思考了 ${m.meta.hidden_reasoning.toLocaleString()} tokens <small>服務商沒回傳內容</small></div>`);
      for (let bi = 0; bi < m.content.length; bi++) {
        const b = m.content[bi];
        if (b.type === 'thinking' && b.text) {
          const tk = 'm' + m.id + ':' + bi;
          const d = document.createElement('details'); d.className = 'think';
          d.dataset.tk = tk;
          // 使用者自己點開過的思考框，重畫後要維持展開（不然模型下一步就自己收起來）；
          // 剛從「思考中」commit 過來的那一個也要記進展開名單，否則下一次重畫又縮回去
          if (m.id === carry && bi === 0) THINK_OPEN.add(tk);
          d.open = THINK_OPEN.has(tk);
          d.innerHTML = `<summary>思考過程 <small>${b.text.length} 字</small></summary><div class="tx"></div>`;
          // 收起來的思考框不建文字節點：一則一萬多字的思考 × 十幾個，光塞 DOM 就會讓整段對話卡住
          d._th = b.text;
          if (d.open) $('.tx', d).textContent = b.text;
          group.appendChild(d);
        } else if (b.type === 'text' && b.text) group.appendChild(md(b.text));
        else if (b.type === 'tool_call') group.appendChild(toolCard(b, results[b.id]));
        else if (b.type === 'image') { const p = document.createElement('div'); p.className = 'thumbs'; p.style.justifyContent = 'flex-start'; p.innerHTML = `<img data-img="${esc(b.path)}" src="${fileUrl(b.path)}">`; group.appendChild(p); }
      }
      if (m.meta && m.meta.stopped) group.insertAdjacentHTML('beforeend', '<div class="stopped">已停止</div>');
    }
  }
  if (!sumDone) putSummary();
  if (running || (S.errors[S.sid] || []).length) {
    if (!group) newGroup({ model: (cur() || {}).model });
    if (running) {
      const lv = document.createElement('div'); lv.id = 'live'; group.appendChild(lv);
      fillLive(lv);
    }
    for (const e of S.errors[S.sid] || []) {
      const d = document.createElement('div');
      if (e && e.stopped) { d.className = 'stopped'; d.textContent = '已停止'; } else {
        // 紅框要能自己關掉：不然出一次錯就留在對話裡，還擋住下面的內容
        d.className = 'errbox closable';
        d.innerHTML = `<span class="ebtx"></span><span class="ebx" title="關掉這個提示">✕</span>`;
        $('.ebtx', d).textContent = e;
        $('.ebx', d).addEventListener('click', (ev) => {
          ev.stopPropagation();
          S.errors[S.sid] = (S.errors[S.sid] || []).filter((x) => x !== e);
          renderChat();
        });
      }
      group.appendChild(d);
    }
    // 子代理的工具確認：卡片不在這個對話裡，單獨列出
    for (const a of Object.values(S.approvals)) {
      if (a.session_id !== S.sid || chat.querySelector(`.tool[data-id="${CSS.escape(a.id)}"]`)) continue;
      const card = toolCard({ id: a.id, name: a.name, args: a.args }, null);
      $('.approve .q', card).innerHTML = `<b>子代理需要你確認</b> · 允許執行 <code>${esc(a.name)}</code>？`;
      group.appendChild(card);
    }
  }
  // ask_user 的卡片：接在對話串最後，跟著內容一起捲；不固定在輸入區上方
  for (const a of Object.values(S.asks)) {
    if (a.session_id !== S.sid || a._sent) continue;
    const div = document.createElement('div');
    div.className = 'msg bot askmsg';
    div.innerHTML = `<div class="who">${esc(whoLine({ model: (cur() || {}).model }))}</div>`
      + `<div class="askcard"><div class="q">${esc(a.question)}</div>`
      + `<div class="opts">${(a.options || []).map((o) => `<button class="btn" data-ask="${esc(a.id)}" data-val="${esc(o)}">${esc(o)}</button>`).join('')}</div>`
      + `<div class="ansrow"><input data-askin="${esc(a.id)}" placeholder="或自己輸入…">`
      + `<button class="btn primary" data-asksend="${esc(a.id)}">送出</button>`
      + `<button class="btn ghost ask-skip" data-askskip="${esc(a.id)}" title="不回答這題，讓模型自己判斷">略過</button></div>`
      + (a.deadline ? `<div class="ask-cd" data-askcd="${esc(a.id)}">${esc(askLeft(a))}</div>` : '') + `</div>`;
    chat.appendChild(div);
    const inp = $('[data-askin]', div);
    if (inp && S.askDraft[a.id]) inp.value = S.askDraft[a.id];
    if (inp && a.id === askFocusId) {
      inp.focus();
      const p = Math.min(askCaret == null ? inp.value.length : askCaret, inp.value.length);
      inp.setSelectionRange(p, p);
    }
  }
  // 已經回答過的提問：留在原處，顯示「已回答：…」或「已略過」，不要一答完就消失
  for (const d of doneHere) {
    const div = document.createElement('div');
    div.className = 'msg bot askmsg';
    div.innerHTML = `<div class="who">${esc(whoLine({ model: (cur() || {}).model }))}</div>`
      + `<div class="askcard askdone"><div class="q">${esc(d.question)}</div>`
      + `<div class="a">${d.timedOut ? `你 ${esc(askSpan(d.waited))}沒回應，已自動略過，讓模型自己判斷繼續` : d.skipped ? '已略過' : d.answer ? '已回答：' + esc(d.answer) : '已停止'}</div></div>`;
    chat.appendChild(div);
  }
  for (const id of Object.keys(S.askDraft)) if (!S.asks[id]) delete S.askDraft[id];
  groups.forEach((g, i) => {
    const gi = g._info;
    if (!gi.lastId || (running && i === groups.length - 1)) return;
    g.dataset.uid = gi.userId || '';
    g.dataset.last = gi.lastId;
    g._text = gi.texts.join(NL + NL);
    const m = modelOf(gi.model);
    g.insertAdjacentHTML('beforeend', `<div class="acts">`
      + `<span class="ib" data-act="copy" title="複製回覆">${ico('copy')}</span>`
      + (gi.userId ? `<span class="ib" data-act="retry" title="重新生成">${ico('refresh')}</span>` : '')
      + `<span class="ib" data-act="fork" title="從這裡分支成新對話">${ico('branch')}</span>`
      + `<span class="meta"${spdTitle(gi.steps)}>${esc([m ? m.label : gi.model, spdText(gi.steps), fmtTime(gi.created)].filter(Boolean).join(' · '))}</span></div>`);
    if (i === groups.length - 1 && !running) g.classList.add('last');
  });
  for (const g of chat.querySelectorAll('.msg.bot')) foldTools(g);
  restoreInnerScrolls(chat, inner);
  if (S.follow !== false) stickBottom(); else chatRestore(chat, anchor, keep);
  updateToBottom();
  renderTerm();
}

// 連續同一種工具（≥3 個、都跑完了）收成一列：十幾條 terminal 各占 34px 會把對話擠掉。
// 點開才看每一條；展開狀態記在 S.openGrp（以第一個工具 id 當 key），重畫不會自己收起來。
function foldTools(root) {
  let kids = [...root.children].filter((k) => k.classList && k.classList.contains('tool'));
  let guard = 0;
  for (let i = 0; i < kids.length && guard++ < 200;) {
    const name = (kids[i].querySelector('.th code') || {}).textContent || '';
    let j = i;
    while (j + 1 < kids.length && ((kids[j + 1].querySelector('.th code') || {}).textContent || '') === name) j++;
    const run = kids.slice(i, j + 1);
    const busy = run.some((k) => k.querySelector('.th .st .spin'));
    if (!name || run.length < 3 || busy) { i = j + 1; continue; }
    const key = run[0].dataset.id || name;
    const open = !!(S.openGrp && S.openGrp.has(key));
    const errs = run.filter((k) => k.classList.contains('err')).length;
    const cmd = (run[0].querySelector('.th .arg') || {}).textContent || '';
    const d = document.createElement('details');
    d.className = 'toolfold';
    d.dataset.grp = key;
    d.innerHTML = `<summary><span class="ti">${TOOL_ICON[name] || '🔧'}</span><b>${esc(name)}</b>`
      + `<span class="n">×${run.length}</span><span class="arg">${esc(cmd)}</span>`
      + `<span class="st">${errs ? `<span class="st-bad">${errs} 個失敗</span>` : `<span class="st-ok">${ico('check')}${run.length}</span>`}</span>`
      + `${ico('right', 'chev')}</summary><div class="tbody"></div>`;
    d.open = open;
    root.insertBefore(d, run[0]);
    const body = d.querySelector('.tbody');
    for (const k of run) body.appendChild(k);
    d.addEventListener('toggle', () => {
      S.openGrp = S.openGrp || new Set();
      if (d.open) S.openGrp.add(key); else S.openGrp.delete(key);
    });
    kids = [...root.children].filter((k) => k.classList && k.classList.contains('tool'));
    i = 0;
  }
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
async function msgAction(act, el) {
  if (act === 'copy') {
    const text = el.classList.contains('user') ? ($('.bubble', el) || {}).textContent || '' : el._text || '';
    await navigator.clipboard.writeText(text); notify('ok', '已複製'); return;
  }
  if (S.running.has(S.sid) && act !== 'fork') { notify('warn', '還在執行中', '先按停止，再重送或重新生成。'); return; }
  if (act === 'retry') { wsSend({ type: 'rerun', session_id: S.sid, from_id: +el.dataset.uid }); return; }
  if (act === 'fork') {
    await forkSession(S.sid, +el.dataset.last);
    return;
  }
  if (act === 'rewind') {
    if (S.running.has(S.sid)) { notify('warn', '還在執行中', '先按停止，再回溯。'); return; }
    const ok = await confirmBox('回溯到這則訊息？', '這則之後的訊息會從對話刪掉（模型之後只看得到到這則為止）。只回溯對話，不會還原任何檔案。刪掉的訊息無法復原。', { ok: '回溯', danger: true });
    if (!ok) return;
    try {
      const r = await api('POST', `/api/sessions/${S.sid}/rewind`, { message_id: +el.dataset.mid });
      notify('ok', '已回溯', `刪掉之後的 ${r.removed} 則訊息`);
      await openSession(S.sid);
    } catch (e) { notify('error', '回溯失敗', e.message); }
    return;
  }
  if (act === 'edit') {
    const bubble = $('.bubble', el), old = bubble ? bubble.textContent : '';
    const box = document.createElement('div');
    box.className = 'editbox';
    box.innerHTML = '<textarea></textarea><div><span class="btn" data-e="cancel">取消</span><span class="btn primary" data-e="send">重送</span></div>';
    const ta = $('textarea', box); ta.value = old;
    if (bubble) bubble.replaceWith(box); else el.appendChild(box);
    const acts = $('.acts', el); if (acts) acts.style.display = 'none';
    ta.focus(); ta.setSelectionRange(old.length, old.length);
    const go = () => {
      const text = ta.value;
      if (!text.trim()) return;
      wsSend({ type: 'rerun', session_id: S.sid, from_id: +el.dataset.mid, text });
    };
    box.onclick = (e) => { const b = e.target.closest('[data-e]'); if (!b) return; b.dataset.e === 'send' ? go() : renderChat(); };
    ta.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); }
      if (e.key === 'Escape') renderChat();
    };
  }
}
// 使用者往上看東西時，重畫後把「畫面最上面那一塊」放回原本的位置：
// 只記 scrollTop 不夠，上面的內容高度一變（展開狀態、補上的內容），畫面就會跳到別處
function chatAnchor(chat) {
  const top = chat.getBoundingClientRect().top;
  for (const el of chat.querySelectorAll('[data-mid], .tool[data-id], [data-tk]')) {
    const r = el.getBoundingClientRect();
    if (r.bottom > top + 4) {
      const sel = el.dataset.mid ? `[data-mid="${CSS.escape(el.dataset.mid)}"]`
        : el.dataset.tk ? `[data-tk="${CSS.escape(el.dataset.tk)}"]` : `.tool[data-id="${CSS.escape(el.dataset.id)}"]`;
      return { sel, off: r.top - top };
    }
  }
  return null;
}
// 內層自己會捲的框（內部指令全文、思考、工具輸出）：重畫前記下捲到哪，重畫後放回去，不然一回話就跳回框頂
function innerScrolls(chat) {
  const out = [];
  for (const el of chat.querySelectorAll('pre, .tx, .ibody, .out')) {
    if (el.scrollTop < 1 && el.scrollLeft < 1) continue;
    const own = el.closest('[data-tk], .tool[data-id], [data-mid]');
    if (!own) continue;
    const sel = own.dataset.tk ? `[data-tk="${CSS.escape(own.dataset.tk)}"]`
      : own.matches('.tool[data-id]') ? `.tool[data-id="${CSS.escape(own.dataset.id)}"]` : `[data-mid="${CSS.escape(own.dataset.mid)}"]`;
    const idx = [...own.querySelectorAll('pre, .tx, .ibody, .out')].indexOf(el);
    out.push({ sel, idx, top: el.scrollTop, left: el.scrollLeft });
  }
  return out;
}
function restoreInnerScrolls(chat, list) {
  for (const x of list) {
    const own = chat.querySelector(x.sel); if (!own) continue;
    const el = own.querySelectorAll('pre, .tx, .ibody, .out')[x.idx]; if (!el) continue;
    el.scrollTop = x.top; el.scrollLeft = x.left;
  }
}
function chatRestore(chat, anchor, keep) {
  const put = () => {
    const el = anchor && chat.querySelector(anchor.sel);
    if (!el) { if (chat.scrollTop !== keep) chat.scrollTop = keep; S.lastTop = chat.scrollTop; return; }
    const d = (el.getBoundingClientRect().top - chat.getBoundingClientRect().top) - anchor.off;
    if (Math.abs(d) > 1) { chat.scrollTop += d; S.progTop = chat.scrollTop; }
    S.lastTop = chat.scrollTop;
  };
  put();
  // 展開中的思考框是點開時才補內容（toggle 事件），補完高度會變，再對一次
  requestAnimationFrame(() => { if (S.follow === false) put(); });
  setTimeout(() => { if (S.follow === false) put(); }, 60);
}
// 自動跟到最底：只有使用者人在底部時才跟；往上滑（滾輪、拖捲軸、鍵盤）就停，自己滑回底部或按「到最底」再恢復
function stickBottom() {
  const c = $('#chat'), want = c.scrollHeight - c.clientHeight;
  if (Math.abs(c.scrollTop - want) > 1) { c.scrollTop = want; S.progTop = c.scrollTop; }
  S.lastTop = c.scrollTop;
}
function updateToBottom() {
  const c = $('#chat');
  $('#toBottom').classList.toggle('on', S.follow === false || c.scrollHeight - c.scrollTop - c.clientHeight > 400);
}
$('#chat').addEventListener('scroll', () => {
  const c = $('#chat'), gap = c.scrollHeight - c.scrollTop - c.clientHeight;
  if (S.progTop != null && Math.abs(c.scrollTop - S.progTop) <= 2) S.progTop = null;   // 程式自己捲的：不改跟隨狀態
  else {
    if (c.scrollTop < (S.lastTop ?? c.scrollTop) - 2) S.follow = false;            // 使用者往上
    if (gap < 8) S.follow = true;                                                // 使用者自己捲回底部
  }
  S.lastTop = c.scrollTop;
  updateToBottom();
}, { passive: true });
// 在思考框、工具輸出這種內層捲動區裡往上捲，是在看那一框，不算離開底部
$('#chat').addEventListener('wheel', (e) => {
  if (e.deltaY >= 0) return;
  const inner = e.target.closest && e.target.closest('.tx, pre');
  if (inner && inner.scrollTop > 0 && inner.scrollHeight > inner.clientHeight) return;
  S.follow = false;
}, { passive: true });
$('#chat').addEventListener('keydown', (e) => { if (['PageUp', 'ArrowUp', 'Home'].includes(e.key)) S.follow = false; });
// 點開內部指令、思考、工具輸出、壓縮摘要＝正在看那一塊：不再自動跟到底，免得模型一回話就被捲走
$('#chat').addEventListener('click', (e) => {
  if (e.target.closest('summary, .tool .th, .toolfold > summary')) S.follow = false;
}, true);
$('#toBottom').onclick = () => { S.follow = true; const c = $('#chat'); c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' }); };
// 直播區只更新有變的部分：整塊清掉重建時，中途量高度會讓瀏覽器把聊天區的捲動位置往上夾，看起來像卡住滑不下去
// 思考框：使用者自己往上捲就不再強制捲到底，捲回底部才恢復；使用者自己展開／收合過就照他的
function fillLive(lv) {
  const l = S.live[S.sid] || { thinking: '', text: '' };
  const LT = S.liveThink || (S.liveThink = { sid: S.sid, open: null, pinned: true, top: 0 });
  if (LT.sid !== S.sid) Object.assign(LT, { sid: S.sid, open: null, pinned: true, top: 0 });
  const hasRunningTool = Object.values(S.tools).some((t) => t.sid === S.sid && t.running);
  const wait = $(':scope > .waiting', lv);
  if (!l.thinking && !l.text && !hasRunningTool) { if (!wait) { lv.innerHTML = '<div class="waiting"><i></i><i></i><i></i></div>'; } return; }
  if (wait) wait.remove();
  let d = $(':scope > details.think', lv);
  if (l.thinking) {
    if (!d) {
      d = document.createElement('details'); d.className = 'think';
      d.innerHTML = '<summary>思考中… <small></small></summary><div class="tx"></div>';
      d.open = LT.open ?? true;   // 預設展開：下面開始輸出文字時不要把思考收起來，會看不到
      $('summary', d).addEventListener('click', () => { LT.open = !d.open; });
      const tx0 = $('.tx', d);
      // 程式捲的位置記在 _progTop；捲動事件的位置不是那裡＝使用者自己捲的（事件會合併，不能用旗標判斷）
      tx0.addEventListener('scroll', () => {
        if (tx0._progTop != null && Math.abs(tx0.scrollTop - tx0._progTop) <= 2) return;
        tx0._progTop = null;
        LT.pinned = tx0.scrollHeight - tx0.scrollTop - tx0.clientHeight < 8; LT.top = tx0.scrollTop;
      }, { passive: true });
      tx0.addEventListener('wheel', (e) => { if (e.deltaY < 0) { LT.pinned = false; tx0._progTop = null; } }, { passive: true });
      lv.prepend(d);
    }
    $('summary small', d).textContent = `${l.thinking.length} 字`;
    const tx = $('.tx', d);
    // 思考文字一次可能是上萬字，每格都整段重設會把主執行緒吃光（打字、按鈕都會卡）
    if (tx._len !== l.thinking.length) {
      const now = performance.now();
      if (!tx._len || now - (tx._at || 0) > 100 || l.thinking.length - tx._len > 600) {
        tx.textContent = l.thinking; tx._len = l.thinking.length; tx._at = now;
      }
    }
    if (d.open) {
      const want = LT.pinned ? tx.scrollHeight : LT.top;
      if (Math.abs(tx.scrollTop - want) > 1) { tx.scrollTop = want; tx._progTop = tx.scrollTop; }
    }
  } else if (d) d.remove();
  const old = $(':scope > .live-md', lv);
  if (l.text) {
    const now = performance.now();
    // 串流時每一格都整段重新 parse markdown，字一多就會把主執行緒吃滿（打字、按送出都會卡）。
    // 有變才重畫，而且最多每 120ms 一次；定稿那次由 renderChat 重畫，不會漏字。
    if (!old || (old._len !== l.text.length
      && (now - (old._at || 0) > 120 || l.text.length - old._len > 500))) {
      const box = md(l.text, true); box.classList.add('live-md');
      box._len = l.text.length; box._at = now;
      box.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
      if (old) { box._len = l.text.length; box._at = now; old.replaceWith(box); } else lv.appendChild(box);
    }
  } else if (old) old.remove();
}
let liveRaf = 0;
// 執行中的工具每秒更新經過時間（只改那個小字，不重畫）
setInterval(() => { for (const e of document.querySelectorAll('.tool .st-run .el[data-t0]')) { const s = Math.floor((Date.now() - Number(e.dataset.t0)) / 1000); if (s >= 2) e.textContent = ` ${s} 秒`; } }, 1000);
function scheduleLive() {
  if (liveRaf) return;
  liveRaf = requestAnimationFrame(() => {
    liveRaf = 0;
    const lv = $('#live'); if (!lv) return renderChat();
    const c = $('#chat'), top = c.scrollTop;
    fillLive(lv);
    if (S.follow !== false) stickBottom(); else if (c.scrollTop !== top) c.scrollTop = top;
  });
}

// ------------------------------------------------------------------ 輸入區
const input = $('#input');
// 每敲一個字就量 scrollHeight 會逼整頁重排（對話越長越慢）；合併成每格最多量一次
let autoRaf = 0;
function autosize() {
  if (autoRaf) return;
  autoRaf = requestAnimationFrame(() => {
    autoRaf = 0;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    renderSendState();
  });
}
input.addEventListener('input', autosize);
// 每敲一個字都重設 innerHTML 會讓整顆按鈕重建（含 SVG），打字就會頓。狀態沒變就不碰 DOM。
let sendSig = '';
function renderSendState() {
  const run = isBusy(S.sid);
  const b = $('#sendBtn');
  const has = !!(input.value.trim() || S.attach.length || (S.files || []).length);
  const act = run && has ? busyAction() : null;
  const cls = 'send' + (run && !has ? ' stop' : has ? ' go' : '');
  const icon = run && !has ? 'stop' : 'up';
  const title = run && !has ? '停止（Esc）' : act ? BUSY_SEND[act] + '（Enter）' : '送出（Enter）';
  const sig = cls + '|' + icon + '|' + title;
  if (sig !== sendSig) {
    sendSig = sig;
    b.className = cls;
    if (b.dataset.icon !== icon) { b.dataset.icon = icon; b.innerHTML = ico(icon); }
    b.title = title;
  }
  const steerOn = !!(act && act !== 'steer' && !S.attach.length);
  const queueOn = !!(act && act !== 'queue');
  if ($('#steerBtn').hidden !== !steerOn) $('#steerBtn').hidden = !steerOn;
  if ($('#queueBtn').hidden !== !queueOn) $('#queueBtn').hidden = !queueOn;
}
// 這個對話還有事在跑：回合中，或方案／目標流程（拓展、各步驟、審核；流程裡的步驟不發 run_start，不在 S.running）
const flowBusy = (sid) => !S.running.has(sid) && !!((S.sessions.find((x) => x.id === sid) || {}).flow);
const isBusy = (sid) => !!sid && (S.running.has(sid) || flowBusy(sid));
function renderComposer() {
  renderQueue(); applyLang(); renderScopePill();
  const hint = $('#runHint');
  hint.classList.toggle('on', isBusy(S.sid));
  const gap = flowBusy(S.sid);
  if (hint.dataset.gap !== String(gap)) {
    hint.dataset.gap = String(gap);
    hint.innerHTML = `<span class="spin"></span>${gap ? (uiLang() === 'en' ? 'Plan in progress · expand, steps and review take turns (quiet gaps are normal) · Esc to stop'
      : '方案進行中 · 拓展、執行、審核輪流跑（步驟之間沒有輸出也不是卡住）· Esc 停止') : busyText(uiLang())}`;
  }
  renderModePill();
  renderPerm();
  renderSendState();
}
function renderAttach() {
  const pics = S.attach.map((a, i) => `<div class="a"><img src="data:${a.media_type};base64,${a.data}"><span data-i="${i}">${ico('x')}</span></div>`).join('');
  const docs = (S.files || []).map((f, i) => {
    const label = f.path ? `貼上內容 (${fmtBytes(f.bytes)})` : `附件：${f.name} (${fmtBytes(f.bytes)})`;
    const sub = f.path ? f.path : '送出後存成檔案，模型需要時自己讀';
    return `<div class="a f"><b>${esc(label)}</b><small>${esc(sub)}</small><span data-f="${i}">${ico('x')}</span></div>`;
  }).join('');
  $('#attach').innerHTML = pics + docs;
  renderSendState();
}
$('#attach').onclick = (e) => {
  const x = e.target.closest('[data-i]'); if (x) { S.attach.splice(+x.dataset.i, 1); renderAttach(); return; }
  const f = e.target.closest('[data-f]'); if (f) { S.files.splice(+f.dataset.f, 1); renderAttach(); }
};
const FILE_MAX = 25 * 1024 * 1024;   // 單檔上限；base64 塞在 WebSocket 裡，太大會拖垮畫面
function addFiles(files) {
  for (const f of files) {
    if (f.size > FILE_MAX) { toast(`${f.name} 太大（${fmtBytes(f.size)}），單檔上限 ${fmtBytes(FILE_MAX)}`); continue; }
    if (f.type.startsWith('image/')) {
      const r = new FileReader();
      r.onload = () => { S.attach.push({ media_type: f.type, data: String(r.result).split(',')[1] }); renderAttach(); };
      r.readAsDataURL(f);
      continue;
    }
    // 其他檔案（xlsx、pdf、程式碼…）：存進 data/files，訊息裡留位置，模型要讀再讀
    const r = new FileReader();
    r.onload = () => {
      S.files.push({ name: f.name, bytes: f.size, media_type: f.type || 'application/octet-stream',
                     data: String(r.result).split(',')[1] });
      renderAttach();
    };
    r.readAsDataURL(f);
  }
}
$('#fileIn').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };
$('#plusBtn').onclick = () => openPop($('#plusMenu'), $('#plusBtn'));
$('#plusMenu').onclick = (e) => {
  const it = e.target.closest('[data-act]'); if (!it) return;
  closePops();
  if (it.dataset.act === 'image') $('#fileIn').click();
  if (it.dataset.act === 'compact') compactNow();
};
const PASTE_TO_FILE = 4000;   // 超過這個字數就存成檔案，不塞進對話
input.addEventListener('paste', async (e) => {
  const files = [...(e.clipboardData || {}).items || []].filter((i) => i.kind === 'file').map((i) => i.getAsFile()).filter(Boolean);
  if (files.length) { e.preventDefault(); addFiles(files); return; }
  const txt = (e.clipboardData || {}).getData ? e.clipboardData.getData('text/plain') : '';
  if (txt.length < PASTE_TO_FILE) return;
  e.preventDefault();
  try {
    const r = await api('POST', '/api/paste', { text: txt });
    S.files.push({ path: r.path, name: r.name, bytes: r.bytes });
    renderAttach();
    notify('ok', `貼上內容存成檔案（${fmtBytes(r.bytes)}）`, '送出後模型需要時會用 read_file 讀');
  } catch (err) { input.value += txt; toast('存檔失敗，改成直接貼進輸入框：' + err.message); }
});
const cbox = $('#cbox');
['dragenter', 'dragover'].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); cbox.classList.add('drag'); }));
['dragleave', 'drop'].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); if (t === 'drop' || !e.relatedTarget) cbox.classList.remove('drag'); }));
document.addEventListener('drop', (e) => { if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });
input.addEventListener('focus', () => cbox.classList.add('focus'));
input.addEventListener('blur', () => cbox.classList.remove('focus'));

// 貼上的長文已經存成檔案：訊息裡只帶位置，模型需要完整內容時自己 read_file
function withFileRefs(text) {
  const refs = (S.files || []).filter((f) => f.path)
    .map((f) => `（貼上內容 ${fmtBytes(f.bytes)}，已存成檔案：${f.path}；需要完整內容時用 read_file 讀）`).join(NL);
  return [text, refs].filter((x) => x && x.trim()).join(NL + NL);
}
async function send() {
  const text = input.value;
  if (!text.trim() && !S.attach.length && !(S.files || []).length) return;
  const m = selectedModel();
  if (!m) {
    const s0 = cur();
    if (s0 && s0.model && S.models.length) { notify('warn', '這個對話的模型已移除', `原本的「${s0.model}」不在模型清單裡，請在右下角重選一個再送。`); setTimeout(() => $('#modelPill').click(), 0); return; }
    notify('warn', '還沒有模型', '先到「設定 → 模型商」新增一個。'); return;
  }
  if (!m.available) { notify('error', `「${m.label}」不能用`, '它需要的插件沒有載入，到插件頁看看是不是停用或載入失敗。'); return; }
  if (S.attach.length && !m.vision) notify('warn', `${m.label} 不能直接看圖`, '圖片會先交給「輔助模型 → 看圖」轉成文字再送。判斷錯了可以到「設定 → 模型商」把這個模型的看圖改成「能看」。');
  if (!S.sid) {
    const s = await api('POST', '/api/sessions', { model: m.id, effort: effortWant(), perm: currentPerm() });
    if (!S.sessions.some((x) => x.id === s.id)) S.sessions.unshift(s);
    S.draft = {};
    const gateDefault = localStorage.getItem('woof.gate') || 'off';
    if (gateDefault !== 'off') { await api('POST', `/api/sessions/${s.id}/gate`, { mode: gateDefault }); S.gate[s.id] = gateDefault; }
    S.sessions = [s, ...S.sessions.filter((x) => x.id !== s.id)]; S.sid = s.id; S.msgs = []; localStorage.setItem('woof.sid', s.id);
    renderSessions(); renderTitle();
  }
  S.follow = true;
  const body = withFileRefs(text);
  const upFiles = (S.files || []).filter((f) => f.data).map((f) => ({ name: f.name, data: f.data, media_type: f.media_type }));
  if (!wsSend({ type: 'send', session_id: S.sid, text: body, images: S.attach, files: upFiles })) return;
  input.value = ''; S.attach = []; S.files = []; renderAttach(); autosize();
}
function stop() { if (isBusy(S.sid)) wsSend({ type: 'stop', session_id: S.sid }); }
// 子代理：只停那一條，不影響主對話
function stopSub(sid) {
  wsSend({ type: 'stop', session_id: sid });
  notify('info', '已叫停那條子代理', '它這一步跑完就停；主對話不受影響');
}
async function closeSub(sid) {
  const s = S.sessions.find((x) => x.id === sid);
  const running = s && (S.running.has(sid) || s.busy);
  if (!await confirmBox('關閉子代理',
    `「${(s && s.title) || '子代理'}」會從側欄消失（對話紀錄一起刪${running ? '，還在跑會先叫停' : ''}）。`,
    { ok: '關閉', danger: true })) return;
  if (running) wsSend({ type: 'stop', session_id: sid });
  const i = S.tabs.findIndex((t) => t.sid === sid);
  if (i >= 0) closeTab(i);
  await deleteSession(sid);
}
$('#sendBtn').onclick = () => ((isBusy(S.sid) && !input.value.trim() && !S.attach.length && !(S.files || []).length) ? stop() : busySubmit(false));
input.addEventListener('keydown', (e) => {
  if ($('#slashMenu').classList.contains('on') && !e.isComposing) {
    const items = $$('#slashMenu .mi');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      S.slashSel = (S.slashSel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach((x, i) => x.classList.toggle('sel', i === S.slashSel));
      return;
    }
    if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') { e.preventDefault(); runSlash(items[S.slashSel] && items[S.slashSel].dataset.cmd); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); $('#slashMenu').classList.remove('on'); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    busySubmit(e.ctrlKey || e.metaKey);
  }
});

// ---- 斜線指令
const SLASH = [
  ['/help', '指令與快捷鍵說明', () => showHelp()],
  ['/goal', '設定目標：拆步驟照做，做完審核驗收；對話選了方案模式就照那個模式走（stop 停止）', () => goalPrompt()],
  ['/plan', '預案模式開關：先出預案、不動手', () => togglePlan()],
  ['/plan-off', '先提方案模式：關閉', () => setGate('off')],
  ['/plan-human', '先提方案：模型提方案，你核准後才執行', () => setGate('human')],
  ['/plan-model', '先提方案：模型提方案，子代理審核通過才執行', () => setGate('model')],
  ['/scope', '這個對話能用哪些插件（模板）', () => openScopeModal()],
  ['/prompt', '上下文：用量、統計、這個對話的總提示詞、壓縮', () => openCtxPanel('prompt')],
  ['/review', '子代理審核最後一則回覆', () => reviewNow('last')],
  ['/review-changes', '審核本對話改過的檔案目前內容', () => reviewNow('changes')],
  ['/new', '開新對話', () => newSession()],
  ['/compact', '手動壓縮較早的訊息', () => compactNow()],
  ['/agent', '派子代理處理一件事', () => { input.value = '請用子代理（delegate_task）處理：'; autosize(); input.focus(); }],
  ['/tools', '開關工具（在插件頁，每個插件底下列它的工具）', () => showView('plug')],
  ['/model', '切換模型', () => $('#modelPill').click()],
  ['/aux', '輔助模型：備用、看圖、壓縮、子代理', () => openSettings('aux')],
  ['/perm', '切換工具執行權限', () => $('#permPill').click()],
  ['/retry', '重新生成最後一則回覆', () => retryLast()],
  ['/continue', '接著做：上一回合中途停下（出錯、停止、步數上限、程式關閉）時從中斷處繼續', () => continueRun()],
  ['/stop', '停止目前的回覆', () => stop()],
  ['/rename', '重新命名這個對話', () => renameCurrent()],
  ['/export', '匯出這個對話成 Markdown', () => (S.sid ? exportSession(S.sid) : notify('warn', '還沒有對話可以匯出'))],
  ['/cost', '看這個對話的用量與耗時', () => costNow()],
  ['/usage', '看額度與用量', () => { setRight(true); openTab('paneKeys'); }],
  ['/plugins', '開啟插件頁', () => { showView('plug'); refreshState(); }],
  ['/settings', '開啟設定', () => openSettings()],
];
function retryLast() {
  if (S.running.has(S.sid)) { notify('warn', '還在執行中', '先按停止。'); return; }
  const g = $$('#chat .msg.bot[data-uid]').filter((x) => x.dataset.uid).pop();
  if (!g) { notify('warn', '沒有可以重新生成的回覆'); return; }
  wsSend({ type: 'rerun', session_id: S.sid, from_id: +g.dataset.uid });
}
async function renameCurrent() {
  const s = cur(); if (!s) { notify('warn', '還沒有對話'); return; }
  const t = await promptBox('重新命名', '對話名稱', s.title || '');
  if (!t) return;
  try { Object.assign(s, await api('PATCH', `/api/sessions/${s.id}`, { title: t })); renderSessions(); renderTitle(); }
  catch (e) { notify('error', '改名失敗', e.message); }
}
function showHelp() {
  const keys = [['Enter', '送出'], ['Shift Enter', '換行'], ['Ctrl N', '新對話'], ['Ctrl K', '搜尋對話'], ['Esc', '關閉選單；執行中按兩下停止'], ['/', '叫出指令選單，↑↓ 選、Enter 執行']];
  modal({
    title: '指令與快捷鍵', wide: true, cancel: '', ok: '知道了',
    body: `<div class="helpgrid"><h5>斜線指令</h5>${SLASH.map(([c, d]) => `<code>${c}</code><span>${esc(slashDesc(c, d))}</span>`).join('')}
      <h5>快捷鍵</h5>${keys.map(([k, d]) => `<kbd>${k}</kbd><span>${d}</span>`).join('')}
      <h5>其他</h5><code>右鍵</code><span>對話清單上按右鍵：改名、釘選、匯出、刪除</span>
      <code>拖拉</code><span>側欄與右欄的邊線可以左右拖拉，雙擊還原</span>
      <code>子代理</code><span>模型會自己判斷要不要派子代理；子對話會縮排在主對話底下</span></div>`,
  });
}
// ---- 工具開關
S.slashSel = 0;
function renderSlash() {
  const v = input.value, menu = $('#slashMenu');
  if (!/^\/[a-z-]*$/i.test(v)) { menu.classList.remove('on'); return; }
  const list = SLASH.filter(([c]) => c.startsWith(v.toLowerCase()));
  if (!list.length) { menu.classList.remove('on'); return; }
  S.slashSel = Math.min(S.slashSel, list.length - 1);
  menu.innerHTML = '<div class="phd">' + (uiLang() === 'en' ? 'Commands' : '指令') + '</div>' + list.map(([c, d], i) => `<div class="mi${i === S.slashSel ? ' sel' : ''}" data-cmd="${c}"><code>${c}</code>${esc(slashDesc(c, d))}</div>`).join('');
  menu.style.left = '0px';
  menu.classList.add('on');
  popAbove(menu, $('#cbox'));
}
function runSlash(cmd) {
  const c = SLASH.find((x) => x[0] === cmd); if (!c) return;
  $('#slashMenu').classList.remove('on');
  input.value = ''; autosize();
  c[2]();
}
// ---- 語言（目前涵蓋指令說明與輸入區提示）
const SLASH_EN = {
  '/help': 'Commands and shortcuts', '/goal': 'Set a goal: split into steps, then a reviewer checks the result; follows the chat\'s plan mode if one is set (stop to end)',
  '/plan': 'Plan mode: propose a plan, do not act', '/plan-off': 'Plan-first mode: off',
  '/plan-human': 'Plan first: model proposes, you approve before it runs', '/plan-model': 'Plan first: model proposes, a subagent reviews before it runs',
  '/scope': 'Plugins for this conversation (templates)', '/prompt': 'View and edit this conversation\'s system prompt', '/review': 'Review the last reply with a subagent', '/new': 'New conversation',
  '/compact': 'Compact older messages', '/agent': 'Delegate a task to a subagent', '/tools': 'Enable or disable tools',
  '/model': 'Switch model', '/aux': 'Auxiliary models', '/perm': 'Tool permission', '/retry': 'Regenerate the last reply', '/continue': 'Continue an interrupted turn from where it stopped',
  '/stop': 'Stop the current reply', '/rename': 'Rename this conversation', '/export': 'Export this conversation to Markdown',
  '/usage': 'Usage and quota', '/plugins': 'Plugins page', '/settings': 'Settings',
};
const I18N = {
  zh: { ph: '輸入訊息，Enter 送出，Shift+Enter 換行', busy: '執行中 · Enter 重新導向 · Ctrl+Enter 排隊 · Esc 停止' },
  en: { ph: 'Message… Enter to send, Shift+Enter for newline', busy: 'Running · Enter redirects · Ctrl+Enter queues · Esc stops' },
};
// 執行中送出：設定頁選 Enter 的預設動作；另外兩個動作在輸入框右下角以圖示鍵出現
//   interrupt＝重新導向目前的執行（停掉再送）、steer＝插話（下一次工具呼叫後送進去）、queue＝排隊（這一回合結束後送）
// Ctrl+Enter 一律排隊；有附圖時插話送不進去，改排隊；輸入框是空的就是停止。
const BUSY_LABEL = { interrupt: '重新導向', steer: '插話', queue: '排隊' };
const BUSY_SEND = { interrupt: '重新導向目前的執行', steer: '插話：送進目前回合', queue: '排隊訊息' };
const BUSY_HINT = { interrupt: '停掉目前的回合，改送這一則', steer: '下一次工具呼叫後送進去，不中斷', queue: '這一回合結束後自動送出' };
// 預設排隊：執行中送訊息不要再「停掉再送」，也不要直接插進目前回合
function busyMode() { const v = localStorage.getItem('woof.busy'); return ['queue', 'steer', 'interrupt'].includes(v) ? v : 'steer'; }
function busyAction() { const m = busyMode(); return m === 'steer' && S.attach.length ? 'queue' : m; }
function redirectMsg() { S.interrupting[S.sid] = true; wsSend({ type: 'stop', session_id: S.sid }); queueMsg(); }
function pendingAsk() { return Object.values(S.asks).find((a) => a.session_id === S.sid) || null; }
function busySubmit(ctrl) {
  // 有待答的提問也算忙碌：方案／目標流程在等回答時不會進 S.running
  const ask = pendingAsk();
  const busy = !!(S.sid && (S.running.has(S.sid) || ask));
  if (!busy) return send();
  // 有 ask_user 在等：這一則就是那一題的回答。不用先點選項、也不會卡在排隊等一個永遠不會結束的回合
  const typed = input.value.trim();
  if (ask && typed) {
    input.value = ''; autosize();
    answerAsk(ask.id, typed);
    return;
  }
  if (ctrl) return queueMsg();
  const a = busyAction();
  if (a === 'steer') return send();
  if (a === 'interrupt') return redirectMsg();
  return queueMsg();
}
$('#steerBtn').onclick = () => { if (!S.attach.length) send(); };
$('#queueBtn').onclick = () => queueMsg();
function busyText(l) {
  const m = busyMode();
  if (l === 'en') return ({ queue: 'Running · Enter queues · Esc stops', steer: 'Running · Enter steers · Ctrl+Enter queues · Esc stops', interrupt: 'Running · Enter redirects · Ctrl+Enter queues · Esc stops' })[m];
  return ({ queue: '執行中 · Enter 排隊 · Esc 停止', steer: '執行中 · Enter 插話 · Ctrl+Enter 排隊 · Esc 停止', interrupt: '執行中 · Enter 重新導向 · Ctrl+Enter 排隊 · Esc 停止' })[m];
}
function uiLang() { return localStorage.getItem('woof.lang') === 'en' ? 'en' : 'zh'; }
function slashDesc(c, d) { return uiLang() === 'en' ? (SLASH_EN[c] || d) : d; }
function applyLang(force) {
  const l = uiLang(), hint = $('#runHint');
  if (!force && input.dataset.lang === l) return;
  input.dataset.lang = l; input.placeholder = I18N[l].ph;
  if (hint) { hint.innerHTML = `<span class="spin"></span>${busyText(l)}`; delete hint.dataset.gap; }
}
document.addEventListener('click', (e) => {
  const bu = e.target.closest('[data-busy]');
  if (bu) { localStorage.setItem('woof.busy', bu.dataset.busy); $$('[data-busy]').forEach((x) => x.classList.toggle('on', x === bu)); applyLang(true); renderSendState(); return; }
  const b = e.target.closest('[data-lang]'); if (!b) return;
  localStorage.setItem('woof.lang', b.dataset.lang);
  $$('[data-lang]').forEach((x) => x.classList.toggle('on', x.dataset.lang === b.dataset.lang));
  applyLang(true); renderSlash(); i18nApply();
});

// ── 介面語言：選 English 時，把畫面上的介面中文換成 i18n_en.js 的對照；切回中文就還原。
// 對話內容、思考、使用者訊息、對話標題、程式碼、輸入框裡的字不翻。
// 工具說明、輸出風格內文也是介面上看得到的字，一樣要翻（.noi18n 是模型名稱這類不該翻的）
const I18N_SKIP = '.md, .bubble, .tx, pre, code, textarea, [contenteditable], #sessionList .t, .th .arg, .noi18n';
const I18N_ATTRS = ['title', 'placeholder', 'aria-label'];
const I18N_HAN = /[\u4e00-\u9fff]/;
const I18N_EN = window.WOOF_EN || {};
const I18N_PAT = Object.keys(I18N_EN).filter((k) => /\{\d+\}/.test(k))
  .sort((a, b) => b.replace(/\{\d+\}/g, '').length - a.replace(/\{\d+\}/g, '').length)
  .map((k) => {
    const ids = []; const src = k.split(/(\{\d+\})/).map((p) => { const m = /^\{(\d+)\}$/.exec(p); if (m) { ids.push(+m[1]); return '(.*?)'; } return p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }).join('');
    // 固定字太短的樣板（例如「{0} 次」），變動部分不能吃進中文，免得整句被誤套
    return { re: new RegExp('^' + src + '$', 's'), ids, en: I18N_EN[k], strict: k.replace(/\{\d+\}|\s/g, '').length < 5 };
  });
function i18nOne(t, depth) {
  let en = I18N_EN[t];
  if (en != null) return en;
  for (const p of I18N_PAT) {
    const m = p.re.exec(t); if (!m) continue;
    if (p.strict && m.slice(1).some((g) => I18N_HAN.test(g))) continue;
    const val = {}; let ok = true;
    p.ids.forEach((id, i) => {
      let g = m[i + 1];
      if (I18N_HAN.test(g)) {   // 變動部分本身也是介面字（例如「圖片」），一起翻；翻不了就換下一個樣板
        const sub = depth < 2 ? i18nOne(g.trim(), depth + 1) : null;
        if (sub == null) ok = false; else g = /^[A-Z][a-z]/.test(sub) && i + p.ids.length > 0 && !p.en.startsWith('{' + id + '}') ? sub[0].toLowerCase() + sub.slice(1) : sub;
      }
      val[id] = g;
    });
    if (!ok) continue;
    return p.en.replace(/\{(\d+)\}/g, (_, d) => val[d] ?? '');
  }
  // 「A · B · C」這種組起來的標籤：逐段翻，每段都翻得了（或本來就沒中文）才算
  for (const sep of [' · ', '・', ' / ', '/']) {
    if (!t.includes(sep)) continue;
    const parts = t.split(sep); let hit = false;
    const outp = parts.map((x) => { const y = x.trim(); if (!I18N_HAN.test(y)) return x; const r = depth < 2 ? i18nOne(y, depth + 1) : null; if (r == null) return null; hit = true; return x.replace(y, r); });
    if (hit && outp.every((x) => x != null)) return outp.join(sep);
  }
  return null;
}
function i18nText(s) {
  const t = s.replace(/\s+/g, ' ').trim();
  if (!t || !I18N_HAN.test(t)) return null;
  const en = i18nOne(t, 0);
  if (en == null) return null;
  const lead = /^\s*/.exec(s)[0], tail = /\s*$/.exec(s)[0];
  return lead + en + tail;
}
function i18nSkip(el) { return !el || (el.closest && el.closest(I18N_SKIP)); }
function i18nNode(n, en) {
  if (n.nodeType === 3) {
    const p = n.parentElement;
    if (!p || i18nSkip(p) || p.tagName === 'SCRIPT' || p.tagName === 'STYLE') return;
    if (en) {
      if (n.__en === n.data) return;
      const out = i18nText(n.data); if (out == null) return;
      if (p.tagName === 'OPTION' && !p.hasAttribute('value')) p.setAttribute('value', p.textContent);   // 選項的值保持原文，只換顯示的字
      n.__zh = n.data; n.__en = out; n.data = out;
    } else if (n.__zh != null && n.data === n.__en) { n.data = n.__zh; n.__zh = n.__en = null; }
    return;
  }
  if (n.nodeType !== 1 || i18nSkip(n)) return;
  for (const a of I18N_ATTRS) {
    if (!n.hasAttribute(a)) continue;
    const v = n.getAttribute(a), keep = 'data-zh-' + a;
    if (en) {
      if (n.getAttribute('data-en-' + a) === v) continue;
      const out = i18nText(v); if (out == null) continue;
      n.setAttribute(keep, v); n.setAttribute('data-en-' + a, out); n.setAttribute(a, out);
    } else if (n.hasAttribute(keep)) {
      if (n.getAttribute('data-en-' + a) === v) n.setAttribute(a, n.getAttribute(keep));
      n.removeAttribute(keep); n.removeAttribute('data-en-' + a);
    }
  }
}
function i18nTree(root, en) {
  if (!root) return;
  if (root.nodeType === 3) { i18nNode(root, en); return; }
  if (root.nodeType !== 1 || i18nSkip(root)) return;
  i18nNode(root, en);
  const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (x) => (x.nodeType === 1 && x.matches(I18N_SKIP)) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  for (let x = w.nextNode(); x; x = w.nextNode()) i18nNode(x, en);
}
const i18nObs = new MutationObserver((list) => {
  for (const m of list) {
    if (m.type === 'childList') m.addedNodes.forEach((x) => i18nTree(x, true));
    else if (m.type === 'characterData') i18nNode(m.target, true);
    else if (m.type === 'attributes') i18nNode(m.target, true);
  }
});
// 英文介面時，沒指定語系的日期時間改用英文格式（不然會出現「下午」）
for (const fn of ['toLocaleString', 'toLocaleDateString', 'toLocaleTimeString']) {
  const orig = Date.prototype[fn];
  Date.prototype[fn] = function (loc, opt) { return orig.call(this, loc === undefined && uiLang() === 'en' ? 'en-US' : loc, opt); };
}
function i18nApply() {
  const en = uiLang() === 'en';
  document.documentElement.lang = en ? 'en' : 'zh-Hant';
  i18nObs.disconnect();
  i18nTree(document.body, en);
  if (en) i18nObs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: I18N_ATTRS });
}

// ---- 排隊訊息：執行中按 Enter 先排隊，這一步結束後依序送出；Ctrl+Enter 直接插入
function queueMsg() {
  if (!S.sid) return;
  const text = input.value.trim();
  if (!text && !S.attach.length && !(S.files || []).length) return;
  (S.queue[S.sid] = S.queue[S.sid] || []).push({
    text: withFileRefs(text), images: S.attach.slice(),
    files: (S.files || []).filter((f) => f.data).map((f) => ({ name: f.name, data: f.data, media_type: f.media_type })),
  });
  input.value = ''; S.attach = []; S.files = []; renderAttach(); autosize(); renderQueue();
}
function renderQueue() {
  const box = $('#queueBox'); if (!box) return;
  const q = (S.sid && S.queue[S.sid]) || [];
  const sq = (S.sid && S.srvQ[S.sid]) || [];
  box.hidden = !q.length && !sq.length;
  const run = S.sid && S.running.has(S.sid);
  const n = q.length + sq.length;
  // 內部通知（系統產生，不是你打的字）：標「內部」，只看不改；這一步結束後送進對話
  const sqh = sq.map((it) => {
    const first = (String(it.text || '').split(NL).find((l) => l.trim()) || '');
    return `<div class="qitem internal" title="${esc(it.text || '')}"><span class="itag">內部</span><span class="ilabel">${esc(INTERNAL_LABEL[it.internal] || it.internal)}</span><span class="qt">${esc(first)}</span><span class="qhint">這一步結束後送進對話</span></div>`;
  }).join('');
  box.innerHTML = n ? `<div class="qhd">${n} 個排隊中<small>${run ? '這一回合結束後依序送出' : '馬上送出'}</small></div>` + sqh + q.map((it, i) => `<div class="qitem"><span class="qt" title="${esc(it.text || '')}">${esc(it.text || '（圖片）')}</span>`
    + `<span class="ib" data-qedit="${i}" title="編輯（放回輸入框）">${ico('edit')}</span>`
    + (run && !(it.images || []).length ? `<span class="ib" data-qins="${i}" title="插話：立即送進目前回合">${ico('steer')}</span>` : '')
    + `<span class="ib" data-qdel="${i}" title="刪除">${ico('trash')}</span></div>`).join('') : '';
}
$('#queueBox').addEventListener('click', (e) => {
  const q = (S.sid && S.queue[S.sid]) || [];
  const ins = e.target.closest('[data-qins]'), del = e.target.closest('[data-qdel]'), ed = e.target.closest('[data-qedit]');
  if (ed) { const it = q.splice(Number(ed.dataset.qedit), 1)[0]; renderQueue(); if (it) { input.value = it.text || ''; S.attach = it.images || []; renderAttach(); autosize(); input.focus(); } return; }
  if (ins) { const it = q.splice(Number(ins.dataset.qins), 1)[0]; renderQueue(); if (it) wsSend({ type: 'send', session_id: S.sid, text: it.text, images: it.images || [] }); }
  if (del) { q.splice(Number(del.dataset.qdel), 1); renderQueue(); }
});
function drainQueue(sid) {
  const q = S.queue[sid]; if (!q || !q.length) return;
  const it = q.shift();
  if (sid === S.sid) renderQueue();
  setTimeout(() => wsSend({ type: 'send', session_id: sid, text: it.text, images: it.images || [], files: it.files || [] }), 300);
}

// ---- 用量（今天／7 天／30 天／此對話）
S.usageRange = S.usageRange || 'today';
function usageCardHtml() {
  const r = S.usageRange;
  return `<div class="qcard usage-card"><div class="qtop"><div class="qhd"><b>用量</b><small>本機紀錄，每次模型呼叫算一次</small></div></div>`
    + `<div class="qbody"><div class="seg useg">${[['today', '今天'], ['7d', '7 天'], ['30d', '30 天'], ['session', '此對話']].map(([k, l]) => `<b class="${r === k ? 'on' : ''}" data-urange="${k}">${l}</b>`).join('')}</div>`
    + `<div id="usageBox">讀取中…</div></div></div><div id="budgetBox"></div>`;
}
const BUDGET_ACT = { warn: '只提醒', pause: '暫停送出', fallback: '改用備用' };
const BUDGET_TIP = { warn: '超過時跳提醒，照常送出', pause: '超過時不送出，等隔天或下個月', fallback: '超過時改用「模型用途 → 備用模型」' };
const money = (v) => (v == null ? '' : v >= 100 ? '$' + Math.round(v).toLocaleString('en-US') : '$' + Number(v).toFixed(2));
let BUDGET = null;
function renderBudget(b) {
  const box = $('#budgetBox'); if (!box || !b) return;
  BUDGET = b;
  if (!b.enabled) { box.innerHTML = ''; return; }   // 在「設定 → 一般 → 花費與預算」開啟
  const head = '<div class="qtop"><div class="qhd"><b>花費與預算</b><small>依模型單價計算，沒填單價的不算</small></div></div>';
  const row = (name, spent, cap) => {
    const p = cap ? Math.min(100, Math.round(spent / cap * 100)) : 0;
    const cls = !cap ? '' : spent >= cap ? ' over' : spent >= cap * 0.8 ? ' near' : '';
    return `<div class="bgstat${cls}"><span class="bgn">${name}</span><b>${money(spent)}</b>`
      + `<small>${cap ? '／ ' + money(cap) : '不限'}</small><i class="bgbar"><i style="width:${p}%"></i></i></div>`;
  };
  const cap = (k, name) => `<label class="bgcap"><span>${name}</span><span class="bgin"><em>$</em><input data-bg="${k}" value="${b[k] || ''}" placeholder="不限" inputmode="decimal"></span></label>`;
  box.innerHTML = `<div class="qcard bgcard">${head}<div class="qbody">`
    + row('今天', b.today, b.daily) + row('本月', b.month, b.monthly)
    + `<div class="bgcaps">${cap('daily', '每日上限')}${cap('monthly', '每月上限')}</div>`
    + `<div class="bgact"><span>超過時</span><div class="seg">${Object.entries(BUDGET_ACT).map(([k, l]) => `<b class="${b.action === k ? 'on' : ''}" data-bgact="${k}" title="${BUDGET_TIP[k]}">${l}</b>`).join('')}</div></div>`
    + (b.over && b.over.length ? `<div class="bgover">${b.over.map(esc).join('<br>')}</div>` : '')
    + (b.action === 'fallback' && !b.fallback ? '<div class="bgnote">還沒設備用模型，超過時只會提醒</div>' : '')
    + '</div></div>';
}
async function saveBudget(patch) {
  const box = $('#budgetBox'); if (!box || !BUDGET) return;
  const body = { ...(patch || {}) };
  box.querySelectorAll('input[data-bg]').forEach((el) => { if (!(el.dataset.bg in (patch || {}))) body[el.dataset.bg] = el.value.trim(); });
  try {
    renderBudget(await api('POST', '/api/budget', body));
  } catch (e) { toast('預算沒存：' + e.message); }
}
document.addEventListener('change', (e) => { if (e.target.closest && e.target.closest('#budgetBox input[data-bg]')) saveBudget(); });
document.addEventListener('click', (e) => {
  if (!e.target.closest) return;
  const act = e.target.closest('#budgetBox [data-bgact]');
  if (act) saveBudget({ action: act.dataset.bgact });
});
function fmtCount(n) {
  const v = Number(n || 0);
  if (v < 1000) return String(Math.round(v));
  if (v < 1e6) return (v / 1000).toFixed(v < 1e4 ? 2 : 1).replace(/\.?0+$/, '') + 'K';
  if (v < 1e9) return (v / 1e6).toFixed(v < 1e7 ? 2 : 1).replace(/\.?0+$/, '') + 'M';
  return (v / 1e9).toFixed(2).replace(/\.?0+$/, '') + 'B';
}
async function loadUsage() {
  const box = $('#usageBox'); if (!box) return;
  const r = S.usageRange;
  if (r === 'session' && !S.sid) { box.innerHTML = '<div class="qempty">先開一個對話</div>'; return; }
  const q = r === 'session' ? `range=all&sid=${encodeURIComponent(S.sid)}` : `range=${r}`;
  let u;
  try { u = await api('GET', `/api/usage?${q}`); } catch (e) { box.innerHTML = `<div class="qempty">讀取失敗：${esc(e.message)}</div>`; return; }
  const f = (n) => Number(n || 0).toLocaleString('en-US');
  const k = (n) => fmtCount(n);
  const maxd = Math.max(1, ...u.daily.map((d) => d.input_tokens + d.output_tokens));
  const pct = (a, b) => (b > 0 ? Math.round(a / b * 100) : 0);
  const hit = pct(u.cache_read, u.input_tokens);
  renderBudget(u.budget);
  const costTip = (u.unpriced || []).length ? `沒填單價、沒算進去：${u.unpriced.map((x) => (modelOf(x) || {}).label || x).join('、')}` : '依模型單價計算';
  box.innerHTML = `<div class="uhead"><div class="uhero" title="${f(u.input_tokens + u.output_tokens)} tokens"><b>${k(u.input_tokens + u.output_tokens)}</b><small>tokens・${f(u.calls)} 次呼叫${u.cost ? `・<span title="${esc(costTip)}">約 ${money(u.cost)}</span>` : ''}</small></div>
      <div class="usplit"><span title="${f(u.input_tokens)}">輸入 <b>${k(u.input_tokens)}</b></span><span title="${f(u.output_tokens)}">輸出 <b>${k(u.output_tokens)}</b></span>`
    + `<span title="輸入裡有 ${f(u.cache_read)} tokens 是讀快取（便宜很多）">快取命中 <b>${hit}%</b></span></div></div>`
    + (u.daily.length > 1 ? `<div class="udays">${u.daily.map((d) => `<div class="uday" title="${esc(d.day)}：${f(d.input_tokens + d.output_tokens)} tokens・${f(d.calls)} 次"><span><i style="height:${Math.max(6, Math.round((d.input_tokens + d.output_tokens) / maxd * 100))}%"></i></span><small>${esc(d.day.slice(5).replace('-', '/'))}</small></div>`).join('')}</div>` : '')
    + (u.by_model.length ? `<div class="umodels">${u.by_model.map((m) => {
      const tot = m.input_tokens + m.output_tokens; const top = u.by_model[0].input_tokens + u.by_model[0].output_tokens;
      return `<div class="umrow" title="輸入 ${f(m.input_tokens)}（快取 ${pct(m.cache_read, m.input_tokens)}%）・輸出 ${f(m.output_tokens)}">
        <span class="uml">${esc((modelOf(m.model) || {}).label || m.model)}</span><span class="umn">${k(tot)}</span><span class="umc">${m.cost != null ? money(m.cost) : f(m.calls) + ' 次'}</span>
        <i class="umbar"><i style="width:${Math.max(2, Math.round(tot / Math.max(1, top) * 100))}%"></i></i></div>`;
    }).join('')}</div>`
      : '<div class="qempty">這個範圍還沒有用量紀錄</div>');
}

// ---- 插件範圍：單一對話關插件、套用模板
function scopeLabel(s) {
  const es = (s && s.engine_state) || {};
  if (es.template) { const t = (S.templates || []).find((x) => x.id === es.template); return t ? t.label : '模板'; }
  const off = (es.plugins_off || []).length;
  return off ? `插件 −${off}` : '插件';
}
function renderScopePill() {
  const t = $('#scopePillTxt'); if (!t) return;
  const s = cur(); t.textContent = s ? scopeLabel(s) : '插件';
  const p = $('#scopePill'); if (p) p.title = '這個對話能用哪些插件（' + (s ? scopeLabel(s) : '全部') + '）';
}
// ---- 設定：工具分區（依插件分組，每個工具一個開關；全域同一份停用清單）
async function renderToolsSet(box) {
  if (!S.toolsAll) await loadToolsAll();
  const tools = S.toolsAll || [];
  if (!tools.length) { box.innerHTML = '<div class="blank">沒有工具（插件沒載入，或工具清單讀取失敗）</div>'; return; }
  const byP = {};
  for (const t of tools) (byP[t.plugin] = byP[t.plugin] || []).push(t);
  const onCount = tools.filter((t) => t.enabled).length;
  const onTok = tools.filter((t) => t.enabled).reduce((a, t) => a + (t.tokens_est || 0), 0);
  box.innerHTML = `<div class="sk-top" data-f="tAll"><span>共 ${tools.length} 個工具，已啟用 ${onCount} 個，啟用中約 ${onTok.toLocaleString()} tokens</span>`
    + `<span class="grow"></span><button class="btn sm" data-tools="allon">全部啟用</button><button class="btn sm" data-tools="alloff">全部停用</button></div>`
    + `<div class="pf-meta"><small>關掉一個工具只是模型不能呼叫它；系統提示裡的內容（記憶檔、使用者偏好等）照樣在，不會因此消失。</small></div>`
    + Object.keys(byP).sort().map((pl) => {
      const list = byP[pl];
      const n = list.filter((t) => t.enabled).length;
      return `<div class="tgrp"><h4>${esc(pl)}<small>${n}/${list.length} 已啟用</small></h4><div class="list">`
        + list.map((t) => `<div class="trow${t.enabled ? '' : ' off'}"><span class="sw sm${t.enabled ? ' on' : ''}" data-tsw="${esc(t.name)}"></span><span class="tn"><code>${esc(t.name)}</code><span class="ctx-tok" title="名稱＋說明＋參數結構的字元數；tok 為粗估">${(t.chars || 0).toLocaleString()} 字 ≈ ${(t.tokens_est || 0).toLocaleString()} tok</span></span><span class="d" title="${esc(t.description || '')}">${esc(t.description || '')}</span></div>`).join('')
        + `</div></div>`;
    }).join('');
  box.onclick = async (e) => {
    const bulk = e.target.closest('[data-tools]');
    if (bulk) {
      const on = bulk.dataset.tools === 'allon';
      const next = (S.toolsAll || []).map((t) => ({ ...t, enabled: on }));
      try {
        await api('POST', '/api/tools', { disabled: on ? [] : next.map((t) => t.name) });
        S.toolsAll = next; notify('ok', on ? '已全部啟用工具' : '已全部停用工具'); renderToolsSet(box);
      } catch (err) { notify('error', '切換失敗', err.message); }
      return;
    }
    const sw = e.target.closest('[data-tsw]');
    if (sw) await toggleToolGlobal(sw.dataset.tsw).then(() => renderToolsSet(box));
  };
}

// ---- 設定：Skills 分區（讀寫權限與資料夾在上，個別 skill 依分類勾選，預設全部關）
async function renderSkillsSet(box) {
  box.innerHTML = '<div class="blank">讀取中…</div>';
  let info;
  try { info = await api('GET', '/api/skills'); } catch (e) { box.innerHTML = `<div class="blank">讀取失敗：${esc(e.message)}</div>`; return; }
  if (!info.plugin_loaded) { box.innerHTML = '<div class="blank">Skills 插件沒有載入。請到「插件」啟用 skills。</div>'; return; }
  const ps = (S.plugins || []).find((x) => x.name === 'skills');
  const sv = (ps && ps.settings) || {};
  const on = new Set(info.enabled || []);
  const byCat = {};
  for (const s of info.skills) (byCat[s.category] = byCat[s.category] || []).push(s);
  const cats = Object.keys(byCat).sort();
  box.innerHTML = `<div class="sk-perm" data-f="skPerm">`
    + `<div class="trow"><span class="sk-l"><b>讀、寫</b><small>就是 skill_view、skill_write 兩個工具：在<span class="genp-doc" data-goset="tools">「設定 → 工具」</span>開關，單一對話用「上下文 → 本回合工具」。skill_view 關掉時，skill 清單也不放進提示詞。</small></span></div>`
    + `<div class="trow"><span class="sk-l"><b>讀取的外部資料夾</b><small>多個用 ; 分隔。留空＝只用 WOOF 自己的 data/skills，不讀任何外部技能庫。</small></span><input class="sk-dirs" value="${esc(sv.dirs || '')}" placeholder="留空＝不讀外部"><button class="btn sm" data-skdirs>套用資料夾</button></div>`    + `<div class="trow"><span class="sk-l"><b>寫入位置</b><small>skill_write 只能寫到這裡（相對 WOOF 資料夾），不碰別的技能庫；覆寫前先備份。</small></span><input class="sk-wdir" value="${esc(sv.write_dir || 'data/skills')}"><button class="btn sm" data-skwdir>套用</button></div>`

    + `</div>`
    + `<div class="sk-top" data-f="skList"><span>共 ${info.skills.length} 個 skill，已勾選 <b id="skCount">${on.size}</b> 個（預設全部關閉）</span><span class="grow"></span>`
    + `<button class="btn sm" data-skall="on">全部勾選</button><button class="btn sm" data-skall="off">全部取消</button></div>`
    + (info.skills.length ? '' : '<div class="blank">找不到任何 skill。外部技能庫要在上面填資料夾，或在 data/skills 建立。</div>')
    + cats.map((c) => {
      const list = byCat[c];
      const n = list.filter((s) => on.has(s.name)).length;
      return `<div class="tgrp" data-cat="${esc(c)}"><h4>${esc(c)}<small>${n}/${list.length} 已勾選</small>`
        + `<span class="grow"></span><button class="btn sm" data-skcat="${esc(c)}" data-skcatv="on">全選</button><button class="btn sm" data-skcat="${esc(c)}" data-skcatv="off">全取消</button></h4><div class="list">`
        + list.map((s) => `<div class="trow${on.has(s.name) ? '' : ' off'}"><span class="sw sm${on.has(s.name) ? ' on' : ''}" data-skname="${esc(s.name)}"></span><code>${esc(s.name)}</code><span class="d" title="${esc(s.path)}">${esc(s.description || '')}</span><small class="sk-src">${esc(s.source)}</small></div>`).join('')
        + `</div></div>`;
    }).join('');
  const save = async (names) => {
    try {
      const r = await api('PUT', '/api/skills/enabled', { enabled: names });
      notify('ok', `已勾選 ${r.enabled.length} 個 skill`);
    } catch (err) { notify('error', '儲存失敗', err.message); }
  };
  const current = () => [...box.querySelectorAll('[data-skname].on')].map((x) => x.dataset.skname);
  box.onclick = async (e) => {
    const go = e.target.closest('[data-goset]');
    if (go) { openSettings(go.dataset.goset); return; }
    if (e.target.closest('[data-skwdir]')) {
      const v = box.querySelector('.sk-wdir').value.trim() || 'data/skills';
      try { await api('POST', '/api/plugins/skills', { settings: { ...sv, write_dir: v } }); notify('ok', '已套用寫入位置'); }
      catch (err) { notify('error', '套用失敗', err.message); }
      return;
    }
    if (e.target.closest('[data-skdirs]')) {
      const v = box.querySelector('.sk-dirs').value;
      try { await api('POST', '/api/plugins/skills', { settings: { ...sv, dirs: v } }); notify('ok', '已套用資料夾'); renderSkillsSet(box); }
      catch (err) { notify('error', '套用失敗', err.message); }
      return;
    }
    const all = e.target.closest('[data-skall]');
    if (all) {
      const names = all.dataset.skall === 'on' ? info.skills.map((s) => s.name) : [];
      await save(names); return renderSkillsSet(box);
    }
    const cat = e.target.closest('[data-skcat]');
    if (cat) {
      const c = cat.dataset.skcat; const want = cat.dataset.skcatv === 'on';
      const inCat = info.skills.filter((s) => s.category === c).map((s) => s.name);
      const set = new Set(current());
      inCat.forEach((n) => (want ? set.add(n) : set.delete(n)));
      await save([...set]); return renderSkillsSet(box);
    }
    const sw = e.target.closest('[data-skname]');
    if (sw) {
      const name = sw.dataset.skname;
      const set = new Set(current());
      set.has(name) ? set.delete(name) : set.add(name);
      await save([...set]); return renderSkillsSet(box);
    }
  };
}

// ---- 系統提示詞：檢視、手動編輯、還原預設
// 輸出風格清單來自後端（內建＋設定頁新增的，置頂的排前面）
const curStyle = () => ((cur() || {}).engine_state || {}).style || 'default';
async function loadStyles() { try { S.styles = (await api('GET', '/api/styles')).styles; } catch { S.styles = S.styles || []; } return S.styles; }
const styleName = (id) => ((S.styles || []).find((s) => s.id === id) || { name: id === 'default' ? '預設' : id }).name;
async function pickStyle(anchor) {
  await loadStyles();
  const items = S.styles.map((s) => ({ v: s.id, label: (s.pinned ? '♥ ' : '') + s.name, sub: s.id === 'default' ? '不另外加指示' : s.text.slice(0, 48) + (s.text.length > 48 ? '…' : '') }));
  items.push({ v: '__manage', label: '新增或修改風格…', sub: '設定 → 輸出風格' });
  openSpick(anchor, items, curStyle(), (v) => { if (v === '__manage') { closeCtxPanel(); openSettings('styles'); } else setStyle(v).then(refreshCtxPanel); });
}
function fmtDur(ms) { if (ms == null) return '—'; return ms >= 1000 ? (ms / 1000).toFixed(1) + ' 秒' : Math.round(ms) + ' 毫秒'; }
async function setStyle(v) {
  if (!S.sid) return;
  try {
    await api('PUT', `/api/sessions/${S.sid}/engine`, { style: v });
    const s = cur(); if (s) s.engine_state = { ...(s.engine_state || {}), style: v };
    notify('ok', '輸出風格：' + styleName(v), '下一則訊息最後會附上新風格，系統提示詞不重組，快取不受影響');
  } catch (e) { notify('error', '切換失敗', e.message); }
}
function costNow() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  S.usageRange = 'session'; setRight(true); openTab('paneKeys');
  $$('[data-urange]').forEach((x) => x.classList.toggle('on', x.dataset.urange === 'session'));
  loadUsage();
}

function sysNotify(title, body) {
  if (!('Notification' in window)) return;
  const show = () => { try { new Notification(`WOOF · ${title}`, { body }); } catch {} };
  if (Notification.permission === 'granted') show();
  else if (Notification.permission !== 'denied') Notification.requestPermission().then((p) => p === 'granted' && show()).catch(() => {});
}

// ------------------------------------------------------------------ 上下文面板：用量、統計、這個對話的總提示詞、壓縮
const CP = { sid: null, d: null, stats: null, draft: null };
// 本回合工具每一列的開關：只改這個對話。權限、插件、計畫模式擋掉的不能開（灰色、不能按）
function cpToolSw(t) {
  if (t.bridge) return '';
  const on = t.enabled && !t.drop_pending;
  const can = t.enabled || t.reason === '設定中停用' || t.session_off;
  const tip = t.drop_pending ? '下次壓縮時關；按一下取消' : !can ? `${t.reason}，要到對應的設定改` : on ? '這個對話關掉' : '這個對話打開';
  return `<span class="sw sm cp-sw${on ? ' on' : ''}${t.drop_pending ? ' pend' : ''}${can ? '' : ' dis'}" data-cp="${!can ? 'noop' : t.drop_pending ? 'toolKeep' : on ? 'toolOff' : 'toolOn'}" data-tn="${esc(t.name)}" title="${esc(tip)}"></span>`;
}
async function loadCtxData() {
  const [d, st] = await Promise.all([api('GET', `/api/sessions/${S.sid}/prompt`), api('GET', `/api/sessions/${S.sid}/stats`)]);
  CP.d = d; CP.stats = st; CP.sid = S.sid;
}
async function openCtxPanel(focus) {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  const p = $('#ctxPanel');
  if (!focus && p.classList.contains('on')) { closeCtxPanel(); return; }
  CP.draft = null;
  p.classList.add('on');
  p.innerHTML = '<div class="qempty">讀取中…</div>';
  try { await loadCtxData(); } catch (e) { p.innerHTML = `<div class="errbox">讀取失敗：${esc(e.message)}</div>`; return; }
  renderCtxPanel();
  if (focus === 'prompt') $('#cpPrompt', p)?.focus();
}
function closeCtxPanel() { $('#ctxPanel').classList.remove('on'); }
async function refreshCtxPanel() {
  if (!$('#ctxPanel').classList.contains('on') || !S.sid) return;
  try { await loadCtxData(); renderCtxPanel(); } catch (e) { notify('error', '上下文面板更新失敗', e.message); }
}
function renderCtxPanel() {
  const p = $('#ctxPanel');
  if (!p.classList.contains('on') || !CP.d || CP.sid !== S.sid) return;
  const d = CP.d, st = CP.stats || {}, u = st.usage || {};
  const usage = S.usage[S.sid], m = selectedModel();
  const win = winOf(usage, m);
  const used = ctxUsed(usage);
  const pct = win ? Math.min(100, used / win * 100) : 0;
  const isOv = d.override !== null && d.override !== undefined;
  const partsAll = d.parts || [];
  const base = partsAll.length ? partsAll[0].text : (d.effective || '');
  const partsList = partsAll.slice(1);
  const tools = d.tools || [];
  const onTools = tools.filter((t) => t.enabled && !t.deferred).length;
  const nDefer = tools.filter((t) => t.deferred).length;
  const fz = d.frozen;
  const auxRows = d.aux || [];
  const AUX_NAMES = { fallback: '備用模型', vision: '看圖（VIS）', compact: '壓縮', subagent: '子代理', review: '自動審核' };
  const draft = CP.draft ?? base;
  const sm = ((cur() || {}).engine_state || {}).summary;
  const tm = st.timing || {};
  const sumTxt = sm && sm.text
    ? `已摘要 ${sm.text.length.toLocaleString()} 字，涵蓋到訊息 #${sm.through}（${new Date(sm.at * 1000).toLocaleString()}）。原始訊息都還在，只是送給模型時改用摘要。`
    : '還沒壓縮。內容變長時會自動壓縮，也可以按下面的「立即壓縮」。';
  const cpSum = sm && sm.text ? sm.text : '';
  const cells = [
    ['回合', st.turns ?? '—'],
    ['模型呼叫', st.model_calls ?? '—'],
    ['工具呼叫', `${st.tool_calls ?? '—'}${st.tool_errors ? `（失敗 ${st.tool_errors}）` : ''}`],
    ['輸入 tokens', (u.input_tokens || 0).toLocaleString()],
    ['輸出 tokens', (u.output_tokens || 0).toLocaleString()],
    ['快取讀取', (u.cache_read || 0).toLocaleString()],
    ['快取命中', u.input_tokens ? Math.round((u.cache_read || 0) / u.input_tokens * 100) + '%' : '—'],
  ];
  p.innerHTML = `<div class="cp-head"><b>上下文</b><span class="cp-x" data-cp="close" title="關閉">${ico('x')}</span></div>
    <div class="cp-use"><div class="cp-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <small>${used.toLocaleString()} / ${win ? win.toLocaleString() : '—'} tokens${win ? `（${pct.toFixed(1)}%）` : ''}</small></div>
    <div class="cp-stats">${cells.map(([k, v]) => `<div><span>${k}</span><b>${esc(String(v))}</b></div>`).join('')}</div>
    <div class="cp-sec">
      <div class="cp-sh"><b>基礎提示詞</b><span class="pr-badge ${isOv ? 'ov' : ''}">${isOv ? '已手動覆寫' : '使用預設'}</span></div>
      <small class="cp-note">只取代這個對話的「基礎提示詞」，插件片段（記憶、專案指示、技能、MCP）照常加入。存了從下一則訊息起生效，但系統提示詞在最前面，<b>改它會讓這個對話的快取整段重算一次</b>（之後照常命中）。只是想補一句要求，直接在對話裡講比較省。</small>
      <textarea id="cpPrompt" class="pr-ta" spellcheck="false">${esc(draft)}</textarea>
      <div class="cp-btns"><span class="btn" data-cp="reset">還原預設</span><span class="btn primary" data-cp="save">儲存覆寫</span></div>
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>其他系統片段</b><span class="pr-badge">${partsList.length} 段，唯讀</span></div>
      <small class="cp-note">這些是送給模型的系統提示詞其餘部分，依插件組成，每段標明來源。</small>
      ${fz ? `<small class="cp-note">這個對話的系統提示詞在 ${new Date(fz.at * 1000).toLocaleString()} 固定下來，之後每回合送同一份，模型的前綴快取才會一直有效。${fz.stale ? '<b>系統提示詞的內容有更新（記憶、Skills、專案指示或輸出風格），還沒放進這個對話的固定版本</b>：輸出風格已經用「設定變更」告訴模型了；其餘會在下次壓縮或開新對話時套用，也可以現在套用（下一則訊息要重新計算一次全部內容）。' : '改基礎提示詞、計畫模式會重組（快取重算一次）；中途改輸出風格或權限不重組，只在下一則訊息最後附一段說明，快取不受影響；記憶、Skills、專案指示的變動等下次壓縮或新對話才套用。'}</small>
      ${fz.stale ? '<div class="cp-btns"><span class="btn" data-cp="refreshPrompt">立即套用新的提示詞</span></div>' : ''}` : ''}
      ${partsList.map((x) => `<details class="cp-def"><summary>${esc(x.source)}<small>　${x.text.length.toLocaleString()} 字</small></summary><pre class="pr-pre">${esc(x.text)}</pre></details>`).join('')}
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>重點便條</b><span class="pr-badge">${d.focus ? '最近一次：第 ' + d.focus.step + ' 步' : '還沒附過'}</span></div>
      <small class="cp-note">預設關閉（插件「focus」）。打開後只在上下文剛壓縮完的下一次請求，於最後面補一次待辦進度、權限、最近出錯、改過的檔；不存進對話。有些模型看到它會把同一件事重想一遍，長任務原地打轉時請保持關閉。</small>
      ${d.focus ? `<pre class="pr-pre">${esc(d.focus.text)}</pre>` : ''}
      ${d.told && d.told.seq ? `<small class="cp-note">這個對話已附過 ${d.told.seq} 次設定變更，最近一次在 ${new Date(d.told.at * 1000).toLocaleString()}。</small>` : ''}
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>本回合工具</b><span class="pr-badge">${onTools} / ${tools.length} 個會送出${nDefer ? `・${nDefer} 個延遲載入` : ''}・約 ${tools.filter((t) => t.enabled).reduce((a, t) => a + (t.tokens_est || 0), 0).toLocaleString()} tokens</span></div>
      <small class="cp-note">模型這一回合真正看得到的工具。灰色的是被排除，後面寫原因。每個工具的「字元」是名稱、說明與參數結構的 JSON 長度；tok 是粗估（英文約 4 字元 1 token，中文約 1.5 字元 1 token），實際計費以模型回報為準。</small>
      <small class="cp-note">右邊的開關只改這個對話，新對話照「設定 → 工具」。工具清單在快取最前面，開或關的那一則整段對話要重算一次；開著的工具也走快取，每次只多一點點。要關建議選「壓縮時一起關」：壓縮本來就整段重算，一起換不多花。權限、插件、計畫模式擋掉的不能在這裡開。</small>
      ${tools.map((t) => `<div class="cp-tool" style="${t.enabled && !t.deferred ? '' : 'opacity:.55'}"><span>${esc(t.name)}</span><b>${esc(t.plugin)}${t.drop_pending ? ' · 壓縮時關' : t.session_on ? ' · 這個對話開著' : t.reason ? ' · ' + esc(t.reason) : ''}<em class="ctx-tok">${(t.chars || 0).toLocaleString()} 字 ≈ ${(t.tokens_est || 0).toLocaleString()} tok</em>${cpToolSw(t)}</b></div>`).join('') || '<small class="cp-note">沒有任何工具。</small>'}
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>輔助模型與搜尋</b></div>
      <small class="cp-note">看圖（VIS）、壓縮、子代理、備用模型用哪個模型。在設定頁的「輔助模型」改。</small>
      ${auxRows.map((a) => `<div class="cp-tool"><span>${esc(AUX_NAMES[a.task] || a.task)}</span><b>${esc(a.label)}</b></div>`).join('')}
      <div class="cp-tool"><span>網路搜尋（WEB）順序</span><b>${esc(d.web_order || '預設：Firecrawl → Tavily')}</b></div>
      <div class="cp-btns"><span class="btn" data-cp="aux">改輔助模型</span><span class="btn" data-cp="webset">改搜尋順序</span></div>
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>輸出風格</b></div>
      <small class="cp-note">只影響這個對話之後的回覆。</small>
      <span class="cp-pick" data-cp="style"><b>${esc(styleName(curStyle()))}</b>${ico('down')}</span>
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>耗時</b></div>
      <div class="cp-stats">
        <div><span>首字延遲（平均）</span><b>${fmtDur(tm.avg_ttft_ms)}</b></div>
        <div><span>模型耗時（平均）</span><b>${fmtDur(tm.avg_model_ms)}</b></div>
        <div><span>工具耗時（總）</span><b>${fmtDur(tm.tool_ms_total)}</b></div>
      </div>
      ${(tm.tools || []).length ? (tm.tools || []).slice(0, 8).map((t) => `<div class="cp-tool"><span>${esc(t.name)}</span><b>${t.calls} 次 · 平均 ${fmtDur(t.avg_ms)}${t.errors ? ` · 失敗 ${t.errors}` : ''}</b></div>`).join('') : '<small class="cp-note">還沒有工具呼叫。</small>'}
    </div>
    <div class="cp-sec">
      <div class="cp-sh"><b>壓縮</b></div>
      <small class="cp-note">${esc(sumTxt)}</small>
      ${cpSum ? `<details class="cp-sum"><summary>看摘要內容</summary>${md(cpSum).outerHTML}</details>` : ''}
      <div class="cp-btns"><span class="btn" data-cp="compact">立即壓縮</span></div>
    </div>`;
}
$('#ctxPanel').addEventListener('input', (e) => { if (e.target.id === 'cpPrompt') CP.draft = e.target.value; });
$('#ctxPanel').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-cp]'); if (!b || !S.sid) return;
  const k = b.dataset.cp;
  if (k === 'close') { closeCtxPanel(); return; }
  if (k === 'aux') { closeCtxPanel(); openSettings('aux'); return; }
  if (k === 'webset') { closeCtxPanel(); showView('plug'); return; }
  if (k === 'compact') { await compactNow(); await refreshCtxPanel(); return; }
  if (k === 'style') { pickStyle(b); return; }
  if (k === 'noop') { notify('info', b.title || '這個工具不能在這裡開'); return; }
  if (k === 'toolOff' || k === 'toolKeep') {
    const name = b.dataset.tn;
    let when = 'keep';
    if (k === 'toolOff') {
      const n = ctxUsed(S.usage[S.sid]) || 0;
      const cost = n ? `約 ${n.toLocaleString()} tokens` : '整段對話';
      when = await modal({
        title: `關掉 ${name}`,
        desc: `工具清單在快取最前面，關掉的那一則要把整段對話重算一次（目前${cost}）。壓縮本來就要整段重算，跟壓縮一起關就不多花。`,
        ok: '壓縮時一起關',
        extra: `<span class="btn" data-x="compactnow">現在壓縮並關</span><span class="btn" data-x="now">現在關（重算${esc(cost)}）</span>`,
        onOpen: (m, finish) => m.addEventListener('click', (e) => { const x = e.target.closest('[data-x]'); if (x) finish(x.dataset.x); }),
      });
      if (!when) return;
      if (when === true) when = 'compress';
    }
    try {
      if (when === 'keep') await api('POST', `/api/sessions/${S.sid}/tools`, { name, on: true });
      else await api('POST', `/api/sessions/${S.sid}/tools`, { name, on: false, when: when === 'now' ? 'now' : 'compress' });
      if (when === 'compactnow') { await compactNow(); }
      else notify('ok', when === 'keep' ? `${name} 不關了` : when === 'now' ? `這個對話關了 ${name}` : `${name} 會在下次壓縮時關掉`, when === 'now' ? '下一則訊息整段重算一次' : '');
      await refreshCtxPanel();
    } catch (err) { notify('error', '改不了', err.message); }
    return;
  }
  if (k === 'toolOn') {
    if (!await confirmBox('這個對話打開工具', `打開 ${b.dataset.tn}。下一則訊息整段對話要重算一次快取，之後照常命中。`, { ok: '打開' })) return;
    try {
      await api('POST', `/api/sessions/${S.sid}/tools`, { name: b.dataset.tn, on: true });
      notify('ok', `這個對話打開了 ${b.dataset.tn}`, '下一則訊息起生效');
      await refreshCtxPanel();
    } catch (err) { notify('error', '改不了', err.message); }
    return;
  }
  if (k === 'refreshPrompt') {
    try { await api('DELETE', `/api/sessions/${S.sid}/prompt-snapshot`); notify('ok', '下一則訊息起套用新的系統提示詞'); await refreshCtxPanel(); }
    catch (err) { notify('error', '套用失敗', err.message); }
    return;
  }
  if (k === 'reset') {
    try { await api('PUT', `/api/sessions/${S.sid}/prompt`, { text: '' }); CP.draft = null; notify('ok', '已還原為預設組合'); await refreshCtxPanel(); }
    catch (err) { notify('error', '還原失敗', err.message); }
    return;
  }
  if (k === 'save') {
    const t = $('#cpPrompt').value;
    try {
      if (t.trim() === (CP.d.default || '').trim()) { await api('PUT', `/api/sessions/${S.sid}/prompt`, { text: '' }); notify('ok', '與預設相同，已清除覆寫'); }
      else { await api('PUT', `/api/sessions/${S.sid}/prompt`, { text: t }); notify('ok', '已儲存提示詞覆寫', '下一則訊息起生效；這個對話的快取會重算一次'); }
      CP.draft = null; await refreshCtxPanel();
    } catch (err) { notify('error', '儲存失敗', err.message); }
  }
});
document.addEventListener('mousedown', (e) => {
  const p = $('#ctxPanel');
  if (p.classList.contains('on') && !e.target.closest('#ctxPanel,#meter')) closeCtxPanel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#ctxPanel').classList.contains('on')) closeCtxPanel(); });
$('#meter').onclick = () => openCtxPanel();

async function openScopeModal() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  let tv, tools, ps;
  try { [tv, tools, ps] = await Promise.all([api('GET', '/api/templates'), api('GET', '/api/tools'), api('GET', '/api/presets')]); }
  catch (e) { notify('error', '讀不到插件資料', e.message); return; }
  S.templates = tv.templates || [];
  const presets = ps.presets || [];
  const es = (cur() || {}).engine_state || {};
  const off = new Set(es.plugins_off || []);
  const byP = {};
  for (const t of tools) (byP[t.plugin] = byP[t.plugin] || []).push(t);
  const plugins = Object.keys(byP).sort();
  const permName = (k) => (PERM[k] || PERM.ask)[0];
  const body = `<div class="scope-top"><label>模式</label><select id="presetSel"><option value="">不指定（自己挑下面的插件）</option>`
    + presets.map((p) => `<option value="${esc(p.id)}"${es.preset === p.id ? ' selected' : ''}>${esc(p.name)}（${p.plugins.length} 個插件・${esc(permName(p.perm))}）</option>`).join('')
    + `</select><span class="btn" data-gopreset>管理模式</span></div>`
    + `<p class="scope-note">套用模式會一次換掉這個對話的插件和權限：權限下一則訊息就生效；插件開關排到下次壓縮或開新對話才換，這個對話的快取不會因此重算。</p>`
    + `<div class="scope-top"><label>模板</label><select id="tplSel"><option value="">全部插件（預設）</option>`
    + S.templates.map((t) => `<option value="${esc(t.id)}"${es.template === t.id ? ' selected' : ''}>${esc(t.label)}（${t.plugins.length} 個插件）</option>`).join('')
    + `</select><button class="btn" data-savetpl>存成模板</button></div>`
    + `<p class="scope-note">這裡是整個插件（連同它的工具和掛鉤）在這個對話開不開。只要關單一工具，用「上下文 → 本回合工具」的開關。</p>`
    + plugins.map((p) => `<div class="tgrp"><h4>${esc(p)}<small>${byP[p].length} 個工具</small></h4><div class="list">`
      + `<div class="trow${off.has(p) ? ' off' : ''}"><span class="sw sm${off.has(p) ? '' : ' on'}" data-plug="${esc(p)}"></span><code>${esc(p)}</code>`
      + `<span class="d">${esc(byP[p].map((t) => t.name).join('、'))}</span></div></div></div>`).join('');
  modal({
    title: '這個對話能用的插件', wide: true, ok: '套用到這個對話', body,
    onOpen: (m, close) => { m.addEventListener('click', async (e) => {
      if (e.target.closest('[data-gopreset]')) { close(false); openSettings('presets'); return; }
      const sw = e.target.closest('[data-plug]');
      if (sw) { sw.classList.toggle('on'); sw.closest('.trow').classList.toggle('off', !sw.classList.contains('on')); const ts = m.querySelector('#tplSel'); if (ts) ts.value = ''; const ps2 = m.querySelector('#presetSel'); if (ps2) ps2.value = ''; return; }
      if (e.target.closest('[data-savetpl]')) {
        const name = await promptBox('存成模板', '模板名稱', '', { desc: '模板會記住目前勾選的插件，之後任何對話都能套用。' });
        if (!name || !name.trim()) return;
        const on = [...m.querySelectorAll('[data-plug]')].filter((x) => x.classList.contains('on')).map((x) => x.dataset.plug);
        try {
          const r = await api('POST', '/api/templates', { templates: [...S.templates, { id: 'tpl-' + Date.now().toString(36), label: name.trim(), plugins: on }] });
          S.templates = r.templates; notify('ok', '已存成模板', name.trim());
        } catch (err) { notify('error', '存模板失敗', err.message); }
      }
    }); },
    onOk: async (m) => {
      const ps2 = m.querySelector('#presetSel'); const pid = ps2 ? ps2.value : '';
      const ts = m.querySelector('#tplSel'); const tid = ts ? ts.value : '';
      try {
        let r, preset = null;
        if (pid) {
          r = await api('POST', `/api/sessions/${S.sid}/preset`, { id: pid });
          preset = pid;
        } else if (tid) r = await api('POST', `/api/sessions/${S.sid}/template`, { id: tid });
        else {
          const on = [...m.querySelectorAll('[data-plug]')].filter((x) => x.classList.contains('on')).map((x) => x.dataset.plug);
          r = await api('POST', `/api/sessions/${S.sid}/plugins`, { off: plugins.filter((p) => !on.includes(p)) });
        }
        const s = cur();
        if (s) {
          const es = { ...(s.engine_state || {}), preset: preset || null, template: r.template || null };
          if (r.pending) { es.pending_off = r.off; es.pending_gen = es.compress_count || 0; }
          else { es.plugins_off = r.off; delete es.pending_off; delete es.pending_gen; }
          s.engine_state = es;
          if (preset) s.perm = r.perm;
        }
        renderScopePill(); renderPerm();
        notify('ok', '已套用到這個對話', r.pending ? '權限下一則訊息生效；插件開關等下次壓縮或開新對話才換，這個對話的快取不會斷。' : '');
      } catch (err) { notify('error', '套用失敗', err.message); return false; }
    },
  });
}
$('#scopePill').onclick = () => openScopeModal();
input.addEventListener('input', renderSlash);
$('#slashMenu').onclick = (e) => { const d = e.target.closest('[data-cmd]'); if (d) runSlash(d.dataset.cmd); };

// ---- 工具執行權限
const PERM = {
  auto: ['全自動', '全部直接執行；危險指令仍先問'],
  edits: ['改檔自動', '讀、寫檔直接做；跑指令、連網先問'],
  ask: ['先問我', '讀檔、搜尋直接做，其餘先問'],
  readonly: ['唯讀', '只能讀和搜尋，寫檔、跑指令擋下'],
  custom: ['自訂', '照設定頁「權限」的表'],
};
// 權限每個對話各自記；新對話用「全自動」，不沿用別的對話
function currentPerm() { const s = cur(); const p = s ? s.perm : (S.draft.perm || S.defaultPerm); return PERM[p] ? p : 'ask'; }
async function loadDefaultPerm() { try { S.defaultPerm = (await api('GET', '/api/permissions')).settings.default_mode; } catch { /* 讀不到就用 ask */ } renderPerm(); }
function renderPerm() {
  const p = currentPerm();
  $('#permPillTxt').textContent = PERM[p][0];
  $('#permPill').classList.toggle('ask', p !== 'auto');
  $('#permPill').title = '工具執行權限：' + PERM[p][0] + '。' + PERM[p][1];
}
$('#permPill').onclick = () => {
  const p = currentPerm(), m = selectedModel();
  $('#permMenu').innerHTML = '<div class="phd">工具執行權限</div>' + Object.entries(PERM).map(([k, [n, d]]) =>
    `<div class="mi${k === p ? ' on' : ''}" data-perm="${k}"><span class="mck">${k === p ? ico('check') : ''}</span>${n}<small>${d}</small></div>`).join('')
    + (m && m.provider === 'claude_cli' ? '<div class="phd">Claude Code CLI 模型由 CLI 自己管權限</div>' : '')
    + '<div class="sep"></div><div class="mi" data-perm-set><span class="mck"></span>權限設定…<small>各類別、危險指令、白名單</small></div>';
  openPop($('#permMenu'), $('#permPill'));
};
$('#permMenu').onclick = async (e) => {
  if (e.target.closest('[data-perm-set]')) { closePops(); openSettings('perms'); return; }
  const d = e.target.closest('[data-perm]'); if (!d) return;
  const s = cur();
  if (s) { try { Object.assign(s, await api('PATCH', `/api/sessions/${s.id}`, { perm: d.dataset.perm })); } catch (err) { notify('error', '切換失敗', err.message); } }
  else S.draft.perm = d.dataset.perm;
  closePops(); renderPerm();
  notify('info', '這個對話的工具權限：' + PERM[d.dataset.perm][0], s ? '下一則訊息會附上權限說明，模型就不會再去叫被擋的工具' : '');
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if ($('.modal-bg')) return;
    if ($('#viewer').classList.contains('on')) closeViewer();
    else if ($('.pop.on')) { closePops(); $$('.pop.ctx').forEach((p) => p.remove()); }
    else stop();
  }
  if (e.ctrlKey && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#search').focus(); $('#search').select(); }
  if (e.ctrlKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newTab(); }
  if (e.ctrlKey && e.key === ',') { e.preventDefault(); openSettings(); }
  if (e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'b') { e.preventDefault(); setSide(document.body.classList.contains('side-off')); }
});

async function compactNow() {
  if (!S.sid) { notify('warn', '還沒有對話可以壓縮'); return; }
  const n = notify('info', '壓縮中…', '', 0);
  try {
    const r = await api('POST', '/api/sessions/' + S.sid + '/compact');
    if (r.ok) { notify('ok', `已壓縮較早的 ${r.count} 則訊息`, '原始訊息還在，只是送給模型時改用摘要。'); await loadSessions(); renderMeter(); }
    else notify('info', '這次沒有壓縮', r.reason || '內容還不夠長，不需要壓縮');
  } catch (e) { notify('error', '壓縮失敗', e.message); }
  n.remove();
}
$('#compactBtn').onclick = compactNow;

// ------------------------------------------------------------------ 右側面板
// ---- 拖拉欄寬：側欄 200–480、右欄 260–640，雙擊還原
const PANE_W = { side: ['--side-w', 260, 200, 480], right: ['--right-w', 320, 260, 640] };
function applyPaneW(k, w) {
  const [v, def, min, max] = PANE_W[k];
  w = Math.round(Math.min(max, Math.max(min, w || def)));
  document.documentElement.style.setProperty(v, w + 'px');
  return w;
}
for (const k of Object.keys(PANE_W)) applyPaneW(k, +localStorage.getItem('woof.w.' + k));
$$('.resizer').forEach((rs) => {
  const k = rs.dataset.rs;
  rs.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    rs.setPointerCapture(e.pointerId);
    const pane = k === 'side' ? $('.side') : $('#right');
    const x0 = e.clientX, w0 = pane.getBoundingClientRect().width;
    rs.classList.add('drag'); document.body.classList.add('resizing');
    const move = (ev) => {
      const zoom = (window.WoofTheme && WoofTheme.read().scale / 100) || 1;
      const dx = (ev.clientX - x0) / (D && D.zoom ? 1 : zoom);
      localStorage.setItem('woof.w.' + k, applyPaneW(k, k === 'side' ? w0 + dx : w0 - dx));
    };
    const up = () => { rs.classList.remove('drag'); document.body.classList.remove('resizing'); rs.removeEventListener('pointermove', move); rs.removeEventListener('pointerup', up); };
    rs.addEventListener('pointermove', move);
    rs.addEventListener('pointerup', up);
  });
  rs.addEventListener('dblclick', () => { localStorage.removeItem('woof.w.' + k); applyPaneW(k, 0); });
});
function setRight(show) {
  $('#right').classList.toggle('hide', !show);
  $('#rightRs').hidden = !show;
  $('#togRight').classList.toggle('on', show);
  localStorage.setItem('woof.right', show ? '1' : '0');
}
function openTab(id) {
  $$('.tabs div').forEach((x) => x.classList.toggle('on', x.dataset.p === id));
  $$('.pane').forEach((p) => p.classList.toggle('on', p.id === id));
  if (id === 'paneKeys') renderKeys(true);
  if (id === 'paneTasks') refreshTasks();
  if (id === 'paneFiles') loadTree();
  if (id === 'paneTerm') syncTerms();
  if (id === 'paneArts') renderArts();
  if (id === 'panePet') petLoad();
  localStorage.setItem('woof.rtab', id);
}
$('#togRight').onclick = () => setRight($('#right').classList.contains('hide'));
setRight(localStorage.getItem('woof.right') !== '0');
$$('.tabs div').forEach((t) => (t.onclick = () => openTab(t.dataset.p)));
setTimeout(() => { const t = localStorage.getItem('woof.rtab'); if (t && $('#' + t) && t !== 'paneTasks') openTab(t); }, 0);
$('#sbTasks').onclick = () => { showView('chat'); setRight(true); openTab('paneTasks'); };
$('#sbDir').onclick = (e) => {
  const r = $('#sbDir').getBoundingClientRect();
  dirMenu(r.left, r.top - 8, true);
};
function dirMenu(x, y, above) {
  const own = S.wd && S.wd.own;
  ctxMenu(x, y, [
    { label: '換這個對話的工作目錄…', run: pickWorkdir },
    ...(own ? [{ label: '改回預設工作目錄', hint: S.wd.default, run: () => setWorkdir('') }] : []),
    '-',
    { label: '在檔案總管開啟', disabled: !(D && S.workdir), run: () => D.openPath(S.workdir) },
    { label: '在終端開啟', run: () => newTerm(S.workdir) },
    { label: '顯示檔案樹', run: () => { setRight(true); openTab('paneFiles'); } },
    { label: '複製路徑', run: () => copyText(S.workdir) },
  ]);
  if (above) { const m = $('.pop.ctx'); if (m) m.style.top = Math.max(8, y - m.getBoundingClientRect().height) + 'px'; }
}
async function pickWorkdir() {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  let p = '';
  if (D && D.pickFolder) p = await D.pickFolder(S.workdir);
  else p = await promptBox('換工作目錄', '資料夾路徑', S.workdir, { mono: true });
  if (p) setWorkdir(p);
}
async function setWorkdir(p) {
  try {
    await api('PUT', `/api/sessions/${S.sid}/workdir`, { path: p });
    await loadWorkdir();
    notify('ok', p ? '這個對話的工作目錄已換成' : '已改回預設工作目錄', S.workdir);
  } catch (e) { notify('error', '換不了工作目錄', e.message); }
}
async function copyText(t) {
  try { await navigator.clipboard.writeText(String(t)); toast('已複製'); }
  catch { const ta = document.createElement('textarea'); ta.value = String(t); document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); toast('已複製'); }
}
// 把文字插到輸入框游標處（檔案樹、預覽、產物的「插入輸入框」）
function insertToInput(text) {
  const ta = $('#input'); if (!ta) return;
  const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? ta.value.length;
  const pre = ta.value.slice(0, s), post = ta.value.slice(e);
  const ins = (pre && !/\s$/.test(pre) ? ' ' : '') + text + (post && !/^\s/.test(post) ? ' ' : ' ');
  ta.value = pre + ins + post;
  ta.focus(); ta.selectionStart = ta.selectionEnd = (pre + ins).length;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}
const quotePath = (p) => /\s/.test(p) ? `"${p}"` : p;
const paneOn = (id) => !$('#right').classList.contains('hide') && $('#' + id) && $('#' + id).classList.contains('on');

const seenDone = new Set();
let tasksPrimed = false;
function noteDone(tasks) {
  for (const task of tasks) {
    if (!task.background) continue;
    if (!tasksPrimed || task.running) { if (!task.running) seenDone.add(task.id); continue; }
    if (seenDone.has(task.id)) continue;
    seenDone.add(task.id);
    const body = (task.command || '').slice(0, 80) + (task.exit_code === 0 ? ' 已完成' : ' 已結束');
    if (D && D.notify) D.notify('WOOF 背景任務', body);
    toast('背景任務' + (task.exit_code === 0 ? '完成' : '結束') + '：' + (task.command || '').slice(0, 40));
  }
  tasksPrimed = true;
}
async function refreshTasks() {
  let tasks = [];
  try { tasks = await api('GET', '/api/tasks'); } catch { return; }
  noteDone(tasks);
  S.tasks = tasks;
  const bg = tasks.filter((t) => t.running && t.background).length;
  const fg = tasks.filter((t) => t.running && !t.background).length;
  $('#sbTasksTxt').textContent = bg ? `${bg} 個背景任務` : fg ? `${fg} 個指令執行中` : '沒有背景任務';
  $('#sbTasks').classList.toggle('live', !!(bg || fg));
  tasks.sort((a, b) => (b.running - a.running) || b.started - a.started);
  const now = Date.now() / 1000;
  const card = (t) => {
    const dur = fmtMs(((t.ended || now) - t.started) * 1000);
    const state = t.running ? '<span class="spin"></span>' : t.killed ? '<span class="dot off"></span>' : t.exit_code === 0 ? '<span class="dot ok"></span>' : '<span class="dot bad"></span>';
    const tail = (t.tail || '').trim().split(NL).slice(-6).join(NL);
    return `<div class="task${t.running ? '' : ' done'}"><div class="t1">${state}<b title="${esc(t.command)}">${esc(t.command)}</b>`
      + `${t.running ? `<span class="btn sm danger" data-stop="${t.id}">結束</span>` : `<span class="tag ${t.exit_code === 0 ? 't-ok' : 't-off'}">${t.killed ? '已結束' : 'exit ' + t.exit_code}</span>`}</div>`
      + `<div class="t2">PID ${t.pid} · ${t.background ? '背景' : '前景'} · ${dur}</div>`
      + `<div class="log">${esc(tail || '（沒有輸出）')}</div></div>`;
  };
  const running = tasks.filter((t) => t.running), done = tasks.filter((t) => !t.running).slice(0, 30);
  $('#paneTasks').innerHTML = tasks.length
    ? (running.length ? `<div class="sec">執行中 <small>${running.length}</small></div>` + running.map(card).join('') : '')
      + (done.length ? `<div class="sec">已結束 <small>${done.length}</small></div>` + done.map(card).join('') : '')
    : '<div class="blank">目前沒有任務<small>agent 用 terminal 跑的指令會出現在這裡，背景任務完成時會跳通知，也可以隨時整串結束。</small></div>';
}
$('#paneTasks').onclick = async (e) => {
  const id = e.target.dataset.stop; if (!id) return;
  await api('POST', `/api/tasks/${id}/stop`); toast('已結束'); refreshTasks();
};
setInterval(refreshTasks, 3000);

// ---- 終端：上面是自己開的終端機（真的 shell，Git Bash／PowerShell），切到「模型的指令」看模型跑過的指令
// xterm 只在第一次打開終端時載入；切換對話時只留目前對話的終端在畫面上，其餘的輸出存在後端，接回來會補上
const TM = { list: [], shells: null, active: {}, views: new Map(), mode: localStorage.getItem('woof.tmMode') || 'mine', loading: null };
function loadXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (TM.loading) return TM.loading;
  const add = (tag, attrs) => new Promise((ok, bad) => { const el = document.createElement(tag); Object.assign(el, attrs); el.onload = ok; el.onerror = () => bad(new Error('載入失敗：' + (attrs.src || attrs.href))); document.head.appendChild(el); });
  TM.loading = Promise.all([
    add('link', { rel: 'stylesheet', href: 'vendor/xterm.css' }),
    add('script', { src: 'vendor/xterm.js' }).then(() => Promise.all([add('script', { src: 'vendor/xterm-addon-fit.js' }), add('script', { src: 'vendor/xterm-addon-web-links.js' })])),
  ]);
  return TM.loading;
}
function termTheme() {
  const cs = getComputedStyle(document.body);
  const v = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
  const dark = document.documentElement.classList.contains('dark') || matchMedia('(prefers-color-scheme: dark)').matches && document.documentElement.dataset.mode !== 'light';
  return {
    background: v('--panel', dark ? '#1b1b1b' : '#fafafa'), foreground: v('--text', dark ? '#e6e6e6' : '#1d1d1d'),
    cursor: v('--text', '#333'), cursorAccent: v('--panel', '#fff'), selectionBackground: v('--sel', 'rgba(128,128,128,.3)'),
  };
}
function renderTerm() {
  const pane = $('#paneTerm'); if (!pane) return;
  if (!$('#tmBar', pane)) {
    pane.innerHTML = `<div class="tm-bar" id="tmBar"><div class="tm-tabs" id="tmTabs"></div><span class="tm-new" id="tmNew" title="新的終端機（右鍵選 shell）">＋</span><span class="grow"></span>
      <div class="seg sm" id="tmMode"><b data-tm="mine">終端機</b><b data-tm="agent">模型的指令</b></div></div>
      <div class="tm-body" id="tmBody"></div><div class="tm-agent" id="tmAgent"></div>`;
  }
  $$('#tmMode b').forEach((b) => b.classList.toggle('on', b.dataset.tm === TM.mode));
  $('#tmBody').hidden = TM.mode !== 'mine'; $('#tmAgent').hidden = TM.mode !== 'agent';
  $('#tmNew').hidden = TM.mode !== 'mine'; $('#tmTabs').hidden = TM.mode !== 'mine';
  renderAgentCmds();
}
function renderAgentCmds() {
  const box = $('#tmAgent'); if (!box || TM.mode !== 'agent') return;
  const results = {};
  for (const m of S.msgs) if (m.role === 'tool') for (const b of m.content || []) if (b.type === 'tool_result') results[b.id] = b;
  const calls = [];
  for (const m of S.msgs) for (const b of m.content || []) {
    if (b.type === 'tool_call' && (b.name === 'terminal' || b.name === 'Bash' || b.name === 'process')) calls.push(b);
  }
  box.innerHTML = calls.length ? calls.slice().reverse().map((c) => {
    const r = results[c.id];
    const cmd = (c.args && (c.args.command || c.args.cmd)) || c.name;
    const out = ((r && r.output) || '').trim().split(NL).slice(-8).join(NL);
    return '<div class="termline"><b>$ ' + esc(cmd) + '</b><div class="log">' + esc(out || '（沒有輸出）') + '</div></div>';
  }).join('') : '<div class="blank">這個對話還沒有跑過指令</div>';
}
async function syncTerms() {
  renderTerm();
  if (TM.mode !== 'mine') return;
  const sid = S.sid || '';
  let r; try { r = await api('GET', `/api/pty?session_id=${encodeURIComponent(sid)}`); } catch (e) { $('#tmBody').innerHTML = `<div class="blank">讀不到終端機：${esc(e.message)}</div>`; return; }
  if ((S.sid || '') !== sid) return;
  TM.list = r.terms; TM.shells = r.shells; S.ptyShells = r.shells;
  // 不屬於這個對話的終端，畫面先拆掉（程序還在後端跑）
  for (const [id, v] of TM.views) if (!TM.list.some((t) => t.id === id)) dropView(id);
  if (!TM.list.length) {
    renderTermTabs();
    $('#tmBody').innerHTML = `<div class="blank tm-empty">這個對話還沒有開終端機<span class="btn sm" data-tm-new>開一個（${esc(S.workdir ? S.workdir.split(/[\\/]/).pop() : '工作目錄')}）</span></div>`;
    return;
  }
  const act = TM.list.find((t) => t.id === TM.active[sid]) ? TM.active[sid] : TM.list[TM.list.length - 1].id;
  await showTerm(act);
}
function renderTermTabs() {
  const sid = S.sid || '';
  $('#tmTabs').innerHTML = TM.list.map((t) => `<span class="tm-tab${t.id === TM.active[sid] ? ' on' : ''}${t.alive ? '' : ' dead'}" data-tid="${t.id}" title="${esc(t.cwd)}">${esc(t.title)}<i data-tclose="${t.id}" title="關閉（結束這個 shell）">×</i></span>`).join('');
}
function dropView(id) {
  const v = TM.views.get(id); if (!v) return;
  try { v.ro.disconnect(); } catch {}
  try { v.ws.onclose = null; v.ws.close(); } catch {}
  try { v.term.dispose(); } catch {}
  v.el.remove(); TM.views.delete(id);
}
async function showTerm(id) {
  const sid = S.sid || '';
  TM.active[sid] = id;
  renderTermTabs();
  try { await loadXterm(); } catch (e) { $('#tmBody').innerHTML = `<div class="blank">${esc(e.message)}</div>`; return; }
  const body = $('#tmBody');
  $$('.tm-empty', body).forEach((x) => x.remove());
  let v = TM.views.get(id);
  if (!v) v = mountTerm(id);
  for (const [k, x] of TM.views) x.el.hidden = k !== id;
  requestAnimationFrame(() => { try { v.fit.fit(); } catch {} v.term.focus(); });
}
function mountTerm(id) {
  const el = document.createElement('div'); el.className = 'tm-view'; el.dataset.tid = id;
  $('#tmBody').appendChild(el);
  const mono = getComputedStyle(document.body).getPropertyValue('--mono').trim() || 'Consolas, monospace';
  const term = new window.Terminal({ fontFamily: mono, fontSize: 12.5, lineHeight: 1.15, cursorBlink: true, scrollback: 5000, allowProposedApi: true, theme: termTheme() });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  try { term.loadAddon(new window.WebLinksAddon.WebLinksAddon((ev, uri) => { if (D && D.openPath) D.openPath(uri); else window.open(uri); })); } catch {}
  term.open(el);
  const ws = new WebSocket(`${BASE.replace(/^http/, 'ws')}/ws/pty/${id}?token=${TOKEN}`);
  const sendSize = () => { if (ws.readyState === 1) ws.send('\u0000' + JSON.stringify({ resize: [term.cols, term.rows] })); };
  ws.onopen = () => { try { fit.fit(); } catch {} sendSize(); };
  let replay = false;
  ws.onmessage = (e) => {
    if (e.data.charCodeAt(0) === 1) { replay = true; term.write(e.data.slice(1), () => { replay = false; }); }
    else term.write(e.data);
  };
  ws.onclose = () => { const t = TM.list.find((x) => x.id === id); if (t) { t.alive = false; renderTermTabs(); } };
  term.onData((d) => { if (!replay && ws.readyState === 1) ws.send(d); });
  term.onResize(sendSize);
  // Ctrl+C 有選取就複製、Ctrl+V 貼上（跟 Windows Terminal 一樣）；其他照常送進 shell
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown') return true;
    const k = e.key.toLowerCase();
    if (e.ctrlKey && k === 'c' && term.hasSelection()) { copyText(term.getSelection()); term.clearSelection(); return false; }
    if (e.ctrlKey && k === 'v') { navigator.clipboard.readText().then((t) => t && ws.readyState === 1 && ws.send(t)); e.preventDefault(); return false; }
    return true;
  });
  el.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (term.hasSelection()) { copyText(term.getSelection()); term.clearSelection(); }
    else navigator.clipboard.readText().then((t) => t && ws.readyState === 1 && ws.send(t));
  });
  el.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes('application/x-woof-path') || e.dataTransfer.types.includes('Files')) e.preventDefault(); });
  el.addEventListener('drop', (e) => {
    e.preventDefault();
    let ps = [];
    const raw = e.dataTransfer.getData('application/x-woof-path');
    if (raw) ps = [raw]; else ps = [...e.dataTransfer.files].map((f) => (D && D.pathOf ? D.pathOf(f) : '')).filter(Boolean);
    if (ps.length && ws.readyState === 1) ws.send(ps.map(quotePath).join(' ') + ' ');
    term.focus();
  });
  const ro = new ResizeObserver(() => { if (!el.hidden && el.offsetWidth) { try { fit.fit(); } catch {} } });
  ro.observe(el);
  const v = { el, term, fit, ws, ro };
  TM.views.set(id, v);
  return v;
}
async function newTerm(cwd, shell) {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  setRight(true);
  if (TM.mode !== 'mine') { TM.mode = 'mine'; localStorage.setItem('woof.tmMode', 'mine'); }
  openTab('paneTerm');
  try {
    const t = await api('POST', '/api/pty', { session_id: S.sid, cwd: cwd || '', shell: shell || '', cols: 100, rows: 28 });
    TM.active[S.sid] = t.id;
    await syncTerms();
  } catch (e) { notify('error', '開不了終端機', e.message); }
}
async function closeTerm(id) {
  dropView(id);
  try { await api('DELETE', `/api/pty/${id}`); } catch {}
  syncTerms();
}
$('#paneTerm').addEventListener('click', (e) => {
  const c = e.target.closest('[data-tclose]'); if (c) { e.stopPropagation(); closeTerm(c.dataset.tclose); return; }
  const t = e.target.closest('[data-tid]'); if (t && t.classList.contains('tm-tab')) { showTerm(t.dataset.tid); return; }
  if (e.target.closest('#tmNew') || e.target.closest('[data-tm-new]')) { newTerm(); return; }
  const m = e.target.closest('[data-tm]'); if (m) { TM.mode = m.dataset.tm; localStorage.setItem('woof.tmMode', TM.mode); syncTerms(); }
});
$('#paneTerm').addEventListener('contextmenu', (e) => {
  if (e.target.closest('#tmNew')) {
    e.preventDefault();
    const sh = TM.shells || { bash: 'Git Bash', powershell: 'PowerShell', cmd: '命令提示字元' };
    ctxMenu(e.clientX, e.clientY, Object.entries(sh).map(([k, n]) => ({ label: '新的 ' + n, run: () => newTerm('', k) })));
    return;
  }
  const t = e.target.closest('.tm-tab'); if (!t) return;
  e.preventDefault();
  const info = TM.list.find((x) => x.id === t.dataset.tid) || {};
  ctxMenu(e.clientX, e.clientY, [
    { label: '複製目錄路徑', run: () => copyText(info.cwd || '') },
    { label: '在檔案總管開啟目錄', disabled: !D, run: () => D.openPath(info.cwd) },
    '-',
    { label: '關閉這個終端機', danger: true, run: () => closeTerm(t.dataset.tid) },
  ]);
});
// 主題換了，終端顏色跟著換
new MutationObserver(() => { const th = termTheme(); for (const v of TM.views.values()) v.term.options.theme = th; })
  .observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-mode'] });
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key === '`') {
    e.preventDefault();
    const on = paneOn('paneTerm');
    if (on) { setRight(false); $('#input') && $('#input').focus(); } else { setRight(true); openTab('paneTerm'); }
  }
});

// ---- 額度：模型商與工具 API 分開
const KEY_TAG = { ok: ['t-ok', '可用'], exhausted: ['t-ex', '額度用完'], rate_limited: ['t-rl', '限流中'], invalid: ['t-ex', '無效'], disabled: ['t-off', '停用'] };
function llmPools() {
  const map = new Map();
  for (const v of S.vendors) if (v.key_pool) { if (!map.has(v.key_pool)) map.set(v.key_pool, []); map.get(v.key_pool).push(v.label || v.id); }
  for (const m of S.models) if (m.key_pool && !map.has(m.key_pool)) map.set(m.key_pool, [m.vendor_label]);
  return map;
}
function keyRows(list) {
  if (!list || !list.length) return '<div class="qempty">還沒有 key · <a data-goset="keys">去新增</a></div>';
  return list.map((k, i) => {
    const [cls, txt] = KEY_TAG[k.status] || ['t-off', k.status];
    const until = k.until ? `${new Date(k.until * 1000).toLocaleString('zh-TW', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })} 恢復` : '';
    return `<div class="qrow"><span class="ql">${esc(k.label || '第 ' + (i + 1) + ' 把')}</span><span class="tag ${cls}">${txt}</span>`
      + `<span class="qh">${esc(k.hint)}${until ? ' · ' + esc(until) : ''}${k.note ? ' · ' + esc(k.note) : ''}</span></div>`;
  }).join('');
}
function renderKeys(fetchUsage) {
  const pane = $('#paneKeys');
  const llm = llmPools();
  const all = S.keys || {};
  let html = '<div class="sec">模型商</div>';
  let shown = 0;
  for (const [pool, names] of llm) {
    if (!(all[pool] || []).length) continue;
    shown += 1;
    html += `<div class="qcard"><div class="qtop">${avatar(names[0])}<div class="qhd"><b>${esc(names.join('、'))}</b><small>金鑰池 ${esc(pool)} · ${(all[pool] || []).length} 把</small></div></div>`
      + `<div class="qbody">${keyRows(all[pool])}</div></div>`;
  }
  if (!shown) html += '<div class="qempty">目前沒有已設好 key 的 API 模型商；設定好 key 後才會出現額度。</div>';
  const tools = Object.keys(all).filter((p) => !llm.has(p));
  html += '<div class="sec">搜尋與工具 API</div>';
  html += tools.length ? tools.map((pool) => `<div class="qcard"><div class="qtop">${avatar(TOOL_POOL_LABEL[pool] || pool)}<div class="qhd"><b>${esc(TOOL_POOL_LABEL[pool] || pool)}</b><small>${esc(TOOL_POOL_DESC[pool] || '金鑰池 ' + pool)} · ${all[pool].length} 把</small></div></div>`
    + `<div class="qbody">${keyRows(all[pool])}</div></div>`).join('') : '<div class="qempty">沒有工具類的 key</div>';
  html += '<p class="foot">同一池的 key 用完一把才換下一把。重置日：商家有提供的以商家為準，其餘依 config 的 billing_day（沒設就不顯示）。</p>';
  pane.innerHTML = usageCardHtml() + '<div class="sec">剩餘額度 <span class="ib" data-quota-refresh title="重新查詢">' + ico('refresh') + '</span></div><div id="quotaBox">讀取中…</div>' + html;
  loadUsage();
  loadQuota();
}
// 每次都重新抓 #quotaBox：分頁可能被重新渲染，舊節點已脫離 DOM，不能寫進舊的 box
async function loadQuota(force) {
  try {
    const r = await api('GET', `/api/quota${force ? '?force=1' : ''}`);
    const rows = (r.vendors || []).map((v) => {
      const head = `<div class="qcard"><div class="qtop">${avatar(v.label)}<div class="qhd"><b>${esc(v.label)}</b><small>金鑰池 ${esc(v.pool)}</small></div></div>`;
      if (!v.supported) return head + '<div class="qbody"><small class="qmuted">這家沒有提供餘額查詢端點</small></div></div>';
      if (!v.ok) return head + `<div class="qbody"><small class="qerr">查詢失敗：${esc(v.error || '未知')}</small></div></div>`;
      const items = (v.items || []).map((it) => {
        const bar = it.pct == null ? '' : `<div class="ubar"><i style="width:${it.pct.toFixed(1)}%" class="${it.pct >= 90 ? 'bad' : it.pct >= 70 ? 'warn' : ''}"></i></div>`;
        const neg = /^欠費/.test(String(it.value));
        return `<div class="qrow"><span class="ql">${esc(it.label)}</span><span class="qh${neg ? ' qerr' : ''}">${esc(it.value)}${it.unit ? ' ' + esc(it.unit) : ''}</span></div>${bar}`;
      }).join('');
      let reset = '';
      if (v.reset_at) reset = `<small class="qmuted">重置：${esc(new Date(v.reset_at).toLocaleDateString('zh-TW'))}</small>`;
      else if (v.next_billing) reset = `<small class="qmuted">帳單日每月 ${v.billing_day} 日 · 下次 ${esc(v.next_billing)}</small>`;
      const warn = v.available === false ? '<small class="qerr">商家回報目前不可用（餘額不足？）</small>' : '';
      return head + `<div class="qbody">${items}${reset}${warn}</div></div>`;
    }).join('');
    const bx = $('#quotaBox'); if (bx) bx.innerHTML = rows || '<div class="qempty">沒有可查詢的 key</div>';
  } catch (e) { const bx = $('#quotaBox'); if (bx) bx.innerHTML = `<div class="qempty">讀取失敗：${esc(e.message)}</div>`; }
}
$('#paneKeys').addEventListener('click', (e) => { if (e.target.closest('[data-quota-refresh]')) loadQuota(true); });
$('#paneKeys').addEventListener('click', (e) => {
  const ur = e.target.closest('[data-urange]');
  if (ur) { S.usageRange = ur.dataset.urange; $$('[data-urange]').forEach((x) => x.classList.toggle('on', x === ur)); loadUsage(); return; }
  const go = e.target.closest('[data-goset]'); if (go) openSettings(go.dataset.goset);
});

// 這個對話寫過／改過的檔（樹上標記、產物面板用）
function sessionWrites() {
  const seen = new Map();
  for (const m of S.msgs) for (const b of m.content || []) {
    if (b.type !== 'tool_call') continue;
    const a = b.args || {};
    const p = ['write_file', 'patch', 'Write', 'Edit'].includes(b.name) ? a.path || a.file_path : null;
    if (p) seen.set(p, { name: b.name, mid: m.id, at: m.created || 0 });
  }
  return seen;
}
const normP = (p) => String(p || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
const absP = (p) => /^[A-Za-z]:[\\/]|^[\\/]{2}/.test(p) ? p : (S.workdir ? S.workdir.replace(/[\\/]+$/, '') + '\\' + String(p).replace(/^\.[\\/]/, '') : p);
// renderFiles：每次訊息更新都會叫到；切對話時同步工作目錄、終端機
let filesTimer = 0;
function renderFiles() {
  if (S._shownSid !== S.sid) {
    S._shownSid = S.sid;
    loadWorkdir();
    if (paneOn('paneTerm')) syncTerms(); else for (const id of [...TM.views.keys()]) dropView(id);
  }
  renderSnapshots();
  clearTimeout(filesTimer);
  filesTimer = setTimeout(() => {
    if (paneOn('paneFiles')) refreshTree();
    if (paneOn('paneArts')) renderArts();
    if (paneOn('paneTerm')) renderAgentCmds();
  }, 400);
}

// ---- 檔案樹：只列工作目錄；node_modules、.git、.venv 這類一律不列，.gitignore 列到的預設藏起來
const FT = { root: '', open: new Set(), cache: new Map(), showIgnored: localStorage.getItem('woof.ftIgnored') === '1', sel: '' };
async function listDir(path) {
  const r = await api('GET', `/api/fs/list?path=${encodeURIComponent(path)}&top=${encodeURIComponent(FT.root)}&ignored=${FT.showIgnored ? 1 : 0}`);
  FT.cache.set(normP(path), r);
  return r;
}
async function loadTree() {
  const pane = $('#paneFiles'); if (!pane) return;
  if (!$('#ftTree', pane)) {
    pane.innerHTML = `<div class="ft-hd"><div class="ft-root" id="ftRoot"></div>
        <div class="ft-acts"><span class="btn sm" data-ft="pick" title="換這個對話的工作目錄">換資料夾</span><span class="btn sm" data-ft="more" title="更多">⋯</span></div></div>
      <div class="ft-tool"><input id="ftFilter" placeholder="篩選已展開的檔名"><span class="chip${FT.showIgnored ? ' on' : ''}" data-ft="ignored" title=".gitignore 列到的檔">顯示忽略的</span></div>
      <div class="ft-tree" id="ftTree"></div><div id="snapBox"></div>`;
    $('#ftFilter', pane).oninput = () => drawTree();
  }
  if (!S.workdir) await loadWorkdir();
  FT.root = S.workdir;
  const nm = FT.root.split(/[\\/]/).filter(Boolean).pop() || FT.root;
  $('#ftRoot').innerHTML = `<b>${esc(nm)}</b><small title="${esc(FT.root)}">${esc(FT.root)}${S.wd && S.wd.own ? '' : '・預設'}</small>`;
  if (!FT.root) { $('#ftTree').innerHTML = '<div class="blank">還沒有工作目錄</div>'; return; }
  try { await listDir(FT.root); } catch (e) { $('#ftTree').innerHTML = `<div class="blank">讀不到資料夾：${esc(e.message)}</div>`; return; }
  await Promise.all([...FT.open].map((p) => listDir(p).catch(() => FT.open.delete(p))));
  drawTree();
  renderSnapshots();
}
let treeBusy = false;
async function refreshTree() {
  if (treeBusy || !FT.root || !FT.cache.size) return loadTree();
  treeBusy = true;
  try { await Promise.all([FT.root, ...FT.open].map((p) => listDir(p).catch(() => { FT.open.delete(p); }))); drawTree(); }
  finally { treeBusy = false; }
}
function drawTree() {
  const box = $('#ftTree'); if (!box) return;
  const q = (($('#ftFilter') || {}).value || '').trim().toLowerCase();
  const writes = new Set([...sessionWrites().keys()].map((p) => normP(absP(p))));
  const out = [];
  const walk = (path, depth) => {
    const r = FT.cache.get(normP(path)); if (!r) return;
    for (const e of r.entries) {
      const isOpen = e.dir && FT.open.has(e.path);
      if (q && !e.dir && !e.name.toLowerCase().includes(q)) continue;
      const mod = !e.dir && writes.has(normP(e.path));
      out.push(`<div class="ft-row${e.dir ? ' dir' : ''}${isOpen ? ' open' : ''}${mod ? ' mod' : ''}${FT.sel === e.path ? ' sel' : ''}${e.hidden ? ' hid' : ''}" style="--d:${depth}" data-fp="${esc(e.path)}" data-dir="${e.dir ? 1 : 0}" draggable="true" title="${esc(e.path)}${mod ? '\n這個對話改過' : ''}">`
        + `<i class="ft-ch"></i><span class="ft-n">${esc(e.name)}</span>${e.dir ? '' : `<small>${fmtBytes(e.size)}</small>`}</div>`);
      if (isOpen) walk(e.path, depth + 1);
    }
    if (r.truncated) out.push(`<div class="ft-row note" style="--d:${depth}">只列前 ${r.entries.length} 項</div>`);
    if (r.ignored && !FT.showIgnored && depth === 0) out.push(`<div class="ft-row note" style="--d:${depth}" data-ft="ignored">另有 ${r.ignored} 項被 .gitignore 藏起來，按這裡顯示</div>`);
  };
  walk(FT.root, 0);
  box.innerHTML = out.join('') || `<div class="blank">${q ? '沒有符合的檔名（只篩已展開的資料夾）' : '資料夾是空的'}</div>`;
}
async function toggleDir(path) {
  if (FT.open.has(path)) { for (const p of [...FT.open]) if (normP(p).startsWith(normP(path))) FT.open.delete(p); drawTree(); return; }
  FT.open.add(path);
  try { await listDir(path); } catch (e) { FT.open.delete(path); notify('error', '打不開資料夾', e.message); }
  drawTree();
}
$('#paneFiles').addEventListener('click', (e) => {
  const a = e.target.closest('[data-ft]');
  if (a) {
    const k = a.dataset.ft;
    if (k === 'pick') pickWorkdir();
    if (k === 'ignored') { FT.showIgnored = !FT.showIgnored; localStorage.setItem('woof.ftIgnored', FT.showIgnored ? '1' : '0'); const c = $('.chip[data-ft="ignored"]'); if (c) c.classList.toggle('on', FT.showIgnored); FT.cache.clear(); loadTree(); }
    if (k === 'more') {
      const r = a.getBoundingClientRect();
      ctxMenu(r.left - 120, r.bottom + 4, [
        { label: '重新整理', run: () => { FT.cache.clear(); loadTree(); } },
        { label: '全部收合', run: () => { FT.open.clear(); drawTree(); } },
        '-',
        { label: '在檔案總管開啟', disabled: !D, run: () => D.openPath(FT.root) },
        { label: '在終端開啟', run: () => newTerm(FT.root) },
        { label: '複製路徑', run: () => copyText(FT.root) },
        ...(S.wd && S.wd.own ? ['-', { label: '改回預設工作目錄', hint: S.wd.default, run: () => setWorkdir('') }] : []),
      ]);
    }
    return;
  }
  const row = e.target.closest('.ft-row[data-fp]'); if (!row) return;
  FT.sel = row.dataset.fp;
  $$('.ft-row.sel').forEach((x) => x.classList.remove('sel')); row.classList.add('sel');
  if (row.dataset.dir === '1') toggleDir(row.dataset.fp); else openPreview(row.dataset.fp);
});
$('#paneFiles').addEventListener('dblclick', (e) => {
  const row = e.target.closest('.ft-row[data-fp]'); if (!row || row.dataset.dir === '1' || !D) return;
  D.openPath(row.dataset.fp);
});
$('#paneFiles').addEventListener('dragstart', (e) => {
  const row = e.target.closest('.ft-row[data-fp]'); if (!row) return;
  e.dataTransfer.setData('application/x-woof-path', row.dataset.fp);
  e.dataTransfer.setData('text/plain', row.dataset.fp);
  e.dataTransfer.effectAllowed = 'copy';
});
$('#paneFiles').addEventListener('contextmenu', (e) => {
  const row = e.target.closest('.ft-row[data-fp]'); if (!row) return;
  e.preventDefault();
  const p = row.dataset.fp, dir = row.dataset.dir === '1';
  const rel = FT.root && normP(p).startsWith(normP(FT.root) + '\\') ? p.slice(FT.root.replace(/[\\/]+$/, '').length + 1) : p;
  ctxMenu(e.clientX, e.clientY, [
    ...(dir ? [{ label: FT.open.has(p) ? '收合' : '展開', run: () => toggleDir(p) }, { label: '在終端開啟這裡', run: () => newTerm(p) }, { label: '設成這個對話的工作目錄', run: () => setWorkdir(p) }]
      : [{ label: '預覽', run: () => openPreview(p) }, { label: '用預設程式開啟', disabled: !D, run: () => D.openPath(p) }]),
    { label: '在檔案總管顯示', disabled: !D, run: () => D.showItem(p) },
    '-',
    { label: '插入到輸入框', run: () => insertToInput(quotePath(p)) },
    { label: '複製完整路徑', run: () => copyText(p) },
    { label: '複製相對路徑', hint: rel, run: () => copyText(rel) },
  ]);
});
// 從檔案樹拖到輸入框：插入路徑
document.addEventListener('drop', (e) => {
  const p = e.dataTransfer && e.dataTransfer.getData('application/x-woof-path');
  if (!p || !e.target.closest('.composer')) return;
  e.preventDefault(); e.stopImmediatePropagation();
  insertToInput(quotePath(p));
}, true);

// ---- 預覽：圖片、影音、Markdown、HTML、程式碼與文字；其他檔用預設程式開
const PV_LANG = { js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', py: 'python', json: 'json', toml: 'ini', ini: 'ini', yml: 'yaml', yaml: 'yaml', md: 'markdown', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', css: 'css', sh: 'bash', bash: 'bash', ps1: 'powershell', bat: 'dos', cmd: 'dos', c: 'c', h: 'c', cpp: 'cpp', cs: 'csharp', java: 'java', go: 'go', rs: 'rust', rb: 'ruby', php: 'php', sql: 'sql', lua: 'lua', kt: 'kotlin', swift: 'swift' };
const AUD_EXT = /\.(mp3|wav|m4a|ogg|flac)$/i;
async function openPreview(path) {
  $$('.pv-bg').forEach((x) => x.remove());
  const name = path.split(/[\\/]/).pop(), ext = (name.split('.').pop() || '').toLowerCase();
  const bg = document.createElement('div'); bg.className = 'pv-bg';
  bg.innerHTML = `<div class="pv"><div class="pv-hd"><div class="pv-t"><b>${esc(name)}</b><small>${esc(path)}</small></div><div class="pv-tg" hidden></div>
    <span class="btn sm" data-pv="insert">插入輸入框</span><span class="btn sm" data-pv="copy">複製路徑</span>${D ? '<span class="btn sm" data-pv="show">在資料夾顯示</span><span class="btn sm" data-pv="open">用預設程式開</span>' : ''}<span class="btn sm" data-pv="close">關閉</span></div>
    <div class="pv-bd"><div class="blank">讀取中…</div></div></div>`;
  document.body.appendChild(bg);
  const bd = $('.pv-bd', bg);
  const close = () => { bg.remove(); document.removeEventListener('keydown', key, true); };
  const key = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', key, true);
  bg.addEventListener('mousedown', (e) => { if (e.target === bg) close(); });
  bg.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pv]'); if (!b) return;
    const k = b.dataset.pv;
    if (k === 'close') close();
    if (k === 'copy') copyText(path);
    if (k === 'insert') { insertToInput(quotePath(path)); close(); }
    if (k === 'show') D.showItem(path);
    if (k === 'open') D.openPath(path);
  });
  const url = fileUrl(path);
  if (IMG_EXT.test(name) || ext === 'svg') { bd.innerHTML = `<div class="pv-img"><img data-img="${esc(path)}" src="${url}"></div>`; $('img', bd).onerror = () => { bd.innerHTML = '<div class="blank">讀不到這張圖（檔案不存在或不在允許的資料夾）</div>'; }; return; }
  if (VID_EXT.test(name)) { bd.innerHTML = `<div class="pv-img"><video src="${url}" controls></video></div>`; return; }
  if (AUD_EXT.test(name)) { bd.innerHTML = `<div class="pv-aud"><audio src="${url}" controls></audio></div>`; return; }
  if (ext === 'pdf') { bd.innerHTML = `<div class="blank">PDF 用預設程式開比較完整<span class="btn sm" data-pv="open">用預設程式開</span></div>`; return; }
  let r;
  try { r = await api('GET', `/api/fs/read?path=${encodeURIComponent(path)}`); }
  catch (e) { bd.innerHTML = `<div class="blank">讀不到：${esc(e.message)}</div>`; return; }
  if (r.binary) { bd.innerHTML = `<div class="blank">這是二進位檔（${fmtBytes(r.size)}），沒辦法直接預覽${D ? '<span class="btn sm" data-pv="open">用預設程式開</span>' : ''}</div>`; return; }
  const note = r.truncated ? `<div class="pv-note">檔案 ${fmtBytes(r.size)}，只顯示前 1 MB</div>` : '';
  const code = () => {
    const lang = PV_LANG[ext];
    let html;
    try { html = lang && hljs.getLanguage(lang) && r.text.length < 400000 ? hljs.highlight(r.text, { language: lang }).value : esc(r.text); } catch { html = esc(r.text); }
    const n = r.text.split('\n').length;
    return note + `<div class="pv-code"><pre class="pv-ln">${Array.from({ length: Math.min(n, 30000) }, (_, i) => i + 1).join('\n')}</pre><pre class="hljs"><code>${html}</code></pre></div>`;
  };
  const views = ext === 'md' || ext === 'markdown' ? { render: () => note + `<div class="pv-md">${md(r.text).outerHTML}</div>`, code }
    : ext === 'html' || ext === 'htm' ? { render: () => note + '<iframe class="pv-html" sandbox="allow-scripts"></iframe>', code } : null;
  const draw = (k) => {
    bd.innerHTML = views ? views[k]() : code();
    const fr = $('iframe.pv-html', bd); if (fr) fr.srcdoc = r.text;
    const tg = $('.pv-tg', bg);
    if (views) { tg.hidden = false; tg.innerHTML = `<div class="seg sm"><b data-pvv="render" class="${k === 'render' ? 'on' : ''}">預覽</b><b data-pvv="code" class="${k === 'code' ? 'on' : ''}">原始碼</b></div>`; }
  };
  bg.addEventListener('click', (e) => { const v = e.target.closest('[data-pvv]'); if (v) draw(v.dataset.pvv); });
  draw('render');
}

// ---- 產物：這個對話裡模型寫出來的檔、回覆裡提到的圖片影音與文件
const DOC_RE = /(?:(?<![\w:/.])MEDIA:|(?<![\w:/.]))(?:[A-Za-z]:[\\/])[^\s`"'<>|*?]+?\.(pdf|docx?|xlsx?|pptx?|csv|html?|md|txt|zip|svg|json)(?![\w])/gi;
const ART = { filter: localStorage.getItem('woof.artF') || 'all' };
const artKind = (p) => IMG_EXT.test(p) || /\.svg$/i.test(p) ? 'img' : VID_EXT.test(p) || AUD_EXT.test(p) ? 'media' : /\.(pdf|docx?|xlsx?|pptx?|csv|md|txt|html?)$/i.test(p) ? 'doc' : 'code';
let artSeq = 0;
async function renderArts() {
  const pane = $('#paneArts'); if (!pane) return;
  const sid = S.sid, seq = ++artSeq;
  const items = new Map();
  for (const m of S.msgs) {
    for (const b of m.content || []) {
      if (b.type === 'tool_call') {
        const a = b.args || {};
        const p = ['write_file', 'patch', 'Write', 'Edit'].includes(b.name) ? a.path || a.file_path : null;
        if (p) items.set(normP(absP(p)), { req: p, how: b.name === 'patch' || b.name === 'Edit' ? '改過' : '寫出', mid: m.id });
      }
      if (m.role === 'assistant' && b.type === 'text') {
        for (const re of [MEDIA_RE, DOC_RE]) for (const x of String(b.text || '').matchAll(re)) {
          const p = x[0].replace(/^MEDIA:/, '');
          if (!items.has(normP(p))) items.set(normP(p), { req: p, how: '提到', mid: m.id });
        }
      }
    }
  }
  if (!items.size) { pane.innerHTML = '<div class="blank">這個對話還沒有產出檔案<small>模型寫出的檔、回覆裡的圖片影音與文件會集中在這裡</small></div>'; return; }
  let st = [];
  try { st = await api('POST', '/api/fs/stat', { session_id: sid, paths: [...items.values()].map((x) => x.req) }); } catch {}
  if (seq !== artSeq || sid !== S.sid) return;
  const list = [...items.values()].map((x, i) => ({ ...x, ...(st[i] || { path: x.req, exists: false }) })).reverse();
  const counts = { all: list.length, img: 0, media: 0, doc: 0, code: 0 };
  for (const x of list) counts[x.kind = artKind(x.path)] += 1;
  const shown = list.filter((x) => ART.filter === 'all' || x.kind === ART.filter);
  const tabs = [['all', '全部'], ['img', '圖片'], ['media', '影音'], ['doc', '文件'], ['code', '程式碼']].filter(([k]) => k === 'all' || counts[k]);
  const imgs = shown.filter((x) => x.kind === 'img' && x.exists && x.allowed);
  const rest = shown.filter((x) => !imgs.includes(x));
  pane.innerHTML = `<div class="art-f">${tabs.map(([k, n]) => `<span class="chip${ART.filter === k ? ' on' : ''}" data-artf="${k}">${n} ${counts[k]}</span>`).join('')}</div>`
    + (imgs.length ? `<div class="art-grid">${imgs.map((x) => `<div class="art-img" data-ap="${esc(x.path)}" title="${esc(x.path)}"><img loading="lazy" src="${fileUrl(x.path)}"><span>${esc(x.path.split(/[\\/]/).pop())}</span></div>`).join('')}</div>` : '')
    + rest.map((x) => {
      const ext = (x.path.split('.').pop() || '').slice(0, 4).toUpperCase();
      return `<div class="file art${x.exists ? '' : ' gone'}" data-ap="${esc(x.path)}" title="${esc(x.path)}"><div class="fi">${esc(ext)}</div><span>${esc(x.path.split(/[\\/]/).pop())}<small>${x.exists ? `${x.how}・${fmtBytes(x.size)}・${fmtTime(x.mtime)}` : '檔案已不存在'}</small></span></div>`;
    }).join('');
}
$('#paneArts').addEventListener('click', (e) => {
  const f = e.target.closest('[data-artf]'); if (f) { ART.filter = f.dataset.artf; localStorage.setItem('woof.artF', ART.filter); renderArts(); return; }
  const a = e.target.closest('[data-ap]'); if (a && !a.classList.contains('gone')) openPreview(a.dataset.ap);
});
$('#paneArts').addEventListener('contextmenu', (e) => {
  const a = e.target.closest('[data-ap]'); if (!a) return;
  e.preventDefault();
  const p = a.dataset.ap, gone = a.classList.contains('gone');
  ctxMenu(e.clientX, e.clientY, [
    { label: '預覽', disabled: gone, run: () => openPreview(p) },
    { label: '用預設程式開啟', disabled: gone || !D, run: () => D.openPath(p) },
    { label: '在檔案總管顯示', disabled: gone || !D, run: () => D.showItem(p) },
    '-',
    { label: '插入到輸入框', run: () => insertToInput(quotePath(p)) },
    { label: '複製路徑', run: () => copyText(p) },
    '-',
    { label: '複製圖片', disabled: gone || !D || !D.readImage, run: () => copyImageFile(p) },
    { label: '圖片另存為…', disabled: gone || !D, run: async () => { const f = await D.saveImage(p); if (f) notify('ok', '已另存', f); } },
  ]);
});
// 複製圖片到剪貼簿：主行程把圖轉成 PNG 位元組，這裡用 ClipboardItem 寫進去
async function copyImageFile(p, name) {
  try {
    const r = await D.readImage(p);
    if (!r || !r.ok) return notify('error', '複製失敗', '檔案不存在或讀不到這張圖');
    const bytes = r.bytes instanceof Uint8Array ? r.bytes : new Uint8Array(r.bytes);
    const blob = new Blob([bytes], { type: r.mime || 'image/png' });
    await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
    notify('ok', '已複製圖片', name || '');
  } catch (e) { notify('error', '複製失敗', e.message); }
}
// 圖片右鍵：複製圖片、複製位址、另存新檔（畫面在 woof:// 底下，複製圖片要交給主行程處理）
function imgMenuItems(p, src) {
  const name = (p || '').split(/[\\/]/).pop() || 'image.png';
  return [
    { label: '複製圖片', disabled: !D || !D.readImage, run: () => copyImageFile(p, name) },
    { label: '複製圖片位址', run: () => copyText(p) },
    '-',
    { label: '圖片另存為…', run: async () => {
        if (D && D.saveImage) { const f = await D.saveImage(p); if (f) notify('ok', '已另存', f); return; }
        const a = document.createElement('a'); a.href = src; a.download = name; a.click();
      } },
  ];
}
// 對話區以外（預覽視窗、寵物欄）的本機檔案連結也能點
document.addEventListener('click', (e) => {
  const fl = e.target.closest('a.flink');
  if (!fl || fl.closest('#chat')) return;
  e.preventDefault(); e.stopPropagation(); openFileLink(flinkPath(fl));
}, true);
// 點本機檔案連結：圖片在看圖器開，其他在右側預覽開；預覽不了的（例如 exe）交給系統
function openFileLink(p) {
  if (IMG_EXT.test(p)) { openViewer({ src: fileUrl(p) }); return; }
  if (/\.(html?|md|txt|json|toml|ya?ml|csv|log|py|js|ts|css|svg|pdf|xml|ini|bat|sh|ps1)$/i.test(p) || !/\.[a-z0-9]{1,5}$/i.test(p)) {
    if (typeof openPreview === 'function') { openPreview(p); return; }
  }
  if (D && D.openPath) D.openPath(p); else window.open(fileUrl(p), '_blank');
}
// 圖片複製：桌面版新外殼走主行程；舊外殼或網路圖，就自己抓下來轉 PNG 放剪貼簿
async function copyImageSrc(src, name) {
  try {
    const r = await fetch(src);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const bmp = await createImageBitmap(await r.blob());
    const cv = document.createElement('canvas'); cv.width = bmp.width; cv.height = bmp.height;
    cv.getContext('2d').drawImage(bmp, 0, 0);
    const blob = await new Promise((ok) => cv.toBlob(ok, 'image/png'));
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    notify('ok', '已複製圖片', name || '');
  } catch (e) { notify('error', '複製失敗', e.message); }
}
// 對話區右鍵：有選字＝複製選取；在連結上＝開啟／複製連結；在圖片上＝複製圖片；其他＝複製整則
function chatCtxItems(e) {
  const items = [];
  const sel = String(window.getSelection() || '');
  const inBox = e.target.closest('#chat, #petHist, #petStage, .pv-body, #taskBar');
  if (sel.trim()) items.push({ icon: 'copy', label: '複製', hint: 'Ctrl+C', run: () => copyText(sel) });
  const fl = e.target.closest('a.flink');
  if (fl) {
    const p = flinkPath(fl);
    items.push({ label: '預覽', run: () => openFileLink(p) });
    if (D && D.openPath) items.push({ label: '用預設程式開啟', run: () => D.openPath(p) });
    if (D && D.showItem) items.push({ label: '在資料夾中顯示', run: () => D.showItem(p.replace(/\//g, '\\')) });
    items.push({ icon: 'copy', label: '複製路徑', run: () => copyText(p) });
  }
  const a = !fl && e.target.closest('a[href]');
  if (a) {
    items.push({ label: '開啟連結', run: () => window.open(a.href, '_blank') });
    items.push({ icon: 'copy', label: '複製連結', run: () => copyText(a.href) });
  }
  const im = e.target.closest('img');
  if (im && im.src && !im.closest('.charc')) {
    const p = im.dataset.img || viewerPath(im.src);
    const name = (p || im.alt || 'image.png').split(/[\\/]/).pop();
    if (items.length) items.push('-');
    items.push({ icon: 'copy', label: '複製圖片', run: () => (p && D && D.readImage ? copyImageFile(p, name) : copyImageSrc(im.src, name)) });
    items.push({ label: '看大圖', run: () => openViewer(im) });
    if (p) {
      items.push({ label: '圖片另存為…', run: async () => {
        if (D && D.saveImage) { const f = await D.saveImage(p); if (f) notify('ok', '已另存', f); return; }
        const x = document.createElement('a'); x.href = im.src; x.download = name; x.click();
      } });
      items.push({ label: '複製圖片路徑', run: () => copyText(p) });
    }
  }
  const msg = inBox && e.target.closest('#chat .msg');
  if (msg && !im) {
    const text = msg.classList.contains('user') ? (($('.bubble', msg) || {}).innerText || '') : (msg._text || ($('.md', msg) || msg).innerText || '');
    if (text.trim()) { if (items.length) items.push('-'); items.push({ icon: 'copy', label: '複製這則訊息', run: () => copyText(text) }); }
  }
  const pr = e.target.closest('pre');
  if (pr && !sel.trim()) items.push({ icon: 'copy', label: '複製程式碼', run: () => copyText((pr.querySelector('code') || pr).innerText) });
  if (sel.trim() && e.target.closest('#chat') && $('#input')) items.push({ label: '引用到輸入框', run: () => insertToInput(sel.split('\n').map((l) => '> ' + l).join('\n') + '\n') });
  return items;
}
document.addEventListener('contextmenu', (e) => {
  if (e.target.closest('input, textarea, [contenteditable]')) return;
  if (e.target.closest('#chat, #petHist, #petStage, .pv-body, #viewer') && !e.target.closest('.pop')) {
    const items = chatCtxItems(e);
    if (items.length) { e.preventDefault(); ctxMenu(e.clientX, e.clientY, items); }
    return;
  }
});
document.addEventListener('contextmenu', (e) => {
  if (e.defaultPrevented) return;
  const im = e.target.closest('img[data-img]');
  if (!im || e.target.closest('#paneArts')) return;
  e.preventDefault();
  ctxMenu(e.clientX, e.clientY, imgMenuItems(im.dataset.img, im.src));
});
$('#paneArts').addEventListener('dragstart', (e) => {
  const a = e.target.closest('[data-ap]'); if (!a) return;
  e.dataTransfer.setData('application/x-woof-path', a.dataset.ap); e.dataTransfer.setData('text/plain', a.dataset.ap);
});
// 工作區快照：模型用 write_file／patch 改檔前存的原檔；可以把檔案還原到某則訊息之前（只動檔案，不動對話）
const SNAP_ST = { changed: ['t-rl', '已修改'], created: ['t-ok', '新建'], deleted: ['t-ex', '已刪除'], same: ['t-off', '與快照相同'], unknown: ['t-off', '未比對'] };
let snapTimer = 0;
function renderSnapshots() { clearTimeout(snapTimer); snapTimer = setTimeout(loadSnapshots, 250); }
async function loadSnapshots() {
  const box = $('#snapBox'); if (!box) return;
  const sid = S.sid; if (!sid) { box.innerHTML = ''; return; }
  let r; try { r = await api('GET', `/api/sessions/${sid}/snapshots`); } catch { return; }
  if (sid !== S.sid) return;
  const cps = r.checkpoints.filter((c) => c.mid !== '_undo' && c.files.length).reverse();
  const undo = r.checkpoints.find((c) => c.mid === '_undo');
  const base = (p) => p.split(/[\\/]/).pop();
  let h = '';
  if (undo && undo.files.length) h += `<div class="snap-undo"><span>剛才還原了 ${undo.files.length} 個檔</span><a data-snap-undo>撤銷還原</a></div>`;
  if (cps.length) {
    h += '<div class="sec">檔案快照<small>模型改檔前自動存的原檔。只還原檔案，不動對話。</small></div>';
    h += cps.map((c, i) => `<div class="snap" data-mid="${c.mid}">
      <div class="snap-hd"><span class="snap-n">${cps.length - i}</span><span class="snap-t" title="${esc(c.text)}">${esc(c.text || (c.gone ? '（訊息已回溯）' : '訊息'))}</span><span class="snap-time">${fmtTime(c.created)}</span></div>
      ${c.files.map((f) => { const st = SNAP_ST[f.state] || SNAP_ST.unknown; return `<div class="snap-f" title="${esc(f.path)}"><span class="snap-fn">${esc(base(f.path))}</span><span class="tag ${st[0]}">${f.existed ? st[1] : (f.state === 'same' ? '已不存在' : '新建')}</span>${f.state === 'same' || f.skipped ? '' : `<a data-snap-file="${esc(f.path)}" title="只還原這個檔">還原</a>`}</div>`; }).join('')}
      <div class="snap-acts"><span class="btn sm" data-snap-all title="把這些檔還原成這則訊息送出前的樣子；之後的快照一併用掉">還原到這則之前</span></div></div>`).join('');
  }
  box.innerHTML = h;
}
$('#paneFiles').addEventListener('click', async (e) => {
  const u = e.target.closest('[data-snap-undo]'), all = e.target.closest('[data-snap-all]'), one = e.target.closest('[data-snap-file]');
  if (!u && !all && !one) return;
  e.stopPropagation();
  const sid = S.sid;
  try {
    let r;
    if (u) r = await api('POST', `/api/sessions/${sid}/snapshots/undo`);
    else {
      const mid = +e.target.closest('[data-mid]').dataset.mid;
      const path = one ? one.dataset.snapFile : '';
      const ok = await modal({ title: path ? '還原這個檔？' : '還原檔案？', danger: true, ok: '還原',
        desc: path ? `${path}\n會變回這則訊息送出前的內容。` : '這則訊息之後模型改過的檔，都會變回送出前的樣子；原本不存在的會刪掉。對話不會變。還原後可以撤銷一次。' });
      if (!ok) return;
      r = await api('POST', `/api/sessions/${sid}/snapshots/restore`, { message_id: mid, path });
    }
    notify(r.notes.length && !r.restored.length ? 'warn' : 'ok', u ? `已撤銷，${r.restored.length} 個檔回到還原前` : `已還原 ${r.restored.length} 個檔`, r.notes.join('\n'));
    loadSnapshots();
  } catch (err) { notify('error', '還原失敗', err.message); }
}, true);


// ------------------------------------------------------------------ 插件頁（清單）
const KIND_LABEL = { provider: '模型連線', engine: '引擎', tool: '工具', hook: '掛鉤', search: '搜尋', connector: '連接器', ui: '介面' };
const HOOK_LABEL = { build_prompt: '系統提示', before_request: '送出前', before_tool: '工具前', after_tool: '工具後', on_message: '收到訊息', on_session: '對話事件', prepare_turn: '回合開始', force_compact: '壓縮', mcp_status: 'MCP 狀態', plugin_unload: '卸載' };
// 有專屬設定頁的欄位（plugin.toml 標 in_settings）不在插件頁重複，改成一行連到那一頁
function pluginForm(p) {
  const owned = {};
  for (const [key, f] of Object.entries(p.settings_schema || {})) if (f.in_settings) (owned[f.in_settings] = owned[f.in_settings] || []).push(f.label ? String(f.label).split(/[（：(:]/)[0] : key);
  const links = Object.entries(owned).map(([sec, names]) => {
    const t = (SECTIONS.find((x) => x[0] === sec) || [, sec])[1];
    const what = names.length > 2 ? `${names.length} 項設定` : names.join('、');
    return `<label>設定</label><div class="pf-owned" title="${esc(names.join('、'))}">${esc(what)}在<span class="genp-doc" data-goset="${esc(sec)}">「設定 → ${esc(t)}」</span>調</div>`;
  }).join('');
  return links + Object.entries(p.settings_schema || {}).filter(([, f]) => !f.in_settings).map(([key, f]) => {
    const v = p.settings[key] ?? f.default ?? '';
    let ctl;
    if (f.type === 'select') ctl = `<select data-set="${key}">${(f.options || []).map((o) => `<option${String(o) === String(v) ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    else if (f.type === 'bool' || f.type === 'boolean') ctl = `<span class="sw sm${v ? ' on' : ''}" data-setsw="${key}"></span>`;
    else if (f.type === 'model') ctl = modelSelectHtml(key, v, p.name, f.filter);
    else if (f.type === 'order') ctl = orderHtml(key, v, f.options || []);
    else if (f.type === 'models') ctl = modelsMultiHtml(key, v, p.name, f.filter);
    else if (String(v).includes(NL) || f.type === 'textarea') ctl = `<textarea data-set="${key}" rows="4">${esc(v)}</textarea>`;
    else ctl = `<input data-set="${key}" data-type="${f.type || 'text'}" value="${esc(v)}"${f.type === 'secret' ? ' type="password"' : ''}>`;
    return `<label>${esc(f.label || key)}${f.help ? `<small>${esc(f.help)}</small>` : ''}</label><div>${ctl}</div>`;
  }).join('');
}
function renderPlugins() {
  const sig = (S.plugins || []).map((p) => `${p.name}${p.enabled}${p.loaded}`).join('|');
  if (sig !== S.toolsSig) { S.toolsSig = sig; loadToolsAll(); }
  const q = ($('#plugSearch').value || '').trim().toLowerCase();
  const kinds = {};
  for (const p of S.plugins) for (const k of p.kinds.length ? p.kinds : ['其他']) kinds[KIND_LABEL[k] || k] = (kinds[KIND_LABEL[k] || k] || 0) + 1;
  const bad = S.plugins.filter((p) => p.error).length;
  const tabs = [['全部', S.plugins.length], ...Object.entries(kinds)];
  if (bad) tabs.push(['有問題', bad]);
  $('#plugFilters').innerHTML = tabs.map(([k, n]) => `<span class="${S.plugFilter === k ? 'on' : ''}" data-k="${esc(k)}">${esc(k)}<small>${n}</small></span>`).join('');
  const list = S.plugins.filter((p) => (S.plugFilter === '全部' || (S.plugFilter === '有問題' ? p.error : p.kinds.some((k) => (KIND_LABEL[k] || k) === S.plugFilter)))
    && (!q || `${p.name} ${p.description}`.toLowerCase().includes(q)));
  $('#plugGrid').innerHTML = list.map((p) => {
    const st = p.error ? '<span class="tag t-ex">載入失敗</span>' : !p.enabled ? '<span class="tag t-off">已停用</span>' : p.loaded ? '<span class="tag t-ok">運作中</span>' : '<span class="tag t-rl">未載入</span>';
    const open = S.openPlugins.has(p.name);
    const pv = p.provides || {};
    const codes = (arr, map = {}) => arr && arr.length ? arr.map((x) => `<code>${esc(map[x] || x)}</code>`).join('') : '<span>—</span>';
    const detail = `<div class="pdetail">`
      + (pv.backends && pv.backends.length ? `<div><span>模型連線</span><div>${codes(pv.backends, BACKEND_LABEL)}</div></div>` : '')
      + (pv.hooks && pv.hooks.length ? `<div><span>掛鉤</span><div>${codes(pv.hooks, HOOK_LABEL)}</div></div>` : '')
      + `<div><span>位置</span><div class="mono">${esc(p.path || '')}</div></div></div>`;
    return `<div class="prow${p.error ? ' err' : ''}${p.enabled ? '' : ' off'}${open ? ' open' : ''}" data-name="${esc(p.name)}">`
      + `<div class="pmain">`
      + `<span class="sw${p.enabled ? ' on' : ''}" title="${p.enabled ? '停用' : '啟用'}"></span>`
      + `<span class="pname"><b>${esc(p.name)}</b><span class="kinds">${p.kinds.map((k) => `<i>${esc(KIND_LABEL[k] || k)}</i>`).join('')}</span></span>`
      + `<span class="pdesc" title="${esc(p.description)}">${esc(p.description)}</span>`
      + `<span class="pst">${st}</span><span class="pver">v${esc(p.version)}</span>`
      + `<span class="pchev">${ico('right')}</span></div>`
      + (p.error ? `<div class="perr">${esc(p.error)}</div>` : '')
      + `<div class="pform">${detail}${pluginForm(p)}<div class="pacts">`
      + `<span class="btn sm" data-pact="folder">${ico('folder')}開啟資料夾</span>`
      + `<span class="grow"></span><span class="btn sm danger" data-pact="remove">${ico('trash')}移除</span></div></div></div>`;
  }).join('') || `<div class="blank">${q ? '找不到符合的插件' : '沒有插件'}</div>`;
}
$('#plugFilters').onclick = (e) => { const k = e.target.closest('[data-k]'); if (k) { S.plugFilter = k.dataset.k; renderPlugins(); } };
$('#plugSearch').addEventListener('input', renderPlugins);
$('#plugGrid').addEventListener('click', async (e) => {
  const row = e.target.closest('.prow'); if (!row) return;
  const name = row.dataset.name;
  const p = S.plugins.find((x) => x.name === name);
  if (e.target.closest('.pmain > .sw')) {
    const on = !e.target.closest('.sw').classList.contains('on');
    try {
      const r = await api('POST', `/api/plugins/${name}`, { enabled: on });
      if (r.error) notify('error', `${name} 載入失敗`, r.error); else notify('ok', `${name} 已${on ? '啟用' : '停用'}`);
    } catch (err) { notify('error', `${name} 切換失敗`, err.message); }
    return;
  }
  const go = e.target.closest('[data-goset]');
  if (go) { openSettings(go.dataset.goset); return; }
  const sw = e.target.closest('[data-setsw]');
  if (sw) { sw.classList.toggle('on'); savePluginSetting(name, sw.dataset.setsw, sw.classList.contains('on')); return; }
  const act = e.target.closest('[data-pact]');
  if (act) {
    if (act.dataset.pact === 'folder' && D && p) D.openPath(p.path);
    if (act.dataset.pact === 'remove') {
      if (!await confirmBox(`移除插件 ${name}`, '資料夾會移到 data/trash，需要時可以搬回 plugins 再重新載入。', { ok: '移除', danger: true })) return;
      try { await api('DELETE', `/api/plugins/${name}`); S.openPlugins.delete(name); notify('ok', `已移除 ${name}`); }
      catch (err) { notify('error', '移除失敗', err.message); }
    }
    return;
  }
  if (e.target.closest('.pmain')) {
    row.classList.toggle('open');
    row.classList.contains('open') ? S.openPlugins.add(name) : S.openPlugins.delete(name);
  }
});
// ---- 工具開關：併入插件頁，每個插件底下列它的工具（全域同一份停用清單）
async function loadToolsAll() {
  if (S.toolsLoading) return;
  S.toolsLoading = true;
  try { S.toolsAll = await api('GET', '/api/tools'); } catch (e) { S.toolsAll = []; notify('error', '讀不到工具清單', e.message); }
  S.toolsLoading = false;
  renderPlugins();
}
function toolRows(p) {
  if (!S.toolsAll) return '<span>讀取中…</span>';
  const ts = S.toolsAll.filter((t) => t.plugin === p.name);
  if (!ts.length) return p.loaded ? '<span>這個插件沒有提供工具</span>' : '<span>啟用後才會列出</span>';
  return ts.map((t) => `<div class="trow${t.enabled ? '' : ' off'}"><span class="sw sm${t.enabled ? ' on' : ''}" data-tsw="${esc(t.name)}"></span><span class="tn"><code>${esc(t.name)}</code><span class="ctx-tok" title="名稱＋說明＋參數結構的字元數；tok 為粗估">${(t.chars || 0).toLocaleString()} 字 ≈ ${(t.tokens_est || 0).toLocaleString()} tok</span></span><span class="d" title="${esc(t.description || '')}">${esc(t.description || '')}</span></div>`).join('');
}
async function toggleToolGlobal(name) {
  const next = (S.toolsAll || []).map((t) => (t.name === name ? { ...t, enabled: !t.enabled } : t));
  const hit = next.find((t) => t.name === name);
  try {
    await api('POST', '/api/tools', { disabled: next.filter((t) => !t.enabled).map((t) => t.name) });
    S.toolsAll = next;
    notify('ok', `${name} 已${hit && hit.enabled ? '啟用' : '停用'}`);
  } catch (e) { notify('error', '切換失敗', e.message); }
  renderPlugins();
}
// ---- 插件設定的兩種新控制項：模型選單、有順序的清單
function modelSelectHtml(key, v, name, filt) {
  const filter = filt === 'vision' ? (m) => !!m.vision : (filt === 'image' || filt === 'video') ? (m) => m.kind === filt : (m) => m.kind !== 'image' && m.kind !== 'video';
  const blank = filt === 'image' ? '自動：第一個有 key 的生圖模型' : filt === 'video' ? '自動：第一個有 key 的影片模型' : '跟著對話目前的模型';
  return mpPairHtml(`plug:${name}:${key}`, v, { filter, blank, set: (mid) => savePluginSetting(name, key, mid) });
}
function orderHtml(key, v, options, names = {}) {
  const on = String(v || '').split(',').map((x) => x.trim()).filter(Boolean);
  const all = [...on, ...options.filter((o) => !on.includes(o))];
  return `<div class="ordlist" data-order="${key}"><input type="hidden" data-set="${key}" value="${esc(on.join(','))}">`
    + all.map((o) => `<div class="ordi"><span class="sw sm${on.includes(o) ? ' on' : ''}" data-ordon="${esc(o)}"></span>${names[o] ? `<span class="ordn">${esc(names[o])}</span>` : `<code>${esc(o)}</code>`}<span class="ordmv"><b data-ordmv="-1" title="往前">↑</b><b data-ordmv="1" title="往後">↓</b></span></div>`).join('')
    + '</div>';
}
// 多選模型（例如子代理可指派的模型）：只列聊天模型，顯示名稱＋模型商
function modelsMultiHtml(key, v, name, filter) {
  const f = filter === 'api' ? (m) => m.provider !== 'claude_cli' : null;
  return mchkHtml(`plug:${name}:${key}`, v, { filter: f, set: (list) => savePluginSetting(name, key, list.join(',')) });
}
function orderEdit(b) {
  const box = b.closest('[data-order]');
  const row = b.closest('.ordi');
  if (b.dataset.ordon !== undefined) b.classList.toggle('on');
  if (b.dataset.ordmv) {
    const d = Number(b.dataset.ordmv);
    const sib = d < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (sib) { if (d < 0) sib.before(row); else sib.after(row); }
  }
  const hid = box.querySelector('input[data-set]');
  hid.value = [...box.querySelectorAll('.ordi')].filter((x) => x.querySelector('[data-ordon]').classList.contains('on')).map((x) => x.querySelector('[data-ordon]').dataset.ordon).join(',');
  hid.dispatchEvent(new Event('change', { bubbles: true }));
}
// capture：工具開關與排序不要觸發整列展開／收合
$('#plugGrid').addEventListener('click', (e) => {
  const tsw = e.target.closest('[data-tsw]');
  if (tsw) { e.stopPropagation(); toggleToolGlobal(tsw.dataset.tsw); return; }
  const ob = e.target.closest('[data-ordon],[data-ordmv]');
  if (ob) { e.stopPropagation(); orderEdit(ob); }
}, true);
async function savePluginSetting(name, key, v, quiet) {
  const p = S.plugins.find((x) => x.name === name);
  try {
    const r = await api('POST', `/api/plugins/${name}`, { settings: { ...p.settings, [key]: v } });
    if (r.error) notify('error', `${name} 套用設定後載入失敗`, r.error); else if (!quiet) notify('ok', '已儲存');
  } catch (err) { notify('error', '儲存失敗', err.message); }
}
$('#plugGrid').addEventListener('change', (e) => {
  const el = e.target; const key = el.dataset.set; if (!key) return;
  let v = el.value;
  if (el.dataset.type === 'number') v = Number(v);
  savePluginSetting(el.closest('.prow').dataset.name, key, v);
});
$('#plugReload').onclick = async () => {
  try {
    const rows = await api('POST', '/api/plugins/reload');
    const bad = rows.filter((r) => r.error);
    if (bad.length) notify('error', `${bad.length} 個插件載入失敗`, bad.map((r) => `${r.name}：${r.error}`).join(NL));
    else notify('ok', `已重新載入 ${rows.length} 個插件`);
  } catch (e) { notify('error', '重新載入失敗', e.message); }
};
$('#plugNew').onclick = () => modal({
  title: '新增插件',
  desc: '會從範本建立 plugins/<名稱> 資料夾，裡面已經有可執行的範例工具，建好後打開資料夾改程式即可。',
  body: `<label class="fld"><span>名稱</span><input data-f="name" placeholder="my_tool" class="mono"><small>英文小寫、數字、底線，以英文開頭</small></label>
    <label class="fld"><span>一句話說明</span><input data-f="desc" placeholder="這個插件做什麼"></label>
    <div class="fld"><span>類型</span><div class="kinds-pick">${[['tool', '工具：給 agent 呼叫'], ['provider', '模型連線'], ['engine', '引擎'], ['hook', '掛鉤：改提示或攔截']].map(([k, n], i) => `<label><input type="checkbox" data-kind="${k}"${i === 0 ? ' checked' : ''}>${n}</label>`).join('')}</div></div>`,
  ok: '建立',
  onOk: async (m) => {
    const name = $('[data-f="name"]', m).value.trim();
    const kinds = $$('[data-kind]', m).filter((c) => c.checked).map((c) => c.dataset.kind);
    const r = await api('POST', '/api/plugins/create', { name, kinds, description: $('[data-f="desc"]', m).value.trim() });
    notify('ok', `已建立 ${r.name}`, r.path);
    S.openPlugins.add(r.name);
    if (D) D.openPath(r.path);
  },
});
async function installPlugin(path) {
  try {
    const r = await api('POST', '/api/plugins/install', { path });
    if (r.error) notify('error', `${r.name} 已安裝但載入失敗`, r.error); else notify('ok', `已安裝 ${r.name}`);
    S.openPlugins.add(r.name);
    return true;
  } catch (e) { notify('error', '安裝失敗', e.message); return false; }
}
$('#plugInstall').onclick = () => modal({
  title: '安裝插件',
  desc: '插件是一個含 plugin.toml 和 __init__.py 的資料夾，也可以是打包好的 .zip。安裝時會複製進 plugins。',
  body: `<div class="dropzone" id="plugDrop">${ico('download')}<b>把資料夾或 .zip 拖到這裡</b><small>或在下面貼上路徑</small></div>
    <label class="fld"><span>路徑</span><input data-f="path" placeholder="D:/plugins/my_plugin 或 D:/plugins/my_plugin.zip" class="mono"></label>`,
  ok: '安裝',
  onOpen: (m, finish) => {
    const dz = $('#plugDrop', m);
    dz.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.add('drag'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
    dz.addEventListener('drop', async (e) => {
      e.preventDefault(); e.stopPropagation(); dz.classList.remove('drag');
      const f = e.dataTransfer.files[0]; const path = f && D && D.pathOf ? D.pathOf(f) : '';
      if (!path) { notify('error', '讀不到拖進來的路徑', '請改用貼上路徑'); return; }
      if (await installPlugin(path)) finish(true);
    });
  },
  onOk: async (m) => {
    const path = $('[data-f="path"]', m).value.trim();
    if (!path) { notify('warn', '請先填路徑或拖進來'); return false; }
    return (await installPlugin(path)) ? true : false;
  },
});
// 插件頁直接拖放安裝
$('#plugView').addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); $('#plugView').classList.add('drag'); });
$('#plugView').addEventListener('dragleave', (e) => { if (!e.relatedTarget || !$('#plugView').contains(e.relatedTarget)) $('#plugView').classList.remove('drag'); });
$('#plugView').addEventListener('drop', (e) => {
  e.preventDefault(); e.stopPropagation(); $('#plugView').classList.remove('drag');
  const f = e.dataTransfer.files[0]; const path = f && D && D.pathOf ? D.pathOf(f) : '';
  if (path) installPlugin(path); else notify('error', '讀不到拖進來的路徑');
});

// ------------------------------------------------------------------ 檢視切換
function showView(v) {
  if (v !== 'set' && S.dirty) flushSave();
  $('#chatView').style.display = v === 'chat' ? 'flex' : 'none';
  $('#plugView').classList.toggle('on', v === 'plug');
  $('#cronView').classList.toggle('on', v === 'cron');
  $('#setView').classList.toggle('on', v === 'set');
  $('#right').style.display = v === 'chat' ? '' : 'none';
  $('#togRight').style.visibility = v === 'chat' ? '' : 'hidden';
  $('#navCron').classList.toggle('on', v === 'cron');
  $('#navPlug').classList.toggle('on', v === 'plug');
  $('#navSet').classList.toggle('on', v === 'set');
  $('.slist').classList.toggle('dimsel', v !== 'chat');
  if (v !== 'chat') $('#tbTitle').textContent = { plug: '插件', cron: '排程工作', set: '設定' }[v]; else renderTitle();
}
// 側邊欄收合（標題列左上角）
function setSide(open) { document.body.classList.toggle('side-off', !open); localStorage.setItem('woof.sideOff', open ? '' : '1'); }
setSide(!localStorage.getItem('woof.sideOff'));
$('#togSide').onclick = () => setSide(document.body.classList.contains('side-off'));
$('#navCron').onclick = () => openCron();
$('#navPlug').onclick = () => { showView('plug'); refreshState(); };
$('#navSet').onclick = () => { setFindClear(); openSettings(); };

// ------------------------------------------------------------------ 設定組（profile）
// 每組各自獨立的金鑰、模型、記憶、Skills、對話與排程。目前這組由後端啟動時的 WOOF_PROFILE 決定，
// 所以切換要重啟後端——桌面程式會接手（換環境變數、重啟 Python、重載畫面）；瀏覽器開啟時只會記下
// 「下次啟動用哪一組」，並老實告訴使用者要重開 WOOF。
function profList() { return S.profList || []; }
function profName(p) { return (p && p.name) || '預設'; }
function curProf() { return profList().find((p) => p.current) || null; }
function renderProf() {
  const p = curProf(), el = $('#navProf');
  if (!el) return;
  const n = profList().length;
  $('#navProfTxt').textContent = profName(p);
  el.title = `設定組：${profName(p)}（共 ${n} 組）\n每組有獨立的金鑰、模型、記憶、Skills、對話與排程`;
}
function profRowHtml(p) {
  const acts = p.is_default ? '<small>預設</small>'
    : `<span class="pact" data-pact="rename" data-pid="${esc(p.id)}" title="改名">${ico('edit')}</span>`
      + `<span class="pact" data-pact="del" data-pid="${esc(p.id)}" title="刪除">${ico('trash')}</span>`;
  return `<div class="mi${p.current ? ' on' : ''}" data-pid="${esc(p.id)}" title="${esc(p.is_default ? '預設那組：專案本來的 config.toml、keys.toml 與 data' : p.id)}">`
    + `<span class="pck">${p.current ? ico('check') : ''}</span><span class="pn">${esc(profName(p))}</span>${acts}</div>`;
}
function profMenuHtml(r) {
  const cur = r.current;
  const rows = r.profiles.map(profRowHtml).join('');
  const hint = r.can_switch ? '切換後 WOOF 會立刻重啟，大約幾秒鐘'
    : '瀏覽器開啟時無法自己重啟後端：這裡只會記下來，下次啟動 WOOF 就會用這一組';
  return '<div class="phd">設定組</div>' + rows
    + '<div class="sep"></div><div class="mi" data-pact="new"><span class="pck"></span><span class="pn">新增設定組…</span></div>'
    + `<div class="pnote">${hint}</div>`;
}
async function refreshProf() {
  try {
    const r = await api('GET', '/api/profiles');
    S.profList = r.profiles || [];
    S.profCanSwitch = !!r.can_switch;
    const c = curProf();
    S.profile = { id: r.current, name: profName(c), count: S.profList.length };
  } catch { if (!S.profList) S.profList = []; }
  renderProf();
  if ($('#profMenu').classList.contains('on')) $('#profMenu').innerHTML = profMenuHtml({ current: (S.profile || {}).id, can_switch: S.profCanSwitch, profiles: profList() });
}
function openProfMenu() {
  const menu = $('#profMenu'), anchor = $('#navProf');
  if (menu.classList.contains('on')) { closePops(); return; }
  closePops();
  menu.classList.add('on'); anchor.classList.add('on');
  const side = $('.side').getBoundingClientRect(), a = anchor.getBoundingClientRect();
  menu.style.left = Math.round(a.left - side.left) + 'px';
  menu.style.width = Math.max(200, Math.round(a.width)) + 'px';
  menu.style.bottom = Math.round(side.bottom - a.top + 6) + 'px';
  refreshProf();
}
async function switchProfile(pid) {
  const p = profList().find((x) => x.id === pid);
  if (!p || p.current) { closePops(); return; }
  const ok = await confirmBox(`切換到「${profName(p)}」？`,
    'WOOF 會換成這一組：它有自己的金鑰、模型、記憶、Skills、對話與排程。目前這組的東西不會不見，切回來就還在。'
    + (S.profCanSwitch ? '' : '\n\n（瀏覽器開啟時只能記下來，下次啟動 WOOF 才會生效。）'), { ok: '切換' });
  if (!ok) return;
  closePops();
  try {
    const r = await api('POST', `/api/profiles/${encodeURIComponent(pid)}/switch`);
    if (!r.restart) { notify('ok', '已切換設定組', `現在是「${profName(p)}」`); return refreshProf(); }
    if (D && D.switchProfile) {
      notify('info', '正在切換設定組', `重啟後端並載入「${profName(p)}」…`);
      const res = await D.switchProfile(pid);
      if (res && res.ok === false) notify('error', '切換失敗', res.error || '未知錯誤');
      else setTimeout(() => location.reload(), 1000);   // 後端換好了，畫面重新連一次（視窗通常已經被主程序重載）
    } else {
      notify('ok', '已記下設定組', `重新啟動 WOOF 就會用「${profName(p)}」`);
    }
  } catch (e) { notify('error', '切換失敗', e.message); }
}
async function createProfile() {
  const name = await promptBox('新增設定組', '名稱', '', {
    desc: '新增一組完全獨立的 WOOF：自己的金鑰、模型、記憶、Skills、對話與排程，跟其他組互不共用。',
    placeholder: '例如：工作',
  });
  if (!name) return;
  try {
    const p = await api('POST', '/api/profiles', { name });
    notify('ok', '已新增設定組', `「${p.name}」— 要開始用請點它切換`);
    await refreshProf();
  } catch (e) { notify('error', '新增失敗', e.message); }
}
async function renameProfile(pid) {
  const p = profList().find((x) => x.id === pid);
  if (!p || p.is_default) return;
  const name = await promptBox('設定組改名', '名稱', profName(p));
  if (!name) return;
  try { await api('PATCH', `/api/profiles/${encodeURIComponent(pid)}`, { name }); notify('ok', '已改名'); await refreshProf(); }
  catch (e) { notify('error', '改名失敗', e.message); }
}
async function deleteProfile(pid) {
  const p = profList().find((x) => x.id === pid);
  if (!p || p.is_default) return;
  const ok = await confirmBox(`刪除設定組「${profName(p)}」？`,
    '這一組的 config.toml、keys.toml 與 data 目錄（對話、記憶、Skills、排程紀錄）會整個刪掉，無法復原。', { ok: '刪除', danger: true });
  if (!ok) return;
  try { await api('DELETE', `/api/profiles/${encodeURIComponent(pid)}`); notify('ok', '已刪除設定組'); await refreshProf(); }
  catch (e) { notify('error', '刪除失敗', e.message); }
}
$('#navProf').onclick = openProfMenu;
$('#profMenu').onclick = (e) => {
  const act = e.target.closest('[data-pact]');
  if (act) {
    const a = act.dataset.pact;
    if (a === 'new') return createProfile();
    if (a === 'rename') return renameProfile(act.dataset.pid);
    if (a === 'del') return deleteProfile(act.dataset.pid);
  }
  const row = e.target.closest('[data-pid]');
  if (row) switchProfile(row.dataset.pid);
};

// ------------------------------------------------------------------ 設定
const SECTIONS = [
  ['vendors', '模型商', '服務商、連線方式與模型清單。對話框的模型選單照這裡分組。'],
  ['aux', '模型用途', '主要模型是新對話的預設；其餘每項可以各自指定服務商與模型，留空就跟這個對話正在用的模型。'],
  ['keys', 'API 金鑰', '同一個金鑰池可以放多把 key，一把用完自動換下一把。'],
  ['prompts', '提示詞與記憶', 'WOOF 寫給模型的內部提示詞，以及設定檔、人物檔、使用者檔、記憶檔。'],
  ['flow', '方案模式', '對話框左下「方案・人工／對抗／拓展」的次數上限、審核等多久，以及審核、拓展模型收到的提示詞。'],
  ['styles', '輸出風格', '每個對話可以選一種風格，內容會放進系統提示。'],
  ['pet', '寵物', '陪你聊天的小角色。牠有自己的對話、記憶和角色檔，跟主對話分開；對你的對話只看不改——不會插話、不會用工具、不會動任何檔案。'],
  ['compress', '上下文壓縮', '對話變長時把較早的內容摘要起來，全文仍留在資料庫。'],
  ['perms', '權限', '模型要用工具時，依類別決定直接執行、先問你、或不允許。'],
  ['tools', '工具', '每個工具的總開關（含 Skill、記憶的讀和寫），依插件分組。停用後模型看不到該工具；整個插件的啟停、安裝在「插件」頁，單一對話在「上下文 → 本回合工具」。'],
  ['skills', 'Skills', '有哪些 skill 文件、從哪些資料夾讀、勾選哪些（預設全部關閉）。讀寫的開關在「工具」。'],
  ['presets', '模式預設組', '把「哪些插件可用」和「工具權限」打包成一個模式，一鍵套用。'],
  ['general', '一般', '工作目錄、終端機、可預覽的資料夾，以及執行中按 Enter 的行為。'],
  ['appearance', '外觀', '顏色、縮放與字級。只存在這台電腦，拉了立即生效。'],
  ['mcp', 'MCP 伺服器', 'WOOF 對外提供 MCP 工具，也可以連外部的 MCP 伺服器。'],
  ['snapshots', '檔案還原', '模型改檔前先存一份原檔，之後可以在對話的「檔案」分頁還原。'],
  ['remote', '連線', '目前連到哪一台 WOOF。桌面版可以改連到別台電腦（家裡主機、雲端主機、WSL）；用瀏覽器開遠端網址時就是那台。'],
  ['about', '關於', ''],
];
// 側欄分組：同一類放一個框，項目長短不一也整齊
const SET_GROUPS = [
  ['模型', ['vendors', 'aux', 'keys']],
  ['對話', ['prompts', 'flow', 'styles', 'compress']],
  ['寵物', ['pet']],
  ['能力', ['perms', 'presets', 'tools', 'skills']],
  ['環境', ['general', 'appearance', 'remote']],
  ['進階', ['mcp', 'snapshots', 'about']],
];
async function renderMcpSec(box) {
  box.innerHTML = '<div class="blank">讀取中…</div>';
  let info;
  try { info = await api('GET', '/api/mcp/info'); } catch (e) { box.innerHTML = `<div class="errbox">讀取失敗：${esc(e.message)}</div>`; return; }
  const st = info.running ? '<span class="tag t-ok">執行中</span>' : `<span class="tag t-ex">未啟動</span> ${esc(info.error || '')}`;
  const tools = ['list_sessions', 'get_session', 'list_models', 'list_vendors', 'get_config', 'get_prompts_mode', 'send_message', 'set_prompts_mode', 'set_compress'];
  box.innerHTML = `<div class="mcpst" data-f="mcpInfo">
      <div class="frow"><div><b>狀態</b></div><div>${st}</div></div>
      <div class="frow"><div><b>網址</b></div><div><code>${esc(info.url)}</code></div></div>
      <div class="frow"><div><b>權杖</b><small>外部模型連線時放在 Authorization: Bearer 後面</small></div><div class="mcp-tok"><code id="mcpTok">${esc(info.token)}</code><span class="btn" id="mcpCopy">複製</span></div></div>
      <div class="frow"><div><b>可用工具</b><small>send_message 會跑完整回合並計費，其餘只讀或只改設定；API 金鑰不會經過 MCP</small></div><div class="mcp-tools">${tools.map((t) => `<code>${t}</code>`).join('')}</div></div>

    </div>
    <div class="sec-h">外部 MCP 伺服器（WOOF 當 client 去連）</div>
    <div id="extMcp" data-f="mcpExt"></div>`;
  $('#mcpCopy').onclick = () => { navigator.clipboard.writeText(info.token); notify('ok', '已複製權杖'); };
  renderExternalMcpSec($('#extMcp'));
}
// ---- 排程工作（側欄「排程工作」→ 獨立頁）：左邊清單、右邊詳情與執行記錄
const CRON = { jobs: [], preview: null, sel: localStorage.getItem('woof.cronSel') || '', q: '', runs: {} };
const CRON_WD = ['日', '一', '二', '三', '四', '五', '六'];
function cronWhen(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit' });
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
  if (days === 0) return `今天 ${hm}`;
  if (days === 1) return `明天 ${hm}`;
  if (days === -1) return `昨天 ${hm}`;
  return `${d.getMonth() + 1}/${d.getDate()}（${CRON_WD[d.getDay()]}）${hm}`;
}
function cronDur(a, b) { const s = Math.max(0, Math.round((b || a) - a)); return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`; }
const CRON_ST = { done: ['t-ok', '完成'], error: ['t-ex', '失敗'], stopped: ['t-off', '已停止'], skipped: ['t-off', '錯過略過'] };
// 狀態：執行中／已暫停／上次失敗／啟用
function cronState(j) { return j.running ? 'running' : !j.enabled ? 'paused' : j.last_status === 'error' ? 'error' : 'active'; }
const CRON_STATE = { running: ['t-rl', '執行中'], paused: ['t-off', '已暫停'], error: ['t-ex', '上次失敗'], active: ['t-ok', '啟用'] };
// 範本：點了帶進編輯視窗，改一改就能建立
const CRON_BP = [
  { name: '每日科技新聞', schedule: '30 8 * * *', prompt: '搜尋今天的科技新聞，挑 5 則最重要的，每則用一句話說重點並附上來源連結。' },
  { name: '工作日待辦整理', schedule: '0 9 * * 1-5', prompt: '看工作目錄最近一天修改過的檔案和 TODO.md，列出今天最該優先處理的 3 件事，每件說明原因。' },
  { name: '每週回顧', schedule: '0 18 * * 5', prompt: '整理這一週的對話：完成了哪些事、還有哪些待辦沒做，寫成一份簡短週報。' },
];
async function loadCron() {
  try { CRON.jobs = (await api('GET', '/api/cron')).jobs; } catch (e) { $('#cronBody').innerHTML = `<div class="errbox">讀取失敗：${esc(e.message)}</div>`; return false; }
  const run = CRON.jobs.filter((j) => j.running).length;
  $('#cronBadge').hidden = !run; $('#cronBadge').textContent = run || '';
  return true;
}
async function renderCronView() {
  if (!(await loadCron())) return;
  const n = CRON.jobs.length, on = CRON.jobs.filter((j) => j.enabled).length;
  $('#cronSub').textContent = n ? `共 ${n} 個，${on} 個啟用。到時間就開一個新對話，把提示詞交給模型執行。` : '到時間就開一個新對話，把提示詞交給模型執行。';
  const body = $('#cronBody');
  if (!n) {
    body.className = 'cron-split empty1';
    body.innerHTML = `<div class="cron-empty"><svg><use href="#i-clock"/></svg><b>暫無排程工作</b>
      <span>例如「每天早上 9 點整理今天的待辦」「工作日 18:00 總結今天的 git 提交」。也可以直接在對話裡請模型幫你建立。</span>
      <span class="btn primary" data-cron-new><svg><use href="#i-plus"/></svg>新排程工作</span></div>
      <div class="cron-bp-wrap"><div class="sec">從範本開始</div>${CRON_BP.map((b, i) => `<div class="cron-bp" data-bp="${i}"><b>${esc(b.name)}</b><span>${esc(b.prompt)}</span><code>${esc(b.schedule)}</code></div>`).join('')}</div>`;
    return;
  }
  body.className = 'cron-split';
  if (!CRON.jobs.some((j) => j.id === CRON.sel)) CRON.sel = CRON.jobs[0].id;
  const q = CRON.q.trim().toLowerCase();
  const vis = CRON.jobs.filter((j) => !q || (j.name + ' ' + j.prompt + ' ' + j.label).toLowerCase().includes(q));
  body.innerHTML = `<div class="cron-side">
      <label class="search"><svg><use href="#i-search"/></svg><input id="cronQ" placeholder="搜尋排程" value="${esc(CRON.q)}"></label>
      <div class="cron-items">${vis.map((j) => { const st = cronState(j); return `<div class="cron-item${j.id === CRON.sel ? ' on' : ''}${j.enabled ? '' : ' off'}" data-cid="${esc(j.id)}">
        <i class="dot d-${st}"></i><div><b>${esc(j.name)}</b><small>${esc(j.label)}${j.enabled && !j.running ? ` · 下次 ${cronWhen(j.next_run)}` : ''}</small></div></div>`; }).join('') || '<div class="blank">沒有相符的排程</div>'}
        <div class="cron-add" data-cron-new><svg><use href="#i-plus"/></svg>新排程工作</div>
        <div class="sec">範本</div>${CRON_BP.map((b, i) => `<div class="cron-item bp" data-bp="${i}" title="${esc(b.prompt)}"><svg><use href="#i-bolt"/></svg><div><b>${esc(b.name)}</b></div></div>`).join('')}
      </div></div>
    <div class="cron-detail" id="cronDetail"></div>`;
  renderCronDetail();
}
async function renderCronDetail() {
  const box = $('#cronDetail'); if (!box) return;
  const j = CRON.jobs.find((x) => x.id === CRON.sel); if (!j) { box.innerHTML = ''; return; }
  const m = (S.models || []).find((x) => x.id === j.model);
  const st = CRON_STATE[cronState(j)];
  const meta = [
    ['頻率', `${esc(j.label)} <code>${esc(j.schedule)}</code>`],
    ['下次', j.enabled ? esc(cronWhen(j.next_run)) : '<span class="dim">已暫停</span>'],
    ['上次', j.last_run ? `${esc(cronWhen(j.last_run))}${j.last_status ? ` <span class="tag ${(CRON_ST[j.last_status] || CRON_ST.error)[0]}">${(CRON_ST[j.last_status] || CRON_ST.error)[1]}</span>` : ''}` : '<span class="dim">還沒執行過</span>'],
    ['模型', esc(m ? `${m.label} · ${m.vendor_label}` : j.model || '主要模型')],
    ['錯過的執行', j.catch_up === 'run' ? '開啟後補跑一次' : '略過'],
    ['已執行', `${j.runs || 0} 次`],
  ];
  box.innerHTML = `<div class="cd-hd"><h3>${esc(j.name)}</h3><span class="tag ${st[0]}">${st[1]}</span><span class="grow"></span>
      ${j.running ? `<span class="btn sm" data-cron-open="${esc(j.running_session || '')}"${j.running_session ? '' : ' hidden'}>開啟對話</span><span class="btn sm danger" data-cron-stop><svg><use href="#i-stop"/></svg>停止</span>` : ''}
      <span class="btn sm" data-cron-tog><svg><use href="#i-${j.enabled ? 'pause' : 'play'}"/></svg>${j.enabled ? '暫停' : '繼續'}</span>
      <span class="btn sm primary${j.running ? ' busy' : ''}" data-cron-run><svg><use href="#i-bolt"/></svg>立即觸發</span>
      <span class="ic sm" data-cron-edit title="編輯"><svg><use href="#i-edit"/></svg></span>
      <span class="ic sm danger" data-cron-del title="刪除"><svg><use href="#i-trash"/></svg></span></div>
    <div class="cd-meta">${meta.map(([k, v]) => `<span>${k}</span><div>${v}</div>`).join('')}</div>
    ${j.last_status === 'error' && j.last_error ? `<div class="cd-err"><svg><use href="#i-alert"/></svg><span>上次執行失敗：${esc(j.last_error)}</span>${j.last_session ? `<a data-cron-open="${esc(j.last_session)}">看對話</a>` : ''}</div>` : ''}
    <div class="sec">提示詞</div><div class="cd-prompt">${esc(j.prompt)}</div>
    <div class="sec" id="cdRunsHd">執行記錄</div><div class="cd-runs" id="cdRuns"><div class="dim">讀取中…</div></div>`;
  let runs = [];
  try { runs = (await api('GET', `/api/cron/${j.id}/runs`)).runs; } catch {}
  if (CRON.sel !== j.id || !$('#cdRuns')) return;
  $('#cdRunsHd').textContent = runs.length ? `執行記錄 · ${runs.length}` : '執行記錄';
  const trig = { manual: '手動', schedule: '排程' };
  $('#cdRuns').innerHTML = (j.running ? `<div class="cd-run live"><span class="spin"></span><span>執行中</span><span class="grow"></span>${j.running_session ? `<a data-cron-open="${esc(j.running_session)}">開啟對話</a>` : ''}</div>` : '')
    + (runs.map((r) => { const s = CRON_ST[r.status] || CRON_ST.error; return `<div class="cd-run"><span class="tag ${s[0]}">${s[1]}</span><span class="cd-when">${esc(cronWhen(r.at))}</span>
      ${r.status === 'skipped' ? '<span class="dim">WOOF 沒開，依設定略過</span>' : `<span class="dim">${trig[r.trigger] || ''} · ${cronDur(r.at, r.end)}</span>`}<span class="grow"></span>
      ${r.session ? (r.gone ? '<span class="dim">對話已刪除</span>' : `<a data-cron-open="${esc(r.session)}" title="${esc(r.title || '')}">開啟對話</a>`) : ''}</div>`; }).join('')
    || (j.running ? '' : '<div class="dim">尚無執行</div>'));
}
$('#cronView').addEventListener('click', async (e) => {
  const t = e.target;
  if (t.closest('[data-cron-new]') || t.closest('#cronNew')) { cronEdit(null); return; }
  const bp = t.closest('[data-bp]'); if (bp) { cronEdit(null, CRON_BP[+bp.dataset.bp]); return; }
  const op = t.closest('[data-cron-open]'); if (op && op.dataset.cronOpen) { e.preventDefault(); await openSession(op.dataset.cronOpen); return; }
  const it = t.closest('.cron-item[data-cid]'); if (it) { CRON.sel = it.dataset.cid; localStorage.setItem('woof.cronSel', CRON.sel); $$('.cron-item', $('#cronView')).forEach((x) => x.classList.toggle('on', x === it)); renderCronDetail(); return; }
  const j = CRON.jobs.find((x) => x.id === CRON.sel); if (!j || !t.closest('#cronDetail')) return;
  try {
    if (t.closest('[data-cron-tog]')) { await api('PUT', '/api/cron/' + j.id, { enabled: !j.enabled }); notify('ok', j.enabled ? '排程工作已暫停' : '排程工作已繼續'); }
    else if (t.closest('[data-cron-run]')) { if (j.running) return; await api('POST', `/api/cron/${j.id}/run`); notify('ok', `已觸發「${j.name}」`, '結果會出現在左側 ⏱ 開頭的對話'); }
    else if (t.closest('[data-cron-stop]')) { await api('POST', `/api/cron/${j.id}/stop`); }
    else if (t.closest('[data-cron-edit]')) { cronEdit(j); return; }
    else if (t.closest('[data-cron-del]')) {
      if (!(await modal({ title: '刪除排程工作？', desc: `「${j.name}」會停止執行。已經產生的對話不會刪除。`, ok: '刪除', danger: true }))) return;
      await api('DELETE', '/api/cron/' + j.id); notify('ok', '排程工作已刪除');
    } else return;
    renderCronView();
  } catch (err) { notify('error', '操作失敗', err.message); }
});
$('#cronView').addEventListener('input', (e) => {
  if (e.target.id !== 'cronQ') return;
  CRON.q = e.target.value; const pos = e.target.selectionStart;
  renderCronView().then(() => { const q = $('#cronQ'); if (q) { q.focus(); q.setSelectionRange(pos, pos); } });
});
function openCron() { showView('cron'); renderCronView(); }
// 排程表單：預設集合（每 15 分鐘／每小時／每天／每週／每月／自訂）→ cron 表達式
function cronParseExpr(x) {
  const p = (x || '').split(/\s+/); const f = { kind: 'custom', expr: x || '', time: '09:00', min: 0, wd: 1, dom: 1 };
  if (p.length !== 5) return f;
  const [mi, h, d, mo, w] = p, hm = () => `${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`;
  if (x === '*/15 * * * *') f.kind = 'every15';
  else if (/^\d+$/.test(mi) && h === '*' && d === '*' && mo === '*' && w === '*') { f.kind = 'hourly'; f.min = +mi; }
  else if (/^\d+$/.test(mi) && /^\d+$/.test(h) && mo === '*') {
    if (d === '*' && w === '*') { f.kind = 'daily'; f.time = hm(); }
    else if (d === '*' && /^\d$/.test(w)) { f.kind = 'weekly'; f.time = hm(); f.wd = +w % 7; }
    else if (/^\d+$/.test(d) && w === '*') { f.kind = 'monthly'; f.time = hm(); f.dom = +d; }
  }
  return f;
}
function cronBuild(f) {
  const [h, m] = (f.time || '09:00').split(':').map((x) => +x || 0);
  return { every15: '*/15 * * * *', hourly: `${+f.min || 0} * * * *`, daily: `${m} ${h} * * *`, weekly: `${m} ${h} * * ${f.wd}`, monthly: `${m} ${h} ${f.dom} * *`, custom: f.expr }[f.kind];
}
async function cronEdit(job, preset) {
  const src = job || preset || null;
  const f = cronParseExpr(src ? src.schedule : '0 9 * * *');
  let model = job ? job.model : '', catchUp = job ? job.catch_up : 'skip';
  const kinds = [['every15', '每 15 分鐘'], ['hourly', '每小時'], ['daily', '每天'], ['weekly', '每週'], ['monthly', '每月'], ['custom', '自訂']];
  const body = `<div class="cron-form">
    <label><span>名稱</span><input id="cfName" maxlength="80" placeholder="例如：每日摘要" value="${esc(src ? src.name : '')}"></label>
    <label><span>提示詞</span><textarea id="cfPrompt" rows="5" placeholder="模型每次執行時要做什麼？寫成完整的指令。">${esc(src ? src.prompt : '')}</textarea></label>
    <div class="cf-row"><span>排程</span><div class="cf-sched">
      <div class="seg" id="cfKind">${kinds.map(([k, n]) => `<b data-k="${k}" class="${f.kind === k ? 'on' : ''}">${n}</b>`).join('')}</div>
      <div class="cf-detail" id="cfDetail"></div>
      <div class="cf-prev" id="cfPrev"></div></div></div>
    <div class="cf-row"><span>模型</span><div>${mpPairHtml('cron', model, { blank: '主要模型', set: (v) => { model = v; } })}</div></div>
    <div class="cf-row"><span>錯過的執行</span><div class="seg" id="cfCatch"><b data-c="skip" class="${catchUp !== 'run' ? 'on' : ''}">略過</b><b data-c="run" class="${catchUp === 'run' ? 'on' : ''}">開啟後補跑一次</b></div></div>
  </div>`;
  let timer = 0;
  const detail = (m) => {
    const d = $('#cfDetail', m), t = `<input type="time" id="cfTime" value="${esc(f.time)}">`;
    d.innerHTML = {
      every15: '<span class="dim">每個 :00、:15、:30、:45</span>',
      hourly: `<span>每小時的第</span><input type="number" id="cfMin" min="0" max="59" value="${f.min}"><span>分</span>`,
      daily: `<span>每天</span>${t}`,
      weekly: `<span>每週</span><select id="cfWd">${CRON_WD.map((w, i) => `<option value="${i}"${f.wd === i ? ' selected' : ''}>週${w}</option>`).join('')}</select>${t}`,
      monthly: `<span>每月</span><input type="number" id="cfDom" min="1" max="31" value="${f.dom}"><span>日</span>${t}`,
      custom: `<input id="cfExpr" class="mono" placeholder="分 時 日 月 週，例如 30 8 * * 1-5" value="${esc(f.expr)}">`,
    }[f.kind];
    preview(m);
  };
  const preview = (m) => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const expr = cronBuild(f), el = $('#cfPrev', m); if (!el) return;
      const r = await api('POST', '/api/cron/preview', { schedule: expr }).catch((e) => ({ ok: false, error: e.message }));
      CRON.preview = r;
      el.className = 'cf-prev' + (r.ok ? '' : ' bad');
      el.innerHTML = r.ok ? `<code>${esc(expr)}</code><span>${esc(r.label)}</span><span class="dim">接下來：${r.next.map(cronWhen).join('、')}</span>` : esc(r.error);
    }, 180);
  };
  await modal({
    title: job ? '編輯排程工作' : '新排程工作', wide: true, ok: job ? '儲存變更' : '建立排程工作', body,
    desc: job ? '變更從下次執行開始生效。' : '到時間就開一個新對話，把提示詞交給模型執行。',
    onOpen: (m) => {
      detail(m);
      m.addEventListener('click', (e) => {
        const k = e.target.closest('#cfKind [data-k]'); if (k) { f.kind = k.dataset.k; if (f.kind === 'custom' && !f.expr) f.expr = cronBuild({ ...f, kind: 'daily' }); $$('#cfKind b', m).forEach((b) => b.classList.toggle('on', b === k)); detail(m); }
        const c = e.target.closest('#cfCatch [data-c]'); if (c) { catchUp = c.dataset.c; $$('#cfCatch b', m).forEach((b) => b.classList.toggle('on', b === c)); }
      });
      m.addEventListener('input', (e) => {
        const id = e.target.id;
        if (id === 'cfTime') f.time = e.target.value || '09:00';
        else if (id === 'cfMin') f.min = Math.max(0, Math.min(59, +e.target.value || 0));
        else if (id === 'cfDom') f.dom = Math.max(1, Math.min(31, +e.target.value || 1));
        else if (id === 'cfWd') f.wd = +e.target.value;
        else if (id === 'cfExpr') f.expr = e.target.value.trim();
        else return;
        preview(m);
      });
      m.addEventListener('change', (e) => { if (e.target.id === 'cfWd') { f.wd = +e.target.value; preview(m); } });
    },
    onOk: async (m) => {
      const payload = { name: $('#cfName', m).value.trim(), prompt: $('#cfPrompt', m).value.trim(), schedule: cronBuild(f), model, catch_up: catchUp };
      if (!payload.prompt) { notify('warn', '提示詞是必填'); $('#cfPrompt', m).focus(); return false; }
      if (job) await api('PUT', '/api/cron/' + job.id, payload);
      else { const r = await api('POST', '/api/cron', { ...payload, enabled: true }); CRON.sel = r.id; localStorage.setItem('woof.cronSel', r.id); }
      notify('ok', job ? '排程工作已更新' : '排程工作已建立');
      return true;
    },
  });
  if ($('#cronView').classList.contains('on')) renderCronView();
}
// 搜尋用：每頁在 S.setQ 有值時顯示這裡的項目，點一下跳到對應分頁並捲到該項
const SET_Q = {
  vendors: [
    ['vlist', '模型商與模型清單', '服務商 廠商 供應商 位址 base_url 金鑰池 連線方式 驗證 思考參數 顯示名稱 上下文 單價 看圖 思考強度 預設'],
    ['vgal', '新增模型商', '範本 新增 加入 OpenRouter DeepSeek OpenAI 本機 Ollama'],
  ],
  aux: [
    ['primary', '主要模型', '新對話 預設模型'],
    ['aux:fallback', '備用模型', '自動切換 限流 額度用完 備援'],
    ['aux:vision', '看圖', '圖片 視覺 圖片分析 vision_analyze'],
    ['aux:compact', '壓縮', '上下文 摘要 摘要模型'],
    ['aux:subagent', '子代理', 'delegate_task 分公司'],
    ['aux:image', '生圖', '圖片生成 image 繪圖'],
    ['aux:video', '生影片', '影片生成 video 動畫'],
    ['aux:review', '自動審核', '審核 權限 放行 先問我'],
    ['auxFollow', '全部跟隨主模型', ''],
  ],
  prompts: [
    ['pmode', '內部提示詞模式', '精簡 常規 提示詞 系統'],
    ['ipBox', '內部提示詞', '審核 規劃 接續 壓縮 看圖 還原預設'],
    ['pfBox', '設定檔・人物檔・使用者檔・記憶檔', 'instructions SOUL USER MEMORY 記憶 人格'],
  ],
  styles: [['styList', '輸出風格', '風格 語氣 內建 自訂 置頂 新增']],
  compress: [
    ['c-window', '上下文大小', 'context window token 上限 多少'],
    ['c-trigger', '壓縮門檻', '門檻 百分比 觸發 何時壓縮'],
    ['c-keep', '保留最近幾輪', '保留 輪 最近'],
    ['c-ratio', '摘要幅度', '摘要 比例 長度'],
    ['c-method', '壓縮方式', '單次 多工 一次摘要'],
    ['c-parallel', '同時跑幾段', '多工 平行 段數'],
    ['cSave', '儲存壓縮設定', '儲存 立即壓縮'],
  ],
  perms: [
    ['pmTable', '各模式的權限', '權限 模式 工具 類別 自動 先問 禁止 自訂'],
    ['pmDefault', '新對話的預設權限', '預設 新對話'],
    ['pmDanger', '危險指令先問', '危險 rm -rf 格式化'],
    ['pmHard', '絕對禁止', '禁止 擋下 黑名單'],
    ['pmAllow', '指令白名單', '白名單 允許 直接執行'],
    ['pmDeny', '指令黑名單', '黑名單 禁止 擋下'],
    ['pmHeadless', '沒人在旁邊時', '排程 沒人 自動 定時'],
    ['pmTry', '試試看', '測試 檢查 指令'],
  ],
  tools: [['tAll', '工具', '工具 開關 停用 啟用 萬用 插件']],
  skills: [
    ['skPerm', 'Skills 權限', 'skills 讀取 寫入 資料夾 技能'],
    ['skList', 'Skills 清單', 'skills 技能 勾選 分類'],
  ],
  keys: [
    ['kSafe', '金鑰保護', '加密 遮罩 redact 保護 明文'],
    ['kList', 'API 金鑰', 'key 金鑰池 模型商 搜尋 工具 firecrawl tavily'],
    ['kAdd', '新增金鑰池', '新增 金鑰池'],
  ],
  snapshots: [
    ['snpUse', '檔案還原用量', '快照 還原 容量 備份'],
    ['snpSet', '還原設定', '保留 天數 上限 自動存 整理'],
  ],
  general: [
    ['gBusy', '執行中按 Enter', '打斷 插話 排隊 interrupt steer queue'],
    ['gWorkdir', '預設工作目錄', '工作目錄 資料夾 路徑'],
    ['gTerm', '終端機', 'shell 終端機 Git Bash PowerShell 命令提示字元'],
    ['gPreview', '可預覽資料夾', '預覽 圖片 資料夾 allowed_dirs'],
    ['gCost', '花費與預算', '花費 預算 金額 上限 單價 計費'],
    ['gFiles', '設定檔與資料夾', 'config.toml keys.toml 資料夾 開發者工具 console'],
  ],
  appearance: [
    ['aLang', '介面語言', '語言 中文 English 英文'],
    ['aMode', '色彩模式', '淺色 深色 系統 主題'],
    ['aPreset', '配色', '配色 主題 色系 預設'],
    ['aBase', '整體底色', '色相 濃度 背景 顏色'],
    ['aArea', '各區域', '色相 濃度 主畫面 側欄 卡片 程式碼'],
    ['aAccent', '強調色', '強調色 顏色 按鈕 連結'],
    ['aSize', '介面縮放與字級', '縮放 字級 大小 字體'],
  ],
  mcp: [
    ['mcpInfo', 'WOOF 的 MCP 服務', 'MCP 網址 權杖 token 對外'],
    ['mcpExt', '外部 MCP 伺服器', 'MCP 外部 伺服器 stdio http 新增'],
  ],
  about: [['aboutAll', '版本與捷徑', '版本 關於 更新 捷徑 桌面']],
};
function setFind(q) {
  const s = String(q || '').trim().toLowerCase();
  const out = [];
  if (!s) return out;
  SECTIONS.forEach(([sec, name]) => {
    (SET_Q[sec] || []).forEach(([id, t, kw]) => {
      if ((t + ' ' + kw).toLowerCase().includes(s)) out.push({ sec, secName: name, id, t });
    });
  });
  return out;
}
function setFindClear() {
  S.setQ = '';
  const i = $('#setQ'); if (i) i.value = '';
}
// 搜尋結果：列出所有分頁裡符合的設定項，點一下跳過去
function renderSetSearch(q) {
  const hits = setFind(q);
  $('#setTitle').hidden = false;
  $('#setTitle').textContent = '搜尋設定';
  $('#setIntro').hidden = false;
  $('#setIntro').textContent = hits.length ? `「${q}」找到 ${hits.length} 項，點一下跳過去。` : `「${q}」沒有符合的設定。換個說法試試，例如「壓縮」「金鑰」「權限」。`;
  const box = $('#setList');
  box.className = 'sec-search';
  box.innerHTML = hits.length
    ? `<div class="card setq-list">${hits.map((h) => `<div class="setq-item" data-qsec="${esc(h.sec)}" data-qid="${esc(h.id)}"><div><b>${esc(h.t)}</b><small>${esc(h.secName)}</small></div><span class="setq-go">前往</span></div>`).join('')}</div>`
    : '<div class="blank">沒有符合的設定項。</div>';
  box.onclick = (e) => {
    const it = e.target.closest('[data-qid]'); if (!it) return;
    setFindClear();
    openSettings(it.dataset.qsec, it.dataset.qid);
  };
}
async function openSettings(sec, focus) {
  if (sec) S.setSec = sec;
  if (!SECTIONS.some((x) => x[0] === S.setSec)) S.setSec = SECTIONS[0][0];
  showView('set');
  if (S.dirty) await flushSave();
  if (!S.cfg || !S.dirty) await loadCfg();
  renderSettings();
  if (focus) setFocus(focus);
}
// 跳到某一項：等該分頁畫出來（有的分頁要先讀 API），再捲過去並閃一下
async function setFocus(id, tries) {
  for (let i = 0; i < (tries || 20); i++) {
    const el = document.querySelector(`#setList [data-f="${id}"]`);
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.remove('setflash');
      void el.offsetWidth;
      el.classList.add('setflash');
      setTimeout(() => el.classList.remove('setflash'), 1400);
      const c = el.querySelector('input:not([type="range"]), select, textarea');
      if (c && window.innerWidth > 900) c.focus({ preventScroll: true });
      return true;
    }
    await new Promise((r) => setTimeout(r, 60));
  }
  return false;
}
async function loadCfg() {
  const c = await api('GET', '/api/settings');
  S.cfg = {
    workdir: c.workdir || '', allowed_dirs: c.allowed_dirs || [], terminal_shell: c.terminal_shell || 'auto',
    vendors: (c.vendors || []).map((v) => ({ ...v })), models: (c.models || []).map((m) => ({ ...m })),
    keys: (c.keys || []).map((k) => ({ ...k, key: '', orig: k.label })),
    aux: { ...(c.aux || {}) },
    default_model: c.default_model || '',
    discovery: c.discovery || {},
    prompts: { ...(c.prompts || { mode: 'normal' }) },
    flow: { ...(c.flow || {}) },
    redact_secrets: c.redact_secrets !== false, keys_encrypted: !!c.keys_encrypted,
    cost_on: !!c.cost_on,
  };
  S.cfgBackends = c.backends || S.backends;
  setDirty(false);
}
// 設定改了就自動存（停手 0.7 秒後送出）；要用到已存設定的動作（測試、取得模型、檢索）先 flushSave()
function setDirty(v) {
  S.dirty = v;
  clearTimeout(S.saveTimer);
  if (v) { setSaveState('wait'); S.saveTimer = setTimeout(() => saveSettings({ auto: true }), 700); }
}
function setSaveState(st, msg) {
  const el = $('#setSaved'); if (!el) return;
  el.className = 'savest ' + st;
  el.textContent = st === 'wait' ? '尚未儲存…' : st === 'busy' ? '儲存中…' : st === 'err' ? `沒存到：${msg || ''}` : st === 'ok' ? '已自動儲存' : '';
  el.title = st === 'err' ? (msg || '') : '設定改了就會自動儲存';
}
async function flushSave() {
  clearTimeout(S.saveTimer);
  if (S.savingP) await S.savingP.catch(() => {});
  if (S.dirty) await saveSettings({ auto: true });
}
function renderSettings() {
  const byId = Object.fromEntries(SECTIONS.map((s) => [s[0], s]));
  $('#setNav').innerHTML = '<div class="snh">設定</div>' + SET_GROUPS.map(([g, ids]) => `<div class="sng"><div class="sng-h">${g}</div>${
    ids.filter((id) => byId[id]).map((id) => `<div class="${S.setSec === id ? 'on' : ''}" data-sec="${id}">${byId[id][1]}</div>`).join('')}</div>`).join('');
  const q = String(S.setQ || '').trim();
  if (q) { renderSetSearch(q); return; }
  const sec = SECTIONS.find((s) => s[0] === S.setSec) || SECTIONS[0];
  $('#setTitle').textContent = sec[1];
  $('#setIntro').textContent = sec[2];
  $('#setIntro').hidden = !sec[2];
  $('#setTitle').hidden = sec[0] === 'about';
  const box = $('#setList');
  box.onclick = null;
  box.className = 'sec-' + sec[0];
  ({ mcp: renderMcpSec, aux: renderAux, compress: renderCompressSec, prompts: renderPromptsSec, flow: renderFlowSec, vendors: renderVendors, keys: renderKeyEditor, appearance: renderAppearance, general: renderGeneral, perms: renderPermsSec, presets: renderPresetsSec, remote: renderRemoteSec, about: renderAbout, styles: renderStylesSec, pet: renderPetSec, snapshots: renderSnapshotsSec, tools: renderToolsSet, skills: renderSkillsSet })[sec[0]](box);}
$('#setNav').onclick = (e) => { const d = e.target.closest('[data-sec]'); if (!d) return; setFindClear(); S.setSec = d.dataset.sec; localStorage.setItem('woof.setSec', S.setSec); renderSettings(); };
$('#setQ').oninput = (e) => { S.setQ = e.target.value; renderSettings(); };
$('#setQ').onkeydown = (e) => {
  if (e.key === 'Escape') { setFindClear(); renderSettings(); e.target.blur(); }
  if (e.key === 'Enter') { const f = $('.setq-item', $('#setList')); if (f) f.click(); }
};
window.addEventListener('beforeunload', () => { if (S.dirty) saveSettings({ auto: true }); });

// ---- 權限：各模式 × 類別的表、危險指令、黑白名單、排程、試試看
const DEC = { auto: ['t-ok', '自動'], ask: ['t-rl', '先問'], deny: ['t-ex', '禁止'] };
async function renderPermsSec(box) {
  box.innerHTML = '<div class="blank">讀取中…</div>';
  let P_; try { P_ = await api('GET', '/api/permissions'); } catch (e) { box.innerHTML = `<div class="errbox">讀取失敗：${esc(e.message)}</div>`; return; }
  const st = P_.settings, modes = Object.keys(P_.modes), now = currentPerm();
  const cell = (m, c) => {
    const d = m === 'custom' ? st.custom[c] : P_.table[m][c];
    const [cls, txt] = DEC[d] || DEC.ask;
    return m === 'custom' ? `<td class="pm-c"><span class="tag ${cls} pm-edit" data-pc="${c}" title="點一下切換：自動 → 先問 → 禁止">${txt}</span></td>` : `<td><span class="tag ${cls}">${txt}</span></td>`;
  };
  box.innerHTML = `<div class="card pm-card" data-f="pmTable"><div class="card-hd"><b>各模式的權限</b><small>欄是模式、列是工具類別。目前這個對話用的是「${esc(PERM[now][0])}」。自訂欄可以點標籤切換。</small></div>
    <div class="pm-wrap"><table class="pm-tb"><thead><tr><th></th>${modes.map((m) => `<th class="${m === now ? 'cur' : ''}">${esc(P_.modes[m][0])}</th>`).join('')}</tr></thead>
    <tbody>${Object.entries(P_.cats).map(([c, [n, d]]) => `<tr><th><b>${esc(n)}</b><small>${esc(d)}</small></th>${modes.map((m) => cell(m, c)).join('')}</tr>`).join('')}</tbody></table></div></div>
    <div class="card">
      <div class="frow" data-f="pmDefault"><div><b>新對話的預設權限</b><small>開新對話時權限鈕的起始值，之後每個對話可以各自切換。</small></div><div class="fctl"><div class="seg">${modes.filter((m) => m !== 'custom').map((m) => `<b data-pdm="${m}" class="${st.default_mode === m ? 'on' : ''}">${esc(P_.modes[m][0])}</b>`).join('')}</div></div></div>
      <div class="frow" data-f="pmDanger"><div><b>危險指令先問</b><small>在會自動執行的模式裡，碰到下面這些指令改成先問你。關掉就照模式直接執行。</small></div><div class="fctl"><span class="sw${st.danger_ask ? ' on' : ''}" data-pk="danger_ask"></span></div></div>
      <div class="pm-chips">${P_.danger.map((x) => `<span>${esc(x)}</span>`).join('')}</div>
      <div class="frow" data-f="pmHard"><div><b>絕對禁止</b><small>任何模式都擋，按「允許」也沒用。</small></div><div></div></div>
      <div class="pm-chips bad">${P_.hardline.map((x) => `<span>${esc(x)}</span>`).join('')}</div>
    </div>
    <div class="card">
      <div class="frow" data-f="pmAllow"><div><b>指令白名單</b><small>開頭符合就直接執行，不問（例如 <code>git status</code>、<code>ls</code>）。指令裡有 &amp;&amp;、;、| 串接時不適用。一行一個。</small></div><textarea class="mono" rows="4" data-pl="allow_cmds" placeholder="git status&#10;git diff&#10;ls">${esc(st.allow_cmds.join(NL))}</textarea></div>
      <div class="frow" data-f="pmDeny"><div><b>指令黑名單</b><small>指令裡出現這些字就擋下，任何模式都一樣。一行一個。</small></div><textarea class="mono" rows="3" data-pl="deny_cmds" placeholder="git push&#10;npm publish">${esc(st.deny_cmds.join(NL))}</textarea></div>
      <div class="frow" data-f="pmAskWait"><div><b>問你問題時等多久</b><small>模型用提問卡片問你時，這段時間沒回就自動略過，並告訴模型你可能不在，讓它自己判斷繼續（收不回來的事會留到最後再問）。在卡片上打字會重新計時。0＝一直等。</small></div><div class="fctl"><label class="cin"><input type="number" data-pat min="0" max="86400" step="30" value="${st.ask_timeout}"><span>秒</span></label></div></div>
      <div class="frow" data-f="pmHeadless"><div><b>沒人在旁邊時</b><small>排程跑的對話碰到要先問的工具，沒有人能按允許。</small></div><div class="fctl"><div class="seg"><b data-ph="deny" class="${st.headless === 'deny' ? 'on' : ''}">擋下</b><b data-ph="approve" class="${st.headless === 'approve' ? 'on' : ''}">直接放行</b></div></div></div>
    </div>
    <div class="card" data-f="pmTry"><div class="card-hd"><b>試試看</b><small>輸入一個指令，看在各模式下會怎樣。</small></div>
      <div class="pm-try"><input class="mono" id="pmCmd" placeholder="例如 rm -rf build" value="git push --force"><span class="btn sm" id="pmTry">檢查</span></div>
      <div class="pm-res" id="pmRes"></div></div>`;
  const save = async (patch, msg) => {
    try { await api('POST', '/api/permissions', patch); notify('ok', msg || '權限設定已儲存'); } catch (e) { notify('error', '儲存失敗', e.message); }
  };
  const tryIt = async () => {
    const cmd = $('#pmCmd', box).value.trim(); if (!cmd) return;
    const rs = await Promise.all(modes.map((m) => api('POST', '/api/permissions/check', { mode: m, tool: 'terminal', command: cmd })));
    $('#pmRes', box).innerHTML = rs.map((r, i) => { const [cls, txt] = DEC[r.decision]; return `<div class="pm-r"><b>${esc(P_.modes[modes[i]][0])}</b><span class="tag ${cls}">${txt}</span><small>${esc(r.reason)}</small></div>`; }).join('');
  };
  $('#pmTry', box).onclick = tryIt;
  $('#pmCmd', box).onkeydown = (e) => { if (e.key === 'Enter') tryIt(); };
  tryIt();
  box.onclick = async (e) => {
    const pc = e.target.closest('[data-pc]');
    if (pc) { const order = ['auto', 'ask', 'deny']; const c = pc.dataset.pc; st.custom[c] = order[(order.indexOf(st.custom[c]) + 1) % 3]; await save({ custom: st.custom }, '自訂權限：' + P_.cats[c][0] + ' → ' + DEC[st.custom[c]][1]); renderPermsSec(box); return; }
    const pk = e.target.closest('[data-pk]');
    if (pk) { pk.classList.toggle('on'); await save({ danger_ask: pk.classList.contains('on') }); tryIt(); return; }
    const ph = e.target.closest('[data-ph]');
    if (ph) { $$('[data-ph]', box).forEach((x) => x.classList.toggle('on', x === ph)); await save({ headless: ph.dataset.ph }); }
    const pdm = e.target.closest('[data-pdm]');
    if (pdm) { $$('[data-pdm]', box).forEach((x) => x.classList.toggle('on', x === pdm)); await save({ default_mode: pdm.dataset.pdm }); S.defaultPerm = pdm.dataset.pdm; renderPerm(); }
  };
  const pat = $('[data-pat]', box);
  if (pat) pat.onchange = async () => { const v = Math.max(0, Math.min(86400, Math.round(+pat.value || 0))); pat.value = v; await save({ ask_timeout: v }, v ? `提問等 ${askSpan(v)}沒回就略過` : '提問會一直等你回答'); };
  $$('[data-pl]', box).forEach((ta) => { ta.onchange = async () => { await save({ [ta.dataset.pl]: ta.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean) }); tryIt(); }; });
}

// ---- 提示詞與記憶：內部提示詞模式＋設定檔、人物檔、使用者檔、記憶檔（直接在這裡編輯）
const PF = { key: 'instructions', files: null, drafts: {} };
async function pfLoad() { const r = await api('GET', '/api/prompt-files'); PF.files = r.files; }
function pfStash() { const ta = $('#pfText'); if (ta && PF.files) PF.drafts[PF.key] = ta.value; }
function memOn(key) {
  const p = (S.plugins || []).find((x) => x.name === 'memory') || {};
  const v = (p.settings || {})[key];
  return v === undefined || v === null ? true : !!v;
}
function renderPromptsSec(box) {
  IP.only = null;
  const mode = (S.cfg.prompts && S.cfg.prompts.mode) || 'normal';
  const seg = (v, n) => `<b data-pmode="${v}" class="${mode === v ? 'on' : ''}">${n}</b>`;
  const PM = { lean: ['精簡', '只留必要指令，不寫語氣與舉例。', '審核只看有沒有證據，明顯沒做才不通過。'],
    normal: ['常規', '判準說清楚，但不冗長。預設。', '允許措辭與排版小瑕疵，不因不完美就判不通過。'] };
  box.innerHTML = `<div class="card">
    <div class="frow" data-f="pmode"><div><b>內部提示詞模式</b><small>子代理、審核、規劃、預案、看圖給模型的指令。改了就自動儲存，下一則訊息起生效。</small></div>
      <div class="fctl"><div class="seg">${seg('lean', '精簡')}${seg('normal', '常規')}</div></div></div>
    <div class="pm-ex"><b>${PM[mode][0]}</b><span>${PM[mode][1]}</span><small>例：${PM[mode][2]}</small></div>
  </div>
  <div class="card ip-card" data-f="ipBox">
    <div class="card-hd"><b>內部提示詞</b><small>WOOF 自己寫給模型的每一段話：審核、規劃、接續、壓縮、看圖。點開可以看全文、直接改（自動儲存），改壞了按「還原預設」。主模型的系統提示詞在對話的「上下文 → 總提示詞」。</small></div>
    <div id="ipBox"><div class="blank">讀取中…</div></div>
  </div>
  <div class="card pf-card" data-f="pfBox">
    <div class="card-hd"><b>設定檔・人物檔・使用者檔・記憶檔</b><small>存在 data/prompts/，每個對話都會把它們放進系統提示。</small></div>
    <div class="frow pf-memsw"><div><b>記憶放進提示詞（讀）</b><small>記憶檔、使用者檔會放進系統提示。關掉只是模型看不到內容——檔案不會被刪，你在下面照樣改得動。</small></div><div class="fctl"><span class="sw${memOn('allow_read') ? ' on' : ''}" data-memsw="allow_read"></span></div></div>
    <div class="frow pf-memsw"><div><b>讓模型自己寫記憶</b><small>就是 memory_write 工具，在<span class="genp-doc" data-goset="tools">「設定 → 工具」</span>開關（單一對話用「上下文 → 本回合工具」）。</small></div><div class="fctl"></div></div>
    <div id="pfBox"><div class="blank">讀取中…</div></div>
  </div>`;
  box.oninput = (e) => {
    const ipt = e.target.closest('[data-ipedit]');
    if (ipt) { ipQueue(ipt.dataset.ipedit, ipt.value); return; }
    if (e.target.id !== 'pfText') return;
    PF.drafts[PF.key] = e.target.value;
    const d = $('#pfDirty'); if (d) d.textContent = '尚未儲存…';
    clearTimeout(PF.timer); PF.timer = setTimeout(() => pfSave(true), 900);
  };
  box.onclick = async (e) => {
    const go = e.target.closest('[data-goset]');
    if (go) { openSettings(go.dataset.goset); return; }
    const ms = e.target.closest('[data-memsw]');
    if (ms) {
      const key = ms.dataset.memsw, on = !ms.classList.contains('on');
      ms.classList.toggle('on', on);
      try {
        const row = await api('POST', '/api/plugins/memory', { settings: { [key]: on } });
        const p = (S.plugins || []).find((x) => x.name === 'memory');
        if (p && row) p.settings = row.settings || p.settings;
        notify('ok', on ? '已允許' : '已關掉');
      } catch (err) {
        ms.classList.toggle('on', !on);
        notify('error', '儲存失敗', err.message);
      }
      return;
    }
    const b = e.target.closest('[data-pmode]');
    if (b) { pfStash(); S.cfg.prompts = { mode: b.dataset.pmode }; setDirty(true); renderPromptsSec(box); return; }
    const t = e.target.closest('[data-pf]');
    if (t) { pfStash(); if (PF.timer) { clearTimeout(PF.timer); PF.timer = null; if (PF.drafts[PF.key] !== undefined) pfSave(true); } PF.key = t.dataset.pf; pfRender(); return; }
    const ipr = e.target.closest('[data-iprow]');
    if (ipr && !e.target.closest('.ip-body')) { const k = ipr.dataset.iprow; IP.open.has(k) ? IP.open.delete(k) : IP.open.add(k); ipRender(); return; }
    const rs = e.target.closest('[data-ipreset]');
    if (rs) { ipReset(rs.dataset.ipreset); return; }
  };
  box.onchange = async (e) => {
    const ipt = e.target.closest('[data-pflim]');
    if (!ipt) return;
    const v = Math.round(Number(ipt.value) || 0);
    const f = PF.files.find((x) => x.key === PF.key);
    try {
      const r = await api('PUT', `/api/prompt-files/${PF.key}/limit`, { limit: v });
      if (f && r) Object.assign(f, r);
      notify('ok', `${f ? f.label : '上限'}改成 ${v} 字`);
    } catch (err) {
      notify('error', '上限沒存到', err.message);
    }
    pfRender();
  };
  pfLoad().then(pfRender).catch((e) => { const x = $('#pfBox'); if (x) x.innerHTML = `<div class="errbox">${esc(e.message)}</div>`; });
  ipLoad();
}
// ---- 方案模式：次數上限、審核時間上限、/goal 要不要跟著模式走，以及審核／拓展的提示詞（跟「內部提示詞」同一份覆寫）
const FLOW_PROMPTS = ['plan_review', 'review', 'expand', 'plan_goal', 'plan_redo', 'review_fail'];
function renderFlowSec(box) {
  IP.only = FLOW_PROMPTS;
  const f = S.cfg.flow = Object.assign({ plan_rounds: 3, fix_rounds: 1, review_timeout: 300, goal_gate: true }, S.cfg.flow || {});
  const num = (k, min, max, step, unit) => `<label class="cin"><input type="number" data-fl="${k}" min="${min}" max="${max}" step="${step}" value="${f[k]}"><span>${unit}</span></label>`;
  const row = (id, title, hint, ctl) => `<div class="frow" data-f="${id}"><div><b>${title}</b><small>${hint}</small></div><div class="fctl">${ctl}</div></div>`;
  const worst = f.plan_rounds + 1 + f.fix_rounds;
  box.innerHTML = `<div class="card">
    ${row('flRounds', '方案最多提幾版', '對抗審核打回、或你按「換個做法」時重提。到上限還沒過：一般對話問你要不要照最後一版做；/goal 不停下來，直接照最後一版做。', num('plan_rounds', 1, 10, 1, '版'))}
    ${row('flFix', '做完驗收後最多補幾輪', '審核模型驗收發現缺口時補做幾輪，每補一輪再驗一次。0＝不補，只把缺口告訴你。', num('fix_rounds', 0, 5, 1, '輪'))}
    ${row('flWait', '每次審核／拓展最多等多久', '審核或拓展的子代理超過這個時間還沒結論，就不等了，照目前這版繼續。0＝一直等。', num('review_timeout', 0, 3600, 30, '秒'))}
    ${row('flGoal', '/goal 也照對話選的模式走', '開著：對話選了方案模式時，/goal 先審核或拓展再做；放著讓它自己跑，打回到上限或你沒回應都照最後一版做。關掉：/goal 直接拆步驟做，做完一樣驗收。', `<span class="sw${f.goal_gate ? ' on' : ''}" data-flgoal></span>`)}
    <div class="pm-ex"><span>依目前設定，一個方案最多審核 ${f.plan_rounds} 次、驗收 ${worst - f.plan_rounds} 次；加上等待上限，不會無限來回。</span></div>
  </div>
  <div class="card ip-card">
    <div class="card-hd"><b>審核與拓展的提示詞</b><small>點開可以直接改（自動儲存），改壞了按「還原預設」。跟「提示詞與記憶 → 內部提示詞」是同一份。</small></div>
    <div id="ipBox"><div class="blank">讀取中…</div></div>
  </div>`;
  box.oninput = (e) => { const ipt = e.target.closest('[data-ipedit]'); if (ipt) ipQueue(ipt.dataset.ipedit, ipt.value); };
  box.onchange = (e) => {
    const el = e.target.closest('[data-fl]'); if (!el) return;
    const [lo, hi] = [+el.min, +el.max]; const v = Math.max(lo, Math.min(hi, Math.round(Number(el.value) || 0)));
    el.value = v; f[el.dataset.fl] = v; setDirty(true); renderFlowSec(box);
  };
  box.onclick = (e) => {
    const g = e.target.closest('[data-flgoal]');
    if (g) { f.goal_gate = !f.goal_gate; g.classList.toggle('on', f.goal_gate); setDirty(true); return; }
    const ipr = e.target.closest('[data-iprow]');
    if (ipr && !e.target.closest('.ip-body')) { const k = ipr.dataset.iprow; IP.open.has(k) ? IP.open.delete(k) : IP.open.add(k); ipRender(); return; }
    const rs = e.target.closest('[data-ipreset]');
    if (rs) ipReset(rs.dataset.ipreset);
  };
  if (IP.data) ipRender(); else ipLoad();
}
// ---- 內部提示詞：woof/prompts.py 的全部段落，逐段檢視、覆寫（data/prompt_overrides.json）
const IP = { data: null, open: new Set(), timers: {} };
async function ipLoad() {
  try { IP.data = await api('GET', '/api/internal_prompts'); ipRender(); }
  catch (e) { const x = $('#ipBox'); if (x) x.innerHTML = `<div class="errbox">${esc(e.message)}</div>`; }
}
function ipRender() {
  const box = $('#ipBox'); if (!box || !IP.data) return;
  const { groups } = IP.data, items = IP.only ? IP.only.map((k) => IP.data.items.find((x) => x.key === k)).filter(Boolean) : IP.data.items;
  const mode = IP.data.mode = (S.cfg.prompts && S.cfg.prompts.mode) || 'normal';
  for (const it of items) it.active = it.override || it[mode];
  const total = items.reduce((n, it) => n + it.active.length, 0);
  const nov = items.filter((it) => it.override).length;
  box.innerHTML = `<div class="ip-sum">${items.length} 段・目前用「${mode === 'lean' ? '精簡' : '常規'}」寫法・合計 ${total} 字${nov ? `・<b>${nov} 段已覆寫</b>` : ''}</div>`
    + (IP.only ? [''] : groups).map((g) => {
      const rows = IP.only ? items : items.filter((it) => it.group === g);
      if (!rows.length) return '';
      return (g ? `<div class="ip-g">${esc(g)}</div>` : '') + rows.map((it) => {
        const open = IP.open.has(it.key);
        return `<div class="ip-row${open ? ' open' : ''}${it.override ? ' ov' : ''}" data-iprow="${it.key}">
          <div class="ip-hd"><b>${esc(it.title)}</b><span class="ip-when">${esc(it.when)}</span>
            <span class="ip-n" data-ipn="${it.key}">${it.override ? '已覆寫・' : ''}${it.active.length} 字</span></div>
          ${open ? `<div class="ip-body">
            <textarea class="mono ip-ta" data-ipedit="${it.key}" spellcheck="false" rows="${Math.min(14, Math.max(2, it.active.split('\n').length + Math.floor(it.active.length / 60)))}">${esc(it.active)}</textarea>
            <div class="ip-bar"><small>${it.vars.length ? `送出時代入：${it.vars.map((v) => `<code>{${esc(v)}}</code>`).join(' ')}・` : ''}<span data-ipst="${it.key}">${it.override ? '已覆寫（兩種寫法都用這段）' : '預設'}</span></small>
              <span class="grow"></span>${it.override ? `<span class="btn sm" data-ipreset="${it.key}">還原預設</span>` : ''}</div>
          </div>` : ''}
        </div>`;
      }).join('');
    }).join('');
}
function ipQueue(key, text) {
  const st = document.querySelector(`[data-ipst="${key}"]`); if (st) st.textContent = '尚未儲存…';
  clearTimeout(IP.timers[key]);
  IP.timers[key] = setTimeout(async () => {
    try {
      await api('PUT', `/api/internal_prompts/${key}`, { text });
      const it = IP.data.items.find((x) => x.key === key);
      const isDef = !text.trim() || text === it.lean || text === it.normal;
      it.override = isDef ? null : text; it.active = isDef ? it[IP.data.mode] : text;
      if (st) st.textContent = isDef ? '預設' : '已自動儲存（兩種寫法都用這段）';
      const n = document.querySelector(`[data-ipn="${key}"]`); if (n) n.textContent = `${it.override ? '已覆寫・' : ''}${it.active.length} 字`;
      const row = document.querySelector(`[data-iprow="${key}"]`); if (row) row.classList.toggle('ov', !!it.override);
      if (row && it.override && !row.querySelector('[data-ipreset]')) $('.ip-bar', row).insertAdjacentHTML('beforeend', `<span class="btn sm" data-ipreset="${key}">還原預設</span>`);
    } catch (e) { if (st) st.innerHTML = `<span class="qerr">沒存到：${esc(e.message)}</span>`; }
  }, 800);
}
async function ipReset(key) {
  clearTimeout(IP.timers[key]);
  try { await api('DELETE', `/api/internal_prompts/${key}`); await ipLoad(); notify('ok', '已還原預設'); }
  catch (e) { notify('error', '還原失敗', e.message); }
}
function pfRender() {
  const box = $('#pfBox');
  if (!box || !PF.files) return;
  const f = PF.files.find((x) => x.key === PF.key) || PF.files[0];
  PF.key = f.key;
  const txt = PF.drafts[f.key] ?? f.text;
  const cnt = f.entries === null || f.entries === undefined ? '' : `共 ${f.entries} 條（條目之間用一行 § 分隔）・`;
  const over = f.limit && (f.chars || 0) > f.limit;
  const count = cnt + (f.limit ? `<span class="${over ? 'qerr' : ''}">${f.chars || 0} 字${over ? '（超過上限，模型可能讀不完整）' : ''}</span>・上限 <input class="pf-lim mono" type="number" min="200" max="200000" step="100" value="${f.limit}" data-pflim title="這個檔的字數上限；模型用工具寫入時超過會被拒絕，手動編輯只提示"> 字・` : '');
  box.innerHTML = `<div class="seg pf-tabs">${PF.files.map((x) => `<b data-pf="${x.key}" class="${x.key === f.key ? 'on' : ''}">${esc(x.label)}</b>`).join('')}</div>
    <div class="pf-meta"><small>${esc(f.hint)}</small><small class="mono">${esc(f.path)}</small></div>
    <textarea id="pfText" class="mono pf-ta" rows="16" spellcheck="false">${esc(txt)}</textarea>
    <div class="pf-bar">
      <span class="muted">${count}<b id="pfDirty">${txt !== f.text ? '尚未儲存…' : '改了就自動儲存'}</b></span>
      <span class="grow"></span>
    </div>`;
}
async function pfSave(auto) {
  pfStash();
  const f = PF.files.find((x) => x.key === PF.key);
  const sent = PF.drafts[f.key] ?? f.text;
  try {
    const r = await api('PUT', `/api/prompt-files/${f.key}`, { text: sent });
    Object.assign(f, r);
    if ((PF.drafts[f.key] ?? f.text) === sent) delete PF.drafts[f.key];
    const d = $('#pfDirty'); if (d) d.textContent = PF.drafts[f.key] === undefined ? '已自動儲存。新對話起生效；進行中的對話等下次壓縮才套用，快取不會斷' : '尚未儲存…';
    if (!auto) pfRender();
  } catch (e) { const d = $('#pfDirty'); if (d) d.innerHTML = `<span class="qerr">沒存到：${esc(e.message)}</span>`; }
}
// ---- 壓縮（設定存在 context 插件，儲存後插件會重新載入）
const CMP_KEYS = ['window', 'method', 'parallel', 'ratio', 'trigger', 'keep'];
// 壓縮用的上下文大小（設定頁填的）；0＝照模型。用量圈、上下文面板都用它算百分比
async function loadCtxCap() {
  try { const p = (await api('GET', '/api/plugins')).find((r) => r.name === 'context'); S.ctxCap = p && p.enabled ? Number((p.settings || {}).window) || 0 : 0; }
  catch { S.ctxCap = S.ctxCap || 0; }
  renderMeter();
}
function winOf(u, m) {
  const mw = Number(m && m.context_window) || 0, cap = Number(S.ctxCap) || 0;
  if (cap > 0) return mw ? Math.min(cap, mw) : cap;
  return mw || Number(u && u.context_window) || 0;
}
async function renderCompressSec(box) {
  box.innerHTML = '<div class="card"><div class="blank">讀取中…</div></div>';
  let rows = [];
  try { rows = await api('GET', '/api/plugins'); } catch (e) { box.innerHTML = `<div class="blank">${esc(e.message)}</div>`; return; }
  const p = rows.find((r) => r.name === 'context');
  if (!p) { box.innerHTML = '<div class="blank">找不到 context 插件，請到「插件」頁確認已載入</div>'; return; }
  const sc = p.settings_schema || {};
  const val = (k) => ((p.settings || {})[k] ?? (sc[k] || {}).default ?? '');
  const seg = (k, opts) => `<div class="seg" data-cseg="${k}">${opts.map((o) => `<b data-cv="${esc(o)}" class="${String(val(k)) === o ? 'on' : ''}">${esc(o)}</b>`).join('')}</div>`;
  const num = (k, min, max, unit = '', cls = '') => `<label class="cin ${cls}"><input type="number" data-cnum="${k}" min="${min}" max="${max}" value="${esc(String(val(k)))}"><span>${unit}</span></label>`;
  const row = (id, title, hint, ctl) => `<div class="frow" data-f="${id}"><div><b>${title}</b><small>${hint}</small></div><div>${ctl}</div></div>`;
  const method = String(val('method')) === '多工' ? '多工' : '單次';
  const cm = selectedModel();
  const capNow = cm && cm.context_window ? `<br>目前的模型 ${esc(cm.label)}：${Number(cm.context_window).toLocaleString()} token。` : '';
  box.innerHTML = `
    <div class="card cmp">
      <div class="cmp-sum">目前：上下文 <b>${Number(val('window')) ? Number(val('window')).toLocaleString() + ' token' : '照模型'}</b>・用到 <b>${esc(String(val('trigger')))}%</b> 壓縮・保留最近 <b>${esc(String(val('keep')))} 輪</b>・<b>${method}</b>${method === '多工' ? `（一次 ${esc(String(val('parallel')))} 段）` : ''}・摘要約原文的 <b>${esc(String(val('ratio')))}%</b></div>
    </div>
    <div class="cmp-h">什麼時候壓縮</div>
    <div class="card cmp">
      ${row('c-window', '上下文大小', `0＝照模型本身的上下文窗口。填 100000 就當作只有 10 萬：壓縮門檻和對話框的用量圈都用它算。比模型本身大時以模型為準。${capNow}`, num('window', 0, 10000000, 'token', 'wide'))}
      ${row('c-trigger', '壓縮門檻', '用量達到上下文大小的這個百分比，就把較早的對話摘要起來。平常是回合結束後在背景做，你看回覆時就壓好了；同一回合工具結果疊太多、超過門檻時，會先壓縮再繼續。', num('trigger', 10, 95, '%'))}
    </div>
    <div class="cmp-h">怎麼壓縮</div>
    <div class="card cmp">
      ${row('c-keep', '保留最近幾輪', '你的一則加上模型的回覆（含中間的工具呼叫）算一輪。最近這幾輪原樣保留，不壓縮。', num('keep', 1, 30, '輪'))}
      ${row('c-ratio', '摘要幅度', '摘要長度佔原文的百分比，5 到 60。建議 10 到 30。', num('ratio', 5, 60, '%'))}
      ${row('c-method', '壓縮方式', '單次＝整段一次摘要。多工＝依字數分段，同時摘要後合併，長對話比較快。', seg('method', ['單次', '多工']))}
      ${row('c-parallel', '同時跑幾段', '只在多工時生效，1 到 8。', num('parallel', 1, 8, '段'))}
      <div class="cmp-foot" data-f="cSave"><span class="btn primary" data-cmpsave>儲存壓縮設定</span><span class="btn" data-cmpnow${S.sid ? '' : ' style="opacity:.5"'}>立即壓縮目前對話</span></div>
    </div>`;
  const vals = { method };
  const pick = (k, v) => { vals[k] = v; box.querySelectorAll(`[data-cseg="${k}"] b`).forEach((b) => b.classList.toggle('on', b.dataset.cv === v)); };
  box.onclick = async (e) => {
    const sb = e.target.closest('[data-cseg] b'); if (sb) return pick(sb.parentElement.dataset.cseg, sb.dataset.cv);
    if (e.target.closest('[data-cmpsave]')) {
      const settings = { method: vals.method };
      box.querySelectorAll('[data-cnum]').forEach((el) => { settings[el.dataset.cnum] = Number(el.value); });
      try { await api('POST', '/api/plugins/context', { settings }); }
      catch (err) { notify('error', '儲存失敗', err.message); return; }
      notify('ok', '壓縮設定已儲存', '下一次壓縮就用新設定');
      loadCtxCap();
      renderCompressSec(box);
      return;
    }
    if (e.target.closest('[data-cmpnow]')) {
      if (!S.sid) { notify('warn', '先開一個對話'); return; }
      try { await api('POST', `/api/sessions/${S.sid}/compact`); }
      catch (err) { notify('error', '壓縮失敗', err.message); }
    }
  };
}
// ---- 輔助模型
const AUX = [
  ['fallback', '備用模型', '預設不用', '主要模型出錯（限流、額度用完、連不上，服務商自己的重試都用完）時，這一回合改用它接手。預設留空：直接停下來告訴你比換一個不熟的模型接著做安全。真的要設就挑一個你也信得過的。'],
  ['vision', '看圖', '圖片分析', 'vision_analyze 工具看本機圖片時用的模型，要選支援看圖的。'],
  ['compact', '壓縮', '上下文壓縮', '把較早的對話寫成摘要。留空＝跟這個對話的模型；建議選便宜、上下文大的模型。'],
  ['subagent', '子代理', 'delegate_task', '子代理（含方案・拓展、審核）預設用的模型。留空＝跟這個對話的模型。需要 API 型連線（Claude Code CLI 不行）。'],
  ['image', '生圖', 'delegate_task image', '主模型派子代理生圖（model 填 image）時用的模型。只列生圖模型；留空＝第一個有 key 的生圖模型。'],
  ['video', '生影片', 'delegate_task video', '主模型派子代理生影片（model 填 video）時用的模型。只列影片模型；留空＝第一個有 key 的影片模型。'],
  ['review', '自動審核', '先問我', '權限要問你的動作，先交給這個模型判斷（只給工具名稱、參數、工作目錄，不給對話內容）。判定安全就直接放行並在對話裡留一行紀錄，判不出來或有疑慮才問你。危險指令一律問你。留空就不審核。建議選便宜、快的模型。'],
];
// ---- 統一下拉選單（取代原生 select，與對話框的浮動選單同風格）
let SPICK_ANCHOR = null;
function spickEl() {
  let el = document.querySelector('.spick');
  if (!el) {
    el = document.createElement('div'); el.className = 'spick'; document.body.appendChild(el);
    el.addEventListener('mousedown', (e) => e.stopPropagation());
    el.addEventListener('click', (e) => {
      const r = e.target.closest('.si'); if (!r || r.classList.contains('dis')) return;
      const fn = el._onPick; closeSpick(); if (fn) fn(r.dataset.v);
    });
  }
  return el;
}
function closeSpick() { const el = document.querySelector('.spick'); if (el) el.classList.remove('on'); SPICK_ANCHOR = null; }
function openSpick(anchor, items, current, onPick) {
  const el = spickEl();
  if (el.classList.contains('on') && SPICK_ANCHOR === anchor) { closeSpick(); return; }
  SPICK_ANCHOR = anchor; el._onPick = onPick;
  el.innerHTML = items.map((it) => `<div class="si${it.v === current ? ' on' : ''}${it.disabled ? ' dis' : ''}" data-v="${esc(it.v)}">`
    + `<span class="sck">${it.v === current ? ico('check') : ''}</span>`
    + `<span class="st"><b>${esc(it.label)}</b>${it.sub ? `<small>${esc(it.sub)}</small>` : ''}</span></div>`).join('')
    || '<div class="sh">沒有可選的項目</div>';
  el.classList.add('on');
  const b = anchor.getBoundingClientRect();
  const w = Math.max(240, b.width);
  el.style.minWidth = w + 'px';
  const h = Math.min(el.scrollHeight, 360);
  el.style.left = Math.max(8, Math.min(b.left, innerWidth - w - 8)) + 'px';
  el.style.top = (b.bottom + 6 + h > innerHeight ? Math.max(8, b.top - h - 6) : b.bottom + 6) + 'px';
}
// ---- 共用模型選擇：先選服務商，再選模型。設定頁選模型的地方都用這組。
const MPICK = {};        // id -> { o, cur }，每次渲染時登記
const MPICK_VENDOR = {}; // id -> 已選服務商（還沒選模型時）
function mpVendorList(filter) {
  const seen = new Map();
  for (const m of S.models || []) if (filter(m) && !seen.has(m.vendor)) seen.set(m.vendor, m.vendor_label || m.vendor || '其他');
  return [...seen].map(([id, label]) => ({ id, label }));
}
function mpVendorOf(id) {
  const p = MPICK[id]; if (!p) return '';
  const m = (S.models || []).find((x) => x.id === p.cur);
  return m ? m.vendor : (MPICK_VENDOR[id] || '');
}
function mpPairHtml(id, cur, o) {
  MPICK[id] = { o, cur: cur || '' };
  const filter = o.filter || (() => true);
  const m = (S.models || []).find((x) => x.id === (cur || ''));
  const vid = mpVendorOf(id);
  const vend = mpVendorList(filter).find((v) => v.id === vid) || (m ? { label: m.vendor_label || m.vendor } : null);
  const vText = vend ? vend.label : (o.blank || '未設定');
  const mText = m ? m.label : (vid ? '請選模型' : '—');
  return `<div class="apair mpick" data-mpid="${esc(id)}">`
    + `<button class="pill2" data-mpv="${esc(id)}"><span class="pk">服務商</span><b>${esc(vText)}</b><svg><use href="#i-down"/></svg></button>`
    + `<button class="pill2" data-mpm="${esc(id)}"${vid ? '' : ' disabled'}><span class="pk">模型</span><b>${esc(mText)}</b><svg><use href="#i-down"/></svg></button>`
    + '</div>';
}
function mpRefresh(id) {
  const p = MPICK[id]; if (!p) return;
  const el = [...document.querySelectorAll('.mpick')].find((x) => x.dataset.mpid === id);
  if (el) el.outerHTML = mpPairHtml(id, p.cur, p.o);
}
function mpSet(id, v) {
  const p = MPICK[id]; if (!p) return;
  const nv = v || '';
  if (nv) delete MPICK_VENDOR[id];
  if (nv !== p.cur) { p.cur = nv; p.o.set(nv); }
  mpRefresh(id);
}
document.addEventListener('click', (e) => {
  const vb = e.target.closest('[data-mpv]');
  if (vb) {
    const id = vb.dataset.mpv, p = MPICK[id]; if (!p) return;
    const filter = p.o.filter || (() => true);
    const items = [{ v: '', label: p.o.blank || '未設定' }, ...mpVendorList(filter).map((v) => ({
      v: v.id, label: v.label, sub: `${(S.models || []).filter((m) => m.vendor === v.id && filter(m)).length} 個模型` }))];
    openSpick(vb, items, mpVendorOf(id), (vid) => {
      if (!vid) { delete MPICK_VENDOR[id]; mpSet(id, ''); return; }
      MPICK_VENDOR[id] = vid;
      const m = (S.models || []).find((x) => x.id === p.cur);
      if (m && m.vendor !== vid) mpSet(id, ''); else mpRefresh(id);
    });
    return;
  }
  const mb = e.target.closest('[data-mpm]');
  if (mb) {
    const id = mb.dataset.mpm, p = MPICK[id]; if (!p) return;
    const vid = mpVendorOf(id), filter = p.o.filter || (() => true);
    const items = (S.models || []).filter((m) => m.vendor === vid && filter(m)).map((m) => {
      const subs = [m.no_key ? '還沒設 API key' : '', m.vision ? '看圖' : '', m.available === false ? '插件未載入' : ''].filter(Boolean);
      return { v: m.id, label: m.label || m.id, sub: subs.join(' · '), disabled: m.available === false };
    });
    openSpick(mb, items, p.cur, (mid) => mpSet(id, mid));
  }
});
// ---- 多選模型：跟模型商頁一樣依服務商分組、可收合，每個模型一個勾選框；可再指定一個「預設」
const MCHK = {};             // id -> { sel:Set, def, o }
const MCHK_OPEN = new Set(); // 展開中的「id|服務商」
function mchkHtml(id, selected, o) {
  const sel = new Set((Array.isArray(selected) ? selected : String(selected || '').split(',')).map((x) => x.trim()).filter(Boolean));
  MCHK[id] = { sel, def: o.def || '', o };
  const filter = o.filter || (() => true);
  const ms = (S.models || []).filter((m) => m.kind !== 'image' && m.kind !== 'video' && !m.hidden && filter(m));
  const groups = new Map();
  for (const m of ms) {
    const k = m.vendor || '';
    if (!groups.has(k)) groups.set(k, { label: m.vendor_label || m.vendor || '其他', list: [] });
    groups.get(k).list.push(m);
  }
  const hasDef = 'def' in o;
  const rows = [...groups].map(([vid, g]) => {
    const n = g.list.filter((m) => sel.has(m.id)).length;
    const open = MCHK_OPEN.has(id + '|' + vid);
    const all = n === g.list.length;
    const items = g.list.map((m) => {
      const on = sel.has(m.id), isDef = hasDef && MCHK[id].def === m.id;
      const subs = [m.model && m.model !== m.label ? m.model : '', m.no_key ? '還沒設 API key' : '', m.vision ? '看圖' : ''].filter(Boolean).join(' · ');
      return `<div class="mci${on ? ' on' : ''}" data-mcm="${esc(m.id)}"><span class="cbx${on ? ' on' : ''}">${on ? ico('check') : ''}</span>`
        + `<span class="mcn"><b>${esc(m.label || m.id)}</b>${subs ? `<small>${esc(subs)}</small>` : ''}</span>`
        + (hasDef ? `<span class="mcdef${isDef ? ' on' : ''}" data-mcdef="${esc(m.id)}" title="${isDef ? '目前的預設' : '設為預設'}">${isDef ? '預設' : '設為預設'}</span>` : '')
        + '</div>';
    }).join('');
    return `<div class="mcg${open ? ' open' : ''}" data-mcv="${esc(vid)}"><div class="mcgh">${avatar(g.label)}`
      + `<div class="vname"><b>${esc(g.label)}</b><small>${g.list.length} 個模型</small></div>`
      + `<span class="mccount${n ? ' on' : ''}">${n ? `已勾 ${n}` : '未勾'}</span>`
      + `<span class="ptlink" data-mcall="${all ? 0 : 1}">${all ? '全部取消' : '全選'}</span>${ico('right', 'chev')}</div>`
      + `<div class="mcgb">${items}</div></div>`;
  }).join('');
  return `<div class="mchk" data-mcid="${esc(id)}">${rows || '<div class="blank">沒有可選的模型</div>'}</div>`;
}
function mchkRefresh(id) {
  const p = MCHK[id]; if (!p) return;
  const el = [...document.querySelectorAll('.mchk')].find((x) => x.dataset.mcid === id);
  if (el) el.outerHTML = mchkHtml(id, [...p.sel], { ...p.o, ...('def' in p.o ? { def: p.def } : {}) });
}
function mchkCommit(id) {
  const p = MCHK[id];
  const order = (S.models || []).map((m) => m.id).filter((x) => p.sel.has(x));
  if ('def' in p.o) p.o.def = p.def;
  p.o.set(order, p.def);
  mchkRefresh(id);
}
document.addEventListener('click', (e) => {
  const box = e.target.closest && e.target.closest('.mchk'); if (!box) return;
  const id = box.dataset.mcid, p = MCHK[id]; if (!p) return;
  e.stopPropagation();
  const g = e.target.closest('.mcg'), vid = g ? g.dataset.mcv : '';
  const d = e.target.closest('[data-mcdef]');
  if (d) {
    const mid = d.dataset.mcdef;
    p.def = p.def === mid ? '' : mid;
    if (p.def) p.sel.add(mid);
    mchkCommit(id); return;
  }
  const all = e.target.closest('[data-mcall]');
  if (all) {
    const ids = [...g.querySelectorAll('[data-mcm]')].map((x) => x.dataset.mcm);
    for (const x of ids) { if (all.dataset.mcall === '1') p.sel.add(x); else { p.sel.delete(x); if (p.def === x) p.def = ''; } }
    MCHK_OPEN.add(id + '|' + vid);
    mchkCommit(id); return;
  }
  const it = e.target.closest('[data-mcm]');
  if (it) {
    const mid = it.dataset.mcm;
    if (p.sel.has(mid)) { p.sel.delete(mid); if (p.def === mid) p.def = ''; } else p.sel.add(mid);
    mchkCommit(id); return;
  }
  if (e.target.closest('.mcgh')) {
    const k = id + '|' + vid;
    MCHK_OPEN.has(k) ? MCHK_OPEN.delete(k) : MCHK_OPEN.add(k);
    g.classList.toggle('open');
  }
}, true);
// 輔助模型各用途：服務商 → 模型
function auxFits(task, m) {
  const v = S.cfg.vendors.find((x) => x.id === m.vendor) || {};
  const provider = m.provider || v.provider;
  if (task !== 'fallback' && provider === 'claude_cli') return false;
  if (task === 'vision' && !m.vision) return false;
  // 生圖／影片模型不會跑工具，不能當子代理、看圖、壓縮、審核；要生圖由主模型 delegate_task 填 model=image／video
  if (task === 'image' || task === 'video') return m.kind === task;
  if (task !== 'fallback' && (m.kind === 'image' || m.kind === 'video')) return false;
  return true;
}
function subagentRowHtml(n, tag) {
  const pl = (S.plugins || []).find((x) => x.name === 'subagent');
  if (!pl) return '';
  const list = String((pl.settings || {}).models || '').split(',').map((x) => x.trim()).filter(Boolean);
  const def = S.cfg.aux.subagent || '';
  const dm = S.cfg.models.find((x) => x.id === (def || S.cfg.default_model));
  const names = list.map((id) => (S.cfg.models.find((x) => x.id === id) || {}).label || id);
  const now = `<span>預設：</span>${esc(def ? (dm ? dm.label || dm.id : def) : '跟隨主模型')}${!def && dm ? `（${esc(dm.label || dm.id)}）` : ''}`
    + `<span>　可指派：</span>${esc(names.length ? names.join('、') : '只用預設')}`;
  return `<div class="auxrow auxwide" data-f="aux:subagent"><div><b>${n}<i>${tag}</i></b>`
    + '<small>勾選子代理可以用的模型（可多選），主代理派工時依任務從勾選的裡面挑；沒指定時用標「預設」的那個，沒設預設就跟隨主模型。需要 API 型連線（Claude Code CLI 不行）。建議只勾便宜或免費的。</small>'
    + `<div class="now">${now}</div></div>`
    + mchkHtml('aux:subagent', list, {
      filter: (x) => auxFits('subagent', x), def,
      set: (sel, d) => {
        if (d) S.cfg.aux.subagent = d; else delete S.cfg.aux.subagent;
        setDirty(true);
        pl.settings = { ...(pl.settings || {}), models: sel.join(',') };
        savePluginSetting('subagent', 'models', sel.join(','), true);   // 設定頁自己會顯示存檔狀態
        const row = document.querySelector('.auxrow.auxwide .now');
        if (row) { const t = subagentRowHtml(n, tag).match(/<div class="now">([\s\S]*?)<\/div>/); if (t) row.innerHTML = t[1]; }
      },
    })
    + '</div>';
}
function genRowHtml(k, n, tag, d) {
  // 存在 media 插件的 image_model／video_model：跟插件設定頁、image_generate／video_generate 工具是同一份
  const pl = (S.plugins || []).find((x) => x.name === 'media');
  const key = `${k}_model`, cur = String(((pl && pl.settings) || {})[key] || '');
  const m = S.cfg.models.find((x) => x.id === cur);
  const none = !(S.models || []).some((x) => x.kind === k);
  const now = m ? `目前：${m.label || m.id}` : none ? `目前：還沒有${n === '生圖' ? '生圖' : '影片'}模型，到「模型商」加一個` : '目前：自動（第一個有 key 的）';
  return `<div class="auxrow" data-f="aux:${k}"><div><b>${n}<i>${tag}</i></b><small>${d}</small><div class="now">${esc(now)}</div></div>`
    + mpPairHtml('aux:' + k, cur, {
      filter: (x) => auxFits(k, x),
      blank: '自動',
      set: (v) => {
        if (!pl) { notify('err', '沒有 media 插件，存不了'); return; }
        pl.settings = { ...(pl.settings || {}), [key]: v || '' };
        savePluginSetting('media', key, v || '', true);
        const row = document.querySelector(`.auxrow[data-f="aux:${k}"] .now`);
        const mm = S.cfg.models.find((x) => x.id === v);
        if (row) row.textContent = mm ? `目前：${mm.label || mm.id}` : '目前：自動（第一個有 key 的）';
        renderGenParams(k, v || '');
      },
    })
    + `</div><div class="genp" data-genp="${k}"></div>`;
}
// 生成參數：選了生圖／影片模型後，列出它能調的比例、解析度、秒數（依官方文件），存成這個模型的預設
async function renderGenParams(k, mid) {
  const box = document.querySelector(`.genp[data-genp="${k}"]`);
  if (!box) return;
  if (!mid) { box.innerHTML = '<small>選定模型後，這裡會列出它能調的參數（比例、解析度、秒數）。</small>'; return; }
  let s;
  try { s = await api('GET', `/api/media/spec?model=${encodeURIComponent(mid)}`); } catch { box.innerHTML = ''; return; }
  const cur = s.current || {};
  const sel = (key, label, opts) => (opts && opts.length
    ? `<label>${label}<select data-gp="${key}">${opts.map((o) => `<option${String(o) === String(cur[key]) ? ' selected' : ''}>${esc(String(o))}</option>`).join('')}</select></label>` : '');
  const secs = s.seconds
    ? `<label>秒數<input type="number" data-gp="seconds" min="${s.seconds[0]}" max="${s.seconds[1]}" value="${esc(String(cur.seconds || ''))}"><em>${s.seconds[0]}–${s.seconds[1]}</em></label>` : '';
  box.innerHTML = `<div class="genp-row">${sel('ratio', '比例', s.ratios)}${sel('size', '解析度', s.sizes)}${sel('resolution', '解析度', s.resolutions)}${secs}</div>
    <small>${esc(s.note || '')} 主模型沒指定就用這裡的值；指定了不支援的會換成最接近的，並回報改了什麼。${s.doc ? ` <a class="genp-doc" data-href="${esc(s.doc)}">官方文件</a>` : ''}</small>`;
  box.onclick = (e) => { const a = e.target.closest('.genp-doc'); if (a) window.open(a.dataset.href, '_blank'); };
  box.onchange = async () => {
    const body = {};
    box.querySelectorAll('[data-gp]').forEach((x) => { body[x.dataset.gp] = x.type === 'number' ? (Number(x.value) || '') : x.value; });
    try { await api('PUT', `/api/media/gen/${encodeURIComponent(mid)}`, body); notify('ok', '已存生成參數'); }
    catch (err) { notify('error', '存不了', err.message); }
  };
}
function auxRowHtml(k, n, tag, d) {
  if (k === 'image' || k === 'video') return genRowHtml(k, n, tag, d);
  if (k === 'subagent' && (S.plugins || []).some((x) => x.name === 'subagent')) return subagentRowHtml(n, tag);
  const cur = S.cfg.aux[k] || '';
  const m = S.cfg.models.find((x) => x.id === cur);
  const dm = S.cfg.models.find((x) => x.id === S.cfg.default_model);
  const off = k === 'fallback' || k === 'review';
  const follow = k === 'fallback' ? '不設備用（出錯就停下）' : k === 'review' ? '不使用（照權限問你）' : '跟隨主模型';
  const now = m ? `目前：${m.label || m.id}` : `目前：${follow}${dm && !off ? `（${dm.label || dm.id}）` : ''}`;
  return `<div class="auxrow" data-f="aux:${k}"><div><b>${n}<i>${tag}</i></b><small>${d}</small><div class="now">${now}</div></div>`
    + mpPairHtml('aux:' + k, cur, {
      filter: (x) => auxFits(k, x),
      blank: follow,
      set: (v) => { if (v) S.cfg.aux[k] = v; else delete S.cfg.aux[k]; setDirty(true); },
    })
    + '</div>';
}
function renderAux(box) {
  const dm = (S.models || []).find((x) => x.id === S.cfg.default_model);
  const primary = `<div class="auxrow" data-f="primary"><div><b>主要模型<i>新對話預設</i></b><small>新對話與每次開啟 app 時使用的模型。已存在的對話仍用它自己的模型。</small><div class="now">${esc(dm ? `目前：${dm.label}` : '目前：未設定（清單第一個可用）')}</div></div>`
    + mpPairHtml('primary', S.cfg.default_model, { blank: '未設定', set: (v) => { S.cfg.default_model = v; setDirty(true); } }) + '</div>';
  const gen = ['image', 'video'];
  const roles = AUX.filter(([k]) => k !== 'fallback' && !gen.includes(k));
  const fb = AUX.find(([k]) => k === 'fallback');
  box.innerHTML = `<div class="card">${primary}${roles.map(([k, n, tag, d]) => auxRowHtml(k, n, tag, d)).join('')}</div>
  <div class="addbar" data-f="auxFollow"><span class="btn" id="auxFollow">全部跟隨主模型</span></div>
  <div class="cmp-h">生圖・生影片</div>
  <div class="card">${AUX.filter(([k]) => gen.includes(k)).map((a) => auxRowHtml(...a)).join('')}<div class="auxnote">不跟隨主模型。主模型派子代理時 model 填 image／video 就用這裡選的；image_generate／video_generate 工具開著的話也用同一個。</div></div>
  <div class="cmp-h">備用模型</div>
  <div class="card">${auxRowHtml(...fb)}<div class="auxnote">這一項不受上面的「全部」影響；預設不設，出錯就停下來告訴你。</div></div>`;
  const mpl = (S.plugins || []).find((x) => x.name === 'media');
  gen.forEach((k) => renderGenParams(k, String(((mpl && mpl.settings) || {})[`${k}_model`] || '')));
}

// ---- 一般
function renderGeneral(box) {
  box.innerHTML = `<div class="card">
    <div class="frow" data-f="gBusy"><div><b>執行中按 Enter</b><small>模型還在跑時送出新訊息的預設動作。另外兩種在輸入框右下角有圖示鍵；Ctrl+Enter 一律排隊</small></div>
      <div class="seg">${['interrupt', 'steer', 'queue'].map((v) => `<b class="${busyMode() === v ? 'on' : ''}" data-busy="${v}" title="${BUSY_HINT[v]}">${BUSY_LABEL[v]}</b>`).join('')}</div></div>
    <div class="frow" data-f="gWorkdir"><div><b>預設工作目錄</b><small>新對話的工作目錄。每個對話可以在輸入框下方的資料夾或右側「檔案」另外換。</small></div><input data-g="workdir" value="${esc(S.cfg.workdir)}" class="mono"></div>
    <div class="frow" data-f="gTerm"><div><b>終端機</b><small>右側「終端」新開分頁用的 shell。</small></div><div class="fctl"><select data-g="terminal_shell">${[['auto', '自動（有 Git Bash 用 Git Bash）'], ...Object.entries(S.ptyShells || { bash: 'Git Bash', powershell: 'PowerShell', cmd: '命令提示字元' })].map(([v, n]) => `<option value="${v}"${(S.cfg.terminal_shell || 'auto') === v ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select></div></div>
  </div>
  <div class="card">
    <div class="frow" data-f="gPreview"><div><b>可預覽資料夾</b><small>對話裡的圖片與檔案，只有這些資料夾底下的能直接顯示。一行一個。</small></div><textarea data-g="allowed_dirs" rows="3" class="mono">${esc(S.cfg.allowed_dirs.join(NL))}</textarea></div>
    <div class="frow" data-f="gCost"><div><b>花費與預算</b><small>依模型單價算金額，可設每日、每月上限。打開後模型商的模型表多一欄「單價」，右側「額度」多一張預算卡。用免費模型或訂閱就不用開。</small></div><div class="fctl"><span class="sw${S.cfg.cost_on ? ' on' : ''}" id="costOn"></span></div></div>
  </div>
  <div class="card">
    <div class="card-hd" data-f="gFiles"><b>設定檔與資料夾</b><small>想直接改檔案或看資料庫時從這裡打開。</small></div>
    <div class="frow"><div><b>設定檔</b><small>模型商、模型、工作目錄（config.toml）</small></div><div class="fctl"><span class="btn" data-open="config.toml">開啟</span></div></div>
    <div class="frow"><div><b>金鑰檔</b><small>所有 API 金鑰（keys.toml）。不要分享這個檔。</small></div><div class="fctl"><span class="btn" data-open="keys.toml">開啟</span></div></div>
    <div class="frow"><div><b>資料夾</b><small>data：對話資料庫、上傳的圖片、紀錄、快照。plugins：插件。</small></div><div class="fctl"><span class="btn" data-open="data">data</span><span class="btn" data-open="plugins">plugins</span></div></div>
    <div class="frow"><div><b>開發者工具</b><small>介面出問題時看 Console</small></div><div class="fctl"><span class="btn" id="devtools">開啟</span></div></div>
  </div>`;
  box.querySelectorAll('[data-open]').forEach((b) => (b.onclick = async () => { if (D) D.openPath((await D.root()) + '\\' + b.dataset.open); }));
  $('#devtools', box).onclick = () => D && D.win('devtools');
}

// ---- 連線
// 桌面版可以改連到別台電腦；瀏覽器版就是用開啟它的那個位址，這裡只顯示目前連到哪、順便登出。
function renderRemoteSec(box) {
  const rb = remoteBase();
  const here = rb || BASE || location.origin;
  const rows = [
    ['目前連線', `<span class="mono">${esc(here)}</span>${rb ? ' <span class="tag t-ok">遠端</span>' : ' <span class="tag t-off">本機</span>'}`],
  ];
  box.innerHTML = `<div class="card">
    <div class="frow"><div><b>連線到…</b><small>別台電腦上的 WOOF。填位址後會重新載入畫面，再用那台的密碼登入。</small></div>
      <div class="fctl"><span class="btn primary" id="rmConnect">連線到…</span>${rb ? '<span class="btn" id="rmLocal">改回本機</span>' : ''}</div></div>
    <div class="frow"><div><b>目前連到</b><small>${D ? '桌面版預設連自己啟動的後端。' : '瀏覽器版：直接開那台電腦的網址即可，例如 http://192.168.1.10:8000。'}</small></div>
      <div class="fctl">${rows.map(([k, v]) => `<span class="tag t-off">${k}</span> ${v}`).join(' ')}</div></div>
    <div class="frow"><div><b>這台裝置的登入</b><small>登入憑證存在這個瀏覽器的本機儲存，換裝置要重新登入。</small></div>
      <div class="fctl"><span class="btn" id="rmLogout">登出</span></div></div>
  </div>
  <div class="card">
    <div class="frow"><div><b>讓別台電腦連進來</b><small>後端預設只聽本機。要開放區網或外網，在後端電腦的 config.toml 設定 [server] bind、密碼與 HTTPS，再啟動 <span class="mono">python -m woof</span>。做法見 README 的「讓別台電腦連進來」。</small></div></div>
  </div>`;
  $('#rmConnect').onclick = connectRemote;
  const back = $('#rmLocal');
  if (back) back.onclick = async () => {
    if (!await confirmBox('改回本機後端', '會回到這台電腦自己啟動的後端，並重新載入畫面。')) return;
    localStorage.removeItem(REMOTE_KEY);
    loginSet('');
    location.reload();
  };
  $('#rmLogout').onclick = () => { loginSet(''); location.reload(); };
}

// ---- 外觀
// 外觀的各區域：key 對應 theme.js 的 AREAS
const THEME_AREAS = [
  ['main', '主畫面', '對話區與設定頁的背景'],
  ['side', '左側欄', '對話清單、新對話與設定入口'],
  ['setnav', '設定選單', '設定頁左邊的分類清單'],
  ['right', '右側面板', '檔案、終端機、工作等面板'],
  ['card', '卡片與輸入框', '設定卡片、輸入框、按鈕、選單'],
  ['bubble', '你的訊息', '對話裡你送出的那則的底色'],
  ['code', '程式碼', '程式碼區塊與工具輸出'],
];
function renderAppearance(box) {
  const t = WoofTheme.read();
  const slider = (key, label, min, max, unit, cls, hint) => `<div class="frow"><div><b>${label}</b><small>${hint}</small></div>`
    + `<div class="slide"><input type="range" class="${cls}" data-t="${key}" min="${min}" max="${max}" value="${t[key]}"><span class="sv" data-tv="${key}">${t[key]}${unit}</span></div></div>`;
  const areaRow = ([k, name, hint]) => {
    const c = WoofTheme.area(t, k), own = !!t.areas[k];
    return `<div class="frow" data-arow="${k}"><div><div class="ar-h"><i class="ar-sw" data-asw="${k}"></i><b>${name}</b><span class="ar-tag${own ? ' own' : ''}" data-atag="${k}">${own ? '自訂' : '跟著整體'}</span></div><small>${hint}</small></div>
      <div class="ar-ctl">
        <div class="slide"><em>色相</em><input type="range" class="hue" data-ta="${k}" data-tk="h" min="0" max="360" value="${c.h}"><span class="sv" data-atv="${k}-h">${c.h}°</span></div>
        <div class="slide"><em>濃度</em><input type="range" class="tint" data-ta="${k}" data-tk="s" min="0" max="100" value="${c.s}" style="--h:${c.h}"><span class="sv" data-atv="${k}-s">${c.s}</span></div>
        <div class="ar-foot"${own ? '' : ' hidden'} data-afoot="${k}"><span class="btn" data-tafollow="${k}">改回跟著整體</span></div>
      </div></div>`;
  };
  box.innerHTML = `<div class="card">
    <div class="frow" data-f="aLang"><div><b>介面語言</b><small>介面的按鈕、選單、設定與提示，以及模型回覆用的語言；已經寫好的對話內容不翻</small></div><div class="seg">${[['zh', '繁體中文'], ['en', 'English']].map(([v, n]) => `<b class="${uiLang() === v ? 'on' : ''}" data-lang="${v}">${n}</b>`).join('')}</div></div>
    <div class="frow" data-f="aMode"><div><b>色彩模式</b><small>淺色、深色或跟著 Windows</small></div>
      <div class="seg">${[['light', '淺色'], ['dark', '深色'], ['system', '跟隨系統']].map(([v, n]) => `<b class="${t.mode === v ? 'on' : ''}" data-mode="${v}">${n}</b>`).join('')}</div></div>
    <div class="frow" data-f="aPreset"><div><b>配色</b><small>一鍵套用整組顏色（各區域會改回跟著整體），之後可以再用下面拉桿微調</small></div>
      <div class="presets">${Object.entries(WoofTheme.presets).map(([n, p]) => `<span data-preset="${esc(n)}" style="--ph:${p.hue};--pt:${p.tint};--pa:${p.accent};--ps:${p.accentSat ?? 100}"><i></i>${esc(n)}</span>`).join('')}</div></div>
    </div>
    <div class="cmp-h" data-f="aBase">整體底色</div>
    <div class="card">
    ${slider('hue', '整體色相', 0, 360, '°', 'hue', '沒有單獨調整的區域都跟著它')}
    ${slider('tint', '整體濃度', 0, 100, '', 'tint', '0 是純白，越大顏色越明顯')}
    </div>
    <div class="cmp-h" data-f="aArea">各區域</div>
    <div class="card">${THEME_AREAS.map(areaRow).join('')}</div>
    <div class="cmp-h" data-f="aAccent">強調色</div>
    <div class="card">
    ${slider('accentSat', '強調色濃度', 0, 100, '', '', '0 是黑白（跟標誌一致），越大越接近彩色')}
    ${slider('accent', '強調色', 0, 360, '°', 'hue', '按鈕、選取、連結的顏色；濃度 0 時不作用')}
    </div>
    <div class="card" data-f="aSize">
    ${slider('scale', '介面縮放', 80, 130, '%', '', '整個視窗的大小')}
    ${slider('chatSize', '對話字級', 12, 18, 'px', '', '只影響訊息與輸入框')}
    <div class="frow"><div><b>預覽</b><small>即時反映目前設定</small></div><div class="tprev"><div class="tp-side"><i class="on"></i><i></i><i></i></div><div class="tp-main"><div class="tp-user">幫我看這段程式</div><div class="tp-bot">好，問題在第 12 行。</div><div class="tp-code"></div><div class="tp-box"><span></span><b></b></div></div><div class="tp-right"></div></div></div>
    <div class="frow"><div></div><div><span class="btn" id="themeReset">恢復預設</span></div></div></div>`;
  $('input[data-t="tint"]', box).style.setProperty('--h', t.hue);
  refreshAreas(box, t);
}
// 拉桿拖動時不重畫整頁（會中斷拖動），只更新色塊、數值、標籤
function refreshAreas(box, t) {
  t = t || WoofTheme.read();
  const dark = WoofTheme.isDark(t.mode);
  for (const [k] of THEME_AREAS) {
    const c = WoofTheme.area(t, k), own = !!t.areas[k];
    const sw = $(`[data-asw="${k}"]`, box); if (!sw) continue;
    sw.style.background = WoofTheme.areaColor(t, k, dark);
    const tag = $(`[data-atag="${k}"]`, box); tag.textContent = own ? '自訂' : '跟著整體'; tag.classList.toggle('own', own);
    $(`[data-afoot="${k}"]`, box).hidden = !own;
    const h = $(`input[data-ta="${k}"][data-tk="h"]`, box), s = $(`input[data-ta="${k}"][data-tk="s"]`, box);
    if (document.activeElement !== h) h.value = c.h;
    if (document.activeElement !== s) s.value = c.s;
    s.style.setProperty('--h', c.h);
    $(`[data-atv="${k}-h"]`, box).textContent = c.h + '°';
    $(`[data-atv="${k}-s"]`, box).textContent = c.s;
  }
}
function onAreaInput(el) {
  const t = WoofTheme.setArea(el.dataset.ta, { [el.dataset.tk]: Number(el.value) });
  refreshAreas(el.closest('.card').parentElement, t);
}

function onThemeInput(el) {
  const key = el.dataset.t;
  const t = WoofTheme.set({ [key]: Number(el.value) });
  const unit = { hue: '°', accent: '°', scale: '%', chatSize: 'px' }[key] || '';
  const sv = $(`[data-tv="${key}"]`); if (sv) sv.textContent = el.value + unit;
  const tint = $('input[data-t="tint"]'); if (tint) tint.style.setProperty('--h', t.hue);
  if (key === 'hue' || key === 'tint') refreshAreas(el.closest('.card').parentElement, t);
}

// ---- 模型商
const discOf = (v) => ((S.cfg && S.cfg.discovery) || {})[v.id];
// 固定模型商：不能刪模型商，只能刪 API key
const FIXED_VENDORS = new Set(['anthropic', 'xai', 'openrouter', 'deepseek', 'openai', 'nvidia', 'ollama', 'claude-cli']);
function discAgo(ts) {
  if (!ts) return '未檢索';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return '剛剛';
  if (s < 3600) return Math.round(s / 60) + ' 分鐘前';
  if (s < 86400) return Math.round(s / 3600) + ' 小時前';
  return Math.round(s / 86400) + ' 天前';
}
// 沒有金鑰的商不顯示檢索標籤（不紅字）；有金鑰才顯示檢索結果
function discTag(v) {
  const d = discOf(v); if (!d || !d.has_key) return '';
  if (d.error) return `<span class="tag t-ex" title="${esc(d.error)}">檢索失敗</span>`;
  return `<span class="tag t-off">已檢索 ${d.count} 個 · ${discAgo(d.at)}</span>`;
}
// 重新檢索：只更新清單，然後打開勾選視窗，勾了才加進設定
async function refreshDisc(v) {
  await flushSave();
  const n = notify('info', `${v.label || v.id}：檢索中…`, '', 0);
  let r;
  try { r = await api('POST', `/api/vendors/${encodeURIComponent(v.id)}/refresh`); }
  catch (e) { n.remove(); notify('error', `${v.label || v.id} 檢索失敗`, e.message); return; }
  n.remove();
  if (!r.ok) { notify('error', `${v.label || v.id} 檢索失敗`, r.error || ''); return; }
  await openDiscovered(v);
}
async function openDiscovered(v) {
  const d = await api('GET', `/api/vendors/${encodeURIComponent(v.id)}/discovered`);
  if (!d.items.length) { notify('warn', '檢索到 0 個模型', d.error || ''); return; }
  await modal({
    title: `從 ${v.label || v.id} 挑模型`, wide: true,
    desc: `共 ${d.items.length} 個。勾選的才會加進設定；已加入的不會重複加。生圖、影片模型加進來也不會出現在聊天的模型選單，在「設定 → 模型用途 → 生圖・生影片」選。`,
    body: pickerHtml(d.items.map((x) => ({ id: x.model, name: x.label || x.model, context: x.context_window, vision: x.vision, free: x.free, reasoning: x.reasoning })),
      new Set(d.items.filter((x) => x.added).map((x) => x.model))),
    ok: '加入勾選',
    onOpen: (m) => bindPicker(m),
    onOk: async (m) => {
      const picked = $$('.picker input:checked', m).map((c) => c.value);
      if (!picked.length) { notify('warn', '還沒有勾選模型'); return false; }
      const r = await api('POST', `/api/vendors/${encodeURIComponent(v.id)}/pick`, { models: picked });
      notify('ok', `已加入 ${r.added} 個模型`, r.skipped ? `${r.skipped} 個本來就在` : '');
      await loadCfg();
      renderSettings();
      refreshState();
    },
  });
}
function vendorStatus(v) {
  if (v.provider === 'claude_cli') return '<span class="tag t-off">本機 CLI</span>';
  if (!v.key_pool) return '<span class="tag t-off">不用 key</span>';
  const n = S.cfg.keys.filter((k) => k.pool === v.key_pool).length;
  // 沒 key 只顯示「未設定」中性標籤，不用紅字
  return n ? `<span class="tag t-ok">${n} 把 key</span>` : '<span class="tag t-off">未設定 key</span>';
}
function hostOf(u) { try { return new URL(u).host; } catch { return u || ''; } }
const VENDOR_TEMPLATES = {
  OpenRouter: { label: 'OpenRouter', provider: 'openai_compat', base_url: 'https://openrouter.ai/api/v1', key_pool: 'openrouter', effort_style: 'openrouter' },
  DeepSeek: { label: 'DeepSeek', provider: 'openai_compat', base_url: 'https://api.deepseek.com', key_pool: 'deepseek', effort_style: 'deepseek' },
  OpenAI: { label: 'OpenAI', provider: 'openai_compat', base_url: 'https://api.openai.com/v1', key_pool: 'openai', effort_style: 'openai' },
  'NVIDIA NIM': { label: 'NVIDIA NIM', provider: 'openai_compat', base_url: 'https://integrate.api.nvidia.com/v1', key_pool: 'nvidia' },
  'LM Studio（本機）': { label: 'LM Studio（本機）', provider: 'openai_compat', base_url: 'http://127.0.0.1:1234/v1' },
  'Actual': { label: 'Actual', provider: 'openai_compat', base_url: 'https://api.actual.inc/v1', key_pool: 'actual' },
  'AI Gateway（Vercel）': { label: 'AI Gateway（Vercel）', provider: 'openai_compat', base_url: 'https://ai-gateway.vercel.sh/v1', key_pool: 'ai-gateway' },
  'Alibaba（Qwen，DashScope 國際）': { label: 'Alibaba（Qwen，DashScope 國際）', provider: 'openai_compat', base_url: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', key_pool: 'alibaba' },
  'Alibaba Coding Plan': { label: 'Alibaba Coding Plan', provider: 'openai_compat', base_url: 'https://coding-intl.dashscope.aliyuncs.com/v1', key_pool: 'alibaba-coding-plan' },
  'Arcee AI': { label: 'Arcee AI', provider: 'openai_compat', base_url: 'https://api.arcee.ai/api/v1', key_pool: 'arcee' },
  'CommandCode': { label: 'CommandCode', provider: 'openai_compat', base_url: 'https://api.commandcode.ai/provider/v1', key_pool: 'commandcode' },
  'DeepInfra': { label: 'DeepInfra', provider: 'openai_compat', base_url: 'https://api.deepinfra.com/v1/openai', key_pool: 'deepinfra' },
  'Fireworks AI': { label: 'Fireworks AI', provider: 'openai_compat', base_url: 'https://api.fireworks.ai/inference/v1', key_pool: 'fireworks' },
  'GMI Cloud': { label: 'GMI Cloud', provider: 'openai_compat', base_url: 'https://api.gmi-serving.com/v1', key_pool: 'gmi' },
  'Hugging Face': { label: 'Hugging Face', provider: 'openai_compat', base_url: 'https://router.huggingface.co/v1', key_pool: 'huggingface' },
  'Kilo Code': { label: 'Kilo Code', provider: 'openai_compat', base_url: 'https://api.kilo.ai/api/gateway', key_pool: 'kilocode' },
  'Kimi / Moonshot（國際）': { label: 'Kimi / Moonshot（國際）', provider: 'openai_compat', base_url: 'https://api.moonshot.ai/v1', key_pool: 'kimi-coding' },
  'MiniMax': { label: 'MiniMax', provider: 'openai_compat', base_url: 'https://api.minimax.io/v1', key_pool: 'minimax' },
  'Nebius Token Factory': { label: 'Nebius Token Factory', provider: 'openai_compat', base_url: 'https://api.tokenfactory.nebius.com/v1', key_pool: 'nebius-token-factory' },
  'NovitaAI': { label: 'NovitaAI', provider: 'openai_compat', base_url: 'https://api.novita.ai/openai/v1', key_pool: 'novita' },
  'Ollama Cloud': { label: 'Ollama Cloud', provider: 'openai_compat', base_url: 'https://ollama.com/v1', key_pool: 'ollama-cloud' },
  'Router': { label: 'Router', provider: 'openai_compat', base_url: 'https://api.router.com/v1', key_pool: 'router' },
  'StepFun': { label: 'StepFun', provider: 'openai_compat', base_url: 'https://api.stepfun.ai/step_plan/v1', key_pool: 'stepfun' },
  'Upstage': { label: 'Upstage', provider: 'openai_compat', base_url: 'https://api.upstage.ai/v1', key_pool: 'upstage' },
  'Xiaomi MiMo': { label: 'Xiaomi MiMo', provider: 'openai_compat', base_url: 'https://api.xiaomimimo.com/v1', key_pool: 'xiaomi' },
  'Z.AI / GLM': { label: 'Z.AI / GLM', provider: 'openai_compat', base_url: 'https://api.z.ai/api/paas/v4', key_pool: 'zai' },
  'Ollama 本機': { label: 'Ollama 本機', provider: 'openai_compat', base_url: 'http://127.0.0.1:11434/v1' },
  'Agnes AI': { label: 'Agnes AI', provider: 'openai_compat', base_url: 'https://apihub.agnes-ai.com/v1', key_pool: 'agnes' },
};
function renderVendors(box) {
  const pools = [...new Set(S.cfg.keys.map((k) => k.pool).concat(S.cfg.vendors.map((v) => v.key_pool).filter(Boolean)))];
  const backends = [...new Set((S.cfgBackends || []).concat(S.cfg.vendors.map((v) => v.provider).filter(Boolean)))];
  const opt = (list, v, labels = {}) => list.map((x) => `<option value="${esc(x)}"${x === v ? ' selected' : ''}>${esc(labels[x] || x)}</option>`).join('');
  const rows = S.cfg.vendors.map((v, vi) => {
    const models = S.cfg.models.map((m, mi) => [m, mi]).filter(([m]) => (m.vendor || '') === v.id);
    const open = S.openVendors.has(v.id);
    const sub = [BACKEND_LABEL[v.provider] || v.provider, hostOf(v.base_url)].filter(Boolean).join(' · ');
    const isOA = v.provider === 'openai_compat';
    const mrows = models.map(([m, mi]) => {
      const effs = m.efforts || [];
      const st = S.models.find((x) => x.id === m.id) || {};
      const autoEff = st.efforts || [];
      // 看圖：沒寫＝自動（先直接送，被拒才記住）；寫 true／false＝手動，蓋過自動偵測
      const visMode = m.vision === true ? 'on' : m.vision === false ? 'off' : 'auto';
      const visLearn = !!(st.learned && st.learned.no_vision);
      return `<div class="mtr" data-mi="${mi}">
        <input data-mf="label" value="${esc(m.label || '')}" placeholder="顯示名稱">
        <input data-mf="model" value="${esc(m.model || '')}" placeholder="API 模型名稱" class="mono">
        <input data-mf="context_window" value="${esc(m.context_window || '')}" placeholder="200000" class="mono">
        ${S.cfg.cost_on ? `<input data-mf="price" value="${esc((m.price || []).join(' / '))}" placeholder="—" class="mono" title="美元／每百萬 token：輸入 / 輸出 / 快取讀取，例如 3 / 15 / 0.3。快取沒填就照輸入價；留空＝不算錢">` : ''}
        <input data-mf="efforts" value="${esc(effs.join(', '))}" placeholder="${autoEff.length ? '自動 ' + autoEff.map((e) => EFFORT_LABEL[e] || e).join('/') : '沒有思考'}" title="${autoEff.length ? '留空＝自動偵測，目前偵測到：' + esc(autoEff.join(', ')) + '。要固定就填，例如 low, medium, high' : '沒偵測到思考強度；這個模型支援的話照 API 填，例如 low, medium, high'}" class="mono">
        <select data-mf="default_effort"><option value="">${effs.length ? '—' : '自動'}</option>${opt(effs, m.default_effort, EFFORT_LABEL)}</select>
        <select data-mvis class="${visMode === 'auto' && visLearn ? 'vis-learned' : ''}" title="${visMode === 'auto' ? (visLearn ? '自動：之前被服務商拒收過圖片，現在改走看圖模型。判斷錯了就改成「能看」' : '自動：先直接把圖送給模型，被拒才記住、改走看圖模型') : visMode === 'on' ? '手動：一律直接送圖給這個模型，自動偵測不會蓋掉' : '手動：圖片一律先交給看圖模型轉成文字'}">${[['auto', visLearn ? '自動·不能' : '自動'], ['on', '能看'], ['off', '不能看']].map(([v, t]) => `<option value="${v}"${v === visMode ? ' selected' : ''}>${t}</option>`).join('')}</select>
        <span class="ib" data-mdel title="刪除模型">${ico('trash')}</span></div>`;
    }).join('');
    return `<div class="vrow${open ? ' open' : ''}" data-vi="${vi}">
      <div class="vhead">${avatar(v.label || v.id)}<div class="vname"><b>${esc(v.label || v.id)}</b><small>${esc(sub)}</small></div>
        ${vendorStatus(v)}${discTag(v)}<span class="vcount">${models.length} 個模型</span>${ico('right', 'chev')}</div>
      <div class="vbody">
        <div class="fgrid">
          <label>名稱</label><input data-vf="label" value="${esc(v.label || '')}">
          <label>代號</label><input data-vf="id" value="${esc(v.id)}" class="mono"${v._new ? '' : ' readonly title="已建立的代號不能改，模型靠它對應"'}>
          <label>連線方式</label><select data-vf="provider">${opt(backends, v.provider, BACKEND_LABEL)}</select>
          ${isOA ? `<label>API 位址</label><input data-vf="base_url" value="${esc(v.base_url || '')}" placeholder="https://…/v1" class="mono">` : ''}
          ${v.provider !== 'claude_cli' ? `<label>金鑰池</label><select data-vf="key_pool"><option value="">（不用 key）</option>${opt(pools, v.key_pool)}<option value="__new">＋ 新的金鑰池…</option></select>` : ''}
          ${isOA ? `<label>思考參數</label><select data-vf="effort_style">${opt(['', 'openai', 'openrouter', 'deepseek', 'nim', 'none'], v.effort_style || '', { '': '自動（依位址判斷）', openai: 'OpenAI reasoning_effort', openrouter: 'OpenRouter reasoning', deepseek: 'DeepSeek thinking', nim: 'NVIDIA enable_thinking', none: '不送' })}</select>
            <label>思考回傳</label><select data-vf="echo_reasoning" title="模型上一步的思考要不要跟著送回去">${opt(['', 'all', 'off'], v.echo_reasoning === true ? 'all' : v.echo_reasoning === false ? 'off' : (v.echo_reasoning || ''), { '': '自動：工具呼叫鏈內保留，新回合捨棄', all: '每回合都保留（DeepSeek 必須）', off: '都不回傳' })}</select>
            <label>API 介面</label><select data-vf="api_style" title="這個模型商走哪種 OpenAI API。沒設定＝自動：官方 OpenAI 的推理模型走 Responses，其他相容端點維持 Chat Completions。模型自己在設定檔寫了就以模型的為準">${opt(['', 'chat', 'responses'], v.api_style || '', { '': '自動（推理模型走 Responses）', chat: 'Chat Completions', responses: 'Responses API' })}</select>` : ''}
        </div>
        <div class="mtable${S.cfg.cost_on ? ' priced' : ''}"><div class="mth"><span>顯示名稱</span><span>API 模型名稱</span><span>上下文</span>${S.cfg.cost_on ? '<span title="美元／每百萬 token：輸入 / 輸出 / 快取讀取">單價 $/M</span>' : ''}<span>思考強度</span><span>預設</span><span>看圖</span><span></span></div>
          ${mrows || '<div class="mtempty">還沒有模型</div>'}</div>
        ${learnedNote(models.map(([m]) => m))}
        <div class="vfoot"><span class="btn" data-addmodel>${ico('plus')}手動加模型</span>${v.provider !== 'claude_cli' ? `<span class="btn" data-fetchmodels>${ico('download')}從 API 取得模型</span>` : ''}${(discOf(v) || {}).has_key ? `<span class="btn" data-refreshmodels>${ico('download')}重新檢索</span>` : ''}<span class="btn" data-probe>${ico('play')}測試連線</span><span class="grow"></span>${FIXED_VENDORS.has(v.id) ? (v.key_pool && S.cfg.keys.some((k) => k.pool === v.key_pool) ? `<span class="btn danger" data-delkey>${ico('trash')}刪除 API key</span>` : '') : `<span class="btn danger" data-delvendor>${ico('trash')}刪除模型商</span>`}</div>
      </div></div>`;
  }).join('');
  box.innerHTML = `<div class="list vlist" data-f="vlist">${rows || '<div class="blank">還沒有模型商<small>從下面的範本挑一個，或按「自訂模型商」填位址和 key。</small></div>'}</div>
    <div class="vgal" id="vgal" data-f="vgal">${vendorGallery()}</div>`;
}
// 新增模型商：範本依類型分組、可搜尋；已加入的標出來，點一下跳到那一列
const VENDOR_GROUPS = [
  ['聚合平台', ['OpenRouter', 'AI Gateway（Vercel）', 'Kilo Code', 'Router', 'Hugging Face', 'DeepInfra', 'Fireworks AI', 'NovitaAI', 'Nebius Token Factory', 'GMI Cloud', 'Actual', 'CommandCode']],
  ['模型原廠', ['DeepSeek', 'OpenAI', 'NVIDIA NIM', 'Agnes AI', 'Z.AI / GLM', 'Kimi / Moonshot（國際）', 'Alibaba（Qwen，DashScope 國際）', 'Alibaba Coding Plan', 'MiniMax', 'Xiaomi MiMo', 'StepFun', 'Upstage', 'Arcee AI']],
  ['雲端', ['Ollama Cloud']],
  ['本機', ['LM Studio（本機）', 'Ollama 本機']],
];
function vendorHost(u) { try { return new URL(u).host; } catch { return ''; } }
function vendorGalleryBody() {
  const q = (S.vgalQ || '').trim().toLowerCase();
  const have = new Map();
  S.cfg.vendors.forEach((v, i) => { const h = vendorHost(v.base_url); if (h) have.set(h, i); });
  const seen = new Set(VENDOR_GROUPS.flatMap((g) => g[1]));
  const groups = [...VENDOR_GROUPS, ['其他', Object.keys(VENDOR_TEMPLATES).filter((k) => !seen.has(k))]];
  const body = groups.map(([g, keys]) => {
    const tiles = keys.filter((k) => VENDOR_TEMPLATES[k]).filter((k) => !q || k.toLowerCase().includes(q) || (VENDOR_TEMPLATES[k].base_url || '').includes(q)).map((k) => {
      const t = VENDOR_TEMPLATES[k];
      const h = vendorHost(t.base_url);
      const at = have.get(h);
      return `<div class="vtile${at !== undefined ? ' have' : ''}" ${at !== undefined ? `data-gotov="${at}"` : `data-tpl="${esc(k)}"`} title="${at !== undefined ? '已加入，點一下跳過去' : '用這個範本新增'}">
        <b>${esc(k)}</b><small>${at !== undefined ? '已加入' : esc(h)}</small></div>`;
    }).join('');
    return tiles ? `<div class="vg-h">${g}</div><div class="vg-grid">${tiles}</div>` : '';
  }).join('');
  return body || '<div class="mtempty">沒有符合的範本，按「自訂模型商」自己填位址</div>';
}
function vendorGallery() {
  return `<div class="vg-top"><b>新增模型商</b><input id="vgalQ" placeholder="搜尋範本，例如 qwen、ollama" value="${esc(S.vgalQ || '')}"><span class="grow"></span><span class="btn" id="addVendor">${ico('plus')}自訂模型商</span></div>
    <div id="vgBody">${vendorGalleryBody()}</div>`;
}
// 自動偵測記住的能力（woof/caps.py → data/model_caps.json）：撞到「不支援思考參數／不能看圖」時記下的
function learnedNote(models) {
  const items = [];
  for (const m of models) {
    const st = S.models.find((x) => x.id === m.id); const l = (st && st.learned) || {};
    const bits = [l.kind === 'image' ? '是生圖模型' : l.kind === 'video' ? '是影片模型' : '', l.no_vision ? '不能直接看圖' : '', l.no_effort ? '不收思考參數' : '', (l.bad_effort || []).length ? `不收強度 ${l.bad_effort.join('、')}` : ''].filter(Boolean);
    if (bits.length) items.push(`<span data-capsone="${esc(m.id)}" title="清掉，下次重新偵測">${esc(m.label || m.model)}：${bits.join('、')} ×</span>`);
  }
  return items.length ? `<div class="mlearn"><em>自動偵測記住</em>${items.join('')}</div>` : '';
}
function slug(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function uniqueId(base, list) { let id = base || 'item', n = 2; while (list.some((x) => x.id === id)) id = `${base}-${n++}`; return id; }
const VTYPES = {
  openai: { n: 'OpenAI 相容', d: 'OpenRouter、DeepSeek、Ollama、各家中轉', provider: 'openai_compat', url: true, key: true },
  anthropic: { n: 'Anthropic API', d: '用 API key 連 Claude', provider: 'anthropic', url: false, key: true },
  claude_cli: { n: 'Claude Code CLI', d: '呼叫本機 claude 指令', provider: 'claude_cli' },
};
// 模型挑選：勾選要加入的模型
function pickerHtml(models, have) {
  const nFree = models.filter((m) => m.free).length;
  // 免費的排前面，其餘照原順序
  const list = [...models].sort((a, b) => (b.free ? 1 : 0) - (a.free ? 1 : 0));
  const tags = (m) => [m.free ? '<em class="pk-free">免費</em>' : '', m.vision === false ? '<em title="模型商目錄的輸入類型沒列圖片。目錄可能寫錯，加進來後會先直接送圖，被拒才改走看圖模型">目錄寫只收文字</em>' : '', m.reasoning === false ? '<em>沒有思考</em>' : ''].join('');
  return `<div class="pickhd"><input data-pq placeholder="搜尋 ${models.length} 個模型">${nFree ? `<label class="pk-only"><input type="checkbox" data-pfree>只看免費（${nFree}）</label>` : ''}<span class="btn sm" data-pall>全選</span><span class="btn sm" data-pnone>全不選</span></div>`
    + `<div class="picker">${list.map((m) => `<label class="${have.has(m.id) ? 'have' : ''}" data-pid="${esc(m.id)}"${m.free ? ' data-free' : ''}><input type="checkbox" value="${esc(m.id)}"${have.has(m.id) ? ' disabled' : ''}><span>${esc(m.name !== m.id ? `${m.name}` : m.id)}${tags(m)}</span><small>${esc(m.id)}${m.context ? ' · ' + fmtCtx(m.context) : ''}${have.has(m.id) ? ' · 已加入' : ''}</small></label>`).join('')}</div>`;
}
function bindPicker(root) {
  const q = $('[data-pq]', root); if (!q) return;
  const fo = $('[data-pfree]', root);
  q.oninput = () => {
    const s = q.value.trim().toLowerCase(), only = fo && fo.checked;
    $$('[data-pid]', root).forEach((l) => (l.style.display = (!s || l.textContent.toLowerCase().includes(s)) && (!only || l.hasAttribute('data-free')) ? '' : 'none'));
  };
  if (fo) fo.onchange = q.oninput;
  $('[data-pall]', root).onclick = () => $$('[data-pid]', root).forEach((l) => { const c = $('input', l); if (l.style.display !== 'none' && !c.disabled) c.checked = true; });
  $('[data-pnone]', root).onclick = () => $$('[data-pid] input', root).forEach((c) => (c.checked = false));
}
function modelFromProbe(vid, pm) {
  return { id: '', vendor: vid, label: pm.name && pm.name !== pm.id ? pm.name : pm.id.split('/').pop(), model: pm.id, context_window: pm.context ? Number(pm.context) : undefined, _new: true };   // 目錄寫只收文字也不寫 vision：目錄可能寫錯，先直接送圖、被拒才自動改走看圖模型
}
async function probe(body) {
  try { return await api('POST', '/api/vendors/probe', body); }
  catch (e) { return { ok: false, models: [], message: e.message }; }
}
function openVendorDialog(tpl) {
  const st = { type: tpl ? (tpl.provider === 'claude_cli' ? 'claude_cli' : tpl.provider === 'anthropic' ? 'anthropic' : 'openai') : 'openai', models: [] };
  const body = `<div class="fld"><span>類型</span><div class="types">${Object.entries(VTYPES).map(([k, v]) => `<div data-vt="${k}" class="${k === st.type ? 'on' : ''}"><b>${v.n}</b><small>${v.d}</small></div>`).join('')}</div></div>
    <div class="chips" data-tpls><em>快速填入</em>${Object.keys(VENDOR_TEMPLATES).filter((k) => VENDOR_TEMPLATES[k].provider === 'openai_compat').map((k) => `<span data-qt="${esc(k)}">${esc(k)}</span>`).join('')}</div>
    <label class="fld"><span>名稱</span><input data-f="label" value="${esc((tpl && tpl.label) || '')}" placeholder="例如 我的中轉站"></label>
    <label class="fld" data-row="url"><span>API 位址</span><input data-f="base_url" value="${esc((tpl && tpl.base_url) || '')}" placeholder="https://api.example.com/v1" class="mono"><small>OpenAI 相容的位址通常以 /v1 結尾</small></label>
    <div class="frow2" data-row="key"><label class="fld"><span>API key</span><input data-f="key" type="password" placeholder="sk-…（本機 Ollama 可以留空）" class="mono"><small>存進 keys.toml，之後可再加更多把輪流用</small></label>
    <label class="fld"><span>或沿用現有金鑰池</span><select data-f="pool"><option value="">不沿用</option>${[...new Set((S.cfg ? S.cfg.keys : []).map((k) => k.pool))].map((p) => `<option>${esc(p)}</option>`).join('')}</select><small>同一組 key 給多個模型商用</small></label></div>
    <div data-row="probe"><span class="btn" data-probe-new>${ico('download')}測試連線並取得模型</span></div>
    <div class="probe" data-probe-out hidden></div>
    <div data-pick></div>`;
  return modal({
    title: '新增模型商', wide: true, body, ok: '新增',
    desc: '填好位址和 key 後按「測試連線並取得模型」，勾選要用的模型就完成。',
    onOpen: (m) => {
      const sync = () => {
        const vt = VTYPES[st.type];
        $$('[data-vt]', m).forEach((d) => d.classList.toggle('on', d.dataset.vt === st.type));
        $('[data-row="url"]', m).hidden = !vt.url;
        $('[data-row="key"]', m).hidden = !vt.key;
        $('[data-tpls]', m).hidden = st.type !== 'openai';
        const lab = $('[data-f="label"]', m);
        if (!lab.value || Object.values(VTYPES).some((x) => x.n === lab.value)) lab.value = st.type === 'openai' ? lab.value : vt.n;
      };
      m.addEventListener('click', async (e) => {
        const vt = e.target.closest('[data-vt]'); if (vt) { st.type = vt.dataset.vt; sync(); return; }
        const qt = e.target.closest('[data-qt]');
        if (qt) { const t = VENDOR_TEMPLATES[qt.dataset.qt]; $('[data-f="label"]', m).value = t.label; $('[data-f="base_url"]', m).value = t.base_url;
          const ps = $('[data-f="pool"]', m), guess = slug(t.label); if ([...ps.options].some((o) => o.value === guess)) ps.value = guess; $$('[data-qt]', m).forEach((x) => x.classList.toggle('on', x === qt)); return; }
        const pb = e.target.closest('[data-probe-new]');
        if (pb) {
          const vt2 = VTYPES[st.type];
          const out = $('[data-probe-out]', m);
          pb.classList.add('busy'); out.hidden = false; out.className = 'probe'; out.textContent = '連線中…';
          const r = await probe({ provider: vt2.provider, auth: vt2.auth, base_url: $('[data-f="base_url"]', m).value.trim(), key: $('[data-f="key"]', m).value.trim(), key_pool: $('[data-f="pool"]', m).value });
          pb.classList.remove('busy');
          out.className = 'probe ' + (r.ok ? 'ok' : 'bad'); out.textContent = r.message;
          st.models = r.models || [];
          $('[data-pick]', m).innerHTML = st.models.length ? pickerHtml(st.models, new Set()) : '';
          bindPicker(m);
        }
      });
      sync();
    },
    onOk: async (m) => {
      const vt = VTYPES[st.type];
      const label = $('[data-f="label"]', m).value.trim() || vt.n;
      const base = vt.url ? $('[data-f="base_url"]', m).value.trim() : '';
      const key = vt.key ? $('[data-f="key"]', m).value.trim() : '';
      if (vt.url && !base) { notify('warn', '請填 API 位址'); return false; }
      if (!S.cfg) await loadCfg();
      const id = uniqueId(slug(label) || slug(hostOf(base)) || 'vendor', S.cfg.vendors);
      const v = { id, label, provider: vt.provider };
      if (vt.auth) v.auth = vt.auth;
      if (base) v.base_url = base;  // 思考參數送法留空＝自動（後端 woof/caps.py 依位址判斷，撞到不支援就記住不送）
      const reuse = vt.key ? $('[data-f="pool"]', m).value : '';
      if (key) { v.key_pool = id; S.cfg.keys.push({ pool: id, label: '主要', key, hint: '', _new: true }); }
      else if (reuse) v.key_pool = reuse;
      else if (vt.provider === 'openai_compat') v.key_pool = id;
      S.cfg.vendors.push(v);
      const picked = $$('[data-pick] input:checked', m).map((c) => st.models.find((x) => x.id === c.value)).filter(Boolean);
      for (const pm of picked) S.cfg.models.push(modelFromProbe(id, pm));
      S.openVendors.add(id);
      await saveSettings();
      notify('ok', `已新增 ${label}`, picked.length ? `加入 ${picked.length} 個模型` : '還沒有模型，展開後可以從 API 取得或手動加');
    },
  });
}
function addVendor(tpl) { return openVendorDialog(tpl); }
async function vendorProbe(v, fetchModels) {
  const n = notify('info', `${v.label || v.id}：連線中…`, '', 0);
  const r = await probe({ provider: v.provider, auth: v.auth, base_url: v.base_url, key_pool: v.key_pool });
  n.remove();
  if (!r.ok) { notify('error', `${v.label || v.id} 連線失敗`, r.message); return; }
  if (!fetchModels) { notify('ok', `${v.label || v.id} 連線正常`, r.message); return; }
  if (!r.models.length) { notify('warn', '連線成功，但沒有回傳模型清單', '請手動加模型。'); return; }
  const have = new Set(S.cfg.models.filter((m) => m.vendor === v.id).map((m) => m.model));
  await modal({
    title: `從 ${v.label || v.id} 加入模型`, wide: true, desc: r.message, body: pickerHtml(r.models, have), ok: '加入',
    onOpen: (m) => bindPicker(m),
    onOk: async (m) => {
      const picked = $$('.picker input:checked', m).map((c) => r.models.find((x) => x.id === c.value)).filter(Boolean);
      if (!picked.length) { notify('warn', '還沒有勾選模型'); return false; }
      for (const pm of picked) S.cfg.models.push(modelFromProbe(v.id, pm));
      S.openVendors.add(v.id);
      await saveSettings();
      notify('ok', `已加入 ${picked.length} 個模型`);
    },
  });
}
$('#setList').addEventListener('click', (e) => {
  if (S.setSec === 'vendors') {
    if (e.target.closest('#addVendor')) return addVendor();
    const tpl = e.target.closest('[data-tpl]'); if (tpl) return addVendor({ ...VENDOR_TEMPLATES[tpl.dataset.tpl] });
    const gv = e.target.closest('[data-gotov]');
    if (gv) {
      const v0 = S.cfg.vendors[+gv.dataset.gotov]; S.openVendors.add(v0.id); renderSettings();
      const row = document.querySelector(`.vrow[data-vi="${gv.dataset.gotov}"]`); if (row) { row.scrollIntoView({ block: 'start', behavior: 'smooth' }); row.classList.add('flash'); setTimeout(() => row.classList.remove('flash'), 1200); }
      return;
    }
    const vr = e.target.closest('.vrow'); if (!vr) return;
    const v = S.cfg.vendors[+vr.dataset.vi];
    if (e.target.closest('[data-refreshmodels]')) { refreshDisc(v); return; }
    if (e.target.closest('.vhead')) {
      S.openVendors.has(v.id) ? S.openVendors.delete(v.id) : S.openVendors.add(v.id);
      vr.classList.toggle('open');
      return;
    }
    if (e.target.closest('[data-addmodel]')) {
      S.cfg.models.push({ id: '', vendor: v.id, label: '', model: '', _new: true });  // 看圖、思考不寫＝預設有（woof/caps.py）
      setDirty(true); renderSettings();
      const last = $$(`.vrow[data-vi="${vr.dataset.vi}"] .mtr`).pop(); if (last) $('input', last).focus();
      return;
    }
    if (e.target.closest('[data-probe]')) { flushSave().then(() => vendorProbe(v, false)); return; }
    if (e.target.closest('[data-fetchmodels]')) { flushSave().then(() => vendorProbe(v, true)); return; }
    if (e.target.closest('[data-delkey]')) {
      const n = S.cfg.keys.filter((k) => k.pool === v.key_pool).length;
      const users = S.cfg.vendors.filter((x) => x.key_pool === v.key_pool).length;
      if (!n) { notify('info', `「${v.label || v.id}」沒有 API key 可刪`); return; }
      confirmBox(`刪除「${v.label || v.id}」的 API key`,
        `會清掉金鑰池「${v.key_pool}」裡的 ${n} 把 key${users > 1 ? `（另外 ${users - 1} 個模型商也用這個金鑰池）` : ''}。模型商和模型都保留。`,
        { ok: '刪除', danger: true }).then((ok) => {
        if (!ok) return;
        S.cfg.keys = S.cfg.keys.filter((k) => k.pool !== v.key_pool);
        setDirty(true); renderSettings();
      });
      return;
    }
    if (e.target.closest('[data-delvendor]')) {
      const n = S.cfg.models.filter((m) => m.vendor === v.id).length;
      confirmBox(`刪除模型商「${v.label || v.id}」`, `${n ? `底下 ${n} 個模型會一起刪除。` : '模型商設定會一起刪除。'}`, { ok: '刪除', danger: true }).then((ok) => {
        if (!ok) return;
        S.cfg.models = S.cfg.models.filter((m) => m.vendor !== v.id);
        S.cfg.vendors.splice(+vr.dataset.vi, 1);
        setDirty(true); renderSettings();
      });
      return;
    }
    const cone = e.target.closest('[data-capsone]');
    if (cone) { api('DELETE', `/api/model_caps/${encodeURIComponent(cone.dataset.capsone)}`).then(async () => { await reloadModels(true); renderSettings(); notify('ok', '已清掉，下次送出會重新偵測'); }); return; }
    const tr = e.target.closest('.mtr');
    if (tr) {
      const m = S.cfg.models[+tr.dataset.mi];
      if (e.target.closest('[data-mdel]')) { S.cfg.models.splice(+tr.dataset.mi, 1); setDirty(true); renderSettings(); }
    }
  }
  if (S.setSec === 'keys') {
    const add = e.target.closest('[data-addkey]');
    if (add) { S.cfg.keys.push({ pool: add.dataset.addkey, label: '', key: '', hint: '', _new: true }); setDirty(true); renderSettings(); return; }
    if (e.target.closest('#addPool')) {
      promptBox('新增金鑰池', '名稱', '', { placeholder: 'openai、firecrawl…', mono: true, desc: '英文小寫。模型商或插件用這個名稱取 key。' }).then((raw) => {
        const name = slug(raw || ''); if (!name) return;
        S.cfg.keys.push({ pool: name, label: '主要', key: '', hint: '', _new: true });
        setDirty(true); renderSettings();
      });
      return;
    }
    const del = e.target.closest('[data-kdel]');
    if (del) { S.cfg.keys.splice(+del.dataset.kdel, 1); setDirty(true); renderSettings(); return; }
    const eye = e.target.closest('[data-keye]');
    if (eye) { const i = eye.previousElementSibling; i.type = i.type === 'password' ? 'text' : 'password'; }
  }
  if (e.target.closest('#costOn')) {
    const on = !S.cfg.cost_on;
    api('POST', '/api/budget', { enabled: on }).then((b) => { S.cfg.cost_on = on; e.target.closest('#costOn').classList.toggle('on', on); renderBudget(b); loadUsage(); })
      .catch((err) => notify('error', '沒存到', err.message));
    return;
  }
  if (e.target.closest('#auxFollow')) {
    // 只動看圖、壓縮、子代理；備用模型單獨一區，自動審核要自己挑
    const keep = {};
    if (S.cfg.aux.fallback) keep.fallback = S.cfg.aux.fallback;
    if (S.cfg.aux.review) keep.review = S.cfg.aux.review;
    S.cfg.aux = keep; setDirty(true); renderSettings(); return;
  }
  if (S.setSec === 'appearance') {
    const md2 = e.target.closest('[data-mode]'); if (md2) { WoofTheme.set({ mode: md2.dataset.mode }); renderSettings(); }
    const pr = e.target.closest('[data-preset]'); if (pr) { WoofTheme.set({ ...WoofTheme.presets[pr.dataset.preset], areas: {} }); renderSettings(); }
    const fo = e.target.closest('[data-tafollow]'); if (fo) { WoofTheme.setArea(fo.dataset.tafollow, null); refreshAreas(fo.closest('.card').parentElement); }
    if (e.target.closest('#themeReset')) { WoofTheme.reset(); renderSettings(); }
  }
});
$('#setList').addEventListener('input', (e) => {
  const el = e.target;
  if (el.id === 'vgalQ') { S.vgalQ = el.value; $('#vgBody').innerHTML = vendorGalleryBody(); return; }
  if (el.dataset.t) { onThemeInput(el); return; }
  if (el.dataset.ta) { onAreaInput(el); return; }
  if (el.dataset.aux) {
    if (el.value) S.cfg.aux[el.dataset.aux] = el.value; else delete S.cfg.aux[el.dataset.aux];
    setDirty(true); renderSettings(); return;
  }
  if (el.dataset.g) {
    if (el.dataset.g === 'allowed_dirs') S.cfg.allowed_dirs = el.value.split(/\r?\n|;/).map((s) => s.trim()).filter(Boolean);
    else S.cfg[el.dataset.g] = el.value.trim();
    setDirty(true); return;
  }
  const vr = el.closest('.vrow');
  if (vr && el.dataset.vf && el.tagName === 'INPUT') {
    const v = S.cfg.vendors[+vr.dataset.vi];
    if (el.dataset.vf === 'id') {
      const nid = slug(el.value) || el.value.trim();
      S.cfg.models.forEach((m) => { if (m.vendor === v.id) m.vendor = nid; });
      S.openVendors.delete(v.id); S.openVendors.add(nid); v.id = nid;
    } else v[el.dataset.vf] = el.value;
    if (el.dataset.vf === 'label') $('.vname b', vr).textContent = el.value || v.id;
    setDirty(true); return;
  }
  const tr = el.closest('.mtr');
  if (tr && el.dataset.mf && el.tagName === 'INPUT') {
    const m = S.cfg.models[+tr.dataset.mi];
    const f = el.dataset.mf;
    if (f === 'efforts') m.efforts = el.value.split(/[,，\s]+/).map((s) => s.trim().toLowerCase()).filter(Boolean);
    else if (f === 'context_window') m.context_window = el.value.trim() ? Number(el.value.replace(/[^0-9]/g, '')) : undefined;
    else if (f === 'price') {
      const nums = el.value.split(/[\/,，\s]+/).map((s) => s.trim()).filter(Boolean).map(Number).filter((n) => Number.isFinite(n) && n >= 0).slice(0, 3);
      m.price = nums.length ? nums : undefined;
    }
    else m[f] = el.value;
    setDirty(true); return;
  }
  const kr = el.closest('[data-ki]');
  if (kr && el.dataset.kf) { S.cfg.keys[+kr.dataset.ki][el.dataset.kf] = el.value; setDirty(true); }
});
$('#setList').addEventListener('change', (e) => {
  const el = e.target;
  if (el.matches('[data-redact]')) { S.cfg.redact_secrets = el.checked; setDirty(true); return; }
  const vr = el.closest('.vrow');
  if (vr && el.dataset.vf && el.tagName === 'SELECT') {
    const v = S.cfg.vendors[+vr.dataset.vi];
    let val = el.value;
    if (val === '__new') {
      el.value = v.key_pool || '';
      promptBox('新的金鑰池', '名稱', v.id, { mono: true, desc: '英文小寫。建好後到「API 金鑰」貼上 key。' }).then((raw) => {
        const name = slug(raw || ''); if (!name) return;
        if (!S.cfg.keys.some((k) => k.pool === name)) S.cfg.keys.push({ pool: name, label: '主要', key: '', hint: '', _new: true });
        v.key_pool = name; setDirty(true); renderSettings();
      });
      return;
    }
    if ((el.dataset.vf === 'echo_reasoning' || el.dataset.vf === 'api_style') && !val) delete v[el.dataset.vf]; else v[el.dataset.vf] = val;
    setDirty(true); renderSettings(); return;
  }
  const tr = el.closest('.mtr');
  if (tr && el.dataset.mf === 'default_effort') { S.cfg.models[+tr.dataset.mi].default_effort = el.value || undefined; setDirty(true); return; }
  if (tr && el.matches('[data-mvis]')) { const m = S.cfg.models[+tr.dataset.mi]; if (el.value === 'auto') delete m.vision; else m.vision = el.value === 'on'; setDirty(true); return; }
  if (tr && el.dataset.mf === 'efforts') renderSettings();
});

// ---- API 金鑰
function renderKeyEditor(box) {
  const llm = new Set(S.cfg.vendors.map((v) => v.key_pool).filter(Boolean));
  const pools = [...new Set(S.cfg.keys.map((k) => k.pool).concat([...llm]))];
  const usedBy = (pool) => S.cfg.vendors.filter((v) => v.key_pool === pool).map((v) => v.label || v.id);
  const status = (pool, label) => { const k = ((S.keys || {})[pool] || []).find((x) => x.label === label); if (!k) return ''; const [c, t] = KEY_TAG[k.status] || ['t-off', k.status]; return `<span class="tag ${c}">${t}</span>`; };
  const block = (pool) => {
    const rows = S.cfg.keys.map((k, i) => [k, i]).filter(([k]) => k.pool === pool).map(([k, i]) => `<div class="krow" data-ki="${i}">
      <input data-kf="label" value="${esc(k.label)}" placeholder="標籤，例如 主要／備用">
      <span class="kin"><input type="password" data-kf="key" value="${esc(k.key)}" placeholder="${k.hint ? esc(k.hint) + '（留空不改）' : '貼上 key'}" class="mono"><span class="ib" data-keye title="顯示／隱藏">${ico('search')}</span></span>
      <span class="cell">${k.error ? `<span class="tag t-ex" title="${esc(k.error)}">解不開，請重貼</span>` : status(pool, k.label)}</span><span class="ib" data-kdel="${i}" title="刪除">${ico('trash')}</span></div>`).join('');
    const who = llm.has(pool) ? usedBy(pool).join('、') : (TOOL_POOL_DESC[pool] ? `${TOOL_POOL_LABEL[pool] || pool} · ${TOOL_POOL_DESC[pool]}` : '工具或插件使用');
    return `<div class="kcard"><div class="ktop">${avatar(llm.has(pool) ? usedBy(pool)[0] || pool : TOOL_POOL_LABEL[pool] || pool)}<div class="khd"><b class="mono">${esc(pool)}</b><small>${esc(who)}</small></div>
      <span class="btn sm" data-addkey="${esc(pool)}">${ico('plus')}加一把</span></div><div class="kbody">${rows || '<div class="qempty">還沒有 key</div>'}</div></div>`;
  };
  const a = pools.filter((p) => llm.has(p)), b = pools.filter((p) => !llm.has(p));
  box.innerHTML = `<div class="ksafe" data-f="kSafe"><div><b>金鑰保護</b><small>${S.cfg.keys_encrypted ? 'keys.toml 裡的 key 用這台電腦的 Windows 帳號加密，檔案被複製走也解不開；換電腦要重新貼 key。' : '這台電腦不支援加密，keys.toml 是明文，不要分享這個檔案。'}畫面上只顯示前 6 碼和後 4 碼。</small></div>
      <label class="ktog"><input type="checkbox" data-redact ${S.cfg.redact_secrets !== false ? 'checked' : ''}><span>工具輸出遮罩金鑰</span><small>終端、讀檔、網頁內容裡出現 key 時，換成「前 6 碼…後 4 碼」再給模型看、存進對話</small></label></div>
    <div data-f="kList"><h3 class="sh">模型商金鑰</h3>${a.map(block).join('') || '<div class="blank">沒有模型商使用金鑰池</div>'}
    <h3 class="sh">搜尋與工具金鑰</h3>${b.map(block).join('') || '<div class="blank">沒有工具類的金鑰池</div>'}</div>
    <div class="addbar" data-f="kAdd"><span class="btn" id="addPool">${ico('plus')}新增金鑰池</span></div>`;
}

// ---- MCP / 進階
function renderExternalMcpSec(box) {
  box.innerHTML = `<div class="list" id="mcpRows"><div class="blank">讀取中…</div></div>
    <div class="card mcpadd" id="mcpAdd"><div class="mcpadd-h"><b>新增伺服器</b><small>stdio 填「指令 參數」；HTTP 填網址，標頭可省略</small></div>
      <div class="mcpform">
        <label>名稱</label><input id="mcpName" placeholder="例如 fs" maxlength="40">
        <label>類型</label><select id="mcpKind"><option value="stdio">stdio（本機指令）</option><option value="http">HTTP（網址）</option></select>
        <label>指令／網址</label><input id="mcpTarget" class="mono" placeholder="npx -y @modelcontextprotocol/server-filesystem C:/path">
        <label>標頭</label><input id="mcpHeader" class="mono" placeholder="可省略，例如 Authorization:Bearer 你的權杖">
        <span></span><span class="mcpbtns"><span class="btn primary" id="mcpSave">加入</span></span>
      </div></div>`;
  $('#mcpKind').addEventListener('change', (e) => { $('#mcpTarget').placeholder = e.target.value === 'http' ? 'https://example.com/mcp' : 'npx -y 套件名 參數'; });
  $('#mcpSave').onclick = addMcpServer;
  loadMcp();
}
async function addMcpServer() {
  const name = $('#mcpName').value.trim(), target = $('#mcpTarget').value.trim(), hdr = $('#mcpHeader').value.trim();
  if (!name || !target) { notify('warn', '名稱與指令／網址都要填'); return; }
  if (/[|\r\n]/.test(name)) { notify('warn', '名稱不能含 | 或換行'); return; }
  const kind = $('#mcpKind').value;
  if (kind === 'http' && !/^https?:\/\//.test(target)) { notify('warn', 'HTTP 要以 http:// 或 https:// 開頭'); return; }
  const line = [name, target, hdr].filter((x, i) => i < 2 || x).join(' | ');
  try {
    const cur = await api('GET', '/api/mcp/servers');
    const lines = String(cur.servers || '').split('\n').filter(Boolean);
    if (lines.some((l) => l.split('|')[0].trim() === name)) { notify('warn', '名稱已存在', name); return; }
    await api('PUT', '/api/mcp/servers', { servers: [...lines, line].join('\n') });
    notify('ok', 'MCP 伺服器已加入', name);
    $('#mcpName').value = ''; $('#mcpTarget').value = ''; $('#mcpHeader').value = '';
    loadMcp();
  } catch (e) { notify('error', '加入失敗', e.message); }
}
async function removeMcpServer(name) {
  const ok = await confirmBox(`移除 MCP 伺服器「${name}」？`, '設定會從清單拿掉，已連線的會斷開。');
  if (!ok) return;
  try {
    const cur = await api('GET', '/api/mcp/servers');
    const lines = String(cur.servers || '').split('\n').filter((l) => l.trim() && l.split('|')[0].trim() !== name);
    await api('PUT', '/api/mcp/servers', { servers: lines.join('\n') });
    notify('ok', '已移除', name); loadMcp();
  } catch (e) { notify('error', '移除失敗', e.message); }
}
async function loadMcp() {
  const box = $('#mcpRows');
  if (!box) return;
  let rows = [];
  try { rows = await api('GET', '/api/mcp'); } catch (e) { box.innerHTML = `<div class="blank">${esc(e.message)}</div>`; return; }
  box.innerHTML = rows.length ? rows.map((r) => {
    const st = r.connected ? `<span class="tag t-ok">已連線 · ${r.tools} 個工具</span>` : `<span class="tag t-ex">${esc(r.error || '尚未連線')}</span>`;
    return `<div class="lrow">${avatar(r.name)}<div class="vname"><b>${esc(r.name)}</b><small>${esc(r.command || r.url || '')}</small></div>${st}<span class="sw${r.enabled ? ' on' : ''}" data-mcp="${esc(r.name)}"></span><span class="ib" data-mcp-rm="${esc(r.name)}" title="移除">${ico('x') || '✕'}</span></div>`;
  }).join('') : '<div class="blank sm">還沒有外部伺服器<small>用下面的表單加入；連上後它的工具會出現在「工具」頁</small></div>';
  box.querySelectorAll('[data-mcp-rm]').forEach((b) => (b.onclick = () => removeMcpServer(b.dataset.mcpRm)));
  box.querySelectorAll('[data-mcp]').forEach((b) => (b.onclick = async () => {
    await api('POST', '/api/mcp/' + encodeURIComponent(b.dataset.mcp), { enabled: !b.classList.contains('on') });
    loadMcp();
  }));
}
function renderAbout(box) {
  box.innerHTML = `<div class="ab-hero"><span class="brand-mark xl"><svg><use href="#i-woof"/></svg></span>
      <b>WOOF</b><small id="aboutVer">個人 AI agent 桌面程式</small></div>
    <div class="ab-wrap" data-f="aboutAll">
      <div class="ab-h">版本資訊</div>
      <dl class="ab-dl" id="aboutGrid"><div><dt>讀取中…</dt><dd></dd></div></dl>
      <div class="ab-h" id="abLnkH" hidden>捷徑</div>
      <div class="card ab-lnk" id="abLnk" hidden>
        <div class="frow"><div><b>桌面捷徑</b><small>在桌面放一個 WOOF 圖示</small></div><div class="fctl"><span class="btn" data-lnk="desktop">建立</span></div></div>
        <div class="frow"><div><b>開始功能表</b><small>加到 Windows 開始功能表，可以用搜尋開啟</small></div><div class="fctl"><span class="btn" data-lnk="start">加入</span></div></div>
      </div>
    </div>`;
  if (!(D && D.appInfo)) return;
  D.appInfo().then((i) => {
    $('#aboutVer').textContent = `版本 ${i.version} · ${i.packaged ? '安裝版' : '開發模式'}`;
    const rows = [['版本', i.version], ['類型', i.packaged ? '安裝版（WOOF.exe）' : '開發模式'], ['Electron', i.electron], ['程式位置', i.exe], ['專案資料夾', i.root || '—']];
    $('#aboutGrid').innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd title="${esc(v || '')}">${esc(v || '—')}</dd></div>`).join('');
    $('#abLnk').hidden = $('#abLnkH').hidden = !i.packaged;
    $('#abLnk').onclick = async (e) => {
      const b = e.target.closest('[data-lnk]'); if (!b) return;
      const r = await D.shortcut(b.dataset.lnk);
      if (r.ok) notify('ok', '捷徑已建立', r.file); else notify('error', '建立捷徑失敗', r.error);
    };
  });
}
// ---- 輸出風格：內建可改（可還原）、自訂可刪；愛心置頂
async function renderStylesSec(box) {
  box.innerHTML = '<div class="card"><div class="blank">讀取中…</div></div>';
  const list = await loadStyles();
  const rows = list.filter((s) => s.id !== 'default');
  const tag = (s) => s.builtin ? `<span class="tag t-off">${s.edited ? '內建・已修改' : '內建'}</span>` : '<span class="tag t-off">自訂</span>';
  box.innerHTML = `<div class="card sty-list" data-f="styList">
    ${rows.map((s) => `<div class="sty" data-sid="${esc(s.id)}">
      <span class="sty-pin${s.pinned ? ' on' : ''}" data-spin title="${s.pinned ? '取消置頂' : '置頂'}">${ico(s.pinned ? 'heart-f' : 'heart')}</span>
      <div class="sty-main"><div class="sty-hd"><b>${esc(s.name)}</b>${tag(s)}</div><p>${esc(s.text)}</p></div>
      <div class="sty-acts"><span class="btn sm" data-sedit>修改</span>${s.builtin ? (s.edited ? '<span class="btn sm" data-sdel>還原</span>' : '') : '<span class="btn sm" data-sdel>刪除</span>'}</div>
    </div>`).join('')}
    <div class="sty-foot"><span class="muted">「預設」不加任何指示，固定在最前面。</span><span class="grow"></span><span class="btn primary" data-snew>新增風格</span></div>
  </div>`;
  box.onclick = async (e) => {
    const row = e.target.closest('[data-sid]'); const s = row && list.find((x) => x.id === row.dataset.sid);
    if (e.target.closest('[data-snew]')) return styleEditor(null);
    if (!s) return;
    if (e.target.closest('[data-spin]')) {
      try { await api('PUT', `/api/styles/${encodeURIComponent(s.id)}/pin`, { pinned: !s.pinned }); renderStylesSec(box); }
      catch (err) { notify('error', '置頂失敗', err.message); }
      return;
    }
    if (e.target.closest('[data-sedit]')) return styleEditor(s);
    if (e.target.closest('[data-sdel]')) {
      const ok = await confirmBox(s.builtin ? `把「${s.name}」還原成內建內容？` : `刪除「${s.name}」？`, s.builtin ? '' : '正在用這個風格的對話會改回「預設」。', { ok: s.builtin ? '還原' : '刪除', danger: !s.builtin });
      if (!ok) return;
      try { await api('DELETE', `/api/styles/${encodeURIComponent(s.id)}`); renderStylesSec(box); }
      catch (err) { notify('error', '操作失敗', err.message); }
    }
  };
}
async function styleEditor(s) {
  await modal({
    title: s ? `修改「${s.name}」` : '新增輸出風格', wide: true, ok: '儲存',
    desc: '內容會原樣放進系統提示，用一兩段話告訴模型回答時要怎麼寫。',
    body: `<label class="fld"><span>名稱</span><input data-sn value="${esc(s ? s.name : '')}" maxlength="20" placeholder="例：條列重點"></label>
      <label class="fld"><span>內容</span><textarea data-st rows="7" placeholder="例：先用一句話給結論，再用條列列出重點，每點不超過兩行。">${esc(s ? s.text : '')}</textarea></label>`,
    onOk: async (m) => {
      const name = $('[data-sn]', m).value.trim(), text = $('[data-st]', m).value.trim();
      if (!name || !text) { notify('warn', '名稱和內容都要填'); return false; }
      await api('POST', '/api/styles', { id: s ? s.id : '', name, text });
      notify('ok', s ? '已修改' : '已新增', name);
    },
  });
  if (S.setSec === 'styles') renderStylesSec($('#setList'));
}
// ---- 模式預設組：把「哪些插件可用」與「工具權限」打包成一鍵套用
const PRESET_PERM = { auto: '全自動', edits: '改檔自動', ask: '先問我', readonly: '唯讀' };
async function loadPresets() {
  try {
    const r = await api('GET', '/api/presets');
    S.presets = r.presets || []; S.presetPlugins = r.plugins || []; S.presetDefault = r.default || '';
  } catch { S.presets = S.presets || []; S.presetPlugins = S.presetPlugins || []; }
  return S.presets;
}
function renderPresetsSec(box) {
  box.innerHTML = '<div class="card"><div class="blank">讀取中…</div></div>';
  loadPresets().then(() => {
    const list = S.presets || [], plugins = S.presetPlugins || [];
    const rows = list.map((p) => `<div class="sty" data-pid="${esc(p.id)}">
      <div class="sty-main"><div class="sty-hd"><b>${esc(p.name)}</b>
        <span class="tag t-off">${p.builtin ? '內建' : '自訂'}</span>
        ${S.presetDefault === p.id ? '<span class="tag t-ok">新對話預設</span>' : ''}
        <span class="tag">${esc(PRESET_PERM[p.perm] || p.perm)}</span>
        <span class="tag t-off">${p.plugins.length} / ${plugins.length} 個插件</span></div>
        ${p.desc ? `<p>${esc(p.desc)}</p>` : ''}
        <div class="pm-chips">${p.plugins.map((x) => `<span>${esc(x)}</span>`).join('') || '<span>（這個模式沒有可用的插件）</span>'}</div></div>
      <div class="sty-acts">
        <span class="btn sm" data-puse>套用目前對話</span>
        ${S.presetDefault === p.id ? '' : '<span class="btn sm" data-pdef>設為新對話預設</span>'}
        ${p.builtin ? '' : '<span class="btn sm" data-pedit>修改</span><span class="btn sm" data-pdel>刪除</span>'}
      </div></div>`).join('');
    box.innerHTML = `<div class="card sty-list">${rows}
      <div class="sty-foot"><span class="muted">套用時權限下一則訊息就換；插件開關排到下次壓縮或開新對話才換，已經在進行的對話不會因此重算提示快取。</span><span class="grow"></span>
        ${S.presetDefault ? '<span class="btn" data-pnodef>取消新對話預設</span>' : ''}
        <span class="btn primary" data-pnew>新增模式</span></div>
    </div>`;
  });
}
$('#setList').addEventListener('click', async (e) => {
  if (S.setSec !== 'presets') return;
  const box = $('#setList');
  const row = e.target.closest('[data-pid]');
  const p = row && (S.presets || []).find((x) => x.id === row.dataset.pid);
  if (e.target.closest('[data-pnew]')) return presetEditor(null);
  if (!p) {
    if (e.target.closest('[data-pnodef]')) {
      try { await api('PUT', '/api/presets/default', { id: '' }); S.presetDefault = ''; notify('ok', '已取消新對話預設'); renderPresetsSec(box); }
      catch (err) { notify('error', '設定失敗', err.message); }
    }
    return;
  }
  if (e.target.closest('[data-puse]')) return applyPresetNow(p);
  if (e.target.closest('[data-pdef]')) {
    try { await api('PUT', '/api/presets/default', { id: p.id }); S.presetDefault = p.id; notify('ok', '新對話會用「' + p.name + '」', '開新對話時套用這個模式的插件與權限。'); renderPresetsSec(box); }
    catch (err) { notify('error', '設定失敗', err.message); }
    return;
  }
  if (e.target.closest('[data-pedit]')) return presetEditor(p);
  if (e.target.closest('[data-pdel]')) {
    const ok = await confirmBox(`刪除「${p.name}」？`, '只影響這個模式本身，已經在進行的對話不會被改。', { ok: '刪除', danger: true });
    if (!ok) return;
    try { await api('DELETE', `/api/presets/${encodeURIComponent(p.id)}`); if (S.presetDefault === p.id) S.presetDefault = ''; notify('ok', '已刪除'); renderPresetsSec(box); }
    catch (err) { notify('error', '刪除失敗', err.message); }
  }
});
async function applyPresetNow(p) {
  if (!S.sid) { notify('warn', '先開一個對話'); return; }
  try {
    const r = await api('POST', `/api/sessions/${S.sid}/preset`, { id: p.id });
    const s = cur();
    if (s) {
      const es = { ...(s.engine_state || {}), preset: r.preset };
      if (r.pending) { es.pending_off = r.off; es.pending_gen = (es.compress_count || 0); }
      else { es.plugins_off = r.off; delete es.pending_off; delete es.pending_gen; }
      s.engine_state = es; s.perm = r.perm;
    }
    renderPerm(); renderScopePill();
    notify('ok', '已套用「' + p.name + '」', r.pending ? '權限下一則訊息生效；插件開關等下次壓縮或開新對話才換。' : '這個對話還沒有訊息，插件已經換了。');
  } catch (err) { notify('error', '套用失敗', err.message); }
}
async function presetEditor(p) {
  await loadPresets();
  const plugins = S.presetPlugins || [];
  const on = new Set(p ? p.plugins : plugins);   // 新模式的預設是全部插件
  const box = (k, n) => `<div class="trow${on.has(k) ? '' : ' off'}"><span class="sw sm${on.has(k) ? ' on' : ''}" data-plug="${esc(k)}"></span><code>${esc(k)}</code></div>`;
  const body = `<label class="fld"><span>名稱</span><input data-pn maxlength="20" value="${esc(p ? p.name : '')}" placeholder="例：隨手問"></label>
    <label class="fld"><span>說明</span><input data-pd maxlength="60" value="${esc(p ? p.desc : '')}" placeholder="一句話說明什麼時候用它"></label>
    <div class="fld"><span>工具權限</span><div class="seg" data-pperm>${Object.entries(PRESET_PERM)
      .map(([k, n]) => `<b data-pv="${k}" class="${(p ? p.perm : 'ask') === k ? 'on' : ''}">${esc(n)}</b>`).join('')}</div>
      <small class="muted">套用這個模式時，對話的權限會換成這個。</small></div>
    <div class="tgrp"><h4>要用的插件<small>沒勾的插件在這個模式裡會被關掉</small></h4><div class="list">
      ${plugins.map(box).join('') || '<div class="blank">沒有插件</div>'}
    </div></div>`;
  const ok = await modal({
    title: p ? `修改「${p.name}」` : '新增模式', wide: true, ok: '儲存', body,
    onOpen: (m) => {
      m.addEventListener('click', (e) => {
        const sw = e.target.closest('[data-plug]');
        if (sw) { const k = sw.dataset.plug; if (on.has(k)) on.delete(k); else on.add(k); sw.classList.toggle('on', on.has(k)); sw.closest('.trow').classList.toggle('off', !on.has(k)); return; }
        const pv = e.target.closest('[data-pv]');
        if (pv) { m.querySelectorAll('[data-pv]').forEach((x) => x.classList.toggle('on', x === pv)); }
      });
    },
    onOk: (m) => {
      const name = $('[data-pn]', m).value.trim();
      if (!name) { notify('warn', '名稱要填'); return false; }
      const desc = $('[data-pd]', m).value.trim();
      const perm = (m.querySelector('[data-pv].on') || {}).dataset?.pv || 'ask';
      const custom = (S.presets || []).filter((x) => !x.builtin)
        .map((x) => ({ id: x.id, name: x.name, desc: x.desc, perm: x.perm, plugins: x.plugins }));
      const item = { id: p ? p.id : '', name, desc, perm, plugins: [...on] };
      const i = custom.findIndex((x) => x.id === item.id);
      if (i >= 0) custom[i] = item; else custom.push(item);
      return api('POST', '/api/presets', { presets: custom }).then(() => notify('ok', p ? '已修改' : '已新增', name));
    },
  });
  if (ok && S.setSec === 'presets') renderPresetsSec($('#setList'));
}
// ---- 檔案還原（快照）的保留規則與容量
const fmtBytes = (n) => n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GB' : n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
async function renderSnapshotsSec(box) {
  box.innerHTML = '<div class="card"><div class="blank">讀取中…</div></div>';
  let u;
  try { u = await api('GET', '/api/snapshots'); } catch (e) { box.innerHTML = `<div class="errbox">${esc(e.message)}</div>`; return; }
  const c = u.settings;
  const pct = Math.min(100, u.bytes / (c.max_total_mb * 1048576) * 100);
  const num = (k, min, max, unit) => `<label class="cin"><input type="number" data-snum="${k}" min="${min}" max="${max}" value="${c[k]}"><span>${unit}</span></label>`;
  const row = (t, h, ctl) => `<div class="frow"><div><b>${t}</b><small>${h}</small></div><div class="fctl">${ctl}</div></div>`;
  box.innerHTML = `<div class="card snp-use" data-f="snpUse">
      <div class="snp-num"><b>${fmtBytes(u.bytes)}</b><span>／ 上限 ${c.max_total_mb} MB</span></div>
      <div class="snp-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <small>${u.sessions} 個對話・${u.checkpoints} 個還原點${u.logical ? `・若每次存整份要 ${fmtBytes(u.logical)}` : ''}${u.last_prune ? `・上次整理 ${fmtTime(u.last_prune)}` : ''}</small>
      <small>同一個檔第一次存完整內容，之後只存改動的那幾段；內容相同的只存一份，全部再壓縮。</small>
    </div>
    <div class="card" data-f="snpSet">
      ${row('自動存原檔', '模型寫檔、改檔前先存一份。關掉之後就不能還原。', `<div class="seg" data-snen>${[[true, '開'], [false, '關']].map(([v, n]) => `<b data-v="${v}" class="${c.enabled === v ? 'on' : ''}">${n}</b>`).join('')}</div>`)}
      ${row('每個對話保留', '超過就刪掉這個對話最舊的還原點。', num('max_per_session', 1, 500, '個'))}
      ${row('保留天數', '對話超過這麼多天沒有新的還原點，就整個清掉。', num('retention_days', 1, 3650, '天'))}
      ${row('總容量上限', '全部加起來超過就從最舊的還原點開始刪。', num('max_total_mb', 10, 100000, 'MB'))}
      ${row('單檔上限', '比這個大的檔不存，還原時會略過。', num('max_file_mb', 1, 1000, 'MB'))}
      <div class="cmp-foot"><span class="btn primary" data-snsave>儲存</span><span class="btn" data-snprune>立即整理</span><span class="grow"></span><span class="muted">開程式時、之後每 6 小時、寫檔時（最多每 10 分鐘）會自動整理。刪掉的對話，它的快照也一起清。</span></div>
    </div>`;
  let enabled = c.enabled;
  box.onclick = async (e) => {
    const sb = e.target.closest('[data-snen] b');
    if (sb) { enabled = sb.dataset.v === 'true'; box.querySelectorAll('[data-snen] b').forEach((b) => b.classList.toggle('on', b === sb)); return; }
    if (e.target.closest('[data-snsave]')) {
      const body = { enabled };
      box.querySelectorAll('[data-snum]').forEach((el) => { body[el.dataset.snum] = Number(el.value); });
      try { await api('PUT', '/api/snapshots/settings', body); notify('ok', '已儲存'); renderSnapshotsSec(box); }
      catch (err) { notify('error', '儲存失敗', err.message); }
      return;
    }
    if (e.target.closest('[data-snprune]')) {
      try {
        const r = await api('POST', '/api/snapshots/prune');
        notify('ok', '整理完成', r.skipped ? r.skipped : `清出 ${fmtBytes(r.freed || 0)}（刪掉 ${r.removed_checkpoints || 0} 個還原點、${r.removed_sessions || 0} 個對話）`);
        renderSnapshotsSec(box);
      } catch (err) { notify('error', '整理失敗', err.message); }
    }
  };
}
// 狀態列右下角版本：點一下開「設定 → 進階」
if (D && D.appInfo) D.appInfo().then((i) => { $('#sbVerTxt').textContent = 'v' + i.version; $('#sbVer').title = `WOOF ${i.version}${i.packaged ? '' : '（開發模式）'} · 點一下看詳情`; });
$('#sbVer').onclick = () => openSettings('about');

// ---- 儲存
// auto：打字途中的自動儲存，不重畫畫面（不搶游標）、沒填完的模型先不送；不帶 auto＝新增模型商等動作後的完整儲存
async function saveSettings(opt = {}) {
  if (S.savingP && !opt._chain) { await S.savingP.catch(() => {}); }
  const clean = (o) => { const r = {}; for (const [k, v] of Object.entries(o)) if (!k.startsWith('_') && v !== '' && v != null && !(Array.isArray(v) && !v.length)) r[k] = v; return r; };
  for (const m of S.cfg.models) {
    if (!m.label && !m.model) continue;   // 還沒填名稱：先不存，填了再存
    if (!m.id) m.id = uniqueId(slug(`${m.vendor}-${m.model || m.label}`) || 'model', S.cfg.models.filter((x) => x !== m));
    if (m.default_effort && (m.efforts || []).length && !m.efforts.includes(m.default_effort)) delete m.default_effort;
  }
  const ready = S.cfg.models.filter((m) => m.label || m.model).map((m) => clean({ ...m, label: m.label || m.model }));
  const sentDirty = S.dirty;
  clearTimeout(S.saveTimer);
  setSaveState('busy');
  const run = (async () => {
    const r = await api('POST', '/api/settings', {
      workdir: S.cfg.workdir, allowed_dirs: S.cfg.allowed_dirs, terminal_shell: S.cfg.terminal_shell || 'auto', default_model: S.cfg.default_model || '',
      vendors: S.cfg.vendors.map(clean), models: ready, aux: S.cfg.aux, prompts: S.cfg.prompts, flow: S.cfg.flow, redact_secrets: S.cfg.redact_secrets !== false,
      keys: S.cfg.keys.map((k) => ({ pool: k.pool, label: k.label, orig_label: k.orig || '', key: k.key || '' })),
    });
    for (const k of S.cfg.keys) k.orig = k.label;   // 標籤改名已寫入，下次以新名稱對應
    for (const m of S.cfg.models) if (m.id) delete m._new;
    if (r && r.dropped_aux && r.dropped_aux.length) notify('warn', '已清除失效的輔助模型指派', r.dropped_aux.join('；'));
    return r;
  })();
  S.savingP = run;
  try {
    await run;
    if (S.dirty === sentDirty) S.dirty = false;
    setSaveState('ok');
    await refreshState();
    if (!opt.auto) { await loadCfg(); loadWorkdir(); renderSettings(); setSaveState('ok'); }
    else if (S.setSec === 'vendors' || S.setSec === 'aux') refreshVendorHints();
  } catch (e) {
    setSaveState('err', e.message);
    if (!opt.auto) notify('error', '儲存失敗', e.message);
  } finally { if (S.savingP === run) S.savingP = null; }
}
// 自動儲存後只更新「自動偵測記住」這類衍生資訊，不重畫輸入框
function refreshVendorHints() {}

// ------------------------------------------------------------------ 視窗按鈕
if (D) {
  $$('[data-win]').forEach((b) => (b.onclick = () => D.win(b.dataset.win)));
  D.onWinState((s) => ($('[data-win="max"]').innerHTML = s === 'max' ? '&#xE923;' : '&#xE922;'));
  D.onBackendExit((c) => { toast(`後端意外結束（exit ${c}），請重開 WOOF`, 10000); });
} else {
  $('.wc').style.display = 'none';
}

// ------------------------------------------------------------------ 啟動
(async () => {
  loginRestore();
  try {
    await refreshState();
    await loadSessions();
    restoreTabs();
    const cur0 = tab();
    if (cur0.sid) { await loadTab(cur0); showView('chat'); renderAll(); }
    else { showView('chat'); renderAll(); renderChat(true); }
    loadWorkdir();
    refreshTasks();
    loadCtxCap();
    loadDefaultPerm();
  } catch (e) {
    $('#chat').innerHTML = `<div class="msg"><div class="errbox">無法連到後端：${esc(e.message)}</div></div>`;
  }
  connect();
})();

// ask_user 卡片現在畫在對話串裡（見 renderChat），事件一併掛在對話區
function askClick(e) {
  const o = e.target.closest('[data-ask]');
  if (o) { answerAsk(o.dataset.ask, o.dataset.val); return true; }
  const s = e.target.closest('[data-asksend]');
  if (s) {
    const id = s.dataset.asksend; const el = $(`[data-askin="${id}"]`); const v = el ? el.value.trim() : '';
    // 空白按送出＝略過這一題，不是當成沒按
    answerAsk(id, v || ASK_SKIP, !v);
    return true;
  }
  const k = e.target.closest('[data-askskip]');
  if (k) { answerAsk(k.dataset.askskip, ASK_SKIP, true); return true; }
  return false;
}
$('#taskBar').addEventListener('click', askClick);
$('#chat').addEventListener('click', askClick);
$('#chat').addEventListener('keydown', (e) => {
  const el = e.target.closest && e.target.closest('[data-askin]');
  if (!el || e.key !== 'Enter' || e.isComposing) return;
  e.preventDefault();
  const v = el.value.trim();
  if (v) answerAsk(el.dataset.askin, v);
});
$('#chat').addEventListener('input', (e) => { const el = e.target.closest && e.target.closest('[data-askin]'); if (el) askTouch(el.dataset.askin); });

i18nApply();   // 介面語言：開啟時套用一次，之後新出現的畫面由 MutationObserver 接手


// ---- 對話群組：分類、拖曳、改名、刪除（刪群組連對話一起刪會問兩次）
function syncGrpMenu() {
  const m = $('#grpMenu');
  if (!m || !m.dataset.gid) return;
  const g = grpById(m.dataset.gid);
  if (!g) { closePops(); return; }
  m.innerHTML = `<div class="mi" data-gact="rename">${ico('edit')}<span>改名</span></div>`
    + `<div class="mi danger" data-gact="del">${ico('trash')}<span>刪除群組…</span></div>`;
}

function openGrpMenu(anchor) {
  const gid = anchor.dataset.gmenu;
  const g = grpById(gid);
  if (!g) return;
  let m = $('#grpMenu');
  if (!m) { m = document.createElement('div'); m.id = 'grpMenu'; m.className = 'pop'; document.body.appendChild(m); }
  if (m.classList.contains('on') && m.dataset.gid === gid) { closePops(); return; }
  closePops();
  m.dataset.gid = gid;
  m.classList.add('on');
  syncGrpMenu();
  const a = (anchor.closest('.ghead') || anchor).getBoundingClientRect();
  m.style.position = 'fixed';
  m.style.width = '152px';
  m.style.left = Math.round(Math.max(8, a.right - 152)) + 'px';
  m.style.top = Math.round(a.bottom + 4) + 'px';
  m.style.bottom = 'auto';
}

async function newGroup() {
  const name = await promptBox('新增群組', '群組名稱（之後可以改）', '', { ok: '建立' });
  if (!name || !name.trim()) return;
  try { await api('POST', '/api/groups', { name: name.trim().slice(0, 40) }); await loadGroups(); }
  catch (e) { notify('error', '建立群組失敗', e.message); }
}

async function renameGroup(gid) {
  const g = grpById(gid);
  if (!g) return;
  const name = await promptBox('群組改名', '群組名稱', g.name, { ok: '儲存' });
  if (!name || !name.trim() || name.trim() === g.name) return;
  try { await api('PATCH', `/api/groups/${gid}`, { name: name.trim().slice(0, 40) }); await loadGroups(); }
  catch (e) { notify('error', '改名失敗', e.message); }
}

async function deleteGroupFlow(gid) {
  const g = grpById(gid);
  if (!g) return;
  const n = S.sessions.filter((s) => s.grp === gid && !s.archived).length;
  const first = await confirmBox(`刪除群組「${g.name}」？`,
    n ? `這個群組裡有 ${n} 個對話。下一步會再問一次，要不要連對話一起刪掉。`
      : '這是空群組，刪掉不會影響任何對話。', { ok: '下一步' });
  if (!first) return;
  let withSessions = false;
  if (n) {
    withSessions = !!(await confirmBox(`「${g.name}」裡的 ${n} 個對話要一起刪掉嗎？`,
      '選「連對話一起刪掉」＝這些對話永久消失，救不回來。選「只刪群組」＝對話留著，回到未分組。',
      { ok: '連對話一起刪掉', cancel: '只刪群組', danger: true }));
  }
  try {
    await api('DELETE', `/api/groups/${gid}?sessions=${withSessions ? 1 : 0}`);
    await loadSessions();
    if (S.sid && !S.sessions.some((s) => s.id === S.sid)) newSession();
    if (S.split && !S.sessions.some((s) => s.id === S.split)) closeSplit();
    if (withSessions) notify('ok', '群組與對話已刪除', `「${g.name}」和 ${n} 個對話都刪了。`);
    else if (n) notify('ok', '群組已刪除', `「${g.name}」刪了，${n} 個對話回到未分組。`);
    else notify('ok', '群組已刪除', '空群組已刪除。');
  } catch (e) { notify('error', '刪除群組失敗', e.message); }
}

async function toggleGroup(gid) {
  const g = grpById(gid);
  if (!g) return;
  g.collapsed = !g.collapsed;
  renderSessions();
  try { await api('PATCH', `/api/groups/${gid}`, { collapsed: g.collapsed }); } catch {}
}

function moveSessionToGroup(sid, gid) {
  const s = S.sessions.find((x) => x.id === sid);
  if (!s) return;
  if (s.parent) {
    // 子代理拖出來：拆成獨立對話，順便放進目標群組
    const before = { parent: s.parent, grp: s.grp };
    s.parent = ''; s.grp = gid;
    renderSessions();
    api('PATCH', `/api/sessions/${sid}`, { parent: '', grp: gid })
      .then(() => { loadGroups(); notify('ok', '已拆成獨立對話', s.title || ''); })
      .catch((e) => { Object.assign(s, before); renderSessions(); notify('error', '拆不出來', e.message); });
    return;
  }
  if ((s.grp || '') === gid) return;
  const before = s.grp || '';
  s.grp = gid;
  renderSessions();
  api('PATCH', `/api/sessions/${sid}`, { grp: gid })
    .then(() => loadGroups())
    .catch((e) => { s.grp = before; renderSessions(); notify('error', '搬到群組失敗', e.message); });
}

(function initGroupsUI() {
  const list = $('#sessionList');
  if (!list || !$('#grpNew')) return;
  $('#grpNew').addEventListener('click', newGroup);

  list.addEventListener('click', (e) => {
    const gm = e.target.closest('[data-gmenu]');
    if (gm) { e.preventDefault(); e.stopPropagation(); openGrpMenu(gm); return; }
    const head = e.target.closest('.ghead');
    if (head && head.dataset.gid) { e.stopPropagation(); toggleGroup(head.dataset.gid); }
  }, true);

  document.addEventListener('click', (e) => {
    const item = e.target.closest('#grpMenu [data-gact]');
    if (item) {
      const gid = $('#grpMenu').dataset.gid;
      const act = item.dataset.gact;
      closePops();
      if (act === 'rename') renameGroup(gid); else deleteGroupFlow(gid);
    }
  }, true);

  list.addEventListener('dragstart', (e) => {
    const it = e.target.closest('.item[data-sid]');
    if (!it) return;
    S.drag = it.dataset.sid;
    it.classList.add('dragging');
    try { e.dataTransfer.setData('text/plain', it.dataset.sid); e.dataTransfer.effectAllowed = 'move'; } catch {}
  });

  const clearDrop = () => list.querySelectorAll('[data-drop].drop').forEach((x) => x.classList.remove('drop'));
  list.addEventListener('dragend', () => {
    S.drag = null;
    clearDrop();
    list.querySelectorAll('.item.dragging').forEach((x) => x.classList.remove('dragging'));
  });
  list.addEventListener('dragover', (e) => {
    const t = e.target.closest('[data-drop]');
    if (!t || !S.drag) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'move'; } catch {}
    clearDrop();
    t.classList.add('drop');
  });
  list.addEventListener('dragleave', (e) => {
    const t = e.target.closest('[data-drop]');
    if (t && !t.contains(e.relatedTarget)) t.classList.remove('drop');
  });
  list.addEventListener('drop', (e) => {
    const t = e.target.closest('[data-drop]');
    if (!t || !S.drag) return;
    e.preventDefault();
    const sid = S.drag;
    S.drag = null;
    clearDrop();
    const gid = t.dataset.drop || '';
    const g = gid && grpById(gid);
    if (g && g.collapsed) { g.collapsed = false; renderSessions(); }
    moveSessionToGroup(sid, gid);
  });
})();

// 並排視窗的關閉鈕（在 #paneB 裡，不歸分頁列管）
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-splitclose]')) closeSplit();
}, true);

// 第二個畫面：載入後直接打開網址帶的那個對話
if (PANE2) {
  const sid2 = new URLSearchParams(location.search).get('sid') || '';
  const go2 = () => { if (sid2) openSession(sid2).catch(() => {}); };
  if (document.readyState === 'complete') setTimeout(go2, 400);
  else window.addEventListener('load', () => setTimeout(go2, 400));
}

// ---- 寵物：側邊自己的對話窗（訊息、提示詞、壓縮都跟主對話分開）
// 版面由上而下：對話紀錄 → 頭頂泡泡（牠最新的一則）→ 牠 → 一顆模型建議 → 固定回覆 → 輸入框
// 雙方都能對對方的訊息按表情；牠一次可以連發幾則，前端一則一則冒出來。
const PET = { cfg: null, st: { face: 'happy', say: '', mood: '', at: 0 }, hist: [], faces: [],
  busy: false, model: '', label: '', lastPetId: '', reveal: null, revealTimer: 0 };

document.addEventListener('input', (e) => {
  if (e.target && e.target.id === 'petIn') petGrow();
});
function petSprite(face) {
  const ch = (PET.st && PET.st.char) || (PET.cfg && PET.cfg.char) || 'dog';
  return `${BASE}/api/pet/sprite/${encodeURIComponent(face || 'happy')}?char=${encodeURIComponent(ch)}&token=${encodeURIComponent(TOKEN)}`;
}
function petReacts() {
  return (PET.st && PET.st.reacts) || ['❤️', '😂', '🥺', '👍', '😮', '🔥', '😴'];
}
function petLines(m) {
  return String((m && m.text) || '').split('\n').map((x) => x.trim()).filter(Boolean);
}
// 最後一則是牠講的 → 放泡泡；其餘放紀錄
function petSplit() {
  const list = PET.hist.slice(-40);
  const last = list[list.length - 1];
  if (last && last.role === 'pet' && petLines(last).length) return { rows: list.slice(0, -1), top: last };
  return { rows: list, top: null };
}
// 新的一則：第一句馬上出現，後面的照字數一句一句冒（像真人連發）
function petMaybeReveal(animate) {
  const lastPet = [...PET.hist].reverse().find((m) => m.role === 'pet');
  const id = lastPet ? lastPet.id : '';
  if (!id || id === PET.lastPetId) return;
  PET.lastPetId = id;
  clearTimeout(PET.revealTimer);
  const lines = petLines(lastPet);
  if (!animate || lines.length < 2) { PET.reveal = null; return; }
  PET.reveal = { id, shown: 1 };
  const step = () => {
    if (!PET.reveal || PET.reveal.id !== id) return;
    PET.reveal.shown += 1;
    if (PET.reveal.shown >= lines.length) PET.reveal = null;
    petPaintStage();
    if (PET.reveal) PET.revealTimer = setTimeout(step, 450 + Math.min(lines[PET.reveal.shown].length * 45, 1200));
  };
  PET.revealTimer = setTimeout(step, 450 + Math.min(lines[1].length * 45, 1200));
}

const PET_DOTS = '<span class="dots"><i></i><i></i><i></i></span>';
function petRx(m) {
  // 訊息下面那顆小表情：使用者訊息顯示牠按的，牠的訊息顯示你按的
  if (m.role === 'pet') {
    return m.ureact ? `<button class="rx mine" data-pet="unrx" data-mid="${esc(m.id || '')}" title="收回">${esc(m.ureact)}</button>` : '';
  }
  return m.react ? `<span class="rx">${esc(m.react)}</span>` : '';
}
function petPokeLabel(m) {
  const pname = (PET.st && PET.st.name) || '沃夫';
  const n = Number((String(m.text || '').match(/(\d+)\s*下/) || [])[1] || 1);
  if (uiLang() === 'en') return n > 1 ? `You patted ${pname} ${n} times` : `You patted ${pname}`;
  return n > 1 ? `你摸了${pname} ${n} 下` : `你摸了摸${pname}`;
}
function petRow(m) {
  if (m.role !== 'pet' && m.kind === 'poke') return `<div class="pnote">${esc(petPokeLabel(m))}</div>`;
  if (m.role !== 'pet') {
    return `<div class="pmsg u">${petAttHtml(m)}${m.text ? `<div class="petline">${esc(m.text)}</div>` : ''}${petRx(m)}</div>`;
  }
  const lines = petLines(m);
  if (!lines.length) return '';
  return `<div class="pmsg p" data-mid="${esc(m.id || '')}">${lines.map((x) => `<div class="petline">${esc(x)}</div>`).join('')}`
    + `<span class="rxadd" data-pet="rxpick" data-mid="${esc(m.id || '')}" title="按表情">☺</span>${petRx(m)}</div>`;
}
// 跟通訊軟體一樣：隔了一段時間才有下一則，就在中間標時間（看得出牠是隔多久自己找你的）
function petWhen(t) {
  const d = new Date(t * 1000), now = new Date();
  const hm = `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `昨天 ${hm}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
function petRows(rows) {
  let prev = 0;
  return rows.map((m) => {
    const at = Number(m.at) || 0;
    const gap = at && (!prev || at - prev > 30 * 60) ? `<div class="pnote ptime">${esc(petWhen(at))}${m.kind === 'nudge' ? ' · 牠主動找你' : ''}</div>` : '';
    if (at) prev = at;
    return gap + petRow(m);
  }).join('');
}
function petPara(x) {
  const aside = /^[（(].*[）)]$/.test(x);
  return `<p${aside ? ' class="aside"' : ''}>${esc(x)}</p>`;
}
function petPaintStage() {
  const img = $('#petImg');
  if (!img) return;
  const src = petSprite(PET.st && PET.st.face);
  if (img.getAttribute('src') !== src) img.src = src;
  const mood = $('#petMood');
  if (mood) mood.textContent = (PET.st && PET.st.mood) || '';
  const bub = $('#petBub');
  if (!bub) return;
  const { top } = petSplit();
  let html = '';
  let size = 0;
  if (PET.busy) {
    html = `<div class="pmsg p top"><div class="bub typing">${PET_DOTS}</div></div>`;
  } else if (top) {
    const lines = petLines(top);
    const n = PET.reveal && PET.reveal.id === top.id ? PET.reveal.shown : lines.length;
    const shown = lines.slice(0, n);
    size = shown.join('').length;
    const multi = shown.length > 1 || size > 22;
    html = `<div class="pmsg p top" data-mid="${esc(top.id || '')}"><div class="bub${multi ? ' multi' : ''}">`
      + shown.map(petPara).join('') + (n < lines.length ? `<p class="more">${PET_DOTS}</p>` : '') + '</div>'
      + `<span class="rxadd" data-pet="rxpick" data-mid="${esc(top.id || '')}" title="按表情">☺</span>${petRx(top)}</div>`;
  }
  bub.innerHTML = html;
  bub.classList.toggle('on', !!html);
  // 牠講很長時，圖縮小讓位給泡泡；泡泡本身有最大高度，超過就在裡面捲
  const wrap = bub.closest('.petwrap');
  if (wrap) wrap.classList.toggle('long', size > 70);
  const b = bub.querySelector('.bub');
  if (b) b.scrollTop = b.scrollHeight;
}
function petPaint() {
  if (!$('#petImg')) return;
  const pname = (PET.st && PET.st.name) || '沃夫';
  const nm = $('#petName');
  if (nm) nm.textContent = pname;
  const inp = $('#petIn');
  if (inp) inp.placeholder = uiLang() === 'en' ? `Talk to ${pname}…` : `跟${pname}說話…`;
  const sb = $('#petSug');
  if (sb) {
    const sug = PET.busy ? [] : ((PET.st && PET.st.sug) || []).slice(0, 1);
    const quick = (PET.cfg && PET.cfg.quick) || [];
    sb.innerHTML = (sug.length
      ? `<button class="sugbtn one" data-pet="sug" data-say="${esc(sug[0])}">${esc(sug[0])}</button>` : '')
      + (quick.length
        ? `<div class="sugrow">${quick.map((x) => `<button class="sugbtn fix" data-pet="sug" data-say="${esc(x)}">${esc(x)}</button>`).join('')}</div>`
        : '');
  }
  const h = $('#petHist');
  if (h) {
    const { rows, top } = petSplit();
    h.innerHTML = rows.length || top || PET.busy
      ? petRows(rows)
      : (PET.cfg && PET.cfg.on === false
        ? '<div class="pethint">主動說話關著：牠不會自己開口。<br>想找牠，直接打字或摸摸牠就好。</div>'
        : `<div class="pethint">${esc(PET_MODES[petMode()].hint)}<br>想找牠說話，直接打字就好。</div>`);
  }
  const pm = $('#petMode');
  if (pm) {
    const k = petMode();
    pm.textContent = PET_MODES[k].label + (PET.cfg && PET.cfg.on === false ? ' · 不主動' : '');
    pm.className = `petmode m-${k}${PET.cfg && PET.cfg.on === false ? ' quiet' : ''}`;
    pm.title = `${PET_MODES[k].label}模式：${PET_MODES[k].desc}（點一下切換）`;
  }
  petPaintStage();
  // 泡泡、建議鈕的高度會把紀錄區擠小：畫完上面再捲到底，最後一則才不會被泡泡蓋住
  if (h) { h.scrollTop = h.scrollHeight; requestAnimationFrame(() => { h.scrollTop = h.scrollHeight; }); }
}

// 紀錄區黏在底部：只有使用者自己捲（滾輪、拖捲軸、按鍵）才算離開底部；
// 程式捲動、面板隱藏（display:none 會把捲動歸零）、圖片晚載入都不算。面板或視窗重新顯示時回到底部
(function petHistStick() {
  const h = $('#petHist'); if (!h || !window.ResizeObserver) return;
  let stick = true, userAt = 0, lastH = h.clientHeight;
  const toEnd = () => { h.scrollTop = h.scrollHeight; };
  const mark = () => { userAt = Date.now(); };
  ['wheel', 'touchmove', 'pointerdown', 'keydown'].forEach((t) => h.addEventListener(t, mark, { passive: true }));
  h.addEventListener('scroll', () => {
    if (!h.clientHeight || Date.now() - userAt > 800) return;
    stick = h.scrollHeight - h.scrollTop - h.clientHeight < 24;
  }, { passive: true });
  new ResizeObserver(() => {
    const shown = !lastH && h.clientHeight;
    lastH = h.clientHeight;
    if (!h.clientHeight) return;
    if (shown) stick = true;
    if (stick) { toEnd(); requestAnimationFrame(toEnd); }
  }).observe(h);
  // 紀錄裡的圖片、字型晚載入會把內容撐高，但紀錄區本身大小沒變，ResizeObserver 不會觸發
  h.addEventListener('load', () => { if (stick) toEnd(); }, true);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && stick) requestAnimationFrame(toEnd); });
  window.addEventListener('focus', () => { if (stick) requestAnimationFrame(toEnd); });
})();
async function petLoad() {
  try {
    const d = await api('GET', '/api/pet');
    PET.cfg = d.cfg; PET.st = d.state || PET.st; PET.faces = d.faces || [];
    PET.model = d.model; PET.label = d.label; PET.hist = d.history || [];
    PET.chars = d.chars || [];
    petMaybeReveal(false);
    petPaint();
  } catch (e) { /* 後端還沒好就算了，下次再讀 */ }
}

function petEvent(p, hist) {
  if (!p) return;
  PET.st = p;
  if (Array.isArray(hist) && !PET.busy) { PET.hist = hist; petMaybeReveal(true); }
  petPaint();
}

function petGrow() {
  const inp = $('#petIn');
  if (!inp) return;
  inp.style.height = 'auto';
  inp.style.height = `${Math.max(32, Math.min((inp.scrollHeight || 30) + 2, 120))}px`;
  inp.style.overflowY = inp.scrollHeight > 120 ? 'auto' : 'hidden';
}

// ---- 丟給寵物的圖片、文件：按 ＋、拖進寵物欄、在輸入框貼上
PET.att = [];
function petAddFiles(files) {
  for (const f of files || []) {
    if (PET.att.length >= 6) { toast('一次最多丟 6 個'); break; }
    if (f.size > FILE_MAX) { toast(`${f.name} 太大（${fmtBytes(f.size)}），上限 ${fmtBytes(FILE_MAX)}`); continue; }
    const r = new FileReader();
    r.onload = () => {
      const url = String(r.result);
      PET.att.push({ name: f.name || (f.type.startsWith('image/') ? '貼上的圖片.png' : '檔案'), media_type: f.type || 'application/octet-stream',
                     bytes: f.size, data: url.split(',')[1], preview: f.type.startsWith('image/') ? url : '' });
      petPaintAtt();
    };
    r.readAsDataURL(f);
  }
}
function petPaintAtt() {
  const box = $('#petAtts');
  if (!box) return;
  box.innerHTML = PET.att.map((a, i) => a.preview
    ? `<div class="pa img"><img src="${a.preview}" alt=""><span data-pet="attx" data-i="${i}" title="拿掉">×</span></div>`
    : `<div class="pa doc"><b>${esc(a.name)}</b><small>${esc(fmtBytes(a.bytes))}</small><span data-pet="attx" data-i="${i}" title="拿掉">×</span></div>`).join('');
  box.classList.toggle('on', PET.att.length > 0);
}
function petAttHtml(m) {
  const at = m.att || [];
  if (!at.length) return '';
  return `<div class="patt">${at.map((a) => a.kind === 'image' || a.preview
    ? `<img src="${a.preview || fileUrl(a.path)}" alt="" data-pet="attopen" data-path="${esc(a.path || '')}" title="${esc(a.name || '')}">`
    : `<span class="pdoc" data-pet="attopen" data-path="${esc(a.path || '')}" title="${esc(a.read === false ? '這種格式牠讀不到內容' : '')}">📄 ${esc(a.name || '檔案')}</span>`).join('')}</div>`;
}
$('#petAttIn').onchange = (e) => { petAddFiles(e.target.files); e.target.value = ''; };
$('#petIn').addEventListener('paste', (e) => {
  const fs = [...(e.clipboardData && e.clipboardData.files || [])];
  if (fs.length) { e.preventDefault(); petAddFiles(fs); }
});
// 拖進寵物欄的檔案給寵物，不要讓主輸入框也亮起「放這裡」（主視窗的 drag 樣式靠 document 的 drop 才拿掉）
['dragenter', 'dragover'].forEach((t) => $('#panePet').addEventListener(t, (e) => {
  if (![...(e.dataTransfer && e.dataTransfer.types || [])].includes('Files')) return;
  e.preventDefault(); e.stopPropagation();
  $('#panePet').classList.add('drop'); $('#cbox').classList.remove('drag');
}));
$('#panePet').addEventListener('dragleave', (e) => { if (!$('#panePet').contains(e.relatedTarget)) $('#panePet').classList.remove('drop'); });
$('#panePet').addEventListener('drop', (e) => {
  $('#panePet').classList.remove('drop'); $('#cbox').classList.remove('drag');
  if (!(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)) return;
  e.preventDefault(); e.stopPropagation();
  petAddFiles(e.dataTransfer.files);
});

async function petSay(forced) {
  const inp = $('#petIn');
  if (!inp || PET.busy) return;
  const text = String(forced || inp.value).trim();
  const att = forced ? [] : PET.att.slice();
  if (!text && !att.length) return;
  petRxClose();
  PET.busy = true; inp.value = ''; petGrow();
  if (att.length) { PET.att = []; petPaintAtt(); }
  PET.hist.push({ role: 'user', text, id: '', att: att.map((a) => ({ name: a.name, preview: a.preview, kind: a.preview ? 'image' : 'file' })) });
  petPaint();
  try {
    const d = await api('POST', '/api/pet/say', { text, session_id: S.sid || '',
      att: att.map((a) => ({ name: a.name, media_type: a.media_type, data: a.data })) });
    PET.st = d; PET.hist = d.history || PET.hist;
    PET.busy = false;
    petMaybeReveal(true);
    petPaint();
  } catch (e) {
    PET.hist.pop();
    PET.busy = false;
    if (att.length) { PET.att = att.concat(PET.att); petPaintAtt(); }
    petPaint();
    notify('error', '牠沒聽到', e.message);
    inp.value = text;
  } finally {
    PET.busy = false; petGrow();
    if (document.activeElement !== inp) inp.focus();
  }
}

async function petReact(mid, emoji) {
  if (!mid) return;
  petRxClose();
  // 先畫上去，不等伺服器
  const m = PET.hist.find((x) => x.id === mid);
  const before = m ? m.ureact : undefined;
  if (m) m.ureact = m.ureact === emoji ? '' : emoji;
  petPaint();
  try {
    const d = await api('POST', '/api/pet/react', { id: mid, emoji });
    PET.st = d.state || PET.st; PET.hist = d.history || PET.hist;
    petPaint();
  } catch (e) {
    if (m) m.ureact = before;
    petPaint();
    notify('error', '沒按到', e.message);
  }
}

function petRxClose() {
  const old = $('#petRxPick');
  if (old) old.remove();
}
function petRxPick(anchor, mid) {
  const old = $('#petRxPick');
  if (old) { const same = old.dataset.mid === mid; old.remove(); if (same) return; }
  const m = PET.hist.find((x) => x.id === mid);
  const cur = m ? m.ureact : '';
  const box = document.createElement('div');
  box.id = 'petRxPick';
  box.dataset.mid = mid;
  box.innerHTML = petReacts().map((e) => `<span class="${e === cur ? 'on' : ''}" data-e="${esc(e)}">${esc(e)}</span>`).join('');
  document.body.appendChild(box);
  const r = anchor.getBoundingClientRect();
  const w = box.offsetWidth;
  const pane = $('#panePet').getBoundingClientRect();
  box.style.left = `${Math.max(pane.left + 6, Math.min(r.left - w / 2, pane.right - w - 6))}px`;
  box.style.top = `${Math.max(8, r.top - box.offsetHeight - 6)}px`;
  box.addEventListener('click', (ev) => {
    const it = ev.target.closest('[data-e]');
    if (it) petReact(mid, it.dataset.e);
  });
  const close = (ev) => {
    if (ev && (box.contains(ev.target) || anchor.contains(ev.target))) return;
    petRxClose();
    document.removeEventListener('mousedown', close, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onKey = (ev) => { if (ev.key === 'Escape') close(); };
  setTimeout(() => {
    document.addEventListener('mousedown', close, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

// 點牠的圖＝摸頭：先動一下給回饋，連點會合併成「摸了 n 下」，停手 0.7 秒後才送
const PET_BOOP = { n: 0, t: 0 };
function petBoop() {
  const img = $('#petImg');
  if (!img || PET.busy) return;
  img.classList.remove('boop');
  void img.offsetWidth;
  img.classList.add('boop');
  PET_BOOP.n += 1;
  clearTimeout(PET_BOOP.t);
  PET_BOOP.t = setTimeout(async () => {
    const n = PET_BOOP.n;
    PET_BOOP.n = 0;
    petRxClose();
    PET.busy = true;
    PET.hist.push({ role: 'user', kind: 'poke', text: n > 1 ? `（連摸了你 ${n} 下）` : '（摸了摸你的頭）', id: '' });
    petPaint();
    try {
      const d = await api('POST', '/api/pet/poke', { n, session_id: S.sid || '' });
      PET.st = d; PET.hist = d.history || PET.hist;
      PET.busy = false;
      petMaybeReveal(true);
      petPaint();
    } catch (e) {
      PET.hist.pop();
      PET.busy = false;
      petPaint();
      notify('error', '牠沒反應', e.message);
    }
  }, 700);
}

async function petSave(patch) {
  try {
    const d = await api('PUT', '/api/pet', patch);
    PET.cfg = d.cfg; PET.st = d.state || PET.st; PET.chars = d.chars || PET.chars;
    PET.hist = d.history || [];
    PET.lastPetId = ''; petMaybeReveal(false);
    petPaint();
    return d;
  } catch (e) { notify('error', '存不起來', e.message); return null; }
}

const PET_MODES = {
  heart: { label: '情感', desc: '不看你的對話，單純陪你聊', hint: '情感模式：牠不看你的對話，會看時間偶爾找你聊兩句。' },
  mix: { label: '混合', desc: '看著你的對話，也會聊心情', hint: '牠會看著你開著的對話，偶爾說一兩句。' },
  work: { label: '工作', desc: '看著你的對話，只聊工作（個性不變）', hint: '工作模式：牠看著你的對話，只聊手上的事。' },
};
function petMode() { const k = PET.cfg && PET.cfg.mode; return PET_MODES[k] ? k : 'mix'; }
function petMenu(anchor) {
  const old = $('#petMenu');
  if (old) { old.remove(); return; }
  const r = anchor.getBoundingClientRect();
  const m = document.createElement('div');
  m.id = 'petMenu';
  m.className = 'mi petmenu';
  const cur = petMode();
  const on = !(PET.cfg && PET.cfg.on === false);
  m.innerHTML = '<div class="pm-hd">模式</div>'
    + Object.entries(PET_MODES).map(([k, x]) => `<div data-pm="mode" data-v="${k}" class="pm-mode${k === cur ? ' on' : ''}">`
      + `<span class="pm-tx"><b>${x.label}</b><small>${x.desc}</small></span><span class="pm-ck">${k === cur ? ico('check') : ''}</span></div>`).join('')
    + `<div class="pm-sep"></div><div data-pm="on" class="pm-sw"><span class="pm-tx"><b>主動說話</b><small>${on ? '會自己找你講話' : '只在你找牠時回'}</small></span><span class="sw sm${on ? ' on' : ''}"></span></div>`
    + '<div class="pm-sep"></div><div data-pm="file">編輯寵物檔</div>'
    + '<div data-pm="reset">清空牠的記憶</div><div data-pm="set">寵物設定</div>';
  document.body.appendChild(m);
  m.style.left = `${Math.max(8, r.right - 236)}px`;
  m.style.top = `${r.bottom + 6}px`;
  const close = (ev) => {
    if (ev && m.contains(ev.target)) return;
    m.remove();
    document.removeEventListener('click', close, true);
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (ev) => { if (ev.key === 'Escape') close(); };
  setTimeout(() => {
    document.addEventListener('click', close, true);
    document.addEventListener('keydown', onKey);
  }, 0);
  m.addEventListener('click', (ev) => {
    const it = ev.target.closest('[data-pm]');
    if (!it) return;
    close();
    const k = it.dataset.pm;
    if (k === 'mode') {
      const v = it.dataset.v;
      if (v !== petMode()) petSave({ mode: v }).then((r) => r && notify('ok', `切到${PET_MODES[v].label}模式`, PET_MODES[v].desc));
      return;
    }
    if (k === 'on') { petSave({ on: !on }); return; }
    if (k === 'set' || k === 'file') openSettings('pet');
    if (k === 'reset') {
      confirmBox('清掉牠記得的事？', '牠自己的對話與摘要會清空。主對話完全不受影響。', { ok: '清空', danger: true })
        .then((ok) => ok && api('POST', '/api/pet/reset', {}).then((d) => {
          PET.st = d.state || PET.st; PET.hist = []; PET.lastPetId = ''; petPaint();
          notify('ok', '牠忘記了', '重新開始。');
        }));
    }
  });
}

$('#panePet').addEventListener('click', (e) => {
  const b = e.target.closest('[data-pet]');
  if (!b) return;
  const k = b.dataset.pet;
  if (k === 'say') petSay();
  if (k === 'sug') petSay(b.dataset.say || '');
  if (k === 'menu') petMenu($('#panePet .petmore') || b);
  if (k === 'boop') petBoop();
  if (k === 'att') $('#petAttIn').click();
  if (k === 'attx') { PET.att.splice(+b.dataset.i, 1); petPaintAtt(); }
  if (k === 'attopen' && b.dataset.path) openPreview(b.dataset.path);
  if (k === 'rxpick') petRxPick(b, b.dataset.mid || '');
  if (k === 'unrx') { const m = PET.hist.find((x) => x.id === b.dataset.mid); if (m) petReact(m.id, m.ureact); }
});
// 雙擊牠的訊息＝按 ❤️（再雙擊收回），跟 Telegram 一樣
$('#panePet').addEventListener('dblclick', (e) => {
  const row = e.target.closest('.pmsg.p[data-mid]');
  if (!row || !row.dataset.mid || e.target.closest('[data-pet]')) return;
  window.getSelection && window.getSelection().removeAllRanges();
  petReact(row.dataset.mid, petReacts()[0]);
});
$('#panePet').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.id === 'petIn' && !e.shiftKey && !e.isComposing) { e.preventDefault(); petSay(); }
});

// ---- 設定 → 寵物
// 數字設定都是「拉桿＋數字框」，兩邊同步；改值只存檔、只更新說明，不重畫整頁（不會跳回頂端）
const PET_NUM = {
  every_sec: { min: 3, max: 120, step: 1, unit: '秒', desc: (v) => `每 ${v} 秒瞄一眼你開著的對話，有新回覆才叫模型，放著不花錢。情感模式不看對話，這兩項不用。` },
  peek: { min: 300, max: 8000, step: 100, unit: 'tk', desc: (v) => `從主對話抓最近約 ${Number(v).toLocaleString()} tokens 給牠看。越多越懂你在忙什麼，也越花錢。` },
  max_out: { min: 40, max: 800, step: 20, unit: 'tk', desc: (v) => `單次最多 ${v} tokens。平常牠只講一兩句，用不到上限；只有你問想法、牠講長的時候才會碰到。太低會被截斷。` },
  ctx: { min: 4000, max: 200000, step: 1000, unit: 'tk', desc: (v) => `${Number(v).toLocaleString()} tokens；超過八成牠會自己整理成筆記。` },
  keep: { min: 2, max: 60, step: 1, unit: '則', desc: (v) => `整理筆記時保留最近 ${v} 則原文，更早的變成筆記。` },
};
function petNumRow(k, title, c) {
  const o = PET_NUM[k];
  return `<div class="frow"><div><b>${title}</b><small data-petd="${k}">${o.desc(c[k])}</small></div>
    <div class="fctl numrng"><input type="range" min="${o.min}" max="${o.max}" step="${o.step}" value="${c[k]}" data-petn="${k}">
      <label class="numbox"><input type="number" inputmode="numeric" min="${o.min}" max="${o.max}" step="${o.step}" value="${c[k]}" data-petn="${k}"><i>${o.unit}</i></label></div></div>`;
}
function petQuietDesc(c) {
  const a = Number(c.quiet_from), b = Number(c.quiet_to);
  return a === b ? '兩個一樣＝整天都可以找你。' : `${a} 點到${b < a ? '隔天 ' : ''}${b} 點不會看時間找你；你還在用、牠看到對話有新東西，照樣會說。`;
}
function renderPetSec(box) {
  if (!box.innerHTML.trim()) box.innerHTML = '<div class="blank">讀取中…</div>';
  api('GET', '/api/pet').then((d) => {
    const c = d.cfg;
    const charCard = (x) => `<div class="charc${c.char === x.id ? ' on' : ''}" data-petchar="${esc(x.id)}">
        <img src="${BASE}/api/pet/sprite/happy?char=${encodeURIComponent(x.id)}&token=${encodeURIComponent(TOKEN)}" alt="">
        <div class="cc-tx"><b>${esc(x.name)}<span class="sig">${esc((x.reacts || []).slice(-1)[0] || '')}</span></b><small>${esc(x.note || '')}</small></div>
        <span class="cc-ck">${ico('check')}</span></div>`;
    const seg = (k, opts) => `<div class="seg">${opts.map(([v, n]) => `<b class="${c[k] === v ? 'on' : ''}" data-petseg="${k}" data-v="${v}">${n}</b>`).join('')}</div>`;
    const sw = (k) => `<span class="sw${c[k] ? ' on' : ''}" data-petf="${k}"></span>`;
    const row = (f, title, desc, ctl) => `<div class="frow" data-f="${f}"><div><b>${title}</b><small>${desc}</small></div><div class="fctl">${ctl}</div></div>`;
    const okModel = (m) => m.provider !== 'claude_cli' && m.kind !== 'image' && m.kind !== 'video';
    const petEffortRow = (c, mid, row) => {
      const m = (S.models || []).find((x) => x.id === mid) || {};
      const es = (m.efforts || []).map((e) => String(e).toLowerCase());
      if (!es.length) return row('pet_effort', '思考強度', `${esc(m.label || '這個模型')} 不能調思考強度。`, '');
      const cur = es.includes(String(c.effort || '').toLowerCase()) ? String(c.effort).toLowerCase() : (m.default_effort || es[es.length - 1]).toLowerCase();
      return row('pet_effort', '思考強度', `照牠目前的模型（${esc(m.label || mid)}）能選的檔位。越高越慢、越花錢，聊天用最低的就夠。`,
        `<div class="seg">${es.map((e) => `<b class="${e === cur ? 'on' : ''}" data-petseg="effort" data-v="${esc(e)}">${esc(EFFORT_LABEL[e] || e)}</b>`).join('')}</div>`);
    };
    box.innerHTML = `<div class="petset">
    <div class="card"><div class="card-hd"><b>角色</b><small>每個角色有自己的角色檔、記憶與表情圖，切換不會共用。右上角的表情是牠的招牌。</small></div>
      <div class="chargrid">${(d.chars || []).map(charCard).join('')}</div>
    </div>
    <div class="card"><div class="card-hd"><b>模式</b><small>寵物欄右上 ⋯ 也能切。你跟牠說話、摸牠，三種模式都一定會回。</small></div>
      ${row('pet_mode', '聊什麼', '情感：不看你的對話，單純陪你聊。混合：看著對話，也聊心情。工作：看著對話，只聊手上的事，個性和口頭禪照舊。', seg('mode', [['heart', '情感'], ['mix', '混合'], ['work', '工作']]))}
    </div>
    <div class="card"><div class="card-hd"><b>自己開口</b><small>牠自己開口有兩種：看到你的對話有新東西時說一句（情感模式沒有這個），或隔一段時間自己找你。</small></div>
      ${row('pet_on', '主動說話', '關掉：牠不會自己開口，也不看你的對話；你找牠時照樣回。', sw('on'))}
      ${row('pet_nudge', '看時間找你', '你一陣子沒找牠，或換到早上、中午、晚上時，牠會自己傳一句，優先接你之前提過的事。每多一則你沒回，就隔更久；連續三則沒回就不再找，等你先開口。', seg('nudge', [['off', '不找'], ['rare', '很少'], ['some', '偶爾'], ['often', '常常']]))}
      <div class="frow" data-f="pet_quiet"><div><b>安靜時段</b><small data-petquiet>${petQuietDesc(c)}</small></div>
        <div class="fctl quiet"><label class="numbox"><input type="number" min="0" max="23" step="1" value="${c.quiet_from}" data-petq="quiet_from"><i>點</i></label><span class="to">到</span><label class="numbox"><input type="number" min="0" max="23" step="1" value="${c.quiet_to}" data-petq="quiet_to"><i>點</i></label></div></div>
      <div class="petlook${c.mode === 'heart' ? ' off' : ''}" data-petlook>
      ${petNumRow('every_sec', '多久瞄一次對話', c)}
      ${petNumRow('peek', '每次看多少', c)}
      </div>
    </div>
    <div class="card"><div class="card-hd"><b>互動</b><small>點寵物欄裡牠的圖＝摸摸牠，牠會回應。</small></div>
      ${row('pet_rx', '按表情', '牠一邊說話一邊對你的訊息按表情的頻率。你只回「好」「哈哈」時，牠只按表情不說話，這種不受限。', seg('react_rate', [['off', '不按'], ['low', '偶爾'], ['high', '常常']]))}
      ${row('pet_quick', '固定回覆', '輸入框上方固定的幾顆，用 | 分隔，最多四個。上面那顆會變的是模型猜你想回的。', `<input data-petf="quick" value="${esc((c.quick || []).join('|'))}" placeholder="怎麼了？|繼續說">`)}
      ${row('pet_speak', '在主對話留言', '預設關＝完全唯讀：看得到你的對話但寫不進去。打開後，牠主動說的話也會以招牌表情開頭出現在主對話。', sw('speak_up'))}
    </div>
    <div class="card"><div class="card-hd"><b>模型</b><small>牠用自己的模型，跟主對話分開算錢。聊天用便宜快速的就夠，例如 DeepSeek Flash。</small></div>
      <div class="frow" data-f="pet_model"><div><b>服務商 → 模型</b><small>先選服務商，再選模型。不選就自動挑一個便宜的。</small></div>
        <div class="fctl">${mpPairHtml('pet:model', c.model || '', { filter: okModel, blank: '自動挑', set: (v) => petSave({ model: v }).then((r) => { if (r && r.cfg) c.model = r.cfg.model; renderPetSec(box); }) })}</div></div>
      ${petEffortRow(c, d.model, row)}
      ${petNumRow('max_out', '一次最多講多少', c)}
    </div>
    <div class="card"><div class="card-hd"><b>記憶</b><small>牠自己的對話紀錄與筆記，跟主對話的壓縮完全分開。</small></div>
      ${petNumRow('ctx', '記憶上限', c)}
      ${petNumRow('keep', '整理後留幾則', c)}
      ${row('pet_reset', `現在有 <span data-petcount>${d.state.msgs}</span> 則訊息`, '清掉不影響主對話，只是牠會忘記你們聊過什麼。', '<button class="btn" data-petf="reset">清空牠的記憶</button>')}
    </div>
    <div class="card"><div class="card-hd"><b>角色檔</b><small>只寫「牠是誰」：檔案、喜歡、討厭、小毛病、口頭禪、語氣、示範。怎麼聊（長度、按表情、輸出格式）是共用規則，不用寫在這裡。</small></div>
      <div class="petfile"><textarea id="petFile" rows="14" class="mono" spellcheck="false" placeholder="讀取中…"></textarea>
      <div class="petfile-ft"><small>存檔後，牠下一句就用新的。</small><button class="btn primary" data-petf="saveFile">儲存角色檔</button></div></div>
    </div></div>`;
    api('GET', '/api/pet/file').then((f) => { const t = $('#petFile'); if (t) t.value = f.text; });
    const saveQuiet = (patch) => petSave(patch).then((r) => {
      if (r && r.cfg) Object.assign(c, r.cfg);
      return r;
    });
    const setNum = (k, v) => {
      box.querySelectorAll(`[data-petn="${k}"]`).forEach((x) => { if (Number(x.value) !== v) x.value = v; });
      const dsc = box.querySelector(`[data-petd="${k}"]`);
      if (dsc) dsc.textContent = PET_NUM[k].desc(v);
    };
    let numTimer = 0;
    box.oninput = (e) => {
      const el = e.target.closest('[data-petn]');
      if (!el) return;
      const k = el.dataset.petn;
      const o = PET_NUM[k];
      const v = Number(el.value);
      if (!Number.isFinite(v) || v < o.min || v > o.max) return;   // 數字框打到一半先不動拉桿
      setNum(k, v);
      clearTimeout(numTimer);
      numTimer = setTimeout(() => saveQuiet({ [k]: v }), el.type === 'range' ? 300 : 700);
    };
    box.onchange = (e) => {
      const q = e.target.closest('[data-petq]');
      if (q) {
        let v = Math.round(Number(q.value));
        if (!Number.isFinite(v)) v = c[q.dataset.petq];
        v = Math.max(0, Math.min(23, v)); q.value = v;
        saveQuiet({ [q.dataset.petq]: v }).then(() => { const s = box.querySelector('[data-petquiet]'); if (s) s.textContent = petQuietDesc(c); });
        return;
      }
      const n = e.target.closest('[data-petn]');
      if (n) {
        const k = n.dataset.petn;
        const o = PET_NUM[k];
        let v = Number(n.value);
        if (!Number.isFinite(v)) v = c[k];
        v = Math.max(o.min, Math.min(o.max, Math.round(v / o.step) * o.step));
        setNum(k, v);
        n.value = v;
        clearTimeout(numTimer);
        if (v !== c[k]) saveQuiet({ [k]: v });
        return;
      }
      const el = e.target.closest('[data-petf]');
      if (el && el.dataset.petf === 'quick') {
        saveQuiet({ quick: el.value }).then((r) => { if (r && r.cfg) el.value = (r.cfg.quick || []).join('|'); });
      }
    };
    box.onclick = (e) => {
      const ch = e.target.closest('[data-petchar]');
      if (ch) {
        if (ch.dataset.petchar !== c.char) {
          box.querySelectorAll('.charc').forEach((x) => x.classList.toggle('on', x === ch));
          saveQuiet({ char: ch.dataset.petchar }).then((r) => {
            api('GET', '/api/pet/file').then((f) => { const t = $('#petFile'); if (t) t.value = f.text; });
            const cnt = box.querySelector('[data-petcount]');
            if (cnt && r && r.state) cnt.textContent = r.state.msgs;
          });
        }
        return;
      }
      const sg = e.target.closest('[data-petseg]');
      if (sg) {
        sg.parentElement.querySelectorAll('b').forEach((x) => x.classList.toggle('on', x === sg));
        if (sg.dataset.petseg === 'mode') { const lk = box.querySelector('[data-petlook]'); if (lk) lk.classList.toggle('off', sg.dataset.v === 'heart'); }
        saveQuiet({ [sg.dataset.petseg]: sg.dataset.v });
        return;
      }
      const el = e.target.closest('[data-petf]');
      if (!el) return;
      const k = el.dataset.petf;
      if (k === 'on' || k === 'speak_up') {
        const on = !el.classList.contains('on');
        el.classList.toggle('on', on);
        saveQuiet({ [k]: on });
      } else if (k === 'saveFile') {
        api('PUT', '/api/pet/file', { text: $('#petFile').value })
          .then(() => notify('ok', '角色檔存好了', '牠下一句就用新的。'))
          .catch((err) => notify('error', '存不起來', err.message));
      } else if (k === 'reset') {
        confirmBox('清掉牠記得的事？', '牠自己的對話與筆記會清空，主對話不受影響。', { ok: '清空', danger: true })
          .then((ok) => ok && api('POST', '/api/pet/reset', {}).then(() => {
            PET.hist = []; PET.lastPetId = ''; petLoad();
            const cnt = box.querySelector('[data-petcount]');
            if (cnt) cnt.textContent = '0';
          }));
      }
    };
  }).catch((err) => { box.innerHTML = `<div class="errbox">讀取失敗：${esc(err.message)}</div>`; });
}

petLoad().then(() => {
  if (PET.cfg && PET.cfg.on && localStorage.getItem('woof.rtab') !== 'panePet') openTab('panePet');
});
