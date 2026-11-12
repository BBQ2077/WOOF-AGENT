// 介面偏好：設定檔 data/ui-preferences.json 才是正本，localStorage 只當快取。
// 這支要最先載入（在 theme.js、app.js 之前）：
//   1. 先拿到要連的後端（桌面版由 preload 給；瀏覽器版用網址上的 token、同一個主機）。
//   2. 用設定檔的內容蓋掉 localStorage 裡的偏好鍵，後面的程式照常讀 localStorage 就好。
//   3. 之後任何程式寫入偏好鍵，150ms 內合併寫回設定檔（桌面版經 IPC 由主程序寫，瀏覽器版經 /api/ui-prefs）。
// 哪些算偏好（跟著設定檔走）：外觀、模型與我的最愛、思考強度、送出行為、語言、權限與模式預設。
// 不算的（留在這個視窗）：目前開的對話、欄寬、側欄開合、設定頁停在哪一頁、排程選了哪一個。
(function () {
  var D = window.woofDesktop || null;
  var qs = new URLSearchParams(location.search);
  // 要連哪台後端：桌面版由主程序決定（data/desktop-conn.json 有設就連那台），瀏覽器版用 localStorage
  // 的 woof.remoteBase（「設定 → 連線 → 連線到…」寫入），沒設就是用開啟它的那個位址。
  var remote = (D && D.conn && D.conn.remote) ? D.conn.base : '';
  if (!remote) {
    try { remote = (localStorage.getItem('woof.remoteBase') || '').trim().replace(/\/+$/, ''); } catch (e) {}
  }
  var login = null;
  try { login = JSON.parse(localStorage.getItem('woof.login') || 'null'); } catch (e) {}
  var token = (login && login.token && (!login.base || login.base === remote)) ? login.token : '';
  if (!token && !remote && D && D.conn) token = D.conn.token;
  if (!token) token = qs.get('token') || '';
  var conn = { base: remote || (D && D.conn && !D.conn.remote ? D.conn.base : ''), token: token, remote: !!remote };
  window.WOOF_CONN = conn;

  var SHARED = ['woof.theme', 'woof.look', 'woof.model', 'woof.pins', 'woof.mgrpOpen', 'woof.effortWant',
    'woof.effortLastOn', 'woof.busy', 'woof.lang', 'woof.perm', 'woof.gate'];
  var isShared = function (k) { return SHARED.indexOf(k) >= 0; };
  var ls = window.localStorage;
  var proto = Storage.prototype;
  var rawSet = proto.setItem, rawRemove = proto.removeItem;

  function browserRead() {
    try {
      var x = new XMLHttpRequest();
      x.open('GET', conn.base + '/api/ui-prefs', false);
      x.setRequestHeader('x-woof-token', conn.token);
      x.send();
      return x.status === 200 ? JSON.parse(x.responseText) : null;
    } catch (e) { return null; }
  }
  function send(changes) {
    if (D && D.prefs) { D.prefs.write(changes); return; }
    fetch(conn.base + '/api/ui-prefs', {
      method: 'PUT', keepalive: true,
      headers: { 'x-woof-token': conn.token, 'content-type': 'application/json' },
      body: JSON.stringify({ values: changes }),
    }).catch(function () {});
  }

  var snap = D && D.prefs ? D.prefs.snapshot : browserRead();
  if (snap && snap.exists) {
    SHARED.forEach(function (k) {
      var v = snap.values[k];
      if (typeof v === 'string') rawSet.call(ls, k, v); else rawRemove.call(ls, k);
    });
  } else if (snap) {
    // 第一次：還沒有設定檔，把這裡現有的偏好存成設定檔
    var seed = {}, any = false;
    SHARED.forEach(function (k) { var v = ls.getItem(k); if (v != null) { seed[k] = v; any = true; } });
    if (any) send(seed);
  }

  var pending = {}, timer = null;
  function flush() {
    clearTimeout(timer); timer = null;
    var keys = Object.keys(pending);
    if (!keys.length) return;
    var out = pending; pending = {};
    send(out);
  }
  function queue(k, v) {
    pending[k] = v;
    if (!timer) timer = setTimeout(flush, 150);
  }
  proto.setItem = function (k, v) {
    rawSet.call(this, k, v);
    if (this === ls && isShared(k)) queue(k, String(v));
  };
  proto.removeItem = function (k) {
    rawRemove.call(this, k);
    if (this === ls && isShared(k)) queue(k, null);
  };
  window.addEventListener('pagehide', flush);
})();
