'use client';

import { useState, useEffect, useCallback } from 'react';

type WSMessage = {
  jsonrpc: string;
  id?: string;
  method?: string;
  result?: any;
  error?: { code: number; message: string };
  params?: any;
};

/**
 * A timer handle, named rather than spelled `ReturnType<typeof setTimeout>`.
 *
 * In the browser this is a number; under Node's types it is a `Timeout` object.
 * The dashboard is built for both, so naming it keeps the difference in one
 * place instead of at every timer, and lets the two callers below share it.
 */
type TimerHandle = ReturnType<typeof setTimeout>;

type PendingRequest = {
  resolve: (value: any) => void;
  reject: (error: any) => void;
  timer: TimerHandle;
  // Kept so activity can push the deadline out for the requests allowed to run
  // long. Without it the map key is the only identity a pending request has, and
  // `req-7` says nothing about whether it is a chat turn or a status poll.
  method: string;
};

/**
 * How long a request may go without the server saying anything.
 *
 * Not a cap on how long a turn may take - a local model with tool rounds can
 * legitimately run for minutes. It is a cap on SILENCE. Any sign of progress
 * (`chat.delta`, `chat.activity`, `chat.tools`) restarts the clock, so a turn
 * that is still working is never declared dead. Before this the clock ran
 * regardless: a slow turn was rejected at three minutes and the reply, which
 * did arrive, was discarded because nothing was waiting for its id.
 */
const REQUEST_TIMEOUT_MS = 180000;

/**
 * Notifications the server sends *while* a chat turn is being worked on.
 *
 * All three mean the same thing to the clock: something is still happening.
 * `chat.delta` streams the answer as it is written, `chat.activity` names the
 * step ("Looking at photo.png…"), and `chat.tools` reports what has been called
 * so far. A turn that is doing any of these has not hung.
 */
const CHAT_PROGRESS_METHODS: Record<string, true> = {
  'chat.delta': true,
  'chat.activity': true,
  'chat.tools': true,
};

type WSState = 'disconnected' | 'connecting' | 'connected';

export type Insight = {
  decision: string;
  context: string;
  tier: string;
  text: string;
  timestamp: number;
};

/**
 * Restart the silence clock for every request still waiting on a chat turn.
 *
 * Called for each progress notification. The server sending anything at all
 * about this conversation is proof it is still working on it, so the deadline
 * moves out rather than expiring under a turn that is doing exactly what it
 * should. Only chat requests are reset: a `browser.status` poll that has gone
 * quiet for three minutes is a genuine failure and should still time out.
 */
function noteActivity(): void {
  pending.forEach((entry, id) => {
    if (!entry.method.startsWith('chat.')) return;
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      pending.delete(id);
      entry.reject(new Error(`Request ${entry.method} timed out`));
    }, REQUEST_TIMEOUT_MS);
  });
}

// ---------------------------------------------------------------------------
// Shared singleton connection — lives at module level so navigating between
// dashboard pages does NOT close the socket. In-flight requests (e.g. a slow
// chat.send) keep their response even if the page that sent them unmounts.
// ---------------------------------------------------------------------------

let sharedSocket: WebSocket | null = null;
let sharedState: WSState = 'disconnected';
let sharedCharacter: string = 'idle';
let sharedInsight: Insight | null = null;
let idCounter = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

const pending = new Map<string, PendingRequest>();
const notificationHandlers = new Map<string, (params: any) => void>();
const stateListeners = new Set<(s: WSState) => void>();
const characterListeners = new Set<(s: string) => void>();
const insightListeners = new Set<(i: Insight | null) => void>();

function setSharedState(next: WSState) {
  sharedState = next;
  stateListeners.forEach((l) => l(next));
}

function rejectAllPending(reason: string) {
  pending.forEach((p) => {
    clearTimeout(p.timer);
    p.reject(new Error(reason));
  });
  pending.clear();
}

/**
 * Where the socket lives.
 *
 * Inside the Electron shell the dashboard is served from 127.0.0.1:3001 and the
 * API is a separate port, so 9876 is the default. When Addled's remote gateway
 * serves this page it injects __ADDLED_WS_URL__, and the socket becomes
 * same-origin — which is what carries the session cookie, and what makes an
 * https page upgrade to wss: automatically.
 */
