// WOOF 桌面外殼：啟動 Python 後端、開視窗、系統匣、結束時收掉整棵程序樹。
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog, Notification, protocol, net } = require('electron');
const { pathToFileURL } = require('url');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

// 專案根目錄：開發時是 desktop 的上一層；打包成 exe 後，從 exe 所在位置往上找有 woof\ 與 .venv\ 的資料夾
// （也可用環境變數 WOOF_ROOT 或 exe 旁的 woof-root.txt 指定）
function findRoot() {
  const ok = (d) => d && fs.existsSync(path.join(d, 'woof', '__main__.py')) && fs.existsSync(path.join(d, '.venv', 'Scripts', 'python.exe'));
  if (!app.isPackaged) return path.resolve(__dirname, '..');
  const exeDir = path.dirname(process.execPath);
  const hint = path.join(exeDir, 'woof-root.txt');
  const cands = [process.env.WOOF_ROOT, fs.existsSync(hint) ? fs.readFileSync(hint, 'utf8').trim() : null];
  for (let d = exeDir, i = 0; i < 5; i++, d = path.dirname(d)) cands.push(d);
  return cands.find(ok) || null;
}
const ROOT = findRoot();
const PY = ROOT ? path.join(ROOT, '.venv', 'Scripts', 'python.exe') : '';
const BRAND = path.join(__dirname, 'brand');
const ICON = path.join(BRAND, process.platform === 'win32' ? 'icon.ico' : 'icon.png');
app.setPath('userData', path.join(app.getPath('appData'), 'woof-desktop'));
// 畫面是本機檔案（web\dist），用自訂的 woof://app/ 載入，不從後端網址載入：
// 畫面的來源固定不變，後端埠每次隨機也不影響。Electron 官方建議用自訂協定取代 file://。
const WEB = ROOT ? path.join(ROOT, 'web', 'dist') : '';
protocol.registerSchemesAsPrivileged([{ scheme: 'woof', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
function registerWebProtocol() {
  protocol.handle('woof', (req) => {
    const u = new URL(req.url);
    let rel = decodeURIComponent(u.pathname).replace(/^\/+/, '') || 'index.html';
    const file = path.resolve(WEB, rel);
    if (u.host !== 'app' || !file.startsWith(WEB + path.sep)) return new Response('not found', { status: 404 });
    return net.fetch(pathToFileURL(file).toString());
  });
}
const TOKEN = crypto.randomBytes(16).toString('hex');

let win = null;
let tray = null;
let backend = null;
let port = null;
let quitting = false;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
}

function log(...a) {
  if (!ROOT) return;
  try { fs.appendFileSync(path.join(ROOT, 'data', 'desktop.log'), `[${new Date().toISOString()}] ${a.join(' ')}\n`); } catch {}
}

// ---- 介面偏好設定檔 data/ui-preferences.json（主題、我的最愛、思考強度、送出行為…）
// 主程序在畫面載入前讀好，preload 同步交給畫面；畫面改了就送回來寫檔。
// 格式與寫法和後端 woof/ui_prefs.py 一致：逐鍵合併、<檔>.lock 資料夾當鎖、先寫暫存檔再改名。
const PREFS_FILE = ROOT ? path.join(ROOT, 'data', 'ui-preferences.json') : null;
function readPrefs() {
  if (!PREFS_FILE) return { exists: false, revision: 0, values: {} };
  let raw;
  try { raw = fs.readFileSync(PREFS_FILE, 'utf8'); } catch { return { exists: false, revision: 0, values: {} }; }
  try {
    const doc = JSON.parse(raw.replace(/^\uFEFF/, ''));
    const values = {};
    for (const [k, v] of Object.entries(doc.values || {})) if (typeof v === 'string') values[k] = v;
    return { exists: true, revision: Number(doc.revision) || 0, values };
  } catch {
    try { fs.renameSync(PREFS_FILE, `${PREFS_FILE}.corrupt-${Date.now()}`); } catch {}
    return { exists: false, revision: 0, values: {} };
  }
}
function withPrefsLock(fn) {
  const lock = PREFS_FILE + '.lock';
  const end = Date.now() + 3000;
  for (;;) {
    try { fs.mkdirSync(lock); break; } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      try { if (Date.now() - fs.statSync(lock).mtimeMs > 10000) { fs.rmdirSync(lock); continue; } } catch {}
      if (Date.now() > end) throw new Error('設定檔被鎖住');
      const t = Date.now() + 20; while (Date.now() < t) {}
    }
  }
  try { return fn(); } finally { try { fs.rmdirSync(lock); } catch {} }
}
function writePrefs(changes) {
  if (!PREFS_FILE || !changes || typeof changes !== 'object') return;
  try {
    withPrefsLock(() => {
      const cur = readPrefs();
      for (const [k, v] of Object.entries(changes)) {
        if (!k.startsWith('woof.') || k.length > 256) continue;
        if (v == null) delete cur.values[k];
        else if (typeof v === 'string' && v.length <= 256 * 1024) cur.values[k] = v;
      }
      const tmp = `${PREFS_FILE}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify({ version: 1, revision: cur.revision + 1, values: cur.values }, null, 1));
      fs.renameSync(tmp, PREFS_FILE);
    });
  } catch (e) { log('寫入介面偏好失敗', e.message); }
}
ipcMain.on('prefs:snapshot', (e) => { e.returnValue = readPrefs(); });
ipcMain.on('prefs:write', (_e, changes) => writePrefs(changes));
// 畫面要連的後端（埠每次啟動隨機）
ipcMain.on('conn', (e) => { e.returnValue = port ? { base: `http://127.0.0.1:${port}`, token: TOKEN } : null; });
// 啟動畫面與視窗底色：照偏好檔裡畫面算好的配色（woof.look），開啟時不閃白
function readLook() {
  const dflt = { dark: false, bg: '#fcfdff', text: '#1f2328', dim: '#5f6878' };
  try { return { ...dflt, ...JSON.parse(readPrefs().values['woof.look']) }; } catch { return dflt; }
}
function bootStatus(text) { if (win && !port) win.webContents.send('boot', text); }

function killBackend() {
  if (!backend || backend.exitCode !== null) return;
  try {
    execFileSync('taskkill', ['/T', '/F', '/PID', String(backend.pid)], { windowsHide: true, stdio: 'ignore' });
  } catch {}
}

// 只用 WOOF 自己的 .venv：清掉從外部繼承的 Python 環境變數
function cleanEnv() {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
  for (const k of ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'CONDA_PREFIX']) delete env[k];
  return env;
}
function startBackend() {
  return new Promise((resolve, reject) => {
    if (!ROOT) return reject(new Error(`找不到 WOOF 專案資料夾（需要有 woof\\ 與 .venv\\）。把 WOOF.exe 放在專案資料夾裡，或在 exe 旁放 woof-root.txt 寫上專案路徑。`));
    if (!fs.existsSync(PY)) return reject(new Error(`找不到 Python 環境：${PY}`));
    bootStatus('正在啟動後端…');
    backend = spawn(PY, ['-m', 'woof', '--token', TOKEN, '--parent-pid', String(process.pid)], {
      cwd: ROOT, windowsHide: true, env: cleanEnv(),
    });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('後端 30 秒內沒有啟動，請看 data/woof.log')), 30000);
    backend.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      const m = buf.match(/WOOF_READY (\d+)/);
      if (!port && /Uvicorn running|Application startup/.test(buf)) bootStatus('正在載入插件…');
      if (m && !port) { bootStatus('正在開啟介面…'); port = Number(m[1]); clearTimeout(timer); resolve(port); }
    });
    backend.stderr.on('data', (d) => {
      const t = d.toString('utf8');
      if (!port && /Application startup|Uvicorn running|plugin|插件/i.test(t)) bootStatus('正在載入插件…');
      log('[backend]', t.trimEnd());
    });
    backend.on('exit', (code) => {
      log('backend exit', code);
      clearTimeout(timer);
      if (!port) reject(new Error(`後端啟動失敗（exit ${code}），請看 data/woof.log`));
      else if (!quitting && win) win.webContents.send('backend-exit', code);
    });
  });
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

const SPLASH = path.join(__dirname, 'splash.html');

function splashQuery(message) {
  const l = readLook();
  const q = new URLSearchParams({ bg: l.bg, text: l.text, dim: l.dim, dark: l.dark ? '1' : '0', v: app.getVersion() });
  if (message) q.set('error', message);
  return q.toString();
}
function showSplash(message) {
  if (!win) return;
  win.loadFile(SPLASH, { search: splashQuery(message) });
}
async function boot() {
  port = null;
  try {
    await startBackend();
  } catch (e) {
    log('start failed', e.message);
    showSplash(e.message);
    return;
  }
  win.loadURL('woof://app/index.html');
}
ipcMain.handle('boot-retry', () => { killBackend(); backend = null; showSplash(); setTimeout(boot, 300); });
ipcMain.handle('open-log', () => { if (ROOT) shell.openPath(path.join(ROOT, 'data', fs.existsSync(path.join(ROOT, 'data', 'woof.log')) ? 'woof.log' : 'desktop.log')); });
// 桌面／開始功能表捷徑：使用者在「設定 → 關於」自己按才建立（指向打包好的 WOOF.exe，圖示是狗掌）
const AUMID = app.isPackaged ? 'WOOF.Desktop' : 'WOOF.Dev';
ipcMain.handle('shortcut', (_e, where) => {
  if (!app.isPackaged) return { ok: false, error: '開發模式沒有 WOOF.exe，先執行 npm run pack' };
  const dir = where === 'start' ? path.join(app.getPath('appData'), 'Microsoft', 'Windows', 'Start Menu', 'Programs') : app.getPath('desktop');
  const file = path.join(dir, 'WOOF.lnk');
  const ok = shell.writeShortcutLink(file, fs.existsSync(file) ? 'replace' : 'create', {
    target: process.execPath, cwd: path.dirname(process.execPath), icon: process.execPath, iconIndex: 0,
    description: 'WOOF 個人 AI agent', appUserModelId: AUMID,
  });
  return ok ? { ok: true, file } : { ok: false, error: '建立失敗' };
});
ipcMain.handle('app-info', () => ({ version: app.getVersion(), electron: process.versions.electron, packaged: app.isPackaged, exe: process.execPath, root: ROOT }));

function createWindow() {
  win = new BrowserWindow({
    width: 1380, height: 880, minWidth: 900, minHeight: 600,
    frame: false, backgroundColor: readLook().bg, show: false, icon: ICON, title: 'WOOF',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, spellcheck: false },
  });
  if (process.platform === 'win32') {
    // 工作列、釘選、跳躍清單用的圖示與 ID 綁在視窗上（打包版用 exe 內嵌圖示，asar 裡的檔 Windows 讀不到）
    win.setAppDetails(app.isPackaged
      ? { appId: AUMID, appIconPath: process.execPath, appIconIndex: 0, relaunchCommand: `"${process.execPath}"`, relaunchDisplayName: 'WOOF' }
      : { appId: AUMID, appIconPath: ICON, appIconIndex: 0 });
  }
  win.loadFile(SPLASH, { search: splashQuery() });
  win.once('ready-to-show', () => win.show());
  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    log('fail-load', code, desc, url);
    if (String(url).startsWith('woof://')) showSplash('畫面載入失敗：' + desc);
  });
  win.on('close', (e) => {
    if (!quitting) { e.preventDefault(); win.hide(); }
  });
  win.on('maximize', () => win.webContents.send('win-state', 'max'));
  win.on('unmaximize', () => win.webContents.send('win-state', 'normal'));
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => {
    const local = url.startsWith('file://') || url.startsWith('woof://app/');
    if (!local) { e.preventDefault(); shell.openExternal(url); }
  });
}

