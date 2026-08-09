'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface Goal {
  id: string;
  title: string;
  description: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'cancelled';
  priority: 'low' | 'normal' | 'high' | 'urgent';
  createdAt: string;
  plan?: { steps: { index: number; description: string; status: string }[] };
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

export default function GoalsPage() {
  const { state: wsState, send } = useWS();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState<string>('normal');
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<string>('all');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Fetch goals from backend on mount
  const fetchGoals = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('goal.list', {});
      if (r?.goals) {
        setGoals(r.goals.map((g: Record<string, unknown>) => ({
          id: g.id as string,
          title: g.title as string,
          description: (g.description as string) || '',
          status: g.status as Goal['status'],
          priority: (g.priority as Goal['priority']) || 'normal',
          createdAt: g.created_at ? new Date((g.created_at as number) * 1000).toISOString() : new Date().toISOString(),
          plan: g.plan as Goal['plan'],
        })));
      }
    } catch {}
  }, [wsState, send]);

  useEffect(() => { fetchGoals(); }, [fetchGoals]);

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
        plan: r?.plan,
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
          filtered.map(goal => (
            <div key={goal.id}
              className={`bg-[#161b22] border border-[#30363d] rounded-lg p-4 cursor-pointer transition-colors hover:border-[#484f58] ${expandedId===goal.id ? 'border-[#3380FF]' : ''}`}
              onClick={() => setExpandedId(expandedId===goal.id ? null : goal.id)}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[goal.status]}`}>{goal.status.replace('_',' ')}</span>
                  <span className={`text-xs font-medium ${PRIORITY_COLORS[goal.priority]}`}>● {goal.priority}</span>
                </div>
                <span className="text-xs text-[#484f58]">{new Date(goal.createdAt).toLocaleDateString()}</span>
              </div>
              <h3 className="text-sm font-medium text-[#e8eaed] mt-2">{goal.title}</h3>
              {expandedId === goal.id && goal.description && (
                <p className="text-xs text-[#8b949e] mt-1">{goal.description}</p>
              )}
              {expandedId === goal.id && (
                <div className="flex gap-2 mt-3 pt-3 border-t border-[#21262d]">
                  {goal.status === 'pending' && <button onClick={(e) => handleStart(goal.id, e)}
                    className="text-xs px-3 py-1 bg-[#1f6feb] text-white rounded hover:bg-[#388bfd]">Start</button>}
                  {goal.status === 'in_progress' && <button onClick={(e) => handleCancel(goal.id, e)}
                    className="text-xs px-3 py-1 bg-[#d29922] text-black rounded hover:bg-[#e2a93b]">Pause</button>}
                  {(goal.status === 'pending' || goal.status === 'in_progress') && <button onClick={(e) => handleCancel(goal.id, e)}
                    className="text-xs px-3 py-1 bg-[#f85149] text-white rounded hover:bg-[#ff6a63]">Cancel</button>}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
