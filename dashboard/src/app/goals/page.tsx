'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface PlanStep {
  index: number;
  kind: string;
  role?: string;
  agentId?: string;
  agent?: string;
  description: string;
  status: string;
  error?: string;
}

interface Finding {
  step?: number;
  role?: string;
  description?: string;
  output?: string;
  round?: number;
}

interface Goal {
  id: string;
  title: string;
  description: string;
  status: string;
  priority: string;
  createdAt: string;
  steps: PlanStep[];
  findings: Finding[];
  rounds: number;
  planKind: string;
}

interface Agent {
  id: string;
  name: string;
  role?: string;
  type?: string;
  status: string;
  currentTask?: string;
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-[#30363d] text-[#8b949e]',
  in_progress: 'bg-[#1f6feb22] text-[#58a6ff]',
  completed: 'bg-[#3fb95022] text-[#3fb950]',
  failed: 'bg-[#f8514922] text-[#f85149]',
  cancelled: 'bg-[#f8514922] text-[#f85149]',
};

const PRIORITY_COLORS: Record<string, string> = {
  low: 'text-[#8b949e]',
  normal: 'text-[#e8eaed]',
  high: 'text-[#d29922]',
  urgent: 'text-[#f85149]',
};

// Mirrors the swarm page's dot colours, so an agent reads the same here as it
// does on the Agents tab.
const STATUS_DOT: Record<string, string> = {
  offline: 'bg-[#484f58]',
  ready: 'bg-[#3fb950]',
  running: 'bg-[#d29922] animate-pulse',
  error: 'bg-[#f85149]',
};

// A step's status is not the same scale as an agent's. Kept separate so a
// completed step cannot be read as a running agent.
const STEP_COLORS: Record<string, string> = {
  pending: 'text-[#8b949e] bg-[#21262d]',
  in_progress: 'text-[#58a6ff] bg-[#1f6feb22]',
  completed: 'text-[#3fb950] bg-[#3fb95022]',
  failed: 'text-[#f85149] bg-[#f8514922]',
  blocked: 'text-[#d29922] bg-[#d2992222]',
  skipped: 'text-[#8b949e] bg-[#21262d]',
};

function ProgressBar({ steps }: { steps: PlanStep[] }) {
  const total = steps.length;
  const done = steps.filter(s => s.status === 'completed').length;
  // Clamped: a re-plan mid-flight can leave the local list briefly out of step
  // with the reported count, and a bar wider than its track looks like a bug
  // even when the number behind it is right.
  const pct = total ? Math.min(100, Math.max(0, Math.round((done / total) * 100))) : 0;
  return (
    <div className="w-full">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-[#8b949e]">{done} / {total} steps</span>
        <span className="text-xs text-[#8b949e]">{pct}%</span>
      </div>
      <div className="w-full h-2 bg-[#21262d] rounded-full overflow-hidden">
        <div className="h-full bg-[#3380FF] transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function StepRow({ step, agent }: { step: PlanStep; agent?: Agent }) {
  return (
    <div className="bg-[#0d1117] rounded-md px-2 py-1.5">
      <div className="flex items-start gap-2">
        <span className="text-xs text-[#484f58] w-4 shrink-0 pt-0.5">{step.index}</span>
        <span className={`text-xs px-1.5 py-0.5 rounded shrink-0 ${STEP_COLORS[step.status] || STEP_COLORS.pending}`}>
          {step.status.replace('_', ' ')}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-xs text-[#e8eaed] break-words">{step.description}</p>
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            {step.agent && (<span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[agent?.status || 'ready'] || STATUS_DOT.ready}`} />)}
            <span className="text-xs text-[#8b949e]">
              {step.kind === 'action' ? '[action]' : (step.agent ? `[agent] ${step.agent}` : `[agent] role: ${step.role || 'general'}`)}
            </span>
            {/* The desk's live state: the difference between "assigned to a
                researcher" and "the researcher is working on it now". */}
            {step.agent && agent && <span className="text-xs text-[#484f58]">· {agent.status}</span>}
          </div>
          {step.agent && agent?.currentTask && (
            <p className="text-xs text-[#58a6ff] mt-1 truncate">📋 {agent.currentTask}</p>
          )}
          {step.error && <p className="text-xs text-[#f85149] mt-1 break-words">⚠ {step.error}</p>}
        </div>
      </div>
    </div>
  );
}

export default function GoalsPage() {
  const { state: wsState, send, onNotification } = useWS();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [agents, setAgents] = useState<Record<string, Agent>>({});
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState<string>('normal');
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<string>('all');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const mapGoal = useCallback((g: Record<string, unknown>): Goal => {
    const plan = (g.plan as Record<string, unknown>) || {};
    return {
      id: g.id as string,
      title: g.title as string,
      description: (g.description as string) || '',
      status: (g.status as string) || 'pending',
      priority: (g.priority as string) || 'normal',
      createdAt: g.created_at
        ? new Date((g.created_at as number) * 1000).toISOString()
        : new Date().toISOString(),
      steps: (plan.steps as PlanStep[]) || [],
      findings: (g.findings as Finding[]) || [],
      rounds: (g.rounds as number) || 0,
      planKind: (plan.kind as string) || '',
    };
  }, []);

  const fetchGoals = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('goal.list', {});
      if (r?.goals) setGoals(r.goals.map(mapGoal));
    } catch {}
  }, [wsState, send, mapGoal]);

  const fetchAgents = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('swarm.list', {});
      const list: Agent[] = r?.agents || [];
      const byId: Record<string, Agent> = {};
      for (const a of list) byId[a.id] = a;
      setAgents(byId);
    } catch {}
  }, [wsState, send]);

  useEffect(() => { fetchGoals(); }, [fetchGoals]);
  useEffect(() => { fetchAgents(); }, [fetchAgents]);

  // Live progress.
  //
  // The backend has always broadcast this — round, step, total, which desk took
  // the step, what it is doing — and the page never listened, so every goal ran
  // with a dead progress display. `onNotification` keeps ONE handler per
  // method, so the Goals page owns `goal.progress`; no other component may
  // subscribe to it.
  useEffect(() => onNotification('goal.progress', (p: any) => {
    const goalId = p?.goalId;
    if (!goalId) return;

    setGoals(prev => prev.map(g => {
      if (g.id !== goalId) return g;
      const next: Goal = { ...g };
      if (typeof p.round === 'number') next.rounds = p.round;

      // A re-plan sends a whole new step list. Adopting it wholesale is the
      // point: merging by index would leave steps from the abandoned plan
      // sitting in the list looking like work still to do. The resolved desk is
      // carried over where the position survives, because a snapshot can be
      // sent before that step has been assigned one.
      if (Array.isArray(p.steps) && p.steps.length) {
        next.steps = (p.steps as PlanStep[]).map(s => {
          const before = g.steps.find(o => o.index === s.index);
          return { ...s, agent: s.agent || before?.agent, agentId: s.agentId || before?.agentId };
        });
      } else if (typeof p.step === 'number' && p.step > 0) {
        next.steps = g.steps.map(s => {
          if (s.index !== p.step) return s;
          if (p.status === 'step_started') return { ...s, status: 'in_progress' };
          if (p.status === 'step_completed') return { ...s, status: 'completed' };
          if (p.status === 'step_failed') return { ...s, status: 'failed', error: p.error };
          return s;
        });
      }

      // The event carrying an agent id is what knows which desk took the step.
      if (p.agentId || p.agent) {
        next.steps = next.steps.map(s =>
          s.index === p.step
            ? { ...s, agentId: p.agentId || s.agentId, agent: p.agent || s.agent }
            : s);
      }

      if (p.status === 'running' || p.status === 'replanning') next.status = 'in_progress';
      else if (p.status === 'completed') next.status = 'completed';
      else if (p.status === 'failed') next.status = 'failed';
      else if (p.status === 'cancelled') next.status = 'cancelled';

      return next;
    }));

    // Keep the agents' state current while a goal runs, and pull the final
    // record once it stops so findings are complete.
    if (p.status === 'step_started' || p.status === 'step_completed') fetchAgents();
    if (p.status === 'completed' || p.status === 'failed' || p.status === 'cancelled') {
      fetchAgents();
      fetchGoals();
    }
  }), [onNotification, fetchAgents, fetchGoals]);

  const handleCreate = async () => {
    if (!title.trim() || wsState !== 'connected') return;
    setCreating(true);
    try {
      const r = await send('goal.create', { title: title.trim(), description: description.trim(), priority });
      const newGoal: Goal = {
        id: r?.goalId || `goal_${Date.now()}`,
        title: title.trim(),
        description: description.trim(),
        status: 'pending',
        priority: priority as Goal['priority'],
        createdAt: new Date().toISOString(),
        steps: (r?.plan?.steps as PlanStep[]) || [],
        findings: [],
        rounds: 0,
        planKind: (r?.plan?.kind as string) || '',
      };
      setGoals(prev => [newGoal, ...prev]);
      setTitle(''); setDescription('');
    } catch {}
    setCreating(false);
  };

  const handleStart = async (goalId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (wsState !== 'connected') return;
    try {
      await send('goal.start', { goalId });
      setGoals(prev => prev.map(g => g.id === goalId ? { ...g, status: 'in_progress' } : g));
      setExpandedId(goalId);
    } catch {}
  };

  const handleCancel = async (goalId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (wsState !== 'connected') return;
    try {
      await send('goal.cancel', { goalId });
      setGoals(prev => prev.map(g => g.id === goalId ? { ...g, status: 'cancelled' } : g));
    } catch {}
  };

  const filtered = filter === 'all' ? goals : goals.filter(g => g.status === filter);

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Goals</h1>
        <div className="flex gap-2">
          {['all','pending','in_progress','completed','failed'].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`text-xs px-2 py-1 rounded-md transition-colors ${filter===f ? 'bg-[#1f6feb] text-white' : 'text-[#8b949e] hover:bg-[#21262d]'}`}>
              {f.replace('_',' ')}
            </button>
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {/* Create goal */}
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <input value={title} onChange={e=>setTitle(e.target.value)} placeholder="Goal title..."
            className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] mb-2 focus:outline-none focus:border-[#3380FF]"/>
          <textarea value={description} onChange={e=>setDescription(e.target.value)} placeholder="Describe what you want to accomplish..." rows={2}
            className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] mb-3 focus:outline-none focus:border-[#3380FF] resize-none"/>
          <div className="flex items-center justify-between">
            <select value={priority} onChange={e=>setPriority(e.target.value)}
              className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]">
              <option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option>
            </select>
            <button onClick={handleCreate} disabled={!title.trim()||creating||wsState!=='connected'}
              className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-1.5 text-sm font-medium transition-colors">
              {creating ? 'Creating...' : 'Create Goal'}
            </button>
          </div>
        </div>

        {/* Goal list */}
        {filtered.length === 0 ? (
          <div className="text-center py-12 text-[#8b949e]">
            <span className="text-4xl mb-3 block">🎯</span>
            <p className="text-sm">{goals.length === 0 ? 'No goals yet. Create one above!' : 'No goals match this filter.'}</p>
          </div>
        ) : (
          filtered.map(goal => {
            const open = expandedId === goal.id;
            return (
              <div key={goal.id}
                className={`bg-[#161b22] border border-[#30363d] rounded-lg p-4 cursor-pointer transition-colors hover:border-[#484f58] ${open ? 'border-[#3380FF]' : ''}`}
                onClick={() => setExpandedId(open ? null : goal.id)}>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[goal.status] || STATUS_COLORS.pending}`}>{goal.status.replace('_',' ')}</span>
                    <span className={`text-xs font-medium ${PRIORITY_COLORS[goal.priority]}`}>- {goal.priority}</span>
                    {goal.rounds > 1 && (
                      <span className="text-xs text-[#d29922]" title="The plan was rewritten this many times">
                        round {goal.rounds} of 3
                      </span>
                    )}
                  </div>
                  <span className="text-xs text-[#484f58]">{new Date(goal.createdAt).toLocaleDateString()}</span>
                </div>
                <h3 className="text-sm font-medium text-[#e8eaed] mt-2">{goal.title}</h3>

                {/* Shown collapsed too, so a running goal is legible from the
                    list without opening it. */}
                {goal.steps.length > 0 && (
                  <div className="mt-3">
                    <ProgressBar steps={goal.steps} />
                  </div>
                )}

                {open && goal.description && (
                  <p className="text-xs text-[#8b949e] mt-3">{goal.description}</p>
                )}

                {open && (
                  <div className="mt-3 pt-3 border-t border-[#21262d] space-y-3">
                    {/* Plan, and who is running each step */}
                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs font-semibold text-[#8b949e] uppercase tracking-wide">Plan</span>
                        <span className="text-xs text-[#484f58]">
                          {goal.planKind === 'fallback'
                            ? 'fallback - no usable plan was produced'
                            : `${goal.steps.length} steps`}
                        </span>
                      </div>
                      {goal.steps.length === 0 ? (
                        <p className="text-xs text-[#484f58]">No plan yet.</p>
                      ) : (
                        <div className="space-y-1.5">
                          {goal.steps.map(step => (
                            <StepRow key={step.index} step={step}
                              agent={step.agentId ? agents[step.agentId] : undefined} />
                          ))}
                        </div>
                      )}
                    </div>

                    {/* What the agents actually produced. A goal that ran but
                        shows nothing is indistinguishable from one that did
                        nothing, so the output is surfaced, not just the status. */}
                    {goal.findings.length > 0 && (
                      <div>
                        <span className="text-xs font-semibold text-[#8b949e] uppercase tracking-wide">Findings</span>
                        <div className="space-y-1.5 mt-2">
                          {goal.findings.map((f, i) => (
                            <div key={i} className="bg-[#0d1117] rounded-md px-2 py-1.5">
                              <div className="flex items-center gap-2">
                                <span className="text-xs text-[#484f58]">step {f.step ?? '?'}</span>
                                <span className="text-xs text-[#8b949e]">{f.role}</span>
                                {typeof f.round === 'number' && f.round > 1 && (
                                  <span className="text-xs text-[#d29922]">round {f.round}</span>
                                )}
                              </div>
                              <p className="text-xs text-[#e8eaed] mt-1 whitespace-pre-wrap break-words">{f.output}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="flex gap-2 pt-1">
                      {goal.status === 'pending' && <button onClick={(e) => handleStart(goal.id, e)}
                        className="text-xs px-3 py-1 bg-[#1f6feb] text-white rounded hover:bg-[#388bfd]">Start</button>}
                      {goal.status === 'in_progress' && <button onClick={(e) => handleCancel(goal.id, e)}
                        className="text-xs px-3 py-1 bg-[#d29922] text-black rounded hover:bg-[#e2a93b]">Pause</button>}
                      {(goal.status === 'pending' || goal.status === 'in_progress') && <button onClick={(e) => handleCancel(goal.id, e)}
                        className="text-xs px-3 py-1 bg-[#f85149] text-white rounded hover:bg-[#ff6a63]">Cancel</button>}
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
