// Addled — Auto Updater for Windows
// Uses electron-updater with GitHub Releases backend

'use strict';

const { app, dialog, BrowserWindow } = require('electron');
const { autoUpdater } = require('electron-updater');
const log = require('electron-log');

// ─── Configure logging ───────────────────────────────────────────────────────
autoUpdater.logger = log;
autoUpdater.logger.transports.file.level = 'info';
log.info('App starting...');

// ─── Update server ───────────────────────────────────────────────────────────
autoUpdater.setFeedURL({
  provider: 'github',
  owner: '8kunoir8',
  repo: 'Addled',
  releaseType: 'release',
});

// ─── Disable auto-download — we'll prompt the user ───────────────────────────
autoUpdater.autoDownload = false;
autoUpdater.autoInstallOnAppQuit = true;

// ─── Event handlers ──────────────────────────────────────────────────────────

autoUpdater.on('checking-for-update', () => {
  log.info('Checking for updates...');
});

autoUpdater.on('update-available', (info) => {
  log.info('Update available:', info.version);
  const win = BrowserWindow.getFocusedWindow();
  if (!win) return;

  dialog.showMessageBox(win, {
    type: 'info',
    title: 'Addled Update Available',
    message: `Version ${info.version} is available.\n\nCurrent: ${app.getVersion()}`,
    detail: info.releaseNotes
      ? `Release notes:\n${String(info.releaseNotes).slice(0, 500)}`
      : 'Would you like to download it now?',
    buttons: ['Download', 'Remind Later'],
    defaultId: 0,
    cancelId: 1,
  }).then(({ response }) => {
    if (response === 0) {
      autoUpdater.downloadUpdate();
    }
  });
});

autoUpdater.on('update-not-available', () => {
  log.info('No updates available.');
});

autoUpdater.on('download-progress', (progress) => {
  const win = BrowserWindow.getFocusedWindow();
  if (win) {
    win.setProgressBar(progress.percent / 100);
    win.webContents.send('update-progress', progress.percent);
  }
  log.info(`Download: ${Math.round(progress.percent)}% (${progress.bytesPerSecond} b/s)`);
});

autoUpdater.on('update-downloaded', (info) => {
  log.info('Update downloaded:', info.version);
  const win = BrowserWindow.getFocusedWindow();
  if (win) win.setProgressBar(-1); // Reset

  dialog.showMessageBox({
    type: 'info',
    title: 'Addled Update Ready',
    message: `Version ${info.version} has been downloaded.`,
    detail: 'The update will be installed when you restart Addled.',
    buttons: ['Restart Now', 'Later'],
    defaultId: 0,
  }).then(({ response }) => {
    if (response === 0) {
      autoUpdater.quitAndInstall(true, true);
    }
  });
});

autoUpdater.on('error', (err) => {
  log.error('Update error:', err.message);
  const win = BrowserWindow.getFocusedWindow();
  if (win) win.setProgressBar(-1);
});

// ─── Export check function ───────────────────────────────────────────────────

function checkForUpdates() {
  if (app.isPackaged) {
    log.info('Starting update check...');
    autoUpdater.checkForUpdates().catch(err => {
      log.warn('Update check failed (may be offline):', err.message);
    });
  } else {
    log.info('Dev mode — skipping update check.');
  }
}

// Check on startup, then every 4 hours
function startUpdateChecks() {
  checkForUpdates();
  setInterval(checkForUpdates, 4 * 60 * 60 * 1000); // 4 hours
}

module.exports = { checkForUpdates, startUpdateChecks, autoUpdater };
