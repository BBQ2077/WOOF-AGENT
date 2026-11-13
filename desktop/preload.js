const { contextBridge, ipcRenderer, webFrame, webUtils } = require('electron');

// 畫面載入前同步拿到：要連的後端、介面偏好設定檔的內容（data/ui-preferences.json）
const conn = ipcRenderer.sendSync('conn');
const prefsSnapshot = ipcRenderer.sendSync('prefs:snapshot');

contextBridge.exposeInMainWorld('woofDesktop', {
  conn,
  prefs: {
    snapshot: prefsSnapshot,
    write: (changes) => ipcRenderer.send('prefs:write', changes),
  },
  win: (action) => ipcRenderer.invoke('win', action),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  showItem: (p) => ipcRenderer.invoke('show-item', p),
  readImage: (p) => ipcRenderer.invoke('read-image', p),
  saveImage: (p) => ipcRenderer.invoke('save-image', p),
  pickFolder: (start) => ipcRenderer.invoke('pick-folder', start || ''),
  root: () => ipcRenderer.invoke('root'),
  profiles: () => ipcRenderer.invoke('profiles'),
  profileDir: (pid) => ipcRenderer.invoke('profile-dir', pid),
  switchProfile: (pid) => ipcRenderer.invoke('profile-switch', pid),
  pathOf: (file) => { try { return webUtils.getPathForFile(file) || ''; } catch (e) { return ''; } },
  zoom: (f) => webFrame.setZoomFactor(Math.max(0.7, Math.min(1.5, Number(f) || 1))),
  notify: (title, body) => ipcRenderer.invoke('notify', title, body),
  onWinState: (fn) => ipcRenderer.on('win-state', (_e, s) => fn(s)),
  onBackendExit: (fn) => ipcRenderer.on('backend-exit', (_e, c) => fn(c)),
  appInfo: () => ipcRenderer.invoke('app-info'),
  remote: () => ipcRenderer.invoke('remote'),
  remoteSet: (base) => ipcRenderer.invoke('remote-set', base),
  shortcut: (where) => ipcRenderer.invoke('shortcut', where),
  onBoot: (fn) => ipcRenderer.on('boot', (_e, t) => fn(t)),
  retryBoot: () => ipcRenderer.invoke('boot-retry'),
  openLog: () => ipcRenderer.invoke('open-log'),
});
