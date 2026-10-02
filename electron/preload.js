// Addled — Preload Script
// Exposes safe IPC bridge to the dashboard renderer process.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // WebSocket info
  getWsPort: () => ipcRenderer.invoke('get-ws-port'),

  // App info
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  getPlatform: () => ipcRenderer.invoke('get-platform'),

  // File dialogs
  showOpenDialog: (options) => ipcRenderer.invoke('show-open-dialog', options),
  showSaveDialog: (options) => ipcRenderer.invoke('show-save-dialog', options),

  // Window controls.
  //
  // The window is frameless, so the dashboard's header draws these buttons and
  // they have no effect without a bridge to the main process. Exposed
  // individually rather than as a generic `invoke(channel, ...)`, because a
  // pass-through would let any renderer code call every `ipcMain` handler —
  // contextBridge exists to keep that surface a fixed, reviewable list.
  minimiseWindow: () => ipcRenderer.invoke('window-minimise'),
  toggleMaximiseWindow: () => ipcRenderer.invoke('window-toggle-maximise'),
  // Hides to the tray; does not quit. See the handler in main.js.
  closeWindow: () => ipcRenderer.invoke('window-close'),
  isMaximised: () => ipcRenderer.invoke('window-is-maximised'),

  // Events (main → renderer)
  onCharacterSleep: (callback) => ipcRenderer.on('character-sleep', callback),
  onCharacterWake: (callback) => ipcRenderer.on('character-wake', callback),

  // Remove listeners
  removeAllListeners: (channel) => ipcRenderer.removeAllListeners(channel),
});
