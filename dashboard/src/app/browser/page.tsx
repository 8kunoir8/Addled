'use client';

import { useState } from 'react';
import { useWS } from '@/lib/useWS';

/**
 * The message from a thrown value. `catch` binds `unknown`, so this is the one
 * place the shape is narrowed; the three catch sites below share it so they
 * cannot drift into one reading `.message` and another printing `[object Object]`.
 */
const errMessage = (e: unknown): string =>
  e instanceof Error ? e.message : String(e);

export default function BrowserPage() {
  const { state: wsState, send } = useWS();
  const [url, setUrl] = useState('');
  const [sessionActive, setSessionActive] = useState(false);
  const [screenshot, setScreenshot] = useState<string|null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [taskInput, setTaskInput] = useState('');
  const [taskRunning, setTaskRunning] = useState(false);

  const addLog = (msg: string) => setLogs(prev => [...prev.slice(-49), `[${new Date().toLocaleTimeString()}] ${msg}`]);

  const runTask = async () => {
    const task = taskInput.trim();
    if (!task || wsState !== 'connected') return;
    setTaskRunning(true);
    addLog(`Task: ${task.slice(0, 80)}${task.length > 80 ? '…' : ''}`);
    try {
      const r = await send('browser.task', { task });
      const engine = r?.engine || '?';  
      addLog(r?.success
        ? `Done via ${engine}${r?.degraded ? ` (degraded — ${r?.fallback_reason || 'fallback'})` : ''}`
        : `Failed (${engine}): ${r?.error || 'unknown'}`);
      if (r?.text) addLog(`Result: ${String(r.text).slice(0, 160)}...`);
      else if (r?.result_text) addLog(`Result: ${String(r.result_text).slice(0, 160)}...`);
    } catch (e) { addLog(`Task error: ${errMessage(e)}`); }
    setTaskRunning(false);
  };

  const handleNavigate = async () => {
    if (!url.trim() || wsState !== 'connected') return;
    setLoading(true); addLog(`Navigating to ${url}`);
    try {
      const r = await send('browser.navigate', { url: url.trim() });
      if (r?.screenshotUrl) setScreenshot(r.screenshotUrl);
      // A degraded result is a plain HTTP fetch standing in for a browser:
      // there is no screenshot and no page to click. Saying "Page loaded"
      // here would present it as a real browse, which is exactly the
      // confusion this flag exists to end.
      if (r?.degraded) {
        addLog(`Fetched over HTTP — ${r?.fallback_reason || 'browser unavailable'} (no page to interact with)`);
        setSessionActive(false);
      } else {
        addLog(`Page loaded (${r?.engine || 'browser'})`); setSessionActive(true);
      }
    } catch (e) { addLog(`Error: ${errMessage(e)}`); }
    setLoading(false);
  };

  const handleAction = async (action: string, selector?: string) => {
    if (wsState !== 'connected') return;
    setLoading(true); addLog(`${action} ${selector||''}`);
    try {
      const r = await send(`browser.${action}`, selector ? { selector } : {});
      if (r?.screenshotUrl) setScreenshot(r.screenshotUrl);
      if (r?.text) addLog(`Extracted: ${r.text.slice(0,100)}...`);
    } catch (e) { addLog(`Error: ${errMessage(e)}`); }
    setLoading(false);
  };

  const handleClose = async () => {
    try { await send('browser.close', {}); } catch {}
    setSessionActive(false); setScreenshot(null); addLog('Session closed');
  };

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center gap-2 px-4 py-3 border-b border-[var(--border)]">
        <h1 className="text-sm font-semibold mr-2">Browser</h1>
        <button onClick={()=>handleAction('go_back')} disabled={!sessionActive||loading} className="text-sm px-2 py-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-30">◀</button>
        <button onClick={()=>handleAction('go_forward')} disabled={!sessionActive||loading} className="text-sm px-2 py-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-30">▶</button>
        <input value={url} onChange={e=>setUrl(e.target.value)} onKeyDown={e=>e.key==='Enter'&&handleNavigate()} placeholder="https://..." className="flex-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-3 py-1.5 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]"/>
        <button onClick={handleNavigate} disabled={!url.trim()||loading||wsState!=='connected'} className="bg-[var(--accent)] hover:bg-[var(--accent-hover)] disabled:opacity-50 text-white rounded-lg px-3 py-1.5 text-sm">{loading?'...':'Go'}</button>
        {sessionActive&&<button onClick={handleClose} className="text-sm px-3 py-1.5 bg-[var(--danger)] text-white rounded-lg hover:bg-[#ff6a63]">Close</button>}
      </header>
      <div className="px-4 py-2 border-b border-[var(--bg-tertiary)] flex gap-2 items-center">
        <span className="text-[11px] text-[var(--text-secondary)] shrink-0">🧠 Task:</span>
        <input value={taskInput} onChange={e=>setTaskInput(e.target.value)} onKeyDown={e=>e.key==='Enter'&&runTask()}
          placeholder="e.g. find the cheapest flight NYC → Tokyo next Friday and list the top 3"
          className="flex-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-3 py-1.5 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]"/>
        <button onClick={runTask} disabled={!taskInput.trim()||taskRunning||wsState!=='connected'}
          className="bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50 text-[var(--text-primary)] rounded-lg px-3 py-1.5 text-sm">
          {taskRunning?'⟳':'Run'}
        </button>
      </div>
      <div className="flex-1 flex">
        <div className="flex-1 bg-[var(--bg-primary)] flex items-center justify-center">
          {screenshot ? <img src={screenshot} alt="Screenshot" className="max-w-full max-h-full object-contain"/> : (
            <div className="text-center text-[var(--text-secondary)]"><span className="text-4xl block mb-3">🌐</span><p className="text-sm">Enter a URL to start browsing</p></div>
          )}
        </div>
        <div className="w-64 border-l border-[var(--border)] p-3 overflow-y-auto">
          <h3 className="text-xs font-medium text-[var(--text-secondary)] mb-2">Action Log</h3>
          <div className="space-y-1">{logs.map((l,i)=><p key={i} className="text-[11px] text-[var(--text-secondary)] font-mono">{l}</p>)}</div>
        </div>
      </div>
    </div>
  );
}
