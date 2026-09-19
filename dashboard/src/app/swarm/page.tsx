'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface Agent { id:string; name:string; emoji:string; type:string; status:'offline'|'ready'|'running'|'error'; currentTask?:string; tools:string[]|null; allTools?:boolean; }

const EMOJI_MAP: Record<string, string> = {
  coder: '👨‍💻', writer: '✍️', analyst: '📊', planner: '🎯',
  researcher: '🔍', devops: '🐳', general: '🤖',
};

const STATUS_DOT:Record<string,string>={offline:'bg-[#484f58]',ready:'bg-[#3fb950]',running:'bg-[#d29922] animate-pulse',error:'bg-[#f85149]'};

// The agent types the backend knows a default prompt for.
const AGENT_TYPES=['coder','writer','analyst','planner','researcher','devops','general'];

export default function SwarmPage() {
  const { state:wsState, send }=useWS();
  const [agents,setAgents]=useState<Agent[]>([]);
  const [taskInputs,setTaskInputs]=useState<Record<string,string>>({});
  const [results,setResults]=useState<Record<string,{ok:boolean;text:string}>>({});
  const [busy,setBusy]=useState<string|null>(null);
  const [newType,setNewType]=useState('coder');
  const [newName,setNewName]=useState('');
  const [error,setError]=useState('');

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
          // null from the backend means every enabled skill. It used to read
          // "chat", which looked like a tool and was not one.
          tools: (a.tools as string[]) || null,
          allTools: (a.allTools as boolean) ?? !a.tools,
        })));
      }
      setError('');
    } catch (e:any) { setError(e?.message || 'Could not list agents'); }
  }, [wsState, send]);

  useEffect(() => { fetchAgents(); }, [fetchAgents]);

  // Agents exist only in this process and there are no stored templates, so
  // without a create control the page shows an empty grid forever.
  const handleCreate = async () => {
    if (wsState !== 'connected') return;
    setBusy('new');
    try {
      const r = await send('swarm.spawn', {
        agentType: newType,
        name: newName.trim() || `${newType} agent`,
      });
      if (r?.agentId) { setNewName(''); await fetchAgents(); }
      else setError(r?.error || 'The agent could not be created');
    } catch (e:any) { setError(e?.message || 'The agent could not be created'); }
    finally { setBusy(null); }
  };

  // Run only. This used to spawn a second agent and then run the original id,
  // so every click left a phantom agent behind and tracked the wrong one.
  const handleRun = async (agent:Agent) => {
    const task=taskInputs[agent.id]?.trim();
    if(!task||wsState!=='connected')return;
    setBusy(agent.id);
    try{
      const r = await send('swarm.run',{agentId:agent.id,task});
      setResults(prev=>({...prev,[agent.id]:{
        ok: !!r?.success,
        text: r?.success ? String(r?.response||'').trim() : String(r?.error||'The task failed'),
      }}));
      setTaskInputs(prev=>({...prev,[agent.id]:''}));
    } catch(e:any){
      setResults(prev=>({...prev,[agent.id]:{ok:false,text:e?.message||'The task failed'}}));
    } finally {
      setBusy(null);
      await fetchAgents();
    }
  };

  // stop() cancels the task *and* removes the agent from the backend, so the
  // card disappears on the next fetch rather than going back to "ready".
  const handleStop=async(agent:Agent)=>{
    if(wsState!=='connected')return;
    setBusy(agent.id);
    try{ await send('swarm.stop',{agentId:agent.id}); }
    catch(e:any){ setError(e?.message||'Could not stop the agent'); }
    finally { setBusy(null); await fetchAgents(); }
  };

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]"><h1 className="text-sm font-semibold">Agent Swarm</h1><span className="text-xs text-[#8b949e]">{agents.filter(a=>a.status==='running').length} running</span></header>
      <div className="flex-1 overflow-y-auto p-4">
        <div className="flex items-center gap-2 mb-4">
          <select value={newType} onChange={e=>setNewType(e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]">
            {AGENT_TYPES.map(t=><option key={t} value={t}>{t}</option>)}
          </select>
          <input value={newName} onChange={e=>setNewName(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')handleCreate();}} placeholder="Agent name (optional)" className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
          <button onClick={handleCreate} disabled={busy==='new'||wsState!=='connected'} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium">{busy==='new'?'Creating…':'Add agent'}</button>
        </div>
        {error&&<p className="text-xs text-[#f85149] mb-3">{error}</p>}
        {agents.length===0&&(
          <p className="text-sm text-[#8b949e] mb-3">No agents yet. Agents live only for this session, so add one above — it appears here until the backend restarts.</p>
        )}
        <div className="grid grid-cols-2 gap-3">
          {agents.map(agent=>(
            <div key={agent.id} className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 hover:border-[#484f58] transition-colors">
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2"><span className="text-2xl">{agent.emoji}</span><div><h3 className="text-sm font-medium text-[#e8eaed]">{agent.name}</h3><p className="text-xs text-[#8b949e]">{agent.type}</p></div></div>
                <span className={`w-2 h-2 rounded-full ${STATUS_DOT[agent.status]}`}/>
              </div>
              {agent.currentTask&&<p className="text-xs text-[#8b949e] mb-2">📋 {agent.currentTask}</p>}
              <div className="flex flex-wrap gap-1 mb-3">
                {agent.allTools
                  ? <span className="text-[10px] px-1.5 py-0.5 bg-[#1f2d3d] rounded text-[#79b8ff]" title="Every enabled skill">all skills &amp; tools</span>
                  : (agent.tools||[]).map(t=><span key={t} className="text-[10px] px-1.5 py-0.5 bg-[#21262d] rounded text-[#8b949e]">{t}</span>)}
              </div>
              {results[agent.id]&&(
                <p className={`text-xs mb-2 whitespace-pre-wrap break-words max-h-32 overflow-y-auto ${results[agent.id].ok?'text-[#e8eaed]':'text-[#f85149]'}`}>{results[agent.id].text.slice(0,800)}</p>
              )}
              {agent.status!=='running'?(
                <div className="flex gap-2">
                  <input value={taskInputs[agent.id]||''} onChange={e=>setTaskInputs(prev=>({...prev,[agent.id]:e.target.value}))} onKeyDown={e=>{if(e.key==='Enter')handleRun(agent);}} placeholder="Assign a task..." className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
                  <button onClick={()=>handleRun(agent)} disabled={!taskInputs[agent.id]?.trim()||busy===agent.id||wsState!=='connected'} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium">{busy===agent.id?'…':'Run'}</button>
                </div>
              ):(
                <button onClick={()=>handleStop(agent)} className="text-xs px-3 py-1 bg-[#f85149] text-white rounded hover:bg-[#ff6a63]">Stop &amp; remove</button>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
