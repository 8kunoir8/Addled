'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

const DAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];

interface CalendarEvent { id:string; title:string; start:string; end:string; source:string; }
interface TaskRow { date:string; task_id:string; title:string; action:string; time:string; kind:string; rule:string; }
interface TaskDetail { id:string; title:string; action:string; time:string; date:string; enabled:boolean; rule:string; payload:string; recurrence:{type:string;weekdays:number[]}; }

export default function CalendarPage() {
  const { state: wsState, send } = useWS();
  const today=new Date();
  const [viewDate,setViewDate]=useState(new Date(today.getFullYear(),today.getMonth(),1));
  const [events,setEvents]=useState<CalendarEvent[]>([]);
  const [taskRows,setTaskRows]=useState<TaskRow[]>([]);
  const [taskDetails,setTaskDetails]=useState<TaskDetail[]>([]);
  const [selectedDate,setSelectedDate]=useState<string|null>(null);
  const [newTitle, setNewTitle] = useState('');
  const [adding, setAdding] = useState(false);
  // add-task form
  const [newTaskTitle,setNewTaskTitle]=useState('');
  const [newTaskTime,setNewTaskTime]=useState('09:00');
  const [newTaskAction,setNewTaskAction]=useState('notify');
  const [newTaskRule,setNewTaskRule]=useState('none');
  // edit state: taskId -> {title,time,action,rule,payload}
  const [editing,setEditing]=useState<Record<string,Record<string,string>>>({});

  const fetchMonth = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('calendar.list', { year: viewDate.getFullYear(), month: viewDate.getMonth() + 1 });
      if (r?.events) setEvents(r.events.map((e: Record<string, unknown>) => ({
        id: e.id as string, title: e.title as string, start: e.start as string,
        end: (e.end as string) || '', source: (e.source as string) || 'local',
      })));
      const t = await send('tasks.month', { year: viewDate.getFullYear(), month: viewDate.getMonth() + 1 });
      if (t?.rows) setTaskRows(t.rows as TaskRow[]);
      const d = await send('tasks.list', {});
      if (d?.tasks) setTaskDetails(d.tasks.map((x: Record<string, unknown>) => ({
        id: x.id as string, title: x.title as string, action: x.action as string,
        time: x.time as string, date: (x.date as string) || '',
        enabled: x.enabled as boolean, rule: x.rule as string,
        payload: (x.payload as string) || '', recurrence: x.recurrence as {type:string;weekdays:number[]},
      })));
    } catch {}
  }, [wsState, send, viewDate]);

  useEffect(() => { fetchMonth(); }, [fetchMonth]);

  const handleAddEvent = async () => {
    if (!newTitle.trim() || !selectedDate || wsState !== 'connected') return;
    setAdding(true);
    try {
      const r = await send('calendar.add', { title: newTitle.trim(), start: selectedDate });
      if (r?.event) setEvents(prev => [...prev, {
        id: r.event.id, title: r.event.title, start: r.event.start,
        end: r.event.end || '', source: 'local',
      }]);
      setNewTitle('');
    } catch {}
    setAdding(false);
  };

  const handleDeleteEvent = async (eventId: string) => {
    try { await send('calendar.delete', { eventId }); setEvents(p => p.filter(e => e.id !== eventId)); } catch {}
  };

  const handleAddTask = async () => {
    if (!newTaskTitle.trim() || !selectedDate || wsState !== 'connected') return;
    try {
      await send('tasks.schedule', {
        title: newTaskTitle.trim(), time: newTaskTime, action: newTaskAction,
        date: selectedDate, recurrence: { type: newTaskRule, weekdays: newTaskRule==='weekly'?[4]:[] },
      });
      setNewTaskTitle('');
      fetchMonth();
    } catch {}
  };

  const handleToggleTask = async (t: TaskDetail) => {
    try {
      await send(t.enabled ? 'tasks.pause' : 'tasks.resume', { taskId: t.id });
      fetchMonth();
    } catch {}
  };

  const handleDeleteTask = async (taskId: string) => {
    try { await send('tasks.cancel', { taskId }); fetchMonth(); } catch {}
  };

  const startEdit = (t: TaskDetail) => setEditing(prev => ({...prev, [t.id]: {
    title: t.title, time: t.time, action: t.action,
    rule: t.recurrence?.type || 'none', payload: t.payload || '',
  }}));

  const saveEdit = async (t: TaskDetail) => {
    const f = editing[t.id]; if (!f) return;
    try {
      await send('tasks.update', { taskId: t.id, fields: {
        title: f.title, time: f.time, action: f.action, payload: f.payload,
        recurrence: { type: f.rule, weekdays: f.rule==='weekly'?[4]:[] },
      }});
      setEditing(prev => { const n={...prev}; delete n[t.id]; return n; });
      fetchMonth();
    } catch {}
  };

  const year=viewDate.getFullYear(),month=viewDate.getMonth();
  const firstDay=new Date(year,month,1).getDay();
  const daysInMonth=new Date(year,month+1,0).getDate();
  const todayStr=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;

  const prevMonth=()=>setViewDate(new Date(year,month-1,1));
  const nextMonth=()=>setViewDate(new Date(year,month+1,1));
  const goToday=()=>setViewDate(new Date(today.getFullYear(),today.getMonth(),1));

  const cells:({day:number;dateStr:string}|null)[]=[];
  for(let i=0;i<firstDay;i++)cells.push(null);
  for(let d=1;d<=daysInMonth;d++){const ds=`${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;cells.push({day:d,dateStr:ds});}
  while(cells.length%7!==0)cells.push(null);

  const selectedEvents=selectedDate?events.filter(e=>(e.start||'').startsWith(selectedDate)):[];
  const selectedTasks=selectedDate?taskRows.filter(r=>r.date===selectedDate):[];
  const taskById=(id:string)=>taskDetails.find(t=>t.id===id);

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Calendar & Tasks</h1>
        <div className="flex items-center gap-2">
          <button onClick={prevMonth} className="text-sm px-2 py-1 text-[#8b949e] hover:text-[#e8eaed]">◀</button>
          <span className="text-sm font-medium text-[#e8eaed]">{MONTHS[month]} {year}</span>
          <button onClick={nextMonth} className="text-sm px-2 py-1 text-[#8b949e] hover:text-[#e8eaed]">▶</button>
          <button onClick={goToday} className="text-xs px-2 py-1 bg-[#21262d] text-[#e8eaed] rounded hover:bg-[#30363d]">Today</button>
        </div>
      </header>
      <div className="flex-1 flex">
        <div className="flex-1 p-4">
          <div className="grid grid-cols-7 gap-px bg-[#30363d] rounded-lg overflow-hidden">
            {DAYS.map(d=><div key={d} className="bg-[#161b22] text-center py-2 text-xs font-medium text-[#8b949e]">{d}</div>)}
            {cells.map((cell,i)=>(<div key={i} onClick={()=>cell&&setSelectedDate(cell.dateStr)} className={`bg-[#161b22] min-h-[80px] p-1.5 ${cell?'cursor-pointer hover:bg-[#21262d]':''} transition-colors`}>
              {cell&&<>
                <span className={`text-xs ${cell.dateStr===todayStr?'bg-[#3380FF] text-white rounded-full w-5 h-5 inline-flex items-center justify-center':'text-[#e8eaed]'}`}>{cell.day}</span>
                {events.filter(e=>(e.start||'').startsWith(cell.dateStr)).map(e=><div key={e.id} className="text-[10px] mt-0.5 px-1 py-0.5 bg-[#1f6feb33] text-[#58a6ff] rounded truncate">{e.title}</div>)}
                {taskRows.filter(r=>r.date===cell.dateStr).map(r=><div key={r.task_id} className={`text-[10px] mt-0.5 px-1 py-0.5 rounded truncate ${r.action==='chat'?'bg-[#2ea04322] text-[#3fb950]':'bg-[#9e6a0322] text-[#d29922]'}`}>⏰ {r.time} {r.title}</div>)}
              </>}
            </div>))}
          </div>
        </div>
        <div className="w-72 border-l border-[#30363d] p-4 flex flex-col overflow-y-auto">
          <h3 className="text-xs font-medium text-[#8b949e] mb-3">{selectedDate||'Select a date'}</h3>

          {selectedDate && (<>
            <p className="text-[10px] uppercase tracking-wide text-[#484f58] mb-1">Events</p>
            <div className="mb-3 flex gap-1">
              <input value={newTitle} onChange={e=>setNewTitle(e.target.value)} placeholder="New event..."
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"/>
              <button onClick={handleAddEvent} disabled={!newTitle.trim()||adding||wsState!=='connected'}
                className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-2 py-1 text-xs">+</button>
            </div>
            {selectedEvents.length===0?<p className="text-xs text-[#484f58] mb-3">No events</p>:selectedEvents.map(e=><div key={e.id} className="mb-2 p-2 bg-[#161b22] rounded border border-[#21262d] group">
              <div className="flex items-start justify-between">
                <p className="text-xs text-[#e8eaed] flex-1">{e.title}</p>
                <button onClick={()=>handleDeleteEvent(e.id)} className="text-[10px] text-[#484f58] hover:text-[#f85149] opacity-0 group-hover:opacity-100 transition-opacity ml-1">×</button>
              </div>
              <p className="text-[10px] text-[#8b949e]">{((e.start||'').slice(11))||'all day'}{e.end ? ` – ${(e.end||'').slice(11)}` : ''}</p>
            </div>)}

            <p className="text-[10px] uppercase tracking-wide text-[#484f58] mb-1">Scheduled tasks</p>
            <div className="mb-3 flex flex-col gap-1">
              <div className="flex gap-1">
                <input value={newTaskTitle} onChange={e=>setNewTaskTitle(e.target.value)} placeholder="Task title..."
                  className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"/>
              </div>
              <div className="flex gap-1">
                <input type="time" value={newTaskTime} onChange={e=>setNewTaskTime(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]"/>
                <select value={newTaskAction} onChange={e=>setNewTaskAction(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]">
                  <option value="notify">🔔 notify</option><option value="chat">💬 chat</option>
                </select>
                <select value={newTaskRule} onChange={e=>setNewTaskRule(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]">
                  <option value="none">once</option><option value="daily">daily</option>
                  <option value="weekly">weekly</option><option value="monthly">monthly</option>
                </select>
                <button onClick={handleAddTask} disabled={!newTaskTitle.trim()||wsState!=='connected'}
                  className="bg-[#238636] hover:bg-[#2ea043] disabled:opacity-50 text-white rounded px-2 py-1 text-xs">+</button>
              </div>
            </div>
            {selectedTasks.length===0?<p className="text-xs text-[#484f58] flex-1">No tasks on this date</p>:selectedTasks.map(r=>{
              const t=taskById(r.task_id);
              const ed=editing[r.task_id];
              return <div key={r.task_id} className="mb-2 p-2 bg-[#161b22] rounded border border-[#21262d] group">
                {ed ? (
                  <div className="flex flex-col gap-1">
                    <input value={ed.title} onChange={e=>setEditing(p=>({...p,[r.task_id]:{...ed,title:e.target.value}}))} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]"/>
                    <div className="flex gap-1">
                      <input type="time" value={ed.time} onChange={e=>setEditing(p=>({...p,[r.task_id]:{...ed,time:e.target.value}}))} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]"/>
                      <select value={ed.action} onChange={e=>setEditing(p=>({...p,[r.task_id]:{...ed,action:e.target.value}}))} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]"><option value="notify">🔔</option><option value="chat">💬</option></select>
                      <select value={ed.rule} onChange={e=>setEditing(p=>({...p,[r.task_id]:{...ed,rule:e.target.value}}))} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]"><option value="none">once</option><option value="daily">daily</option><option value="weekly">weekly</option><option value="monthly">monthly</option></select>
                    </div>
                    <input value={ed.payload} onChange={e=>setEditing(p=>({...p,[r.task_id]:{...ed,payload:e.target.value}}))} placeholder="payload (reminder text / prompt)" className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
                    <div className="flex gap-1">
                      <button onClick={()=>t&&saveEdit(t)} className="flex-1 bg-[#238636] hover:bg-[#2ea043] text-white rounded px-2 py-1 text-[10px]">Save</button>
                      <button onClick={()=>setEditing(p=>{const n={...p};delete n[r.task_id];return n;})} className="flex-1 bg-[#30363d] hover:bg-[#484f58] text-[#e8eaed] rounded px-2 py-1 text-[10px]">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div>
                    <div className="flex items-start justify-between">
                      <p className="text-xs text-[#e8eaed] flex-1">{r.action==='chat'?'💬':'🔔'} {r.title}</p>
                      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button onClick={()=>t&&startEdit(t)} className="text-[10px] text-[#8b949e] hover:text-[#3380FF]">✎</button>
                        <button onClick={()=>t&&handleToggleTask(t)} className={`text-[10px] ${t?.enabled?'text-[#3fb950]':'text-[#484f58]'}`}>{t?.enabled?'⏸':'▶'}</button>
                        <button onClick={()=>handleDeleteTask(r.task_id)} className="text-[10px] text-[#484f58] hover:text-[#f85149]">×</button>
                      </div>
                    </div>
                    <p className="text-[10px] text-[#8b949e]">⏰ {r.time} · {r.rule}{(t&&!t.enabled)?' · paused':''}</p>
                    {t?.payload && <p className="text-[10px] text-[#484f58] truncate">{t.payload}</p>}
                  </div>
                )}
              </div>;
            })}
          </>)}
        </div>
      </div>
    </div>
  );
}
