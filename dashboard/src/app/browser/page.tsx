'use client';

import { useState } from 'react';
import { useWS } from '@/lib/useWS';

export default function BrowserPage() {
  const { state: wsState, send } = useWS();
  const [url, setUrl] = useState('');
  const [sessionActive, setSessionActive] = useState(false);
  const [screenshot, setScreenshot] = useState<string|null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  const addLog = (msg: string) => setLogs(prev => [...prev.slice(-49), `[${new Date().toLocaleTimeString()}] ${msg}`]);

  const handleNavigate = async () => {
    if (!url.trim() || wsState !== 'connected') return;
    setLoading(true); addLog(`Navigating to ${url}`);
    try {
      const r = await send('browser.navigate', { url: url.trim() });
      if (r?.screenshotUrl) setScreenshot(r.screenshotUrl);
      addLog('Page loaded'); setSessionActive(true);
    } catch (e: any) { addLog(`Error: ${e.message}`); }
    setLoading(false);
  };

  const handleAction = async (action: string, selector?: string) => {
    if (wsState !== 'connected') return;
    setLoading(true); addLog(`${action} ${selector||''}`);
    try {
      const r = await send(`browser.${action}`, selector ? { selector } : {});
      if (r?.screenshotUrl) setScreenshot(r.screenshotUrl);
      if (r?.text) addLog(`Extracted: ${r.text.slice(0,100)}...`);
    } catch (e: any) { addLog(`Error: ${e.message}`); }
    setLoading(false);
  };

  const handleClose = async () => {
    try { await send('browser.close', {}); } catch {}
    setSessionActive(false); setScreenshot(null); addLog('Session closed');
  };

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center gap-2 px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold mr-2">Browser</h1>
        <button onClick={()=>handleAction('go_back')} disabled={!sessionActive||loading} className="text-sm px-2 py-1 text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-30">◀</button>
        <button onClick={()=>handleAction('go_forward')} disabled={!sessionActive||loading} className="text-sm px-2 py-1 text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-30">▶</button>
        <input value={url} onChange={e=>setUrl(e.target.value)} onKeyDown={e=>e.key==='Enter'&&handleNavigate()} placeholder="https://..." className="flex-1 bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-1.5 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"/>
        <button onClick={handleNavigate} disabled={!url.trim()||loading||wsState!=='connected'} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-3 py-1.5 text-sm">{loading?'...':'Go'}</button>
        {sessionActive&&<button onClick={handleClose} className="text-sm px-3 py-1.5 bg-[#f85149] text-white rounded-lg hover:bg-[#ff6a63]">Close</button>}
      </header>
      <div className="flex-1 flex">
        <div className="flex-1 bg-[#0d1117] flex items-center justify-center">
          {screenshot ? <img src={screenshot} alt="Screenshot" className="max-w-full max-h-full object-contain"/> : (
            <div className="text-center text-[#8b949e]"><span className="text-4xl block mb-3">🌐</span><p className="text-sm">Enter a URL to start browsing</p></div>
          )}
        </div>
        <div className="w-64 border-l border-[#30363d] p-3 overflow-y-auto">
          <h3 className="text-xs font-medium text-[#8b949e] mb-2">Action Log</h3>
          <div className="space-y-1">{logs.map((l,i)=><p key={i} className="text-[11px] text-[#8b949e] font-mono">{l}</p>)}</div>
        </div>
      </div>
    </div>
  );
}
