// 打包後收尾：只留繁中、簡中、英文介面語系檔（Electron 預設帶 50 多種，佔約 40 MB）
const fs = require('fs');
const path = require('path');
const dir = path.join(__dirname, '..', 'release', 'WOOF-win32-x64', 'locales');
const keep = new Set(['en-US.pak', 'zh-TW.pak', 'zh-CN.pak']);
let n = 0;
for (const f of fs.readdirSync(dir)) if (!keep.has(f)) { fs.rmSync(path.join(dir, f)); n++; }
console.log(`locales: 移除 ${n} 個，保留 ${[...keep].join(', ')}`);
