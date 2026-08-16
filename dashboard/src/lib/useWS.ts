'use client';

import { useState, useEffect, useRef, useCallback } from 'react';

type WSMessage = {
  jsonrpc: string;
  id?: string;
  method?: string;
  result?: any;
  error?: { code: number; message: string };
  params?: any;
};

type PendingRequest = {
  resolve: (value: any) => void;
  reject: (error: any) => void;
  timer: ReturnType<typeof setTimeout>;
};

type WSState = 'disconnected' | 'connecting' | 'connected';

export type Insight = {
  decision: string;
  context: string;
  tier: string;
  text: string;
  timestamp: number;
};

export function useWS() {
  const [state, setState] = useState<WSState>('disconnected');
  const [characterState, setCharacterState] = useState<string>('idle');
  const [insight, setInsight] = useState<Insight | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef<Map<string, PendingRequest>>(new Map());
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const idCounterRef = useRef(0);
  const handlersRef = useRef<Map<string, (params: any) => void>>(new Map());

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setState('connecting');
    const ws = new WebSocket('ws://127.0.0.1:9876');

    ws.onopen = () => {
      setState('connected');
      wsRef.current = ws;
    };

    ws.onclose = () => {
      setState('disconnected');
      wsRef.current = null;
      // Reconnect after 2s
      reconnectTimerRef.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);

        // If it has an id, it's a response to a pending request
        if (msg.id && pendingRef.current.has(msg.id)) {
          const { resolve, reject, timer } = pendingRef.current.get(msg.id)!;
          clearTimeout(timer);
          pendingRef.current.delete(msg.id);

          if (msg.error) {
            reject(new Error(msg.error.message));
          } else {
            resolve(msg.result);
          }
          return;
        }

        // If it has a method and no id, it's a server push notification
        if (msg.method && !msg.id) {
          const handler = handlersRef.current.get(msg.method);
          if (handler) {
            handler(msg.params || {});
          }

          // Handle state changes
          if (msg.method === 'state.changed') {
            setCharacterState(msg.params?.state || 'idle');
          }

          // Proactive observer insights (bot noticed something)
          if (msg.method === 'observer.insight') {
            setInsight({
              decision: msg.params?.decision || 'suggest',
              context: msg.params?.context || 'unknown',
              tier: msg.params?.tier || 'medium',
              text: msg.params?.text || '',
              timestamp: msg.params?.timestamp || Date.now(),
            });
          }
          return;
        }
      } catch (e) {
        console.error('WS message parse error:', e);
      }
    };
  }, []);

  const disconnect = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setState('disconnected');
  }, []);

  const send = useCallback((method: string, params: any = {}): Promise<any> => {
    return new Promise((resolve, reject) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        reject(new Error('WebSocket not connected'));
        return;
      }

      const id = `req-${++idCounterRef.current}`;
      const timer = setTimeout(() => {
        pendingRef.current.delete(id);
        reject(new Error(`Request ${method} timed out`));
      }, 90000);

      pendingRef.current.set(id, { resolve, reject, timer });

      wsRef.current.send(JSON.stringify({
        jsonrpc: '2.0',
        id,
        method,
        params,
      }));
    });
  }, []);

  const onNotification = useCallback((method: string, handler: (params: any) => void) => {
    handlersRef.current.set(method, handler);
    return () => {
      handlersRef.current.delete(method);
    };
  }, []);

  // Auto-connect on mount
  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  return {
    state,
    characterState,
    insight,
    send,
    onNotification,
    connect,
    disconnect,
  };
}