function wsUrl(): string {
  const injected = (globalThis as any).__ADDLED_WS_URL__;
  if (typeof injected === 'string' && injected) {
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${window.location.host}${injected}`;
  }
  return 'ws://127.0.0.1:9876';
}

function connect() {
  if (
    sharedSocket &&
    (sharedSocket.readyState === WebSocket.OPEN ||
      sharedSocket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  setSharedState('connecting');
  const ws = new WebSocket(wsUrl());
  sharedSocket = ws;

  ws.onopen = () => {
    setSharedState('connected');
  };

  ws.onclose = () => {
    sharedSocket = null;
    // Never leave callers hanging — reject all pending requests on drop.
    rejectAllPending('Connection closed');
    setSharedState('disconnected');
    // When the page came from the remote gateway, a dropped socket usually means
    // the session lapsed rather than the backend being down. Ask, and go to the
    // login form instead of sitting on a dashboard that looks dead.
    if ((globalThis as any).__ADDLED_WS_URL__ && window.location.pathname !== '/login') {
      fetch('/api/session', { credentials: 'same-origin', cache: 'no-store' })
        .then((r) => { if (r.status === 401) window.location.replace('/login'); })
        .catch(() => { /* offline; the reconnect loop will keep trying */ });
    }
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };

  ws.onerror = () => {
    try {
      ws.close();
    } catch {
      /* ignore */
    }
  };

  ws.onmessage = (event) => {
    try {
      const msg: WSMessage = JSON.parse(event.data);

      // Response to a pending request
      if (msg.id && pending.has(msg.id)) {
        const { resolve, reject, timer } = pending.get(msg.id)!;
        clearTimeout(timer);
        pending.delete(msg.id);
        if (msg.error) {
          reject(new Error(msg.error.message));
        } else {
          resolve(msg.result);
        }
        return;
      }

      // Server push notification
      if (msg.method && !msg.id) {
        // Progress, not silence: the turn is still running, so the deadline
        // moves. Done before the listeners below so a page with no handler for
        // these still benefits from them.
        if (CHAT_PROGRESS_METHODS[msg.method]) {
          noteActivity();
        }
        const handler = notificationHandlers.get(msg.method);
        if (handler) handler(msg.params || {});

        if (msg.method === 'state.changed') {
          sharedCharacter = msg.params?.state || 'idle';
          characterListeners.forEach((l) => l(sharedCharacter));
        }

        if (msg.method === 'observer.insight') {
          sharedInsight = {
            decision: msg.params?.decision || 'suggest',
            context: msg.params?.context || 'unknown',
            tier: msg.params?.tier || 'medium',
            text: msg.params?.text || '',
            timestamp: msg.params?.timestamp || Date.now(),
          };
          insightListeners.forEach((l) => l(sharedInsight));
        }
        return;
      }
    } catch (e) {
      console.error('WS message parse error:', e);
    }
  };
}

function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (sharedSocket) {
    sharedSocket.close();
    sharedSocket = null;
  }
  setSharedState('disconnected');
}

export function useWS() {
  const [state, setState] = useState<WSState>(sharedState);
  const [characterState, setCharacterState] = useState<string>(sharedCharacter);
  const [insight, setInsight] = useState<Insight | null>(sharedInsight);

  useEffect(() => {
    // Share one socket across all pages — do NOT close it on unmount,
    // otherwise in-flight requests die when the user switches tabs.
    if (!sharedSocket) connect();

    const onState = (s: WSState) => setState(s);
    const onCharacter = (s: string) => setCharacterState(s);
    const onInsight = (i: Insight | null) => setInsight(i);

    stateListeners.add(onState);
    characterListeners.add(onCharacter);
    insightListeners.add(onInsight);

    return () => {
      stateListeners.delete(onState);
      characterListeners.delete(onCharacter);
      insightListeners.delete(onInsight);
    };
  }, []);

  const send = useCallback((method: string, params: any = {}): Promise<any> => {
    const { promise, resolve, reject } = Promise.withResolvers<any>();
    if (!sharedSocket || sharedSocket.readyState !== WebSocket.OPEN) {
      reject(new Error('WebSocket not connected'));
      return promise;
    }
    const ws = sharedSocket;

    const id = `req-${++idCounter}`;
    const entry: PendingRequest = {
      resolve,
      reject,
      timer: setTimeout(() => {
        pending.delete(id);
        reject(new Error(`Request ${method} timed out`));
      }, REQUEST_TIMEOUT_MS),
      method,
    };
    pending.set(id, entry);

    ws.send(JSON.stringify({
      jsonrpc: '2.0',
      id,
      method,
      params,
    }));
    return promise;
  }, []);

  const onNotification = useCallback((method: string, handler: (params: any) => void) => {
    notificationHandlers.set(method, handler);
    return () => {
      if (notificationHandlers.get(method) === handler) {
        notificationHandlers.delete(method);
      }
    };
  }, []);

  const connectNow = useCallback(() => connect(), []);
  const disconnectNow = useCallback(() => disconnect(), []);

  return {
    state,
    characterState,
    insight,
    send,
    onNotification,
    connect: connectNow,
    disconnect: disconnectNow,
  };
}
