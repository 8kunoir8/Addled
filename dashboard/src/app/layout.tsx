'use client';

import { useWS } from '@/lib/useWS';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState } from 'react';
import "./globals.css";

const NAV_ITEMS = [
  { href: '/chat', label: 'Chat', icon: '💬' },
  { href: '/memory', label: 'Memory', icon: '🧠' },
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
  const { state: wsState, characterState, insight } = useWS();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [dismissedInsight, setDismissedInsight] = useState<number | null>(null);

  const wsStatusColor =
    wsState === 'connected' ? 'bg-green-500' :
    wsState === 'connecting' ? 'bg-yellow-500' : 'bg-red-500';

  const showInsight = insight && insight.timestamp !== dismissedInsight;

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
          {showInsight && insight && (
            <div className="mx-3 mt-2 px-3 py-2 rounded-md border border-[#d29922]/40 bg-[#1f1a10] text-sm flex items-start gap-2">
              <span className="text-base leading-5">💡</span>
              <span className="flex-1 text-[#e8eaed] leading-snug">
                <span className="font-semibold text-[#d29922]">Addled noticed</span>
                {insight.context !== 'unknown' && <> · {insight.context}</>}:
                {insight.text}
              </span>
              <button
                onClick={() => setDismissedInsight(insight.timestamp)}
                className="text-[#8b949e] hover:text-[#e8eaed] px-1 leading-none"
                aria-label="Dismiss insight"
              >
                ×
              </button>
            </div>
          )}
          {children}
        </main>
      </body>
    </html>
  );
}
