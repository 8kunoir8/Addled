'use client';

import { useState } from 'react';

const DAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];

interface CalendarEvent { id:string; title:string; start:string; end:string; source:string; }

export default function CalendarPage() {
  const today=new Date();
  const [viewDate,setViewDate]=useState(new Date(today.getFullYear(),today.getMonth(),1));
  const [events,setEvents]=useState<CalendarEvent[]>([]);
  const [selectedDate,setSelectedDate]=useState<string|null>(null);

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

  const selectedEvents=selectedDate?events.filter(e=>e.start.startsWith(selectedDate)):[];

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Calendar</h1>
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
              {cell&&<><span className={`text-xs ${cell.dateStr===todayStr?'bg-[#3380FF] text-white rounded-full w-5 h-5 inline-flex items-center justify-center':'text-[#e8eaed]'}`}>{cell.day}</span>
                {events.filter(e=>e.start.startsWith(cell.dateStr)).map(e=><div key={e.id} className="text-[10px] mt-0.5 px-1 py-0.5 bg-[#1f6feb33] text-[#58a6ff] rounded truncate">{e.title}</div>)}</>}
            </div>))}
          </div>
        </div>
        <div className="w-64 border-l border-[#30363d] p-4">
          <h3 className="text-xs font-medium text-[#8b949e] mb-3">{selectedDate||'Select a date'}</h3>
          {selectedEvents.length===0?<p className="text-xs text-[#484f58]">No events</p>:selectedEvents.map(e=><div key={e.id} className="mb-2 p-2 bg-[#161b22] rounded border border-[#21262d]"><p className="text-xs text-[#e8eaed]">{e.title}</p><p className="text-[10px] text-[#8b949e]">{e.start} – {e.end}</p></div>)}
        </div>
      </div>
    </div>
  );
}
