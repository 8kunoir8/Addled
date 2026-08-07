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

  // Events (main → renderer)
  onCharacterSleep: (callback) => ipcRenderer.on('character-sleep', callback),
  onCharacterWake: (callback) => ipcRenderer.on('character-wake', callback),

  // Remove listeners
  removeAllListeners: (channel) => ipcRenderer.removeAllListeners(channel),
});
