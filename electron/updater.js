// Addled — Auto Updater for Windows
// Uses electron-updater with GitHub Releases backend

'use strict';

const { app, dialog, BrowserWindow, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const log = require('electron-log');
const fs = require('fs');
const path = require('path');
const https = require('https');

const GH_OWNER = '8kunoir8';
const GH_REPO = 'Addled';

// ─── Configure logging ───────────────────────────────────────────────────────
autoUpdater.logger = log;
autoUpdater.logger.transports.file.level = 'info';
log.info('App starting...');

// ─── Update server ───────────────────────────────────────────────────────────
// NOTE: GitHub retired the releases.atom feed that electron-updater's
// "github" provider depends on (404s). We discover the latest version via
// the GitHub API instead, then point electron-updater at that release's
// assets (latest.yml + blockmap) with a generic feed for delta updates.

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

const CHECK_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000; // weekly

function updateStateFile() {
  return path.join(app.getPath('userData'), 'update-check.json');
}

function loadLastCheck() {
  try {
    const data = JSON.parse(fs.readFileSync(updateStateFile(), 'utf8'));
    return typeof data.lastCheck === 'number' ? data.lastCheck : null;
  } catch {
    return null; // never checked
  }
}

function saveLastCheck(ts) {
  try {
    fs.writeFileSync(updateStateFile(), JSON.stringify({ lastCheck: ts }));
  } catch (e) {
    log.warn('Could not persist update check time:', e.message);
  }
}

function compareVersions(a, b) {
  const pa = String(a).split('.').map(Number);
  const pb = String(b).split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x !== y) return x - y;
  }
  return 0;
}

// Ask the GitHub API for the latest release, then let electron-updater
// download from that tag's assets (supports blockmap differential updates).
function checkForUpdates() {
  if (!app.isPackaged) {
    log.info('Dev mode — skipping update check.');
    return;
  }
  saveLastCheck(Date.now());
  log.info('Starting update check (GitHub API)...');

  const req = https.get(
    `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/releases/latest`,
    {
      headers: {
        'User-Agent': 'Addled-Updater',
        Accept: 'application/vnd.github+json',
      },
    },
    (res) => {
      // Private repo fallback: unauthenticated API returns 404. Keep the
      // weekly cadence running — once the repo goes public, updates flow.
      if (res.statusCode === 404) {
        log.info('Latest-release API returned 404 (repo may still be private) — will retry on the next weekly check.');
        res.resume();
        return;
      }
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        if (res.statusCode !== 200) {
          log.warn(`Update check: GitHub API status ${res.statusCode}`);
          return;
        }
        try {
          const rel = JSON.parse(body);
          const tag = rel.tag_name;
          const latest = String(tag || '').replace(/^v/, '');
          const current = app.getVersion();
          if (!latest) {
            log.warn('Update check: no tag_name in GitHub response');
            return;
          }
          log.info(`Latest release: v${latest} (current: v${current})`);
          if (compareVersions(latest, current) <= 0) {
            log.info('Up to date.');
            return;
          }
          // Newer release: feed electron-updater from that tag's assets.
          autoUpdater.setFeedURL({
            provider: 'generic',
            url: `https://github.com/${GH_OWNER}/${GH_REPO}/releases/download/${tag}/`,
          });
          autoUpdater.checkForUpdates().catch((err) => {
            log.warn('Feed check failed, offering download page:', err.message);
            offerDownloadPage(latest, rel.html_url);
          });
        } catch (e) {
          log.warn('Update check parse failed:', e.message);
        }
      });
    },
  );
  req.on('error', (err) => {
    log.warn('Update check failed (may be offline):', err.message);
  });
}

// Fallback: if the generic feed download fails, offer the release page.
function offerDownloadPage(latest, htmlUrl) {
  const win = BrowserWindow.getFocusedWindow();
  const target = htmlUrl || `https://github.com/${GH_OWNER}/${GH_REPO}/releases/latest`;
  const opts = {
    type: 'info',
    title: 'Addled Update Available',
    message: `Version v${latest} is available (current: v${app.getVersion()}).`,
    detail: 'Open the release page to download the installer.',
    buttons: ['Open Download Page', 'Remind Later'],
    defaultId: 0,
    cancelId: 1,
  };
  if (win) dialog.showMessageBox(win, opts).then(({ response }) => {
    if (response === 0) shell.openExternal(target);
  });
  else dialog.showMessageBox(opts).then(({ response }) => {
    if (response === 0) shell.openExternal(target);
  });
}

// Weekly gate: only contact GitHub for the latest release when 7+ days
// have passed since the previous check.
function maybeCheckMonthly() {
  const last = loadLastCheck();
  if (last && Date.now() - last < CHECK_INTERVAL_MS) {
    log.info('Weekly update check skipped — last check:', new Date(last).toISOString());
    return;
  }
  checkForUpdates();
}

// Check on first launch (if never checked or >7 days), then re-evaluate
// the weekly window once a day for long-running sessions.
function startUpdateChecks() {
  maybeCheckMonthly();
  setInterval(maybeCheckMonthly, 24 * 60 * 60 * 1000); // daily gate re-check
}

module.exports = { checkForUpdates, startUpdateChecks, autoUpdater };
