/* 五子棋（跟寵物下一局）── gomoku 插件的前端頁面
   由 WOOF 載入後呼叫 window.WOOF.page() 註冊，開在分頁列的 ＋ 或寵物選單。
   棋由寵物的模型下、台詞也是他講，用的是他平常的記憶；他說的話出現在右邊寵物欄（這頁不另外放對話）。
   這裡只管規則（落點合不合法、誰連成五）與畫面，不會把任何程式算的候選點給模型。

   預先思考：輪到你的時候，先猜你最可能下的幾個點（你滑鼠停住的那一格、加上程式猜的兩個），
   用「你下在那裡之後」的完整棋面各問一次模型。你真的下在那裡，就直接用他已經想好的那一手；
   問的就是那個確切的棋面，所以不會不即時。沒猜中就照常問。這些猜測只用來決定要先問哪幾格，模型看不到。
   預先想的結果只有真的用到才寫進他的紀錄；你在寵物欄跟他講了話，先想好的就作廢重想（他要看到最新的對話）。 */
(function () {
  'use strict';
  const W = window.WOOF;
  if (!W || !W.page) return;

  const PLUGIN = 'gomoku';
  const SIZE = 15, N = SIZE * SIZE, EMPTY = 0, BLACK = 1, WHITE = 2;
  const DIRS = [[1, 0], [0, 1], [1, 1], [1, -1]];
  const ASK_MS = 190000;  // 一次最多等多久（後端自己會在 180 秒放棄；思考開越高越久）
  const SPEC_MAX = 3;     // 每一手最多先替你想幾個點
  const LETTERS = 'ABCDEFGHIJKLMNO';
  // 難度：想多久照他目前模型自己有的檔位（簡單＝最低、普通＝寵物設定、高手＝最高），再加上下法的說法
  const DIFF = {
    easy: { label: '簡單', hint: '他想最少、隨性下，偶爾放水' },
    normal: { label: '普通', hint: '照「設定 → 寵物」的思考強度，認真下' },
    hard: { label: '高手', hint: '用他的模型最高的思考強度、全力下；一手可能要等比較久' },
  };
  const HOVER_MS = 450;   // 滑鼠在一格停多久就開始先想

  Object.assign(window.WOOF_EN || {}, {
    '五子棋': 'Gomoku', '悔棋': 'Undo', '認輸': 'Resign', '簡單': 'Easy', '普通': 'Normal', '高手': 'Expert', '先手': 'First move',
    '再按一次確定': 'Click again to confirm', '新局': 'New game', '你先下': 'You go first', '你贏了！': 'You win!', '再來一局': 'Play again', '你先': 'You first', '他先': 'Pet first',
    '輪到你': 'Your turn', '你贏了': 'You win', '他贏了': 'Pet wins', '和棋': 'Draw', '再想一次': 'Try again',
    '預先思考': 'Think ahead', '跟他下五子棋': 'Play Gomoku with your pet',
    '先手從下一局開始換・15×15・連五即勝・無禁手': 'Side changes apply from the next game · 15×15 · Five in a row wins · No forbidden moves',
  });

  const inb = (x, y) => x >= 0 && y >= 0 && x < SIZE && y < SIZE;
  const ix = (x, y) => y * SIZE + x;
  const other = (p) => (p === BLACK ? WHITE : BLACK);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const petName = () => { try { return W.pet.name() || '他'; } catch (e) { return '他'; } };

  /* ---------------------------------------------------------- 規則 */
  function winLine(b, x, y, p) {
    if (!inb(x, y) || b[ix(x, y)] !== p) return null;
    for (const d of DIRS) {
      const cells = [[x, y]];
      for (const s of [1, -1]) {
        let cx = x + d[0] * s, cy = y + d[1] * s;
        while (inb(cx, cy) && b[ix(cx, cy)] === p) { cells.push([cx, cy]); cx += d[0] * s; cy += d[1] * s; }
      }
      if (cells.length >= 5) {
        cells.sort((a, c) => (a[0] === c[0] ? a[1] - c[1] : a[0] - c[0]));
        return cells;
      }
    }
    return null;
  }

  // 只用來猜「你」下一手可能下哪（決定先替哪幾格問模型），模型看不到
  function lineScore(b, x, y, p) {
    let sc = 0;
    for (const d of DIRS) {
      let len = 1, open = 0, cx = x + d[0], cy = y + d[1];
      while (inb(cx, cy) && b[ix(cx, cy)] === p) { len++; cx += d[0]; cy += d[1]; }
      if (inb(cx, cy) && b[ix(cx, cy)] === EMPTY) open++;
      cx = x - d[0]; cy = y - d[1];
      while (inb(cx, cy) && b[ix(cx, cy)] === p) { len++; cx -= d[0]; cy -= d[1]; }
      if (inb(cx, cy) && b[ix(cx, cy)] === EMPTY) open++;
      sc += len >= 5 ? 1e6 : len === 4 ? (open ? 1e5 / (3 - open) : 0) : len === 3 ? (open === 2 ? 1e4 : open ? 1e3 : 0) : len === 2 ? (open === 2 ? 100 : 10) : open;
    }
    return sc;
  }
  function guessHuman(b, human, n) {
    const pet = other(human), list = [];
    for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++) {
      if (b[ix(x, y)] !== EMPTY) continue;
      let near = false;
      for (let dy = -2; dy <= 2 && !near; dy++) for (let dx = -2; dx <= 2; dx++) {
        if ((dx || dy) && inb(x + dx, y + dy) && b[ix(x + dx, y + dy)] !== EMPTY) { near = true; break; }
      }
      if (near) list.push({ x, y, s: lineScore(b, x, y, human) + lineScore(b, x, y, pet) * 0.95 });
    }
    list.sort((a, c) => c.s - a.s);
    return list.slice(0, n);
  }

  /* ---------------------------------------------------------- 狀態 */
  const G = {
    el: null, cv: null, ctx: null, ui: {}, ro: null, tick: 0,
    board: new Int8Array(N), moves: [], turn: BLACK, human: BLACK,
    over: false, winner: 0, line: null, thinking: false, stuck: '', hover: null, rev: 0,
    diff: localStorage.getItem('woof.gomoku.diff') || 'normal',
    nextPet: localStorage.getItem('woof.gomoku.first') === 'pet',   // 下一局誰先（換了不會打斷這一局）
    pre: localStorage.getItem('woof.gomoku.pre') !== '0',
    score: { u: 0, p: 0, d: 0 }, rst: false, rstT: 0, t0: 0, took: 0,
    px: 0, pad: 16, cell: 30, hoverT: 0, q: null, recorded: new Set(), lastUser: '',
  };
  const SP = { map: new Map(), live: 0, n: 0 };   // 預先思考：棋面 → 模型的回答（Promise）
  const petColor = () => other(G.human);
  const petFirst = () => petColor() === BLACK;
  if (!DIFF[G.diff]) G.diff = 'normal';

  /* ---------------------------------------------------------- 畫面 */
  function build(el) {
    const ico = (n) => '<svg class="ic"><use href="#i-' + n + '"></use></svg>';
    el.innerHTML =
      '<div class="gk"><div class="gk-in">' +
      '  <div class="gk-hd">' +
      '    <div class="gk-pl gk-me"><i class="gk-dot"></i><div class="gk-who"><b>你</b><small class="gk-mec"></small></div><em class="gk-su">0</em></div>' +
      '    <div class="gk-mid"><span class="gk-turn"></span><small class="gk-took"></small>' +
      '      <span class="btn primary gk-retry" data-gk="retry" hidden>再想一次</span></div>' +
      '    <div class="gk-pl gk-pet"><em class="gk-sp">0</em><div class="gk-who"><b class="gk-pn"></b><small class="gk-pc"></small></div><span class="gk-av"></span></div>' +
      '  </div>' +
      '  <div class="gk-stage"><div class="gk-wrap"><canvas class="gk-cv"></canvas>' +
      '    <div class="gk-over"><b class="gk-rtxt"></b><small class="gk-rsub"></small><span class="btn primary" data-gk="new">再來一局</span></div></div></div>' +
      '  <div class="gk-bar">' +
      '    <span class="gk-tb" data-gk="undo">' + ico('refresh') + '悔棋</span>' +
      '    <span class="gk-tb" data-gk="resign">' + ico('x') + '<span class="gk-rsl">認輸</span></span>' +
      '    <span class="gk-tb" data-gk="new">' + ico('plus') + '新局</span>' +
      '    <span class="gk-sep"></span>' +
      '    <div class="gk-seg gk-diff">' + Object.keys(DIFF).map((k) => '<b data-gk="diff" data-v="' + k + '" title="' + esc(DIFF[k].hint) + '">' + DIFF[k].label + '</b>').join('') + '</div>' +
      '    <span class="gk-tb gk-gear" data-gk="menu" title="設定">' + ico('gear') + '</span>' +
      '    <div class="gk-pop">' +
      '      <div class="gk-row"><span>先手<small class="gk-next"></small></span><div class="gk-seg"><b data-gk="side" data-v="me">你先</b><b data-gk="side" data-v="pet">他先</b></div></div>' +
      '      <label class="gk-row gk-pre"><span>預先思考<small>先猜你會下哪，你下在那就秒回；token 多花 2～3 倍</small></span><span class="gk-sw"></span></label>' +
      '      <div class="gk-rule">先手從下一局開始換・15×15・連五即勝・無禁手</div>' +
      '    </div>' +
      '  </div>' +
      '</div></div>';
    const q = (s) => el.querySelector(s);
    G.el = el; G.cv = q('.gk-cv'); G.ctx = G.cv.getContext('2d');
    G.ui = { turn: q('.gk-turn'), took: q('.gk-took'), over: q('.gk-over'), rtxt: q('.gk-rtxt'), rsub: q('.gk-rsub'),
      undo: q('[data-gk="undo"]'), resign: q('[data-gk="resign"]'), rsl: q('.gk-rsl'), retry: q('.gk-retry'), pre: q('.gk-pre'),
      stage: q('.gk-stage'), me: q('.gk-me'), pet: q('.gk-pet'), mec: q('.gk-mec'), pc: q('.gk-pc'), pn: q('.gk-pn'),
      su: q('.gk-su'), sp: q('.gk-sp'), next: q('.gk-next'), av: q('.gk-av'), pop: q('.gk-pop'), gear: q('.gk-gear'), mid: q('.gk-mid') };
    el.addEventListener('click', onClick);
    G.cv.addEventListener('click', onBoard);
    G.cv.addEventListener('mousemove', onMove);
    G.cv.addEventListener('mouseleave', () => { G.hover = null; clearTimeout(G.hoverT); draw(); });
    G.ro = new ResizeObserver(() => resize());
    G.ro.observe(G.ui.stage);
    G.tick = setInterval(() => { if (G.thinking) updateUI(); }, 200);
    window.addEventListener('woof:event', onWoof);
    G.outside = (e) => { if (G.menu && !e.target.closest('.gk-pop,.gk-gear')) { G.menu = false; updateUI(); } };
    document.addEventListener('mousedown', G.outside);
  }

  // 你在寵物欄跟他講了話：先想好的棋是照舊的對話想的，作廢
  function onWoof(e) {
    const ev = e.detail;
    if (!ev || !String(ev.type || '').startsWith('pet')) return;
    setTimeout(updateUI, 0);   // 換了寵物（名字、頭像、模型能選的思考檔位）就跟著換
    if (!Array.isArray(ev.history) || !SP.map.size) return;
    const last = [...ev.history].reverse().find((m) => m.role !== 'pet');
    if (last && last.kind !== 'game' && last.id !== G.lastUser) { G.lastUser = last.id; SP.map.clear(); SP.n = 0; if (canPlay()) guessAhead(); }
  }
  // 寫紀錄、讓他回話：照順序一件一件來，紀錄才不會亂
  function queue(fn) { G.q = (G.q || Promise.resolve()).then(fn).catch(() => {}); return G.q; }
  function record(body) { return queue(() => W.act(PLUGIN, 'record', body)); }

  function updateUI() {
    if (!G.el) return;
    const u = G.ui, name = petName();
    const color = (p) => (p === BLACK ? '執黑' : '執白') + (p === BLACK ? '・先手' : '');
    u.pn.textContent = name;
    u.mec.textContent = color(G.human); u.pc.textContent = color(petColor());
    u.me.classList.toggle('black', G.human === BLACK); u.pet.classList.toggle('black', petColor() === BLACK);
    u.su.textContent = G.score.u; u.sp.textContent = G.score.p;
    u.me.classList.toggle('on', !G.over && G.turn === G.human);
    u.pet.classList.toggle('on', !G.over && G.turn !== G.human);
    u.pet.classList.toggle('think', G.thinking);
    const fk = G.over ? (G.winner === G.human ? 'sad' : G.winner ? 'proud' : 'happy') : G.thinking ? 'think' : G.stuck ? 'confused' : 'happy';
    let src = ''; try { src = W.pet.face(fk) || ''; } catch (e) { src = ''; }
    if (u.av.dataset.src !== src) {   // 網址裡有角色，換寵物也會換
      u.av.dataset.src = src;
      u.av.innerHTML = src ? '<img alt="" draggable="false" src="' + esc(src) + '">' : '';
    }
    let t = '', sub = '';
    if (G.over) { t = G.winner === 0 ? '和棋' : G.winner === G.human ? '你贏了' : name + '贏了'; sub = '第 ' + G.moves.length + ' 手'; }
    else if (G.thinking) { t = name + '在想'; sub = ((performance.now() - G.t0) / 1000).toFixed(1) + ' 秒'; }
    else if (G.stuck) { t = name + '卡住了'; sub = ''; u.retry.title = G.stuck; }
    else { t = G.turn === G.human ? (G.moves.length ? '輪到你' : '你先下') : '輪到' + name; sub = G.took ? '他上一手想了 ' + (G.took / 1000).toFixed(1) + ' 秒' : (G.score.d ? '和棋 ' + G.score.d + ' 局' : ''); }
    u.turn.textContent = t; u.took.textContent = sub;
    u.mid.classList.toggle('think', G.thinking);
    u.retry.hidden = !G.stuck;
    const canUndo = !G.over && G.moves.some((m) => m.p === G.human);
    u.undo.classList.toggle('dis', !canUndo);
    u.resign.classList.toggle('dis', G.over);
    u.rsl.textContent = G.rst ? '再按一次確定' : '認輸';
    u.resign.classList.toggle('danger', G.rst);
    u.pop.classList.toggle('on', !!G.menu);
    u.gear.classList.toggle('on', !!G.menu);
    u.pre.classList.toggle('on', G.pre);
    G.el.querySelectorAll('[data-gk="side"]').forEach((b) => b.classList.toggle('on', (b.dataset.v === 'pet') === G.nextPet));
    G.el.querySelectorAll('[data-gk="diff"]').forEach((b) => b.classList.toggle('on', b.dataset.v === G.diff));
    u.next.textContent = G.nextPet !== petFirst() ? '下一局換成' + (G.nextPet ? name + '先' : '你先') : '';
    u.over.classList.toggle('on', G.over);
    G.el.querySelector('.gk-wrap').classList.toggle('done', G.over);
  }

  function resize() {
    if (!G.cv) return;
    const st = G.ui.stage;
    const w = Math.max(260, Math.min(760, Math.floor(Math.min(st.clientWidth, st.clientHeight || st.clientWidth)) - 8));
    if (!st.clientWidth || w === G.px) return;
    const dpr = window.devicePixelRatio || 1;
    G.px = w; G.pad = Math.max(24, Math.round(w * 0.065)); G.cell = (w - G.pad * 2) / (SIZE - 1);
    G.cv.style.width = w + 'px'; G.cv.style.height = w + 'px';
    G.cv.width = Math.round(w * dpr); G.cv.height = Math.round(w * dpr);
    G.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function stone(ctx, cx, cy, r, p, alpha) {
    ctx.save();
    if (alpha) ctx.globalAlpha = alpha;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
    const g = ctx.createRadialGradient(cx - r * 0.38, cy - r * 0.42, r * 0.08, cx, cy, r * 1.02);
    if (p === BLACK) { g.addColorStop(0, '#7a7d84'); g.addColorStop(0.32, '#33363c'); g.addColorStop(1, '#0b0c0e'); }
    else { g.addColorStop(0, '#ffffff'); g.addColorStop(0.55, '#f3f1ec'); g.addColorStop(1, '#c9c3b6'); }
    ctx.fillStyle = g;
    if (!alpha) { ctx.shadowColor = 'rgba(40,25,5,.38)'; ctx.shadowBlur = r * 0.45; ctx.shadowOffsetX = r * 0.08; ctx.shadowOffsetY = r * 0.18; }
    ctx.fill();
    ctx.shadowColor = 'transparent';
    if (p === WHITE) { ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(70,55,30,.32)'; ctx.stroke(); }
    ctx.restore();
  }

  function draw() {
    if (!G.ctx || !G.px) return;
    const ctx = G.ctx, R = G.px, P = G.pad, c = G.cell;
    const css = getComputedStyle(G.el);
    const gc = css.getPropertyValue('--gk-grid').trim() || 'rgba(72,54,30,.6)';
    const lc = css.getPropertyValue('--gk-label').trim() || 'rgba(72,54,30,.55)';
    ctx.clearRect(0, 0, R, R);
    ctx.strokeStyle = gc;
    for (let i = 0; i < SIZE; i++) {
      const p = Math.round(P + i * c) + 0.5;
      ctx.lineWidth = i === 0 || i === SIZE - 1 ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(P, p); ctx.lineTo(R - P, p); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(p, P); ctx.lineTo(p, R - P); ctx.stroke();
    }
    ctx.fillStyle = gc;
    for (const s of [[3, 3], [11, 3], [3, 11], [11, 11], [7, 7]]) {
      ctx.beginPath(); ctx.arc(Math.round(P + s[0] * c) + 0.5, Math.round(P + s[1] * c) + 0.5, Math.max(2.5, c * 0.1), 0, Math.PI * 2); ctx.fill();
    }
    // 座標：上面 A–O、左邊 1–15
    ctx.fillStyle = lc; ctx.font = '500 ' + Math.max(9, Math.round(c * 0.3)) + 'px system-ui, "Microsoft JhengHei UI", sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (let i = 0; i < SIZE; i++) {
      ctx.fillText(LETTERS[i], P + i * c, P * 0.45);
      ctx.fillText(String(i + 1), P * 0.45, P + i * c);
    }
    const r = c * 0.45;
    for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++) {
      const v = G.board[ix(x, y)];
      if (v) stone(ctx, P + x * c, P + y * c, r, v);
    }
    // 連成五：每顆加一圈金光
    if (G.line && G.line.length >= 5) {
      ctx.save(); ctx.strokeStyle = '#f2b705'; ctx.lineWidth = Math.max(2.2, c * 0.09);
      ctx.shadowColor = 'rgba(242,183,5,.85)'; ctx.shadowBlur = c * 0.4;
      for (const p of G.line) { ctx.beginPath(); ctx.arc(P + p[0] * c, P + p[1] * c, r + 1.5, 0, Math.PI * 2); ctx.stroke(); }
      ctx.restore();
    }
    const lm = G.moves[G.moves.length - 1];
    if (lm) {
      ctx.beginPath(); ctx.arc(P + lm.x * c, P + lm.y * c, Math.max(2.6, c * 0.12), 0, Math.PI * 2);
      ctx.fillStyle = lm.p === BLACK ? '#ff6b5e' : '#e5484d'; ctx.fill();
    }
    if (G.hover && canPlay() && G.board[ix(G.hover.x, G.hover.y)] === EMPTY) stone(ctx, P + G.hover.x * c, P + G.hover.y * c, r, G.human, 0.45);
  }

  /* ---------------------------------------------------------- 操作 */
  const canPlay = () => !G.over && !G.thinking && !G.stuck && G.turn === G.human;

  function hit(e) {
    const r = G.cv.getBoundingClientRect(); if (!r.width) return null;
    const sc = G.px ? r.width / G.px : 1;
    const mx = (e.clientX - r.left) / sc, my = (e.clientY - r.top) / sc;
    const x = Math.round((mx - G.pad) / G.cell), y = Math.round((my - G.pad) / G.cell);
    if (!inb(x, y)) return null;
    if (Math.abs(mx - (G.pad + x * G.cell)) > G.cell * 0.46 || Math.abs(my - (G.pad + y * G.cell)) > G.cell * 0.46) return null;
    return { x, y };
  }
  function onBoard(e) { if (!canPlay()) return; const p = hit(e); if (p) humanPlay(p.x, p.y); }
  function onMove(e) {
    if (!canPlay()) return;
    const p = hit(e);
    const h = p && !G.board[ix(p.x, p.y)] ? p : null;
    if ((G.hover && h && G.hover.x === h.x && G.hover.y === h.y) || (!G.hover && !h)) return;
    G.hover = h; draw();
    clearTimeout(G.hoverT);
    if (h) G.hoverT = setTimeout(() => speculate(h.x, h.y), HOVER_MS);
  }
  function onClick(e) {
    const b = e.target.closest('[data-gk]');
    if (b) {
      const k = b.dataset.gk;
      if (k === 'new') { G.menu = false; reset(G.nextPet); }
      else if (k === 'menu') { G.menu = !G.menu; updateUI(); }
      else if (k === 'side') { G.nextPet = b.dataset.v === 'pet'; localStorage.setItem('woof.gomoku.first', G.nextPet ? 'pet' : 'me'); updateUI(); }
      else if (k === 'undo') undo();
      else if (k === 'resign') resign();
      else if (k === 'retry') petTurn(null);
      else if (k === 'diff' && DIFF[b.dataset.v] && b.dataset.v !== G.diff) {
        G.diff = b.dataset.v; localStorage.setItem('woof.gomoku.diff', G.diff); updateUI();
        SP.map.clear(); SP.n = 0; if (canPlay()) guessAhead();   // 先想好的是舊難度的，重想
      }
      return;
    }
    if (e.target.closest('.gk-pre')) {
      G.pre = !G.pre; localStorage.setItem('woof.gomoku.pre', G.pre ? '1' : '0');
      if (!G.pre) SP.map.clear(); else if (canPlay()) guessAhead();
      updateUI();
    }
  }

  /* ---------------------------------------------------------- 問模型 */
  function grid(b) {
    const rows = [];
    for (let y = 0; y < SIZE; y++) { const r = []; for (let x = 0; x < SIZE; x++) r.push(b[ix(x, y)]); rows.push(r); }
    return rows;
  }
  function withTimeout(p, ms) {
    let t = 0;
    return Promise.race([p, new Promise((_, rej) => { t = setTimeout(() => rej(new Error('他想太久了')), ms); })])
      .finally(() => clearTimeout(t));
  }
  function callMove(b, moves, note) {
    return withTimeout(W.act(PLUGIN, 'move', {
      board: grid(b), moves: moves.slice(-16).map((m) => [m.x, m.y, m.p]),
      pet: petColor(), human: G.human, diff: G.diff, retry_note: note || '',
    }), ASK_MS);
  }
  const usable = (r, b) => r && Array.isArray(r.move) && inb(r.move[0], r.move[1]) && b[ix(r.move[0], r.move[1])] === EMPTY;
  const keyOf = (b) => G.diff + ':' + b.join('');

  // 預先思考：假設你下在 (x,y)，先用那個棋面問模型
  function speculate(x, y) {
    if (!G.pre || !canPlay() || G.board[ix(x, y)] !== EMPTY) return;
    if (SP.n >= SPEC_MAX) return;
    const b = G.board.slice(); b[ix(x, y)] = G.human;
    if (winLine(b, x, y, G.human)) return;   // 你下這裡就贏了，不用先想
    const key = keyOf(b);
    if (SP.map.has(key)) return;
    SP.n++;
    const moves = G.moves.concat([{ x, y, p: G.human }]);
    const pr = callMove(b, moves, '').catch((e) => ({ error: e.message }));
    SP.map.set(key, { pr, t0: performance.now() });
  }
  function guessAhead() {
    if (!G.pre || !G.moves.length) return;
    const rev = G.rev;
    // 等紀錄寫好再問，他才看得到自己剛剛那句
    (G.q || Promise.resolve()).then(() => {
      if (rev !== G.rev || !canPlay()) return;
      for (const g of guessHuman(G.board, G.human, 2)) speculate(g.x, g.y);
    });
  }

  async function petTurn(spec) {
    if (G.over || !G.el) return;
    const rev = G.rev;
    G.thinking = true; G.stuck = ''; G.turn = petColor(); G.t0 = performance.now();
    updateUI(); draw();
    await (G.q || Promise.resolve());   // 前一句還在寫進紀錄就先等
    if (rev !== G.rev) return;
    let r = null, err = '', taken = '';
    if (spec) {
      r = await spec.pr;
      if (rev !== G.rev) return;
      if (!usable(r, G.board)) { err = (r && r.error) || ''; r = null; }
    }
    for (let i = 0; !r && i < 2; i++) {
      const note = !i ? '' : taken ? `上一次你選的 ${taken} 已經有子了，換一個空點。` : '上一次的回覆看不懂，這次只回 JSON。';
      try { r = await callMove(G.board, G.moves, note); } catch (e) { r = { error: e.message }; }
      if (rev !== G.rev) return;
      if (!usable(r, G.board)) { err = (r && r.error) || '回覆不合法'; taken = (r && r.taken) || ''; r = null; }
    }
    G.thinking = false;
    G.took = performance.now() - G.t0;
    if (!r) {
      G.stuck = err || '沒有回應';
      updateUI(); draw();
      return;
    }
    const hm = [...G.moves].reverse().find((m) => m.p === G.human);
    const opening = !G.moves.length;
    const res = place(r.move[0], r.move[1], petColor());
    updateUI(); draw();
    record({ human: hm && !G.recorded.has(hm) ? [hm.x, hm.y] : null, pet: r.move, say: r.say || '', face: r.face || '', opening });
    if (hm) G.recorded.add(hm);
    if (res === petColor()) react('win');
    else if (res === 0) react('draw');
    else guessAhead();
  }

  function place(x, y, p) {
    if (!inb(x, y) || G.board[ix(x, y)] !== EMPTY) return null;
    G.board[ix(x, y)] = p;
    G.moves.push({ x, y, p });
    G.turn = other(p);
    const line = winLine(G.board, x, y, p);
    if (line) { endGame(p, line); return p; }
    if (G.moves.length >= N) { endGame(0, null); return 0; }
    return null;
  }

  function humanPlay(x, y) {
    if (!canPlay() || G.board[ix(x, y)] !== EMPTY) return;
    G.hover = null; clearTimeout(G.hoverT);
    const r = place(x, y, G.human);
    updateUI(); draw();
    const spec = SP.map.get(keyOf(G.board));
    SP.map.clear(); SP.n = 0;   // 沒猜中的那幾個不要了
    if (r !== null) return;
    petTurn(spec || null);
  }

  // 開新局、悔棋、認輸、輸贏：寫進他的紀錄，讓他自己回一句（不用罐頭台詞），寵物欄看得到
  function react(event, extra) {
    const body = Object.assign({ event, score: Object.assign({}, G.score) }, extra || {});
    return queue(() => withTimeout(W.act(PLUGIN, 'react', body), ASK_MS));
  }

  function endGame(w, line, reason) {
    G.over = true; G.winner = w; G.line = line || null; G.thinking = false; G.hover = null;
    SP.map.clear(); SP.n = 0;
    if (w === 0) G.score.d++; else if (w === G.human) G.score.u++; else G.score.p++;
    G.ui.rtxt.textContent = w === 0 ? '和棋' : w === G.human ? '你贏了！' : petName() + '贏了';
    G.ui.rsub.textContent = (reason === 'resign' ? '你認輸了・' : '') + '比分 你 ' + G.score.u + '：' + G.score.p + ' ' + petName();
    G.rev++;
    const lm = G.moves[G.moves.length - 1];
    if (w === G.human) react('lose', { move: lm ? [lm.x, lm.y] : null });
    else if (reason === 'resign') react('resign');
    else if (w === 0 && lm && lm.p === G.human) react('draw');
    // 他贏、他下滿的和棋：等他那一手寫進紀錄後再說（petTurn 裡處理）
    updateUI(); draw();
  }

  function reset(byPet) {
    G.rev++;
    G.board = new Int8Array(N); G.moves = []; G.turn = BLACK; G.human = byPet ? WHITE : BLACK;
    G.over = false; G.winner = 0; G.line = null; G.thinking = false; G.stuck = ''; G.hover = null; G.took = 0;
    G.rst = false; clearTimeout(G.rstT); G.recorded = new Set();
    SP.map.clear(); SP.n = 0;
    updateUI(); draw();
    if (G.turn !== G.human) petTurn(null);
    else react('start');
  }

  function undo() {
    if (G.over || !G.moves.some((m) => m.p === G.human)) return;
    G.rev++;
    G.thinking = false; G.stuck = '';
    SP.map.clear(); SP.n = 0;
    const pop = () => { const m = G.moves.pop(); G.board[ix(m.x, m.y)] = EMPTY; return m; };
    if (G.moves[G.moves.length - 1].p === petColor()) pop();
    if (G.moves.length && G.moves[G.moves.length - 1].p === G.human) pop();
    G.line = null; G.hover = null;
    G.turn = G.moves.length ? other(G.moves[G.moves.length - 1].p) : BLACK;
    updateUI(); draw();
    react('undo');
    if (G.turn === petColor()) petTurn(null);
    else guessAhead();
  }

  function resign() {
    if (G.over) return;
    if (!G.rst) {
      G.rst = true; updateUI();
      clearTimeout(G.rstT);
      G.rstT = setTimeout(() => { G.rst = false; updateUI(); }, 2600);
      return;
    }
    clearTimeout(G.rstT); G.rst = false;
    endGame(petColor(), null, 'resign');
  }

  /* ---------------------------------------------------------- 註冊成 WOOF 的頁面 */
  W.page({
    id: 'gomoku',
    label: '五子棋',
    mount(el) {
      G.score = { u: 0, p: 0, d: 0 };
      build(el);
      reset(G.nextPet);
      requestAnimationFrame(resize); setTimeout(resize, 0);   // 視窗在背景時 rAF 不會跑
    },
    show() { G.px = 0; requestAnimationFrame(resize); setTimeout(resize, 0); },
    hide() { G.hover = null; clearTimeout(G.hoverT); },
    unmount() {
      G.rev++;
      clearInterval(G.tick); clearTimeout(G.hoverT); clearTimeout(G.rstT);
      window.removeEventListener('woof:event', onWoof);
      document.removeEventListener('mousedown', G.outside);
      if (G.ro) G.ro.disconnect();
      SP.map.clear(); SP.n = 0;
      G.el = null; G.cv = null; G.ctx = null; G.ro = null; G.thinking = false;
    },
  });
  // 測試、除錯用：看棋局狀態（不改棋局）
  W.gomoku = { state: () => ({ moves: G.moves.slice(), over: G.over, winner: G.winner, turn: G.turn, human: G.human, thinking: G.thinking, stuck: G.stuck, diff: G.diff, pre: G.pre, spec: [...SP.map.keys()].length, took: G.took, lastUser: G.lastUser }) };
})();
