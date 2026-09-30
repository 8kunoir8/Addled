// Addled — Electron Shell
// Created by Kunoir — 2026

'use strict';

const { app, BrowserWindow, shell, ipcMain, dialog, screen, Tray, Menu, nativeImage } = require('electron');
const { spawn, spawnSync } = require('child_process');
const net = require('net');
const fs = require('fs');
const path = require('path');
const http = require('http');

// ─── Windows App User Model ID ───────────────────────────────────────────────
if (process.platform === 'win32') {
  app.setAppUserModelId('Addled');
}

// ─── Single-instance lock ─────────────────────────────────────────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}
app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
});

// ─── Globals ──────────────────────────────────────────────────────────────────
let mainWindow = null;
let tray = null;
let pythonProcess = null;
let pythonRestarts = 0;
let nextProcess = null;
let navPollTimer = null;
let dashboardUrl = null;
const isDev = !app.isPackaged;
const DASHBOARD_PORT = 3000;
const WS_PORT = 9876;

// ─── Auto-updater ─────────────────────────────────────────────────────────────
let updater = null;
try {
  updater = require('./updater');
} catch (e) {
  console.log('[Addled] Auto-updater not available:', e.message);
}

// ─── Paths ────────────────────────────────────────────────────────────────────
const ROOT_DIR = isDev
  ? path.join(__dirname, '..')
  : path.join(path.dirname(app.getPath('exe')), 'resources');
const BACKEND_DIR = isDev
  ? path.join(ROOT_DIR, 'backend')
  : path.join(process.resourcesPath, 'backend');
const DASHBOARD_DIR = isDev
  ? path.join(ROOT_DIR, 'dashboard')
  : path.join(process.resourcesPath, 'dashboard');

// The bundled llamafile server that the backend starts on demand. It holds
// several gigabytes of VRAM/RAM, so it must never outlive the app.
const LLAMAFILE_EXE = path.join(BACKEND_DIR, 'memory', 'models', 'llamafile',
                                'llamafile.exe');

/**
 * Kill any running bundled llamafile server.
 *
 * `pythonProcess.kill()` is a hard terminate on Windows: the backend is given
 * no chance to run its own `local_llm.stop()`, so the model server it spawned
 * survived every app exit. A leftover held ~5.8 GB of VRAM for hours with no
 * window open to explain it, and the only cleanup was `_reap_orphans()` at the
 * NEXT startup.
 *
 * Matching is by image path, so an unrelated llamafile the user runs from
 * somewhere else is left alone. This is best-effort and synchronous on
 * purpose: there is no time for async work in `before-quit`.
 */
function killLlamafile() {
  if (process.platform !== 'win32') return;
  try {
    // Only processes whose image is exactly our bundled runtime are hit.
    const find = spawnSync('powershell', [
      '-NoProfile', '-NonInteractive', '-Command',
      'Get-CimInstance Win32_Process -Filter "Name=\'llamafile.exe\'" | ' +
      'Where-Object { $_.ExecutablePath -eq $env:ADDLED_LF } | ' +
      'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }',
    ], {
      timeout: 8000,
      env: { ...process.env, ADDLED_LF: LLAMAFILE_EXE },
      windowsHide: true,
    });
    if (find.error) {
      console.log('[Addled] llamafile cleanup error:', find.error.message);
    }
  } catch (e) {
    console.log('[Addled] llamafile cleanup failed:', e.message);
  }
}

// In packaged mode, resolve Python from bundled resources or common install locations
function resolvePython() {
  if (isDev) return process.platform === 'win32' ? 'python' : 'python3';

  // Check common Windows Python install locations
  const candidates = [
    path.join(ROOT_DIR, 'python', 'python.exe'),           // bundled
    path.join(process.resourcesPath, 'python', 'python.exe'), // extraResources
    'python',                                                // PATH
    'python3',
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python314', 'python.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python313', 'python.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python312', 'python.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python311', 'python.exe'),
    path.join('C:', 'Python314', 'python.exe'),
    path.join('C:', 'Python313', 'python.exe'),
    path.join('C:', 'Python312', 'python.exe'),
  ];

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
    // For PATH-based ones, check via spawnSync
    if (candidate === 'python' || candidate === 'python3') {
      try {
        const r = require('child_process').spawnSync(candidate, ['--version'], { timeout: 3000 });
        if (r.status === 0) return candidate;
      } catch {}
    }
  }
  return 'python'; // fallback — will fail but show error
}