function createTray() {
  const img = nativeImage.createFromPath(path.join(BRAND, 'tray.png'));
  tray = new Tray(img);
  tray.setToolTip('WOOF');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '開啟 WOOF', click: showWindow },
    { label: '開啟專案資料夾', click: () => ROOT && shell.openPath(ROOT) },
    { type: 'separator' },
    { label: '結束 WOOF', click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);
}

ipcMain.handle('win', (_e, action) => {
  if (!win) return;
  if (action === 'min') win.minimize();
  else if (action === 'max') win.isMaximized() ? win.unmaximize() : win.maximize();
  else if (action === 'close') win.hide();
  else if (action === 'quit') { quitting = true; app.quit(); }
  else if (action === 'devtools') win.webContents.toggleDevTools();
});
ipcMain.handle('open-path', (_e, p) => shell.openPath(p));
// 選資料夾（對話的工作目錄）：回傳路徑，取消回傳空字串
ipcMain.handle('pick-folder', async (e, start) => {
  const w = BrowserWindow.fromWebContents(e.sender);
  const r = await dialog.showOpenDialog(w, { title: '選擇工作目錄', defaultPath: start || undefined, properties: ['openDirectory', 'createDirectory'] });
  return r.canceled || !r.filePaths.length ? '' : r.filePaths[0];
});
ipcMain.handle('show-item', (_e, p) => shell.showItemInFolder(p));
ipcMain.handle('root', () => ROOT);
ipcMain.handle('notify', (_e, title, body) => {
  if (Notification.isSupported()) new Notification({ title: title || 'WOOF', body: String(body || '') }).show();
});

app.whenReady().then(async () => {
  // 工作列圖示跟著 AppUserModelID 找開始功能表裡同 ID 的捷徑。開發模式的通知曾讓 Electron 自動建了一個
  // 「Electron.lnk」（原子圖示）掛在舊 ID「WOOF」上，所以打包版改用自己的 ID，開發版另用一個，互不沾。
  if (process.platform === 'win32') app.setAppUserModelId(AUMID);
  registerWebProtocol();
  createWindow();
  createTray();
  await boot();
});

app.on('before-quit', () => { quitting = true; killBackend(); });
app.on('window-all-closed', () => {});
