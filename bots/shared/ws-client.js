// Shared WebSocket client for bot bridges
// Uses the same JSON-RPC 2.0 protocol as the dashboard

const WebSocket = require('ws');

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

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Not connected'));
        return;
      }

      const id = `bot-${++this.idCounter}`;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Request ${method} timed out`));
      }, 30000);

      this.pending.set(id, { resolve, reject, timer });

      this.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    });
  }

  onNotification(method, handler) {
    this.handlers.set(method, handler);
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

module.exports = { AddledWSClient };
