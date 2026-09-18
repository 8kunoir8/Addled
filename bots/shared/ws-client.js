// Shared WebSocket client for bot bridges
// Uses the same JSON-RPC 2.0 protocol as the dashboard

const WebSocket = require('ws');

// Every request used to share one 30s cap. The backend's slowest path — a local
// model, tool rounds, and the background compaction job it runs after a turn —
// legitimately takes minutes, and the dashboard allows 180s for *every* call
// for exactly that reason (dashboard/src/lib/useWS.ts). A 30s cap therefore
// abandoned work the backend was still doing and threw the finished answer
// away, which reached the user as "Request chat.send timed out".
const DEFAULT_TIMEOUT_MS = 30000;
// Chat gets its own, much longer budget. Override with
// ADDLED_BOT_CHAT_TIMEOUT_MS if a slow machine needs even longer.
const CHAT_TIMEOUT_MS = Number(process.env.ADDLED_BOT_CHAT_TIMEOUT_MS) || 600000;
const LONG_METHODS = new Set(['chat.send']);

function timeoutFor(method, override) {
  if (Number(override) > 0) return Number(override);
  return LONG_METHODS.has(method) ? CHAT_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
}

class AddledWSClient {
  constructor(url = 'ws://127.0.0.1:9876') {
    this.url = url;
    this.ws = null;
    this.pending = new Map();
    this.handlers = new Map();
    this.idCounter = 0;
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 30000;
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);

      this.ws.on('open', () => {
        console.log('[Bot Bridge] Connected to Addled backend');
        this.reconnectDelay = 1000;
        resolve();
      });

      this.ws.on('close', () => {
        console.log('[Bot Bridge] Disconnected from Addled backend');
        this.ws = null;
        // Fail in-flight requests now. Leaving them pending made each one sit
        // out the full timeout and then report a timeout, hiding the cause.
        this._failPending('Disconnected from Addled backend');
        this._scheduleReconnect();
      });

      this.ws.on('error', (err) => {
        console.error('[Bot Bridge] WebSocket error:', err.message);
        reject(err);
      });

      this.ws.on('message', (data) => {
        try {
          const msg = JSON.parse(data.toString());

          // Response to pending request
          if (msg.id && this.pending.has(msg.id)) {
            const { resolve, reject, timer } = this.pending.get(msg.id);
            clearTimeout(timer);
            this.pending.delete(msg.id);
            if (msg.error) reject(new Error(msg.error.message));
            else resolve(msg.result);
            return;
          }

          // Server push notification
          if (msg.method && !msg.id) {
            const handler = this.handlers.get(msg.method);
            if (handler) handler(msg.params || {});
          }
        } catch (e) {
          console.error('[Bot Bridge] Parse error:', e);
        }
      });
    });
  }

  _scheduleReconnect() {
    console.log(`[Bot Bridge] Reconnecting in ${this.reconnectDelay}ms...`);
    setTimeout(() => {
      this.connect().catch(() => {});
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    }, this.reconnectDelay);
  }

  send(method, params = {}, opts = {}) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Not connected'));
        return;
      }

      const id = `bot-${++this.idCounter}`;
      const timeout = timeoutFor(method, opts.timeout);
      const timer = setTimeout(() => {
        this.pending.delete(id);
        // Sub-second budgets are used by the checks, and "after 0s" reads as a
        // bug rather than as a deliberate override.
        const secs = timeout >= 10000
          ? `${Math.round(timeout / 1000)}s`
          : `${(timeout / 1000).toFixed(1)}s`;
        reject(new Error(`Request ${method} timed out after ${secs}`));
      }, timeout);

      this.pending.set(id, { resolve, reject, timer });

      this.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    });
  }

  /** Settle everything waiting on a connection that is gone. */
  _failPending(reason) {
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(new Error(reason));
    }
    this.pending.clear();
  }

  onNotification(method, handler) {
    this.handlers.set(method, handler);
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this._failPending('Disconnected');
  }
}

module.exports = { AddledWSClient, CHAT_TIMEOUT_MS, DEFAULT_TIMEOUT_MS };
