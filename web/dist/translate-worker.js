// 離線英翻中的 Web Worker：主程式把 bergamot-translator.js（MPL-2.0）和這支合成一份 Blob 啟動。
// 只用 CPU（WASM）；模型檔由主程式從後端讀好再傳進來。
//   {type:'init', files:{wasm, model, lex, srcvocab, trgvocab}} → {type:'ready', ms}
//   {type:'tr', id, text} → {type:'done', id, text, ms} 或 {type:'fail', id, error}
/* global loadBergamot */
'use strict';
const ALIGN = { model: 256, lex: 64, srcvocab: 64, trgvocab: 64 };
let B = null, model = null, svc = null;

function textConfig(cfg) {
  let s = '\n';
  for (const [k, v] of Object.entries(cfg)) s += `            ${k}: ${v}\n`;
  return s + '            ';
}

function init(files) {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    B = loadBergamot({
      INITIAL_MEMORY: 41943040, print: () => {}, printErr: () => {}, wasmBinary: files.wasm,
      onAbort() { reject(new Error('翻譯引擎載入失敗')); },
      onRuntimeInitialized() {
        setTimeout(() => {
          try {
            const mem = {};
            for (const k of Object.keys(ALIGN)) {
              const m = new B.AlignedMemory(files[k].byteLength, ALIGN[k]);
              m.getByteArrayView().set(new Uint8Array(files[k]));
              mem[k] = m;
            }
            const vocab = new B.AlignedMemoryList();
            vocab.push_back(mem.srcvocab); vocab.push_back(mem.trgvocab);
            // 跟 Firefox 用的設定一樣（beam 1、關掉品質評分）
            model = new B.TranslationModel('en', 'zh-Hant', textConfig({
              'beam-size': '1', normalize: '1.0', 'word-penalty': '0', 'max-length-break': '128',
              'mini-batch-words': '1024', workspace: '128', 'max-length-factor': '2.0', 'skip-cost': 'true',
              'cpu-threads': '0', quiet: 'true', 'quiet-translation': 'true', 'gemm-precision': 'int8shiftAlphaAll',
              alignment: 'soft',
            }), mem.model, mem.lex, vocab, null);
            svc = new B.BlockingService({ cacheSize: 0 });
            resolve(performance.now() - t0);
          } catch (e) { reject(e); }
        }, 0);
      },
    });
  });
}

// 這行要不要翻：沒有英文字母、程式碼縮排、符號比字母多（路徑、指令、JSON）就原樣保留
function shouldTranslate(line) {
  const t = line.trim();
  if (!/[A-Za-z]{2}/.test(t)) return false;
  if (/^( {4}|\t)/.test(line) && !/^\s*([-*+]|\d+[.)])\s/.test(line)) return false;
  const letters = (t.match(/[A-Za-z]/g) || []).length;
  const symbols = (t.match(/[{}[\]()<>=;$\\|/_`"'#@&*^~]/g) || []).length;
  if (symbols > letters * 0.5 && !/\s[a-z]+\s[a-z]+\s/i.test(t)) return false;
  return true;
}

const CJK = '\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef';
const PUNCT = { ',': '，', ':': '：', ';': '；', '!': '！', '?': '？', '(': '（', ')': '）' };
function polish(s) {
  return s
    .replace(new RegExp(`([${CJK}])\\s*([,:;!?])(?!\\d)\\s*`, 'g'), (m, a, p) => a + PUNCT[p])
    .replace(new RegExp(`([\`)])\\s*([,;])\\s*(?=[${CJK}])`, 'g'), (m, a, p) => a + PUNCT[p])
    .replace(new RegExp(`([${CJK}])\\s*\\(([^()]*[${CJK}][^()]*)\\)`, 'g'), (m, a, b) => `${a}（${b}）`)
    .replace(new RegExp(`([${CJK}]) +(?=[${CJK}])`, 'g'), '$1');
}

// 路徑、檔名、變數名、函式呼叫：翻譯前包上反引號，引擎會原樣保留反引號裡的字
const CODEISH = /(`[^`\n]*`)|((?:[A-Za-z]:)?[\w.~-]*[\\/][\w.\\/~-]+|\b[A-Za-z_][\w]*(?:_[\w]+|\.[A-Za-z_]\w*)+(?:\(\))?|\b[A-Za-z_]\w*\(\)|--?[a-z][\w-]+)/g;
function guard(s) {
  const added = [];
  const t = s.replace(CODEISH, (m, ticked, code) => {
    if (ticked) return ticked;
    added.push(code);
    return '`' + code + '`';
  });
  return [t, added];
}

function translate(text) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  const out = lines.slice();
  const jobs = [];   // [行號, 前綴, 內容]
  let fence = false;
  lines.forEach((line, i) => {
    if (/^\s*(```|~~~)/.test(line)) { fence = !fence; return; }
    if (fence || !shouldTranslate(line)) return;
    const m = /^(\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*|#{1,6}\s+)*(?:\*\*|__)?)(.*?)((?:\*\*|__)?[:：]?\s*)$/.exec(line);
    const [body, added] = guard(m[2]);
    jobs.push([i, m[1], body, added, m[3]]);
  });
  if (!jobs.length) return text;
  const msgs = new B.VectorString(), opts = new B.VectorResponseOptions();
  for (const [, , body] of jobs) { msgs.push_back(body); opts.push_back({ qualityScores: false, alignment: false, html: false }); }
  const res = svc.translate(model, msgs, opts);
  // 加上的反引號留著：顯示時路徑、變數名用程式字型，底線不會被 Markdown 吃掉
  jobs.forEach(([i, pre, , , post], k) => { out[i] = pre + polish(res.get(k).getTranslatedText()) + post; });
  msgs.delete(); opts.delete(); res.delete();
  return out.join('\n');
}

self.onmessage = async (e) => {
  const d = e.data || {};
  if (d.type === 'init') {
    try { self.postMessage({ type: 'ready', ms: await init(d.files) }); }
    catch (err) { self.postMessage({ type: 'error', error: String(err && err.message || err) }); }
  } else if (d.type === 'tr') {
    const t0 = performance.now();
    try { self.postMessage({ type: 'done', id: d.id, text: translate(d.text), ms: performance.now() - t0 }); }
    catch (err) { self.postMessage({ type: 'fail', id: d.id, error: String(err && err.message || err) }); }
  }
};
