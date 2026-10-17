'use client';

import { useEffect, useRef, useState } from 'react';
import { useWS } from '@/lib/useWS';

interface Segment {
  start: number;
  end: number;
  text: string;
  at: string;
}

interface Action {
  what: string;
  who?: string;
}

interface Meeting {
  id: string;
  title: string;
  source?: string;
  started_at?: number;
  ended_at?: number | null;
  summary?: string;
  decisions?: string[];
  actions?: Action[];
  open_questions?: string[];
  transcript?: string;
  segments?: Segment[];
  segment_count?: number;
  summarised?: boolean;
  chars?: number;
}

export default function MeetingsPage() {
  const { state: wsState, send, onNotification } = useWS();
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [stats, setStats] = useState<any>({});
  const [selected, setSelected] = useState<Meeting | null>(null);
  const [path, setPath] = useState('');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [showTranscript, setShowTranscript] = useState(false);
  const selectedRef = useRef<string | null>(null);

  selectedRef.current = selected?.id ?? null;

  const load = async () => {
    if (wsState !== 'connected') return;
    setLoading(true);
    try {
      const r = await send('meetings.list', { limit: 100 });
      setMeetings(r?.meetings || []);
      setStats(r?.stats || {});
      setError('');
    } catch (e: any) {
      setError(e.message || 'could not list meetings');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [wsState]); // eslint-disable-line react-hooks/exhaustive-deps

  // The backend broadcasts while a long summarise runs, so the button can say
  // what it is doing. Without this a slow summarise is indistinguishable from
  // a dead one, which is the same confusion that cost measurement rounds.
  useEffect(() => {
    if (wsState !== 'connected') return;
    // `onNotification` is how every pushed method arrives — a DOM event would
    // never fire, because `useWS` routes notifications through its own map.
    return onNotification('meetings.progress', (params: any) => {
      if (!params || params.id !== selectedRef.current) return;
      setBusy(params.what || 'Working…');
    });
  }, [wsState, onNotification]);

  const open = async (id: string) => {
    try {
      const r = await send('meetings.get', { id });
      if (r?.success) {
        setSelected(r.meeting);
        setShowTranscript(false);
        setNotice('');
        setError('');
      } else {
        setError(r?.error || 'could not open that meeting');
      }
    } catch (e: any) {
      setError(e.message);
    }
  };

  const transcribe = async () => {
    const p = path.trim();
    if (!p) return;
    setBusy('Transcribing…');
    setNotice('');
    setError('');
    try {
      const r = await send('meetings.transcribe', { path: p, title: title.trim() });
      if (!r?.success) {
        setError(r?.error || 'could not transcribe that file');
      } else {
        setPath('');
        setTitle('');
        setNotice(`Saved "${r.meeting.title}" with ${
          (r.meeting.segments || []).length} timestamped segments.`);
        await load();
        open(r.meeting.id);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  const summarise = async () => {
    if (!selected) return;
    setBusy('Summarising…');
    setNotice('');
    setError('');
    try {
      const r = await send('meetings.summarise', { id: selected.id });
      if (!r?.success) {
        setError(r?.error || 'the summariser did not answer');
      } else {
        // A partial summary must be labelled as partial. Presenting it as the
        // whole meeting is the failure this flag exists to prevent.
        setNotice(r.partial
          ? `Summarised ${r.parts_ok} of ${r.blocks} parts — ` +
            'the rest could not be read, so this summary is incomplete.'
          : '');
        setSelected(r.meeting);
        await load();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  const remove = async (id: string) => {
    if (!confirm('Delete this meeting and its transcript? This cannot be undone.')) return;
    try {
      const r = await send('meetings.delete', { id });
      if (r?.success) {
        if (selected?.id === id) setSelected(null);
        await load();
      }
    } catch (e: any) {
      setError(e.message);
    }
  };

  const rename = async (id: string, newTitle: string) => {
    try {
      await send('meetings.rename', { id, title: newTitle });
      await load();
      if (selected?.id === id) setSelected({ ...selected, title: newTitle });
    } catch { /* a rename is not worth an error banner */ }
  };

  const fmtDate = (ts?: number) =>
    ts ? new Date(ts * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }) : '';

  const fmtLength = (m: Meeting) => {
    if (!m.started_at || !m.ended_at) return null;
    const secs = Math.max(0, Math.round(m.ended_at - m.started_at));
    const mins = Math.floor(secs / 60);
    return mins < 1 ? `${secs}s` : `${mins} min`;
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <div className="flex items-baseline gap-2 text-xs">
          <h1 className="text-sm font-semibold text-[var(--text-primary)]">Meetings</h1>
          <span className="text-[var(--text-secondary)]">
            {stats.count || 0} saved · {stats.summarised || 0} summarised
          </span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {/* Creating one from a recording is the primary action, so it is first. */}
        <section className="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg p-3">
          <div className="text-xs text-[var(--text-secondary)] mb-2">
            Transcribe a recording into a meeting. Nothing is uploaded — the
            audio is transcribed on this machine.
          </div>
          <div className="flex gap-2 mb-2">
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="Path to a recording (.wav, .mp3, .m4a, .mp4…)"
              className="flex-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded px-2 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]"
              onKeyDown={(e) => { if (e.key === 'Enter') transcribe(); }}
            />
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Title (optional)"
              className="w-48 bg-[var(--bg-primary)] border border-[var(--border)] rounded px-2 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]"
            />
            <button
              onClick={transcribe}
              disabled={!path.trim() || !!busy}
              className="bg-[var(--accent)] hover:bg-[var(--accent-hover)] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium"
            >
              {busy === 'Transcribing…' ? 'Transcribing…' : 'Transcribe'}
            </button>
          </div>
          {busy && busy !== 'Transcribing…' && (
            <div className="text-xs text-[var(--text-secondary)]">{busy}</div>
          )}
        </section>

        {notice && (
          <div className="text-xs text-[var(--success)] bg-[#0d2818] border border-[#238636] rounded px-3 py-2">
            {notice}
          </div>
        )}
        {error && (
          <div className="text-xs text-[var(--danger)] bg-[#2d1214] border border-[#8b2c31] rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="grid grid-cols-[280px_1fr] gap-4">
          <section className="space-y-1">
            {loading && <div className="text-xs text-[var(--text-secondary)]">Loading…</div>}
            {!loading && meetings.length === 0 && (
              <div className="text-xs text-[var(--text-secondary)]">
                No meetings yet. Point at a recording above.
              </div>
            )}
            {meetings.map((m) => (
              <button
                key={m.id}
                onClick={() => open(m.id)}
                className={`w-full text-left px-3 py-2 rounded border ${
                  selected?.id === m.id
                    ? 'bg-[var(--bg-secondary)] border-[var(--accent)]'
                    : 'border-[var(--border)] hover:border-[var(--text-secondary)]'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="flex-1 text-sm text-[var(--text-primary)] truncate">
                    {m.title}
                  </span>
                  {m.summarised && <span className="text-[10px] text-[var(--success)]">✓</span>}
                </div>
                <div className="text-[11px] text-[var(--text-secondary)]">
                  {fmtDate(m.started_at)}
                  {fmtLength(m) ? ` · ${fmtLength(m)}` : ''}
                  {m.segment_count ? ` · ${m.segment_count} lines` : ''}
                  {(m.actions?.length ?? 0) > 0 ? ` · ${m.actions!.length} actions` : ''}
                </div>
              </button>
            ))}
          </section>

          <section>
            {!selected && (
              <div className="text-xs text-[var(--text-secondary)]">
                Pick a meeting to read it.
              </div>
            )}
            {selected && (
              <div className="space-y-4">
                <div>
                  <input
                    value={selected.title}
                    onChange={(e) => setSelected({ ...selected, title: e.target.value })}
                    onBlur={(e) => {
                      if (e.target.value.trim() && e.target.value !== selected.title) {
                        rename(selected.id, e.target.value.trim());
                      }
                    }}
                    className="w-full bg-transparent text-base font-semibold text-[var(--text-primary)] border-b border-transparent hover:border-[var(--border)] focus:border-[var(--accent)] focus:outline-none"
                  />
                  <div className="flex items-center gap-3 text-[11px] text-[var(--text-secondary)] mt-1">
                    <span>{fmtDate(selected.started_at)}</span>
                    {fmtLength(selected) && <span>{fmtLength(selected)}</span>}
                    {selected.source && <span>{selected.source}</span>}
                    <button
                      onClick={() => remove(selected.id)}
                      className="text-[var(--danger)] hover:underline ml-auto"
                    >
                      Delete
                    </button>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={summarise}
                    disabled={!!busy}
                    className="bg-[var(--accent)] hover:bg-[var(--accent-hover)] disabled:opacity-50 text-white rounded px-3 py-1 text-xs font-medium"
                  >
                    {busy === 'Summarising…' ? 'Summarising…'
                      : selected.summarised ? 'Re-summarise' : 'Summarise'}
                  </button>
                  <button
                    onClick={() => setShowTranscript((v) => !v)}
                    className="border border-[var(--border)] hover:border-[var(--text-secondary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] rounded px-3 py-1 text-xs"
                  >
                    {showTranscript ? 'Hide transcript' : 'Show transcript'}
                  </button>
                </div>

                {selected.summarised ? (
                  <>
                    <section>
                      <h2 className="text-[11px] text-[var(--text-secondary)] mb-1">Summary</h2>
                      <p className="text-sm text-[var(--text-primary)] whitespace-pre-wrap break-words">
                        {selected.summary}
                      </p>
                    </section>

                    {(selected.decisions?.length ?? 0) > 0 && (
                      <section>
                        <h2 className="text-[11px] text-[var(--text-secondary)] mb-1">Decisions</h2>
                        <ul className="space-y-0.5">
                          {selected.decisions!.map((d, i) => (
                            <li key={i} className="text-sm text-[var(--text-primary)]">• {d}</li>
                          ))}
                        </ul>
                      </section>
                    )}

                    {(selected.actions?.length ?? 0) > 0 && (
                      <section>
                        <h2 className="text-[11px] text-[var(--text-secondary)] mb-1">
                          Action items
                        </h2>
                        <ul className="space-y-0.5">
                          {selected.actions!.map((a, i) => (
                            <li key={i} className="text-sm text-[var(--text-primary)]">
                              • {a.what}
                              {a.who
                                ? <span className="text-[var(--text-secondary)]"> — {a.who}</span>
                                : <span className="text-[var(--text-secondary)]"> — owner not stated</span>}
                            </li>
                          ))}
                        </ul>
                      </section>
                    )}

                    {(selected.open_questions?.length ?? 0) > 0 && (
                      <section>
                        <h2 className="text-[11px] text-[var(--text-secondary)] mb-1">
                          Left open
                        </h2>
                        <ul className="space-y-0.5">
                          {selected.open_questions!.map((q, i) => (
                            <li key={i} className="text-sm text-[var(--text-primary)]">• {q}</li>
                          ))}
                        </ul>
                      </section>
                    )}
                  </>
                ) : (
                  <div className="text-xs text-[var(--text-secondary)]">
                    Not summarised yet.
                  </div>
                )}

                {showTranscript && (
                  <section>
                    <h2 className="text-[11px] text-[var(--text-secondary)] mb-1">
                      Transcript {(selected.segments?.length ?? 0) > 0
                        && `· ${selected.segments!.length} segments`}
                    </h2>
                    {(selected.segments?.length ?? 0) > 0 ? (
                      <div className="space-y-1">
                        {selected.segments!.map((s, i) => (
                          <div key={i} className="flex gap-2 text-sm">
                            <span className="text-[var(--accent)] font-mono text-xs pt-0.5 w-12 shrink-0">
                              {s.at}
                            </span>
                            <span className="text-[var(--text-primary)] break-words">{s.text}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-[var(--text-primary)] whitespace-pre-wrap break-words">
                        {selected.transcript}
                      </p>
                    )}
                  </section>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
