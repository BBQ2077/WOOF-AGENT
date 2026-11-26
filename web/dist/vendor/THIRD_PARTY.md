# 第三方套件

`web/dist/vendor/` 裡的檔案是下列開源套件的原版發行檔，未經修改，各自的版權宣告保留在檔頭。

| 檔案 | 套件 | 授權 | 來源 |
|---|---|---|---|
| `highlight.min.js`、`hljs.css` | highlight.js 11.12.0（含 GitHub Dark 主題） | BSD-3-Clause | https://github.com/highlightjs/highlight.js |
| `marked.min.js` | marked 18.2.0 | MIT | https://github.com/markedjs/marked |
| `purify.min.js` | DOMPurify 3.4.17 | Apache-2.0 或 MPL-2.0 | https://github.com/cure53/DOMPurify |
| `xterm.js`、`xterm.css`、`xterm-addon-fit.js`、`xterm-addon-web-links.js` | xterm.js 與官方附加元件 | MIT | https://github.com/xtermjs/xterm.js |
| `bergamot/bergamot-translator.js` | Bergamot 翻譯引擎的 JS 接頭 v0.6.0（Mozilla Firefox Translations，配 WASM 3.0 版） | MPL-2.0 | https://github.com/mozilla/translations |

離線翻譯的模型和 WASM 引擎不放在這裡：第一次用時從 Mozilla 的 Remote Settings 下載到 `data/translate/`，逐一核對 sha256。