// In packaged mode, resolve Node.js from Electron's built-in node
function resolveNode() {
  if (isDev) return process.platform === 'win32' ? 'node.exe' : 'node';

  // Electron bundles its own Node.js — use it
  const electronNode = path.join(
    path.dirname(app.getPath('exe')),
    process.platform === 'win32' ? 'node.exe' : 'node'
  );

  // If not found, try system PATH
  return fs.existsSync(electronNode) ? electronNode : (process.platform === 'win32' ? 'node.exe' : 'node');
}

// ─── Port detection ───────────────────────────────────────────────────────────
function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, '127.0.0.1');
  });
}

async function findFreePort(start, count) {
  for (let i = 0; i < count; i++) {
    if (await isPortFree(start + i)) return start + i;
  }
  return start;
}

// ─── Spawn Python Backend ─────────────────────────────────────────────────────
function startPythonBackend() {
  const pythonCmd = resolvePython();

  // Verify Python exists before spawning
  if (!isDev) {
    try {
      const check = require('child_process').spawnSync(pythonCmd, ['--version'], { timeout: 5000 });
      if (check.error || check.status !== 0) {
        console.error('[Python] Python not found. Install Python 3.11+ or add it to PATH.');
        console.error('[Python] Download: https://www.python.org/downloads/');
        return;
      }
    } catch (e) {
      console.error('[Python] Cannot run Python:', e.message);
      return;
    }
  }

  pythonProcess = spawn(pythonCmd, ['main.py'], {
    cwd: BACKEND_DIR,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONNOUSERSITE: '1', // isolate bundled Python from any user site-packages
    },
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[Python:err] ${data.toString().trim()}`);
  });

  // ── Self-healing: respawn the backend if it crashes ─────────────────────

  pythonProcess.on('close', (code) => {
    console.log(`[Python] Process exited with code ${code}`);
    pythonProcess = null;
    if (app.isQuitting) return;
    pythonRestarts += 1;
    if (pythonRestarts > 5) {
      console.error('[Python] Backend crashed 5 times — giving up. Restart Addled.');
      return;
    }
    console.log(`[Python] Respawning backend in 3s (attempt ${pythonRestarts}/5)...`);
    setTimeout(() => {
      if (!app.isQuitting) startPythonBackend();
    }, 3000);
  });

  pythonProcess.on('error', (err) => {
    console.error(`[Python] Failed to start: ${err.message}`);
    pythonProcess = null;
  });
}

// ─── Spawn Next.js Dashboard ──────────────────────────────────────────────────
function startNextDashboard() {
  if (isDev) {
    // In dev, Next.js runs separately
    console.log('[Next.js] Dev mode — start dashboard with: cd dashboard && npm run dev');
    return;
  }

  // In packaged mode, try running next start with system Node.js
  const nodeCmd = resolveNode();
  const nextBin = path.join(DASHBOARD_DIR, 'node_modules', '.bin', 'next');

  if (!fs.existsSync(nextBin + (process.platform === 'win32' ? '.cmd' : ''))) {
    console.warn('[Next.js] Dashboard server not found. Will try connecting to localhost:3000');
    console.warn('[Next.js] Start the dashboard manually: cd dashboard && npm run dev');
    return;
  }

  nextProcess = spawn(nodeCmd, [nextBin, 'start', '-p', String(DASHBOARD_PORT)], {
    cwd: DASHBOARD_DIR,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, NODE_ENV: 'production' },
  });

  nextProcess.stdout.on('data', (data) => {
    console.log(`[Next.js] ${data.toString().trim()}`);
  });

  nextProcess.stderr.on('data', (data) => {
    console.error(`[Next.js:err] ${data.toString().trim()}`);
  });

  nextProcess.on('close', (code) => {
    console.log(`[Next.js] Process exited with code ${code}`);
    nextProcess = null;
  });

  nextProcess.on('error', (err) => {
    console.error(`[Next.js] Failed to start: ${err.message}`);
    nextProcess = null;
  });
}

// ─── GUI navigation polling (backend → window show + navigate) ─────────────
function startNavPolling() {
  if (navPollTimer) return;
  navPollTimer = setInterval(() => {
    const req = http.get('http://127.0.0.1:9877/api/nav', (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        try {
          const j = JSON.parse(data);
          if (j && j.path && dashboardUrl) {
            const target = dashboardUrl + j.path;
            if (mainWindow && !mainWindow.isDestroyed()) {
              if (mainWindow.webContents.getURL() !== target) {
                mainWindow.loadURL(target);
              }
              mainWindow.show();
              mainWindow.focus();
            }
          }
        } catch (e) { /* ignore malformed */ }
      });
    });
    req.on('error', () => {});
    req.setTimeout(2000, () => req.destroy());
  }, 1500);
}

// ─── Create Main Window ───────────────────────────────────────────────────────
async function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  mainWindow = new BrowserWindow({
    width: Math.min(1280, width),
    height: Math.min(800, height),
    minWidth: 900,
    minHeight: 600,
    title: 'Addled',
    icon: path.join(__dirname, 'icons', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
    backgroundColor: '#0d1117',
  });

  // Load dashboard — static files in production, dev server in dev
  if (isDev) {
    dashboardUrl = `http://localhost:${DASHBOARD_PORT}`;
  } else {
    // Start a local static file server for the dashboard
    const servePort = 3001;
    const outDir = isDev
      ? path.join(DASHBOARD_DIR, 'out')
      : path.join(DASHBOARD_DIR); // extraResources maps dashboard/out → dashboard/

    if (fs.existsSync(outDir)) {
      // Simple static file server for Next.js export
      const root = path.resolve(outDir);
      const isInside = (candidate) =>
        candidate === root || candidate.startsWith(root + path.sep);

      const server = http.createServer((req, res) => {
        const rawPath = req.url === '/' ? '/index.html' : req.url.split('?')[0];

        // Decode, resolve, then verify containment. `path.join` silently
        // collapses '..', so without this check a request for
        // /../../backend/memory/settings.json escapes the build directory and
        // is served — that file holds every API key this app has.
        let urlPath = rawPath;
        try {
          urlPath = decodeURIComponent(rawPath);
        } catch {
          res.writeHead(400, { 'Content-Type': 'text/plain' });
          res.end('Bad request');
          return;
        }

        let filePath = path.resolve(root, '.' + urlPath);
        if (!isInside(filePath)) {
          console.warn(`[Dashboard] Refused a path outside the build: ${urlPath}`);
          res.writeHead(404, { 'Content-Type': 'text/plain' });
          res.end('Not found');
          return;
        }

        // Next's static export writes a route's RSC payload to
        //   <route>/__next.<route>/__PAGE__.txt
        // but the client REQUESTS it as
        //   <route>/__next.<route>.__PAGE__.txt
        // — dot-joined, not slash-joined. The root route happens to have the
        // dot form (__next.__PAGE__.txt) so it always worked, and only
        // sub-routes 404'd. The visible effect was not a broken page: it was
        // every sidebar prefetch failing, which silently downgrades Next's
        // client-side navigation to a full page load.
        //
        // Mapped explicitly rather than by rewriting every dot, because a real
        // filename may legitimately contain one.
        if (!fs.existsSync(filePath)) {
          const dotRsc = /^(.*\/)?(__next\.[^/]+)\.(__PAGE__|_full|_tree)\.txt$/
            .exec(urlPath);
          if (dotRsc) {
            const candidate = path.resolve(
              root, '.' + (dotRsc[1] || '/') + dotRsc[2] + '/' + dotRsc[3] + '.txt');
            if (isInside(candidate) && fs.existsSync(candidate)) {
              filePath = candidate;
            }
          }
        }

        if (fs.existsSync(filePath) && fs.statSync(filePath).isDirectory()) {
          // trailing-slash export → dir/index.html; else Next puts file.html
          // at the top level (e.g. /settings → settings.html)
          const idx = path.join(filePath, 'index.html');
          filePath = fs.existsSync(idx) ? idx : filePath + '.html';
        }

        const missing = !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory();
        if (missing) {
          // A missing asset is a 404. Only a route (no file extension) falls
          // back to the app shell — answering 200 with index.html for anything
          // missing hides real breakage.
          const leaf = urlPath.split('/').pop() || '';
          if (leaf.includes('.')) {
            res.writeHead(404, { 'Content-Type': 'text/plain' });
            res.end('Not found');
            return;
          }
          filePath = path.join(root, 'index.html');
        }

        if (!isInside(filePath)) {
          res.writeHead(404, { 'Content-Type': 'text/plain' });
          res.end('Not found');
          return;
        }

        const ext = path.extname(filePath).toLowerCase();
        const mimeTypes = {
          '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
          '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
          '.ico': 'image/x-icon',
        };
        try {
          const data = fs.readFileSync(filePath);
          res.writeHead(200, {
            'Content-Type': mimeTypes[ext] || 'text/plain',
            'X-Content-Type-Options': 'nosniff',
          });
          res.end(data);
        } catch (err) {
          console.error(`[Dashboard] Could not read ${filePath}: ${err.message}`);
          res.writeHead(500, { 'Content-Type': 'text/plain' });
          res.end('Internal error');
        }
      });
      server.listen(servePort, '127.0.0.1');
      dashboardUrl = `http://127.0.0.1:${servePort}`;
      console.log(`[Dashboard] Serving static files on ${dashboardUrl}`);
      startNavPolling();
    } else {
      console.warn('[Dashboard] No static build found. Run: cd dashboard && npm run build');
      dashboardUrl = `http://127.0.0.1:${DASHBOARD_PORT}`; // fallback
    }
  }

  mainWindow.loadURL(dashboardUrl);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('close', (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  // Open external links in browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

// ─── System Tray ──────────────────────────────────────────────────────────────
function createTray() {
  // Create a simple 16x16 icon programmatically if no icon file exists
  const iconPath = path.join(__dirname, 'icons', 'tray-icon.png');
  let trayIcon;
  try {
    trayIcon = nativeImage.createFromPath(iconPath);
    if (trayIcon.isEmpty()) throw new Error('Empty icon');
  } catch {
    // Create a simple colored square as fallback
    const size = 16;
    const canvas = Buffer.alloc(size * size * 4);
    for (let i = 0; i < size * size; i++) {
      canvas[i * 4] = 0x33;     // R
      canvas[i * 4 + 1] = 0x80; // G
      canvas[i * 4 + 2] = 0xFF; // B
      canvas[i * 4 + 3] = 0xFF; // A
    }
    trayIcon = nativeImage.createFromBuffer(canvas, { width: size, height: size });
  }

  tray = new Tray(trayIcon);
  tray.setToolTip('Addled');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show Dashboard',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    { type: 'separator' },
    {
      label: 'Character: Sleep',
      click: () => {
        if (mainWindow) mainWindow.webContents.send('character-sleep');
      },
    },
    {
      label: 'Character: Wake',
      click: () => {
        if (mainWindow) mainWindow.webContents.send('character-wake');
      },
    },
    { type: 'separator' },
    {
      label: 'Check for Updates',
      click: () => {
        if (updater) updater.checkForUpdates();
      },
    },
    {
      label: 'Restart Addled',
      click: () => {
        app.isQuitting = true;
        app.relaunch();
        app.quit();
      },
    },
    { type: 'separator' },
    {
      label: 'Quit Addled',
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);

  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ─── IPC Handlers ─────────────────────────────────────────────────────────────
function setupIPC() {
  ipcMain.handle('get-ws-port', () => WS_PORT);
  ipcMain.handle('get-app-version', () => app.getVersion());
  ipcMain.handle('get-platform', () => process.platform);

  ipcMain.handle('show-open-dialog', async (event, options) => {
    return dialog.showOpenDialog(mainWindow, options);
  });

  ipcMain.handle('show-save-dialog', async (event, options) => {
    return dialog.showSaveDialog(mainWindow, options);
  });
}

// ─── App Lifecycle ────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  setupIPC();
  createTray();

  // Check if backend is already running on WS port
  const backendRunning = !(await isPortFree(WS_PORT));
  if (!backendRunning) {
    startPythonBackend();
    console.log('[Addled] Starting Python backend...');
  } else {
    console.log('[Addled] Backend already running on port', WS_PORT);
  }

  // Start auto-updater (weekly checks against the latest GitHub release)
  if (updater) updater.startUpdateChecks();

  // Wait briefly for backend to start, then create window
  await new Promise(resolve => setTimeout(resolve, 2000));
  await createWindow();

  console.log('[Addled] Started successfully');
});

app.on('window-all-closed', () => {
  // Don't quit — keep running in tray
});

app.on('before-quit', () => {
  app.isQuitting = true;

  // Cleanup child processes. NOTE: `kill()` is immediate and permits no
  // cleanup in the child, so the backend cannot stop the model server it
  // started — that has to happen here. See killLlamafile().
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  if (nextProcess) {
    nextProcess.kill();
    nextProcess = null;
  }
  killLlamafile();
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  }
});
