'use client';

import { useState } from 'react';

interface BotStatus { id:string; name:string; platform:string; status:'disconnected'|'connecting'|'connected'; icon:string; instructions:string; }

const BOTS:BotStatus[]=[
  {id:'telegram',name:'Telegram Bot',platform:'telegram',status:'disconnected',icon:'✈️',instructions:'1. Create a bot with @BotFather\n2. Set TELEGRAM_BOT_TOKEN env var\n3. Start the bridge'},
  {id:'whatsapp',name:'WhatsApp Bot',platform:'whatsapp',status:'disconnected',icon:'💬',instructions:'1. Scan QR code to pair\n2. Keep phone connected\n3. Messages appear here'},
  {id:'discord',name:'Discord Bot',platform:'discord',status:'disconnected',icon:'🎮',instructions:'1. Create app at discord.com/developers\n2. Set DISCORD_BOT_TOKEN env var\n3. Invite bot to server'},
];

export default function BotsPage() {
  const [bots,setBots]=useState<BotStatus[]>(BOTS);
  const [tokens,setTokens]=useState<Record<string,string>>({});
  const [expanded,setExpanded]=useState<string|null>(null);

  const statusColor:Record<string,string>={disconnected:'bg-[#484f58]',connecting:'bg-[#d29922] animate-pulse',connected:'bg-[#3fb950]'};

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center px-4 py-3 border-b border-[#30363d]"><h1 className="text-sm font-semibold">Bot Bridges</h1></header>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {bots.map(bot=>(
          <div key={bot.id} className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <span className="text-2xl">{bot.icon}</span>
                <div><h3 className="text-sm font-medium text-[#e8eaed]">{bot.name}</h3><p className="text-xs text-[#8b949e]">{bot.platform}</p></div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${statusColor[bot.status]}`}/>
                <span className="text-xs text-[#8b949e]">{bot.status}</span>
              </div>
            </div>

            {expanded===bot.id&&(
              <div className="mb-3 p-3 bg-[#0d1117] rounded border border-[#21262d]">
                <p className="text-xs text-[#8b949e] whitespace-pre-line">{bot.instructions}</p>
                <input type="password" value={tokens[bot.id]||''} onChange={e=>setTokens(p=>({...p,[bot.id]:e.target.value}))}
                  placeholder={`${bot.platform.toUpperCase()}_BOT_TOKEN`} className="mt-2 w-full bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] font-mono"/>
              </div>
            )}

            <div className="flex gap-2">
              <button onClick={()=>setExpanded(expanded===bot.id?null:bot.id)} className="text-xs px-3 py-1 bg-[#21262d] text-[#e8eaed] rounded hover:bg-[#30363d]">
                {expanded===bot.id?'Hide Setup':'Setup'}
              </button>
              <button
                onClick={() => {
                  if (bot.status === 'connected') return;
                  setBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'connecting' as const } : b));
                  setTimeout(() => setBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'disconnected' as const } : b)), 2000);
                }}
                disabled={bot.status==='connected' || bot.status==='connecting'}
                className="text-xs px-3 py-1 bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded">
                {bot.status==='connected'?'Connected':bot.status==='connecting'?'Connecting...':'Connect'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
