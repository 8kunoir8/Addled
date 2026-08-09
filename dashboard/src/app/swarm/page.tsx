'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface Agent { id:string; name:string; emoji:string; type:string; status:'offline'|'ready'|'running'|'error'; currentTask?:string; tools:string[]; }

const EMOJI_MAP: Record<string, string> = {
  coder: '👨‍💻', writer: '✍️', analyst: '📊', planner: '🎯',
  researcher: '🔍', devops: '🐳', general: '🤖',
};

const STATUS_DOT:Record<string,string>={offline:'bg-[#484f58]',ready:'bg-[#3fb950]',running:'bg-[#d29922] animate-pulse',error:'bg-[#f85149]'};

export default function SwarmPage() {
  const { state:wsState, send }=useWS();
  const [agents,setAgents]=useState<Agent[]>([]);
  const [taskInputs,setTaskInputs]=useState<Record<string,string>>({});
  const [spawning,setSpawning]=useState<string|null>(null);

  // Fetch agents from backend
  const fetchAgents = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('swarm.list', {});
      if (r?.agents) {
        setAgents(r.agents.map((a: Record<string, unknown>) => ({
          id: a.id as string,
          name: a.name as string,
          emoji: EMOJI_MAP[a.type as string] || '🤖',
          type: a.type as string,
          status: (a.status as Agent['status']) || 'ready',
          currentTask: a.currentTask as string | undefined,
          tools: (a.tools as string[]) || ['all'],
        })));
      }
    } catch {}
  }, [wsState, send]);

  useEffect(() => { fetchAgents(); }, [fetchAgents]);

  const handleSpawn=async(agent:Agent)=>{
    const task=taskInputs[agent.id]?.trim();
    if(!task||wsState!=='connected')return;
    setSpawning(agent.id);
    try{
      await send('swarm.spawn',{agentType:agent.type,name:agent.name,tools:agent.tools});
      // Also run the task
      await send('swarm.run',{agentId:agent.id,task});
      setAgents(prev=>prev.map(a=>a.id===agent.id?{...a,status:'running',currentTask:task}:a));
    }
    catch{setAgents(prev=>prev.map(a=>a.id===agent.id?{...a,status:'error'}:a));}
    setSpawning(null);
  };

  const handleStop=async(agent:Agent)=>{
    if(wsState!=='connected')return;
    try{
      await send('swarm.stop',{agentId:agent.id});
      setAgents(prev=>prev.map(a=>a.id===agent.id?{...a,status:'ready',currentTask:undefined}:a));
    }catch{}
  };

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]"><h1 className="text-sm font-semibold">Agent Swarm</h1><span className="text-xs text-[#8b949e]">{agents.filter(a=>a.status==='running').length} running</span></header>
      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-2 gap-3">
          {agents.map(agent=>(
            <div key={agent.id} className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 hover:border-[#484f58] transition-colors">
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2"><span className="text-2xl">{agent.emoji}</span><div><h3 className="text-sm font-medium text-[#e8eaed]">{agent.name}</h3><p className="text-xs text-[#8b949e]">{agent.type}</p></div></div>
                <span className={`w-2 h-2 rounded-full ${STATUS_DOT[agent.status]}`}/>
              </div>
              {agent.currentTask&&<p className="text-xs text-[#8b949e] mb-2">📋 {agent.currentTask}</p>}
              <div className="flex flex-wrap gap-1 mb-3">{agent.tools.map(t=><span key={t} className="text-[10px] px-1.5 py-0.5 bg-[#21262d] rounded text-[#8b949e]">{t}</span>)}</div>
              {agent.status!=='running'?(
                <div className="flex gap-2">
                  <input value={taskInputs[agent.id]||''} onChange={e=>setTaskInputs(prev=>({...prev,[agent.id]:e.target.value}))} placeholder="Assign a task..." className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
                  <button onClick={()=>handleSpawn(agent)} disabled={!taskInputs[agent.id]?.trim()||spawning===agent.id||wsState!=='connected'} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium">{spawning===agent.id?'...':'Run'}</button>
                </div>
              ):(
                <button onClick={()=>handleStop(agent)} className="text-xs px-3 py-1 bg-[#f85149] text-white rounded hover:bg-[#ff6a63]">Stop</button>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
