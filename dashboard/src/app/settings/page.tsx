'use client';

import { useState, useEffect } from 'react';
import { useWS } from '@/lib/useWS';

type SettingsData = Record<string, any>;

const SECTION_ICONS: Record<string, string> = {
  providers: '🔌', character: '🎭', voice: '🎤', safety: '🛡️',
  notifications: '🔔', memory: '🧠', integrations: '🔗', appearance: '🎨', about: 'ℹ️',
};

export default function SettingsPage() {
  const { state: wsState, send } = useWS();
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [activeSection, setActiveSection] = useState('providers');
  const [saving, setSaving] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<{ key: string; ok: boolean } | null>(null);

  useEffect(() => {
    if (wsState === 'connected') {
      send('settings.get', {}).then(r => setSettings(r?.settings || {})).catch(() => {});
    }
  }, [wsState, send]);

  const updateSetting = async (section: string, key: string, value: any) => {
    if (wsState !== 'connected') return;
    setSaving(`${section}.${key}`);
    try {
      await send('settings.set', { section, key, value });
      setSettings((prev: any) => {
        if (!key) return { ...prev, [section]: value };  // top-level setting
        return { ...prev, [section]: { ...prev?.[section], [key]: value } };
      });
      setSaveStatus({ key: `${section}.${key}`, ok: true });
    } catch { setSaveStatus({ key: `${section}.${key}`, ok: false }); }
    setSaving(null);
    setTimeout(() => setSaveStatus(null), 2000);
  };

  const sections = ['providers','character','voice','safety','notifications','memory','integrations','appearance','about'];

  if (!settings) return (
    <div className="flex items-center justify-center h-full text-[#8b949e]">
      <div className="text-center"><div className="animate-spin text-2xl mb-3">⟳</div><p>{wsState === 'connected' ? 'Loading...' : 'Connecting...'}</p></div>
    </div>
  );

  return (
    <div className="flex h-full">
      <div className="w-48 border-r border-[#30363d] p-2 space-y-0.5 overflow-y-auto">
        {sections.map(s => (
          <button key={s} onClick={() => setActiveSection(s)}
            className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${activeSection === s ? 'bg-[#1f6feb] text-white' : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]'}`}>
            <span className="mr-2">{SECTION_ICONS[s]}</span>{s.charAt(0).toUpperCase()+s.slice(1)}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-6">
        <h2 className="text-lg font-semibold mb-6">{SECTION_ICONS[activeSection]} {activeSection.charAt(0).toUpperCase()+activeSection.slice(1)}</h2>
        {activeSection==='providers'&&<ProvidersSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='character'&&<CharacterSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='voice'&&<VoiceSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='safety'&&<SafetySection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='notifications'&&<NotificationsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='memory'&&<MemorySection/>}
        {activeSection==='integrations'&&<IntegrationsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='appearance'&&<AppearanceSection/>}
        {activeSection==='about'&&<AboutSection/>}
      </div>
    </div>
  );
}

function SettingRow({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="py-3 border-b border-[#21262d]">
      <div className="flex items-center justify-between">
        <div className="flex-1"><label className="text-sm font-medium text-[#e8eaed]">{label}</label>
          {description && <p className="text-xs text-[#8b949e] mt-0.5">{description}</p>}</div>
        <div className="ml-4">{children}</div>
      </div>
    </div>
  );
}

function SaveIndicator({ settingKey, saving, status }: { settingKey: string; saving: string|null; status: {key:string;ok:boolean}|null }) {
  if (saving===settingKey) return <span className="text-xs text-[#d29922]">Saving...</span>;
  if (status?.key===settingKey) return <span className={`text-xs ${status.ok?'text-[#3fb950]':'text-[#f85149]'}`}>{status.ok?'✓ Saved':'✗ Failed'}</span>;
  return null;
}

function ProvidersSection({ settings, update, saving, status }: any) {
  const p=settings?.providers||{}, builtin=p.builtin||{}, active=p.active||'deepseek';
  return <div className="space-y-1">
    <SettingRow label="Active Provider">
      <select value={active} onChange={e=>update('providers','active',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]">
        {Object.keys(builtin).map(id=><option key={id} value={id}>{builtin[id]?.name||id}</option>)}
      </select>
    </SettingRow>
    {Object.entries(builtin).map(([id,cfg]:[string,any])=>
      <SettingRow key={id} label={`${cfg.name} API Key`}>
        <div className="flex items-center gap-2">
          <input type="password" value={cfg.api_key||''} onChange={e=>{const u={...builtin,[id]:{...cfg,api_key:e.target.value}};update('providers','builtin',u)}}
            placeholder="sk-..." className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-56 font-mono"/>
          {cfg.api_key&&<span className="text-xs text-[#3fb950]">✓ Set</span>}
        </div>
      </SettingRow>)}
    <SettingRow label="Default Model"><input type="text" value={builtin[active]?.default_model||''} readOnly className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#8b949e] w-48"/></SettingRow>
  </div>;
}

function CharacterSection({ settings, update, saving, status }: any) {
  const c=settings?.character||{}, shapes=['triangle','circle','diamond','hexagon','star','square'];
  return <div className="space-y-1">
    <SettingRow label="Agent Name"><input type="text" value={settings?.agent_name||'Addled'} onChange={e=>update('agent_name','',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-40"/></SettingRow>
    <SettingRow label="Shape"><div className="flex gap-1.5">{shapes.map(s=><button key={s} onClick={()=>update('character','shape',s)} className={`px-2.5 py-1 rounded text-xs border transition-colors ${c.shape===s?'border-[#3380FF] bg-[#1f6feb22] text-[#3380FF]':'border-[#30363d] text-[#8b949e] hover:border-[#484f58]'}`}>{s}</button>)}</div></SettingRow>
    <SettingRow label="Color"><div className="flex items-center gap-2"><input type="color" value={c.color||'#3380FF'} onChange={e=>update('character','color',e.target.value)} className="w-8 h-8 rounded cursor-pointer border-0"/><input type="text" value={c.color||'#3380FF'} onChange={e=>update('character','color',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed] w-24 font-mono"/></div></SettingRow>
    <SettingRow label="Size" description={`${c.size||64}px`}><input type="range" min={32} max={128} value={c.size||64} onChange={e=>update('character','size',parseInt(e.target.value))} className="w-32"/></SettingRow>
    <SettingRow label="Glow" description={`${Math.round((c.glow_intensity||0.6)*100)}%`}><input type="range" min={0} max={100} value={Math.round((c.glow_intensity||0.6)*100)} onChange={e=>update('character','glow_intensity',parseInt(e.target.value)/100)} className="w-32"/></SettingRow>
    <SettingRow label="Eyes"><button onClick={()=>update('character','eyes',c.eyes===false)} className={`w-10 h-5 rounded-full transition-colors ${c.eyes!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${c.eyes!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
    <SettingRow label="Speed"><select value={c.movement_speed||'medium'} onChange={e=>update('character','movement_speed',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="slow">Slow</option><option value="medium">Medium</option><option value="fast">Fast</option></select></SettingRow>
  </div>;
}

function VoiceSection({ settings, update, saving, status }: any) {
  const v=settings?.voice||{};
  return <div className="space-y-1">
    <SettingRow label="TTS Engine"><select value={v.tts_engine||'edge'} onChange={e=>update('voice','tts_engine',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="edge">Edge TTS (Free)</option><option value="elevenlabs">ElevenLabs</option><option value="openai">OpenAI TTS</option><option value="cosyvoice">CosyVoice (Local)</option></select></SettingRow>
    <SettingRow label="Wake Word"><input type="text" value={v.wake_word||'hey addled'} onChange={e=>update('voice','wake_word',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-40"/></SettingRow>
    <SettingRow label="Auto TTS"><button onClick={()=>update('voice','auto_tts',v.auto_tts===false)} className={`w-10 h-5 rounded-full transition-colors ${v.auto_tts!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${v.auto_tts!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
    <SettingRow label="Language"><select value={v.language||'en'} onChange={e=>update('voice','language',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="en">English</option><option value="zh">Chinese</option><option value="ja">Japanese</option><option value="ko">Korean</option><option value="auto">Auto</option></select></SettingRow>
  </div>;
}

function SafetySection({ settings, update, saving, status }: any) {
  const s=settings?.safety||{};
  const Toggle=({label,desc,key}:{label:string;desc?:string;key:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('safety',key,s[key]===false)} className={`w-10 h-5 rounded-full transition-colors ${s[key]!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${s[key]!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <SettingRow label="File Access"><select value={s.file_access_mode||'workspace_only'} onChange={e=>update('safety','file_access_mode',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="workspace_only">Workspace Only</option><option value="custom">Custom</option><option value="unrestricted">Full</option></select></SettingRow>
    <SettingRow label="Kill Switch"><input type="text" value={s.kill_switch_hotkey||'ctrl+shift+alt+k'} onChange={e=>update('safety','kill_switch_hotkey',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-44 font-mono"/></SettingRow>
    <Toggle label="Clipboard Filter" desc="Redact secrets from clipboard" key="clipboard_filter"/>
    <Toggle label="Prompt Guard" desc="Block injection attempts" key="prompt_guard"/>
    <SettingRow label="Quiet Hours"><div className="flex items-center gap-2 text-sm text-[#e8eaed]"><input type="time" value={s.quiet_hours_start||'22:00'} onChange={e=>update('safety','quiet_hours_start',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed]"/><span className="text-[#8b949e]">to</span><input type="time" value={s.quiet_hours_end||'07:00'} onChange={e=>update('safety','quiet_hours_end',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed]"/></div></SettingRow>
    <Toggle label="Meeting Auto-Sleep" key="meeting_auto_sleep"/>
    <Toggle label="Gaming Auto-Sleep" key="gaming_auto_sleep"/>
  </div>;
}

function NotificationsSection({ settings, update, saving, status }: any) {
  const n=settings?.notifications||{};
  return <div className="space-y-1">
    <SettingRow label="Bubble Duration" description={`${n.bubble_duration_s||8}s`}><input type="range" min={3} max={30} value={n.bubble_duration_s||8} onChange={e=>update('notifications','bubble_duration_s',parseInt(e.target.value))} className="w-32"/></SettingRow>
    <SettingRow label="Max Bubbles"><input type="number" min={1} max={10} value={n.max_bubbles||3} onChange={e=>update('notifications','max_bubbles',parseInt(e.target.value))} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-20"/></SettingRow>
    <SettingRow label="Show State"><button onClick={()=>update('notifications','show_character_state',n.show_character_state===false)} className={`w-10 h-5 rounded-full transition-colors ${n.show_character_state!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${n.show_character_state!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
  </div>;
}

function MemorySection() {
  const [msg,setMsg]=useState('');
  return <div className="space-y-1">
    <SettingRow label="Chat History"><button onClick={()=>setMsg('History would be cleared (Phase 5)')} className="px-3 py-1.5 text-sm bg-[#21262d] hover:bg-[#30363d] text-[#e8eaed] rounded">Clear History</button></SettingRow>
    <SettingRow label="Vector Store"><span className="text-xs text-[#8b949e]">SQLite + numpy (384-dim)</span></SettingRow>
    <SettingRow label="Export Data"><button onClick={()=>setMsg('Export coming in Phase 5')} className="px-3 py-1.5 text-sm bg-[#21262d] hover:bg-[#30363d] text-[#e8eaed] rounded">Export All</button></SettingRow>
    {msg&&<p className="text-xs text-[#d29922] mt-2">{msg}</p>}
  </div>;
}

function IntegrationsSection({ settings, update, saving, status }: any) {
  const i=settings?.integrations||{};
  return <div className="space-y-1">
    <SettingRow label="Calendar" description={i.calendar_provider?'Connected':'Not connected'}><button className={`px-3 py-1.5 text-sm rounded ${i.calendar_provider?'bg-[#21262d] hover:bg-[#30363d] text-[#e8eaed]':'bg-[#3380FF] hover:bg-[#4d94ff] text-white'}`}>{i.calendar_provider?'Disconnect':'Connect'}</button></SettingRow>
    <SettingRow label="IMAP"><input type="text" value={i.email_imap||''} placeholder="imap.gmail.com" onChange={e=>update('integrations','email_imap',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-48"/></SettingRow>
    <SettingRow label="SMTP"><input type="text" value={i.email_smtp||''} placeholder="smtp.gmail.com" onChange={e=>update('integrations','email_smtp',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-48"/></SettingRow>
  </div>;
}

function AppearanceSection() {
  return <div className="space-y-1">
    <SettingRow label="Theme"><select className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option>Dark</option><option>Light</option><option>System</option></select></SettingRow>
    <SettingRow label="Accent"><input type="color" defaultValue="#3380FF" className="w-8 h-8 rounded cursor-pointer border-0"/></SettingRow>
  </div>;
}

function AboutSection() {
  return <div className="space-y-4">
    <div className="text-center py-8"><div className="text-4xl mb-3">△</div><h3 className="text-lg font-semibold">Addled</h3><p className="text-sm text-[#8b949e] mt-1">AI Desktop Companion</p><p className="text-xs text-[#484f58] mt-1">Version 1.0.0</p></div>
    <div className="space-y-1">
      <SettingRow label="Repository"><a href="https://github.com/8kunoir8/Addled" target="_blank" rel="noopener noreferrer" className="text-sm text-[#3380FF] hover:underline">github.com/8kunoir8/Addled</a></SettingRow>
      <SettingRow label="License"><span className="text-sm text-[#8b949e]">MIT</span></SettingRow>
      <SettingRow label="Stack"><span className="text-sm text-[#8b949e]">Python + PyQt6 + Electron + Next.js</span></SettingRow>
      <SettingRow label="Author"><span className="text-sm text-[#8b949e]">Kunoir</span></SettingRow>
    </div>
  </div>;
}
