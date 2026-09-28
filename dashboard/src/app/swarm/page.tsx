'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface Agent { id:string; name:string; emoji:string; type:string; status:'offline'|'ready'|'running'|'error'; currentTask?:string; tools:string[]|null; allTools?:boolean; }

/** A flow that was interrupted and still has steps left to run. */
interface Checkpoint { flowId:string; goal:string; steps:number; finished:number; updated:number; }

/** One agent's own notebook — what it did, kept between runs. */
interface NotebookEntry { task?:string; output?:string; kind?:string; flow?:string; timestamp?:number; }
interface Notebook { agentId:string; name:string; summary:string; entries:NotebookEntry[]; flows:string[]; updated:number; digest:string; }

interface FlowStep { id:string; agentId:string; task:string; parallel:string[]; merge:string; until:string; notContains:boolean; retry:string; maxAttempts:number; dependsOn:number[]; optional:boolean; judgeMode:boolean; onPass:string; onFail:string; }
interface FlowResult { agent:string; task:string; success:boolean; output:string; error?:string; parallel?:boolean; merged?:boolean; gate?:boolean; attempt?:number; skipped?:boolean; }
interface FlowJump { from:number; to:number|string; reason:string; }

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

  // A flow: ordered steps, each one seeing what the agents before it produced.
  const [flowGoal,setFlowGoal]=useState('');
  const [flowSteps,setFlowSteps]=useState<FlowStep[]>([]);
  const [flowRunning,setFlowRunning]=useState(false);
  const [flowResult,setFlowResult]=useState<{ok:boolean;text:string;steps:FlowResult[];jumps?:FlowJump[]}|null>(null);
  const [memoryInfo,setMemoryInfo]=useState<{entries:number;chars:number}|null>(null);

  // Flows that were interrupted. A flow is the longest-running thing in the
  // app, so it is the thing most likely to outlive a session — and until it
  // could be resumed, closing the app threw away every finished step.
  const [checkpoints,setCheckpoints]=useState<Checkpoint[]>([]);
  const [resuming,setResuming]=useState<string|null>(null);

  // One agent's own notebook, opened on request. Not fetched for every agent
  // on load: the entries can be long and usually nobody is looking.
  const [notebook,setNotebook]=useState<Notebook|null>(null);
  const [notebookQuery,setNotebookQuery]=useState('');
  const [notebookHits,setNotebookHits]=useState<NotebookEntry[]|null>(null);

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

  const fetchMemory = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('swarm.memory', {});
      setMemoryInfo({ entries: Number(r?.entries||0), chars: Number(r?.chars||0) });
    } catch { /* the badge just stays as it was */ }
  }, [wsState, send]);

  useEffect(() => { fetchMemory(); }, [fetchMemory]);

  const clearMemory = async () => {
    if (wsState !== 'connected') return;
    try { await send('swarm.memory', { clear: true }); } catch { /* ignore */ }
    await fetchMemory();
  };

  const fetchCheckpoints = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('swarm.checkpoints', {});
      setCheckpoints(r?.flows || []);
    } catch { /* the panel just stays as it was */ }
  }, [wsState, send]);

  useEffect(() => { fetchCheckpoints(); }, [fetchCheckpoints]);

  const resumeFlow = async (flowId:string) => {
    if (wsState !== 'connected') return;
    setResuming(flowId);
    try {
      const r = await send('swarm.resume', { flowId });
      setFlowResult({
        ok: Boolean(r?.success),
        text: r?.success
          ? (r?.message || `Resumed — ${(r?.steps||[]).length} step(s) recorded.`)
          : (r?.error || 'Could not resume that flow.'),
        steps: (r?.steps || []) as FlowResult[],
        jumps: r?.jumps as FlowJump[] | undefined,
      });
      setError(r?.success ? '' : (r?.error || ''));
    } catch (e:any) {
      setError(e?.message || 'Could not resume that flow.');
    }
    setResuming(null);
    // It is no longer interrupted once it has been picked up.
    await fetchCheckpoints();
  };

  const discardCheckpoint = async (flowId:string) => {
    if (wsState !== 'connected') return;
    try { await send('swarm.discardCheckpoint', { flowId }); } catch { /* ignore */ }
    await fetchCheckpoints();
  };

  const openNotebook = async (agentId:string) => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('swarm.notebook', { agentId });
      setNotebook(r?.success === false ? null : (r as Notebook));
      setNotebookHits(null);
      setNotebookQuery('');
      setError(r?.success === false ? (r?.error || 'Could not read it') : '');
    } catch (e:any) { setError(e?.message || 'Could not read that notebook'); }
  };

  const searchNotebook = async () => {
    if (!notebook?.agentId || !notebookQuery.trim()) return;
    try {
      const r = await send('swarm.notebookSearch',
        { agentId: notebook.agentId, query: notebookQuery.trim() });
      setNotebookHits(r?.results || []);
    } catch { setNotebookHits([]); }
  };

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

  const addFlowStep=(agentId:string)=>{
    const first=agents[0]?.id||'';
    setFlowSteps(prev=>[...prev,{id:Math.random().toString(36).slice(2),agentId:agentId||first,task:'',parallel:[],merge:'',until:'',notContains:false,retry:'',maxAttempts:3,dependsOn:[],optional:false,judgeMode:false,onPass:'',onFail:''}]);
  };
  const updateFlowStep=(id:string,patch:Partial<FlowStep>)=>setFlowSteps(prev=>prev.map(s=>s.id===id?{...s,...patch}:s));
  const removeFlowStep=(id:string)=>setFlowSteps(prev=>prev.filter(s=>s.id!==id));
  const moveFlowStep=(id:string,dir:-1|1)=>setFlowSteps(prev=>{
    const i=prev.findIndex(s=>s.id===id); if(i<0)return prev;
    const j=i+dir; if(j<0||j>=prev.length)return prev;
    const next=[...prev]; [next[i],next[j]]=[next[j],next[i]]; return next;
  });

  const togglePeer=(stepId:string,agentId:string)=>setFlowSteps(prev=>prev.map(s=>s.id===stepId?{
    ...s, parallel: s.parallel.includes(agentId)
      ? s.parallel.filter(a=>a!==agentId)
      : [...s.parallel, agentId],
  }:s));

  const runFlow=async()=>{
    if(wsState!=='connected')return;
    const steps=flowSteps
      .filter(s=>s.agentId&&s.task.trim())
      .map((s, i)=>({
        agentId:s.agentId,
        task:s.task.trim(),
        parallel:s.parallel.filter(a=>a!==s.agentId),
        // "auto" deliberately sends nothing: the backend only merges when a
        // merge agent is named, so an unnamed merge means "keep both answers"
        // rather than re-running the lead to summarise itself.
        merge:s.merge||'',
        // 1-based step numbers, the same ones the UI shows.
        dependsOn:s.dependsOn.filter(d=>d>=1&&d<=flowSteps.length&&d!==i+1),
        optional:s.optional,
        // Branch targets are only sent when set, so an untouched step keeps
        // the exact behaviour it had before branching existed.
        ...(s.onPass?{onPass:s.onPass}:{}),
        ...(s.onFail?{onFail:s.onFail}:{}),
        // A gate needs a verdict and an agent to send rejected work back to.
        // Sending one without the other is refused by the backend, so it is
        // only included when both are set.
        ...(s.until.trim()&&s.retry
          ? { until: s.judgeMode
              ? { judge: s.retry, criterion: s.until.trim(), maxAttempts: s.maxAttempts }
              : s.notContains
                ? { notContains: s.until.trim(), agent: s.retry, maxAttempts: s.maxAttempts }
                : { contains: s.until.trim(), agent: s.retry, maxAttempts: s.maxAttempts },
              retry: s.retry }
          : {}),
      }));
    if(!steps.length){ setFlowResult({ok:false,text:'Add at least one step with a task.',steps:[]}); return; }
    setFlowRunning(true); setFlowResult(null);
    try{
      const r=await send('swarm.flow',{steps,goal:flowGoal.trim()});
      setFlowResult({
        ok:!!r?.success,
        text:String(r?.response||r?.error||'').trim(),
        steps:Array.isArray(r?.steps)?r.steps: [],
        jumps:Array.isArray(r?.jumps)?r.jumps: [],
      });
    }catch(e:any){
      setFlowResult({ok:false,text:e?.message||'The flow failed',steps:[]});
    }finally{ setFlowRunning(false); await fetchAgents(); await fetchMemory(); }
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

        {/* ---------------- interrupted flows ---------------- */}
        {/* Shown above the builder because it is the thing that needs an
            answer: work already paid for, waiting to be finished. */}
        {checkpoints.length>0&&(
          <div className="mb-5 bg-[#1c1a12] border border-[#d2992244] rounded-lg p-3">
            <h2 className="text-sm font-medium text-[#e8eaed] mb-1">Interrupted flows</h2>
            <p className="text-[11px] text-[#8b949e] mb-2">
              These stopped part-way. The finished steps are kept, so resuming continues from where it stopped rather than running everything again.
            </p>
            <div className="space-y-1.5">
              {checkpoints.map(cp=>(
                <div key={cp.flowId} className="flex items-center gap-2 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1.5">
                  <span className="text-xs text-[#e8eaed] min-w-0 flex-1 truncate" title={cp.goal||cp.flowId}>
                    {cp.goal || cp.flowId}
                  </span>
                  <span className="text-[10px] text-[#d29922] shrink-0">
                    {cp.finished} of {cp.steps} done
                  </span>
                  <button onClick={()=>resumeFlow(cp.flowId)} disabled={resuming!==null||wsState!=='connected'}
                    className="text-[10px] px-2 py-0.5 rounded bg-[#21262d] hover:bg-[#30363d] text-[#58a6ff] disabled:opacity-50 shrink-0">
                    {resuming===cp.flowId?'Resuming…':'Resume'}
                  </button>
                  <button onClick={()=>discardCheckpoint(cp.flowId)} disabled={resuming!==null||wsState!=='connected'}
                    className="text-[10px] text-[#8b949e] hover:text-[#f85149] disabled:opacity-50 shrink-0"
                    title="Throw it away without resuming">✕</button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ---------------- flow builder ---------------- */}
        {agents.length>0&&(
          <div className="mb-5 bg-[#161b22] border border-[#30363d] rounded-lg p-4">
            <div className="flex items-center gap-2 mb-3">
              <h2 className="text-sm font-medium text-[#e8eaed]">Flow</h2>
              <span className="text-xs text-[#8b949e]">run agents in order — each step sees what the ones before it produced</span>
              {memoryInfo && (
                <span className="ml-auto flex items-center gap-2 text-[10px] text-[#8b949e]">
                  <span title="Finished steps the swarm still remembers, shared across flows">
                    🧠 {memoryInfo.entries} remembered
                  </span>
                  {memoryInfo.entries > 0 && (
                    <button onClick={clearMemory} className="text-[#8b949e] hover:text-[#f85149]" title="Forget the shared memory">clear</button>
                  )}
                </span>
              )}
            </div>
            <input value={flowGoal} onChange={e=>setFlowGoal(e.target.value)} placeholder="Overall goal (optional)" className="w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58] mb-3"/>
            <div className="space-y-2 mb-3">
              {flowSteps.map((step,index)=>(
                <div key={step.id} className="border border-[#30363d] rounded p-2 space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-[#484f58] w-4 shrink-0 text-right">{index+1}</span>
                    <select value={step.agentId} onChange={e=>updateFlowStep(step.id,{agentId:e.target.value})} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] shrink-0">
                      {agents.map(a=><option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                    <input value={step.task} onChange={e=>updateFlowStep(step.id,{task:e.target.value})} placeholder="What this agent should do…" className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
                    <button onClick={()=>moveFlowStep(step.id,-1)} disabled={index===0} className="text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-30 px-1" title="Move up">↑</button>
                    <button onClick={()=>moveFlowStep(step.id,1)} disabled={index===flowSteps.length-1} className="text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-30 px-1" title="Move down">↓</button>
                    <button onClick={()=>removeFlowStep(step.id)} className="text-[#8b949e] hover:text-[#f85149] px-1" title="Remove step">✕</button>
                  </div>
                  {agents.length>1&&(
                    <div className="flex items-center gap-2 pl-6 flex-wrap">
                      <span className="text-[10px] text-[#484f58]">also work this step:</span>
                      {agents.filter(a=>a.id!==step.agentId).map(a=>(
                        <label key={a.id} className="flex items-center gap-1 text-[10px] text-[#8b949e] cursor-pointer">
                          <input type="checkbox" checked={step.parallel.includes(a.id)} onChange={()=>togglePeer(step.id,a.id)} className="accent-[#3380FF]"/>
                          {a.name}
                        </label>
                      ))}
                      {step.parallel.length>0&&(
                        <>
                          <span className="text-[10px] text-[#484f58] ml-1">merge with:</span>
                          <select value={step.merge} onChange={e=>updateFlowStep(step.id,{merge:e.target.value})} className="bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]">
                            <option value="">no merge (keep both)</option>
                            {agents.map(a=><option key={a.id} value={a.id}>merge with {a.name}</option>)}
                          </select>
                        </>
                      )}
                    </div>
                  )}
                  {/* A gate sends rejected work back to an agent until the
                      verdict matches — a review loop. */}
                  {/* Dependencies: which earlier steps must finish first. */}
                  {flowSteps.length>1&&(
                    <div className="flex items-center gap-2 pl-6 flex-wrap">
                      <span className="text-[10px] text-[#484f58]">after:</span>
                      {flowSteps.map((other,oi)=>oi===index?null:(
                        <label key={other.id} className="flex items-center gap-1 text-[10px] text-[#8b949e] cursor-pointer">
                          <input type="checkbox" checked={step.dependsOn.includes(oi+1)}
                            onChange={()=>setFlowSteps(prev=>prev.map(s=>s.id===step.id?{
                              ...s,
                              dependsOn: s.dependsOn.includes(oi+1)
                                ? s.dependsOn.filter(d=>d!==oi+1)
                                : [...s.dependsOn, oi+1],
                            }:s))}
                            className="accent-[#3380FF]"/>
                          {oi+1}
                        </label>
                      ))}
                      {step.dependsOn.length>0&&(
                        <span className="text-[10px] text-[#484f58]">this step waits for those to finish</span>
                      )}
                    </div>
                  )}
                  {agents.length>1&&step.parallel.length===0&&(
                    <div className="flex items-center gap-2 pl-6 flex-wrap">
                      <span className="text-[10px] text-[#484f58]">repeat until</span>
                      {!step.judgeMode&&(
                        <select value={step.notContains?'notContains':'contains'} onChange={e=>updateFlowStep(step.id,{notContains:e.target.value==='notContains'})} className="bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]">
                          <option value="contains">verdict has</option>
                          <option value="notContains">verdict lacks</option>
                        </select>
                      )}
                      {step.judgeMode&&<span className="text-[10px] text-[#8b949e]">criterion</span>}
                      <input value={step.until} onChange={e=>updateFlowStep(step.id,{until:e.target.value})}
                        placeholder={step.judgeMode?'e.g. has tests and handles errors':'PASS'}
                        className="flex-1 min-w-[12rem] bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed] placeholder-[#484f58]"/>
                      <span className="text-[10px] text-[#484f58]">{step.judgeMode?'judged by':'checked by'}</span>
                      <select value={step.retry} onChange={e=>updateFlowStep(step.id,{retry:e.target.value})} className="bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]">
                        <option value="">no gate</option>
                        {agents.filter(a=>a.id!==step.agentId).map(a=><option key={a.id} value={a.id}>{a.name}</option>)}
                      </select>
                      {step.retry&&(
                        <>
                          <span className="text-[10px] text-[#484f58]">max</span>
                          <input type="number" min={1} max={5} value={step.maxAttempts} onChange={e=>updateFlowStep(step.id,{maxAttempts:Math.max(1,Math.min(5,Number(e.target.value)||1))})} className="w-12 bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]"/>
                        </>
                      )}
                      <label className="flex items-center gap-1 text-[10px] text-[#8b949e] cursor-pointer"
                        title="Let the agent decide, instead of matching text. Costs one extra model call per check, but 'FAIL, does not pass' no longer reads as a pass.">
                        <input type="checkbox" checked={step.judgeMode} onChange={()=>updateFlowStep(step.id,{judgeMode:!step.judgeMode})} className="accent-[#3380FF]"/>
                        judge it
                      </label>
                      {step.until&&step.retry&&!step.judgeMode&&(
                        <span className="text-[10px] text-[#484f58]" title="Matching is case-insensitive substring. 'PASS' also matches inside 'NOT PASSED'.">
                          ⓘ substring match
                        </span>
                      )}
                    </div>
                  )}
                  <div className="flex items-center gap-2 pl-6">
                    <label className="flex items-center gap-1 text-[10px] text-[#8b949e] cursor-pointer"
                      title="If this step fails, its dependents are skipped and the flow carries on instead of stopping.">
                      <input type="checkbox" checked={step.optional}
                        onChange={()=>updateFlowStep(step.id,{optional:!step.optional})}
                        className="accent-[#3380FF]"/>
                      optional — carry on if it fails
                    </label>
                  </div>
                  {/* Branching: where to go instead of just on to the next step. */}
                  {flowSteps.length>1&&(
                    <div className="flex items-center gap-2 pl-6 flex-wrap">
                      <span className="text-[10px] text-[#484f58]">on pass go to</span>
                      <select value={step.onPass} onChange={e=>updateFlowStep(step.id,{onPass:e.target.value})} className="bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]">
                        <option value="">next step (default)</option>
                        <option value="done">done — finish</option>
                        <option value="stop">stop — fail</option>
                        {flowSteps.map((_,oi)=>oi===index?null:<option key={oi} value={String(oi+1)}>step {oi+1}</option>)}
                      </select>
                      <span className="text-[10px] text-[#484f58]">on fail go to</span>
                      <select value={step.onFail} onChange={e=>updateFlowStep(step.id,{onFail:e.target.value})} className="bg-[#0d1117] border border-[#30363d] rounded px-1.5 py-0.5 text-[10px] text-[#e8eaed]">
                        <option value="">stop the flow (default)</option>
                        <option value="done">done — finish</option>
                        <option value="stop">stop — fail</option>
                        {flowSteps.map((_,oi)=>oi===index?null:<option key={oi} value={String(oi+1)}>step {oi+1}</option>)}
                      </select>
                      {(step.onPass||step.onFail)&&(
                        <span className="text-[10px] text-[#d29922]" title="A branch can loop. The flow stops after a jump budget rather than running forever.">
                          ⚠ may loop — budgeted
                        </span>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {flowSteps.length===0&&<p className="text-xs text-[#484f58]">No steps yet. Add one to start a flow.</p>}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={()=>addFlowStep('')} className="text-xs px-2 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed]">+ Add step</button>
              <button onClick={runFlow} disabled={flowRunning||wsState!=='connected'||!flowSteps.length} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium">
                {flowRunning?'Running flow…':'Run flow'}
              </button>
            </div>

            {flowResult&&(
              <div className="mt-3 space-y-2">
                {flowResult.steps.map((s,i)=>(
                  <div key={i} className="border-l-2 pl-2" style={{borderColor:s.success?'#3fb950':'#f85149'}}>
                    <p className="text-[11px] text-[#8b949e]">
                      {i+1}. <span className="text-[#e8eaed]">{s.agent}</span> — {s.task}
                      {s.parallel&&<span className="text-[#58a6ff]"> · parallel</span>}
                      {s.merged&&<span className="text-[#a371f7]"> · merged</span>}
                      {s.gate&&<span className="text-[#d29922]"> · gate</span>}
                      {s.skipped&&<span className="text-[#8b949e]"> · skipped</span>}
                      {(s.attempt||0)>1&&<span className="text-[#8b949e]"> · attempt {s.attempt}</span>}
                      {!s.success&&!s.skipped&&<span className="text-[#f85149]"> · {s.error}</span>}
                    </p>
                    {s.success&&s.output&&(
                      <p className="text-[11px] text-[#c9d1d9] whitespace-pre-wrap break-words max-h-32 overflow-y-auto mt-0.5">{String(s.output).slice(0,600)}</p>
                    )}
                  </div>
                ))}
                {flowResult.steps.length===0&&(
                  <p className="text-xs text-[#f85149] whitespace-pre-wrap break-words">{flowResult.text}</p>
                )}
                {!!(flowResult.jumps&&flowResult.jumps.length)&&(
                  <p className="text-[10px] text-[#8b949e] pt-1 border-t border-[#21262d]">
                    branches: {flowResult.jumps.map((j,i)=>(
                      <span key={i} className="text-[#d29922]">
                        {i>0?' · ':''}step {j.from} {j.reason} → {String(j.to)}
                      </span>
                    ))}
                  </p>
                )}
              </div>
            )}
          </div>
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
              {(agent.status!=='running')?(
                <div className="flex gap-2">
                  <input value={taskInputs[agent.id]||''} onChange={e=>setTaskInputs(prev=>({...prev,[agent.id]:e.target.value}))} onKeyDown={e=>{if(e.key==='Enter')handleRun(agent);}} placeholder="Assign a task..." className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
                  <button onClick={()=>handleRun(agent)} disabled={!taskInputs[agent.id]?.trim()||busy===agent.id||wsState!=='connected'} className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium">{busy===agent.id?'…':'Run'}</button>
                  {/* Each desk keeps its own record of what it has done, on
                      disk, so it survives a restart — and only its own, which
                      is the difference from the shared memory above. */}
                  <button onClick={()=>openNotebook(agent.id)} disabled={wsState!=='connected'}
                    title="What this agent has done before — kept across restarts"
                    className="text-[10px] px-2 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] hover:border-[#484f58] disabled:opacity-50 shrink-0">
                    📓 Record
                  </button>
                </div>
              ):(
                <button onClick={()=>handleStop(agent)} className="text-xs px-3 py-1 bg-[#f85149] text-white rounded hover:bg-[#ff6a63]">Stop &amp; remove</button>
              )}
            </div>
          ))}
        </div>

        {/* ---------------- one agent's notebook ---------------- */}
        {notebook&&(
          <div className="mt-4 bg-[#161b22] border border-[#30363d] rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <h2 className="text-sm font-medium text-[#e8eaed]">
                📓 {notebook.name || notebook.agentId}
              </h2>
              <span className="text-[11px] text-[#8b949e]">
                its own record — {notebook.entries.length} entr{notebook.entries.length===1?'y':'ies'}
                {notebook.flows.length?` across ${notebook.flows.length} flow(s)`:''}
              </span>
              <button onClick={()=>{setNotebook(null);setNotebookHits(null);}}
                className="ml-auto text-[#8b949e] hover:text-[#e8eaed] text-sm" title="Close">✕</button>
            </div>
            {notebook.summary&&(
              <div className="mb-2">
                <p className="text-[10px] uppercase tracking-wide text-[#484f58] mb-1">Folded summary</p>
                <p className="text-xs text-[#8b949e] whitespace-pre-wrap break-words max-h-32 overflow-y-auto bg-[#0d1117] border border-[#21262d] rounded p-2">{notebook.summary}</p>
              </div>
            )}
            {/* The digest is what actually rides into the agent's next task, so
                showing it explains how much of this the agent really carries. */}
            {notebook.digest&&(
              <div className="mb-2">
                <p className="text-[10px] uppercase tracking-wide text-[#484f58] mb-1">
                  Carried into its next task
                </p>
                <p className="text-xs text-[#8b949e] whitespace-pre-wrap break-words max-h-40 overflow-y-auto bg-[#0d1117] border border-[#21262d] rounded p-2">{notebook.digest}</p>
              </div>
            )}
            <div className="flex gap-2 mb-2">
              <input value={notebookQuery} onChange={e=>setNotebookQuery(e.target.value)}
                onKeyDown={e=>{if(e.key==='Enter')searchNotebook();}}
                placeholder="Ask this agent's record for something earlier…"
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
              <button onClick={searchNotebook} disabled={!notebookQuery.trim()}
                className="text-xs px-3 py-1 rounded bg-[#21262d] hover:bg-[#30363d] text-[#58a6ff] disabled:opacity-50">Search</button>
            </div>
            {notebookHits!==null&&(
              notebookHits.length===0
                ? <p className="text-xs text-[#8b949e]">Nothing in this agent&rsquo;s record matches that.</p>
                : <div className="space-y-1.5 max-h-52 overflow-y-auto">
                    {notebookHits.map((h,i)=>(
                      <div key={i} className="bg-[#0d1117] border border-[#21262d] rounded p-2">
                        <p className="text-[10px] text-[#484f58] mb-0.5">{h.task||'earlier work'}</p>
                        <p className="text-xs text-[#e8eaed] whitespace-pre-wrap break-words">{(h.output||'').slice(0,600)}</p>
                      </div>
                    ))}
                  </div>
            )}
            {notebookHits===null&&notebook.entries.length>0&&(
              <div className="space-y-1.5 max-h-52 overflow-y-auto">
                {notebook.entries.slice().reverse().map((e,i)=>(
                  <div key={i} className="bg-[#0d1117] border border-[#21262d] rounded p-2">
                    <p className="text-[10px] text-[#484f58] mb-0.5">{e.task||'work'}</p>
                    <p className="text-xs text-[#8b949e] whitespace-pre-wrap break-words">{(e.output||'').slice(0,400)}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
