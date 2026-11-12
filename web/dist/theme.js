/* 外觀：色彩模式、色調、強調色、縮放。放在 <head> 同步載入，避免開啟時閃色。 */
(function () {
  'use strict';
  var KEY = 'woof.theme';
  // 預設「墨黑」：跟狗掌標誌同一組黑白，按鈕與選取用文字色，不帶藍
  // accentSat：強調色濃度，0＝黑白（墨色），100＝原本的彩色強調
  var DEFAULTS = { mode: 'light', hue: 220, tint: 6, accent: 220, accentSat: 0, scale: 100, chatSize: 14 };
  var PRESETS = {
    '墨黑': { hue: 220, tint: 6, accent: 220, accentSat: 0 },
    '純白': { hue: 220, tint: 0, accent: 220, accentSat: 0 },
    '暖白': { hue: 38, tint: 30, accent: 24, accentSat: 100 },
    '白藍': { hue: 215, tint: 30, accent: 220, accentSat: 100 },
    '薄荷': { hue: 160, tint: 30, accent: 165, accentSat: 100 },
    '薰衣草': { hue: 265, tint: 30, accent: 262, accentSat: 100 },
  };

  function read() {
    var t = {};
    try { t = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { t = {}; }
    var out = {};
    for (var k in DEFAULTS) out[k] = t[k] == null ? DEFAULTS[k] : t[k];
    return out;
  }

  function isDark(mode) {
    if (mode === 'dark') return true;
    if (mode === 'system') return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    return false;
  }

  function hsl(h, s, l, a) {
    s = Math.max(0, Math.min(100, s));
    return a == null ? 'hsl(' + h + ' ' + s + '% ' + l + '%)' : 'hsl(' + h + ' ' + s + '% ' + l + '% / ' + a + ')';
  }

  function vars(t) {
    var h = +t.hue, s = +t.tint, a = +t.accent, k = Math.max(0, Math.min(100, +t.accentSat)) / 100;
    var accD = 90 - 24 * k, accL = 14 + 35.6 * k;
    if (isDark(t.mode)) {
      return {
        '--bg': hsl(h, s * 0.3, 9), '--panel': hsl(h, s * 0.3, 7), '--panel2': hsl(h, s * 0.3, 12),
        '--line': hsl(h, s * 0.3, 19), '--line2': hsl(h, s * 0.3, 25),
        '--text': '#e6e8ee', '--dim': hsl(h, 8, 62), '--faint': hsl(h, 8, 45),
        '--acc': hsl(a, 90 * k, accD), '--acc2': hsl(a, 90 * k, Math.min(97, accD + 8)), '--acc-soft': hsl(a, 90 * k, accD, 0.12 + 0.04 * k),
        '--on-acc': '#0b0d12', '--sel': 'rgb(255 255 255 / .09)', '--hover': 'rgb(255 255 255 / .06)',
        '--focus': 'rgb(255 255 255 / .32)', '--focus-ring': 'rgb(255 255 255 / .06)',
        '--bubble': hsl(h, s * 0.3, 16), '--code': hsl(h, s * 0.3, 6), '--code-text': '#c9d1d9',
        '--code-inline': 'rgb(255 255 255 / .08)', '--shadow': '0 12px 32px rgb(0 0 0 / .45)',
        '--ok': '#4cc38a', '--warn': '#e5b454', '--bad': '#ef5d62',
        '--err-bg': 'rgb(239 93 98 / .1)', '--err-text': '#ff9ea0',
      };
    }
    return {
      '--bg': hsl(h, s * 0.3, 99.6), '--panel': hsl(h, s, 97.3), '--panel2': '#ffffff',
      '--line': hsl(h, s * 1.6, 90), '--line2': hsl(h, s * 0.9, 83),
      '--text': '#1f2328', '--dim': hsl(h, 6, 40), '--faint': hsl(h, 5, 58),
      '--acc': hsl(a, 100 * k, accL), '--acc2': hsl(a, 95 * k, k ? accL - 9.6 : 28), '--acc-soft': hsl(a, 100 * k, k ? 50 : 20, k ? 0.1 : 0.07),
      '--on-acc': '#ffffff', '--sel': hsl(h, s * 0.7, 92.5), '--hover': 'rgb(0 0 0 / .045)',
      '--bubble': hsl(h, s * 0.45, 95.3), '--code': hsl(h, s * 0.5, 97.6), '--code-text': '#1f2328',
      '--code-inline': 'rgb(0 0 0 / .055)', '--shadow': '0 10px 30px rgb(15 23 42 / .12)',
      '--focus': hsl(h, s * 0.6, 68), '--focus-ring': 'rgb(15 23 42 / .05)',
      '--ok': '#1f8a65', '--warn': '#b7791f', '--bad': '#d13b4f',
      '--err-bg': 'rgb(209 59 79 / .07)', '--err-text': '#a61e40',
    };
  }

  // hsl()/#hex → #rrggbb（BrowserWindow 底色只吃 hex）
  function toHex(c) {
    if (/^#[0-9a-f]{6}$/i.test(c)) return c;
    var m = /hsl\(([\d.]+) ([\d.]+)% ([\d.]+)%/.exec(c);
    if (!m) return c;
    var h = +m[1] / 360, s = +m[2] / 100, l = +m[3] / 100;
    function f(n) { var k = (n + h * 12) % 12, a = s * Math.min(l, 1 - l); var x = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)); return ('0' + Math.round(x * 255).toString(16)).slice(-2); }
    return '#' + f(0) + f(8) + f(4);
  }

  function apply(t) {
    t = t || read();
    var root = document.documentElement;
    var v = vars(t);
    for (var k in v) root.style.setProperty(k, v[k]);
    root.style.setProperty('--chat-fs', (+t.chatSize || 14) + 'px');
    root.classList.toggle('dark', isDark(t.mode));
    root.style.colorScheme = isDark(t.mode) ? 'dark' : 'light';
    if (window.woofDesktop && window.woofDesktop.zoom) window.woofDesktop.zoom((+t.scale || 100) / 100);
    // 讓啟動畫面與視窗底色跟介面一致：算好的配色存成偏好 woof.look（跟著設定檔，主程序開視窗前讀）
    var look = JSON.stringify({ dark: isDark(t.mode), bg: toHex(v['--bg']), text: toHex(v['--text']), dim: toHex(v['--dim']) });
    if (localStorage.getItem('woof.look') !== look) localStorage.setItem('woof.look', look);
  }

  function set(patch) {
    var t = read();
    for (var k in patch) t[k] = patch[k];
    localStorage.setItem(KEY, JSON.stringify(t));
    apply(t);
    return t;
  }

  function reset() { localStorage.removeItem(KEY); apply(read()); return read(); }

  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (read().mode === 'system') apply();
    });
  }
  window.WoofTheme = { read: read, set: set, reset: reset, apply: apply, presets: PRESETS, defaults: DEFAULTS };
  apply();
})();
