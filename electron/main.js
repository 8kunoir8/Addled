// Addled — Electron Shell
// Created by Kunoir — 2026

'use strict';

const { app, BrowserWindow, shell, ipcMain, dialog, screen, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');
const path = require('path');

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
let nextProcess = null;
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
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[Python:err] ${data.toString().trim()}`);
  });

  pythonProcess.on('close', (code) => {
    console.log(`[Python] Process exited with code ${code}`);
    pythonProcess = null;
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
  let dashboardUrl;
  if (isDev) {
    dashboardUrl = `http://localhost:${DASHBOARD_PORT}`;
  } else {
    // Start a local static file server for the dashboard
    const http = require('http');
    const servePort = 3001;
    const outDir = isDev
      ? path.join(DASHBOARD_DIR, 'out')
      : path.join(DASHBOARD_DIR); // extraResources maps dashboard/out → dashboard/

    if (fs.existsSync(outDir)) {
      // Simple static file server for Next.js export
      const server = http.createServer((req, res) => {
        let filePath = path.join(outDir, req.url === '/' ? 'index.html' : req.url.split('?')[0]);
        if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
          filePath = path.join(outDir, 'index.html');
        }
        const ext = path.extname(filePath).toLowerCase();
        const mimeTypes = {
          '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
          '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
          '.ico': 'image/x-icon',
        };
        try {
          const data = fs.readFileSync(filePath);
          res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'text/plain' });
          res.end(data);
        } catch {
          res.writeHead(200, { 'Content-Type': 'text/html' });
          res.end(fs.readFileSync(path.join(outDir, 'index.html')));
        }
      });
      server.listen(servePort, '127.0.0.1');
      dashboardUrl = `http://127.0.0.1:${servePort}`;
      console.log(`[Dashboard] Serving static files on ${dashboardUrl}`);
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

  // Start auto-updater checks (every 4 hours)
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

  // Cleanup child processes
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  if (nextProcess) {
    nextProcess.kill();
    nextProcess = null;
  }
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  }
});
