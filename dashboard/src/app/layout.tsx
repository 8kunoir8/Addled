'use client';

import { useWS } from '@/lib/useWS';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState, useEffect } from 'react';
import "./globals.css";

const NAV_ITEMS = [
  { href: '/chat', label: 'Chat', icon: '💬' },
  { href: '/memory', label: 'Memory', icon: '🧠' },
  { href: '/skills', label: 'Skills', icon: '🧩' },
  { href: '/goals', label: 'Goals', icon: '🎯' },
  { href: '/code', label: 'Code', icon: '💻' },
  { href: '/swarm', label: 'Swarm', icon: '🐝' },
  { href: '/browser', label: 'Browser', icon: '🌐' },
  { href: '/calendar', label: 'Calendar', icon: '📅' },
  { href: '/bots', label: 'Bots', icon: '🤖' },
  { href: '/settings', label: 'Settings', icon: '⚙️' },
];

const STATE_LABELS: Record<string, string> = {
  idle: 'Idle 🟢',
  listening: 'Listening 🎤',
  observing: 'Observing 👁️',
  thinking: 'Thinking 🤔',
  has_suggestion: 'Has Suggestion 💡',
  acting: 'Acting ⚡',
  speaking: 'Speaking 🔊',
  sleeping: 'Sleeping 😴',
  blocked: 'Blocked 🚫',
  error: 'Error ❌',
  working: 'Working ⚙️',
  dreaming: 'Dreaming ✨',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { state: wsState, characterState, send, onNotification } = useWS();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [desktopPrompt, setDesktopPrompt] = useState(false);
  const [browserPrompt, setBrowserPrompt] = useState<string | null>(null);

  // Desktop-control permission request from the backend
  useEffect(() => onNotification('desktop.permissionRequest', () => {
    setDesktopPrompt(true);
  }), [onNotification]);

  // Browser backend auto-install request
  useEffect(() => onNotification('browser.installRequest', (p: any) => {
    setBrowserPrompt(p?.backend || 'playwright');
  }), [onNotification]);

  // Backend-initiated navigation (e.g. character right-click → Settings)
  useEffect(() => onNotification('ui.navigate', (p: any) => {
    let path = p?.path || '/';
    if (!path.startsWith('/')) path = '/' + path;
    if (typeof window !== 'undefined' && window.location.pathname !== path) {
      window.location.href = path;
    }
  }), [onNotification]);

  const wsStatusColor =
    wsState === 'connected' ? 'bg-green-500' :
    wsState === 'connecting' ? 'bg-yellow-500' : 'bg-red-500';

  return (
    <html lang="en" className="dark">
      <body className="flex h-screen overflow-hidden">
        <aside className={`sidebar flex flex-col transition-all duration-200 ${sidebarCollapsed ? 'w-14' : 'w-[220px]'}`}>
          <div className="flex items-center gap-2 px-3 py-3 border-b border-[#30363d]">
            <button
              onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
              className="text-lg hover:bg-[#21262d] rounded p-1 transition-colors"
            >
              △
            </button>
            {!sidebarCollapsed && <span className="font-semibold text-sm">Addled</span>}
          </div>
          <nav className="flex-1 py-2 overflow-y-auto">
            {NAV_ITEMS.map((item) => {
              const isActive = pathname === item.href || pathname?.startsWith(item.href + '/');
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`sidebar-nav-item flex items-center gap-3 px-3 py-2 mx-1 rounded-md text-sm ${isActive ? 'active' : ''}`}
                >
                  <span className="text-base">{item.icon}</span>
                  {!sidebarCollapsed && <span>{item.label}</span>}
                </Link>
              );
            })}
          </nav>
          <div className="border-t border-[#30363d] p-3">
            <div className="flex items-center gap-2 text-xs text-[#8b949e]">
              <span className={`w-2 h-2 rounded-full ${wsStatusColor}`} />
              {!sidebarCollapsed && (
                <span>{STATE_LABELS[characterState] || characterState || 'Disconnected'}</span>
              )}
            </div>
            {!sidebarCollapsed && wsState === 'disconnected' && (
              <p className="text-[10px] text-[#f85149] mt-1 leading-tight">
                Backend not running.<br/>
                Start it: <code className="text-[#58a6ff]">python backend/main.py</code>
              </p>
            )}
          </div>
        </aside>
        <main className="flex-1 overflow-hidden flex flex-col">
          {children}
        </main>

        {/* Desktop-control permission prompt */}
        {desktopPrompt && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-[#d29922] bg-[#161b22] p-4 shadow-xl">
            <p className="text-sm font-semibold text-[#e8eaed]">🖱 Desktop control requested</p>
            <p className="text-xs text-[#8b949e] mt-1">
              The agent wants to move the mouse / type on your desktop. Grant access for this session only?
              <br /><span className="text-[#484f58]">Emergency stop: mouse to top-left corner, or Ctrl+Shift+Alt+K.</span>
            </p>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => { send('desktop.grant', {}).catch(() => {}); setDesktopPrompt(false); }}
                className="flex-1 bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded-md py-1.5 text-sm font-medium">
                Allow this session
              </button>
              <button
                onClick={() => { send('desktop.revoke', {}).catch(() => {}); setDesktopPrompt(false); }}
                className="flex-1 bg-[#3d1f1f] hover:bg-[#5a2a2a] text-[#f85149] rounded-md py-1.5 text-sm font-medium">
                Deny
              </button>
            </div>
          </div>
        )}

        {/* Browser backend install prompt */}
        {browserPrompt && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-[#3380FF] bg-[#161b22] p-4 shadow-xl">
            <p className="text-sm font-semibold text-[#e8eaed]">⬇ Install {browserPrompt}?</p>
            <p className="text-xs text-[#8b949e] mt-1">
              Addled needs <span className="font-mono text-[#58a6ff]">{browserPrompt}</span> for this task
              {browserPrompt === 'playwright' ? ' (Playwright + Chromium, ~170 MB)' : ' (the browser-use framework)'}.
              Install it now?
            </p>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => { send('browser.installApprove', { backend: browserPrompt }).catch(() => {}); setBrowserPrompt(null); }}
                className="flex-1 bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded-md py-1.5 text-sm font-medium">
                Install
              </button>
              <button
                onClick={() => setBrowserPrompt(null)}
                className="flex-1 bg-[#21262d] hover:bg-[#30363d] text-[#8b949e] rounded-md py-1.5 text-sm font-medium">
                Not now
              </button>
            </div>
          </div>
        )}
      </body>
    </html>
  );
}
