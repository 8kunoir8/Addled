'use client';

import { useCallback, useEffect, useState } from 'react';
import { useWS } from '@/lib/useWS';

// Everything here comes from bots.status, which checks rather than assumes:
// node on PATH, scripts on disk, dependencies installed, token saved. The page
// used to be hardcoded "disconnected" with a Connect button that faked
// "connecting" for two seconds and then reset itself.
interface Bot {
  id: string;
  name: string;
  label: string;
  icon: string;
  setup: string;
  token_env: string;
  has_token: boolean;
  running: boolean;
  pid: number | null;
  script_found: boolean;
  deps_installed: boolean | null;
  node: string;
  ready: boolean;
  blockers: string[];
  log: string[];
}

export default function BotsPage() {
  const { state: wsState, send } = useWS();
  const [bots, setBots] = useState<Bot[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('bots.status', {});
      if (r?.success) {
        setBots(Object.values(r.platforms || {}) as Bot[]);
        setError('');
      } else {
        setError(r?.error || 'Could not read bot status');
      }
    } catch (e: any) { setError(e?.message || 'Could not read bot status'); }
  }, [wsState, send]);

  useEffect(() => { load(); }, [load]);

  // Poll only while something is actually running, so the log stays live
  // without hammering the backend on an idle page.
  useEffect(() => {
    if (!bots.some(b => b.running)) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [bots, load]);

  const saveToken = async (bot: Bot) => {
    setBusy(bot.id); setError(''); setNote('');
    try {
      const r = await send('bots.setToken', {
        platform: bot.id,
        token: (drafts[bot.id] ?? '').trim(),
      });
      if (r?.success) {
        setDrafts(p => ({ ...p, [bot.id]: '' }));
        setNote(`${bot.label} token ${r.saved ? 'saved' : 'cleared'}.`);
        await load();
      } else setError(r?.error || 'Could not save the token');
    } catch (e: any) { setError(e?.message || 'Could not save the token'); }
    finally { setBusy(null); }
  };

  const toggleRun = async (bot: Bot) => {
    setBusy(bot.id); setError(''); setNote('');
    try {
      const r = bot.running
        ? await send('bots.stop', { platform: bot.id })
        : await send('bots.start', { platform: bot.id });
      if (!r?.success && !r?.already) {
        setError(`${bot.label}: ${r?.error || 'it did not start'}`);
      } else {
        setNote(`${bot.label} ${bot.running ? 'stopped' : 'started'}${r?.pid ? ` (pid ${r.pid})` : ''}.`);
      }
      await load();
    } catch (e: any) { setError(`${bot.label}: ${e?.message || 'it did not start'}`); }
    finally { setBusy(null); }
  };

  const dot = (bot: Bot) =>
    bot.running ? 'bg-[#3fb950]'
    : bot.ready ? 'bg-[#d29922]'
    : 'bg-[#484f58]';

  const state = (bot: Bot) =>
    bot.running ? `running${bot.pid ? ` (pid ${bot.pid})` : ''}`
    : bot.ready ? 'ready to start'
    : 'not ready';

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Bot Bridges</h1>
        <button onClick={load} className="text-xs px-2 py-1 rounded border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed]">
          Refresh
        </button>
      </header>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {wsState !== 'connected' && <p className="text-sm text-[#f85149]">Backend not connected.</p>}
        {error && <p className="text-sm text-[#f85149]">{error}</p>}
        {note && <p className="text-sm text-[#3fb950]">{note}</p>}

        {bots.map(bot => (
          <div key={bot.id} className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <span className="text-2xl">{bot.icon}</span>
                <div>
                  <h3 className="text-sm font-medium text-[#e8eaed]">{bot.name}</h3>
                  <p className="text-xs text-[#8b949e]">{bot.label}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${dot(bot)} ${bot.running ? 'animate-pulse' : ''}`} />
                <span className="text-xs text-[#8b949e]">{state(bot)}</span>
              </div>
            </div>

            {bot.blockers.length > 0 && (
              <ul className="mb-3 space-y-0.5">
                {bot.blockers.map((b, i) => (
                  <li key={i} className="text-xs text-[#d29922]">• {b}</li>
                ))}
              </ul>
            )}

            {bot.token_env && (
              <div className="flex gap-2 mb-3">
                <input
                  type="password"
                  value={drafts[bot.id] ?? ''}
                  onChange={e => setDrafts(p => ({ ...p, [bot.id]: e.target.value }))}
                  placeholder={bot.has_token ? `${bot.token_env} saved — paste a new one to replace` : bot.token_env}
                  className="flex-1 bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] font-mono placeholder-[#484f58]"
                />
                <button
                  onClick={() => saveToken(bot)}
                  disabled={busy === bot.id}
                  className="text-xs px-3 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-50">
                  {bot.has_token && !(drafts[bot.id] ?? '').trim() ? 'Clear' : 'Save'}
                </button>
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={() => setExpanded(expanded === bot.id ? null : bot.id)}
                className="text-xs px-3 py-1 bg-[#21262d] text-[#e8eaed] rounded hover:bg-[#30363d]">
                {expanded === bot.id ? 'Hide Setup' : 'Setup'}
              </button>
              <button
                onClick={() => toggleRun(bot)}
                disabled={busy === bot.id || (!bot.ready && !bot.running)}
                className={`text-xs px-3 py-1 rounded text-white disabled:opacity-50 ${bot.running ? 'bg-[#f85149] hover:bg-[#ff6a63]' : 'bg-[#3380FF] hover:bg-[#4d94ff]'}`}>
                {busy === bot.id ? '…' : bot.running ? 'Stop' : 'Start'}
              </button>
            </div>

            {expanded === bot.id && (
              <div className="mt-3 p-3 bg-[#0d1117] rounded border border-[#21262d]">
                <p className="text-xs text-[#8b949e] whitespace-pre-line">{bot.setup}</p>
                <p className="text-[11px] text-[#484f58] mt-2 font-mono">
                  node {bot.script_found ? bot.id + '-bot.js' : '…'} · deps {bot.deps_installed === false ? 'missing' : 'ok'} · node {bot.node || 'not found'}
                </p>
              </div>
            )}

            {bot.log.length > 0 && (
              <pre className="mt-3 p-2 bg-[#0d1117] rounded border border-[#21262d] text-[10px] leading-relaxed text-[#8b949e] max-h-40 overflow-y-auto whitespace-pre-wrap">
                {bot.log.slice(-12).join('\n')}
              </pre>
            )}
          </div>
        ))}

        {bots.length === 0 && wsState === 'connected' && (
          <p className="text-sm text-[#8b949e]">No bot bridges reported.</p>
        )}
      </div>
    </div>
  );
}
