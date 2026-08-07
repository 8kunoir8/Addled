// Addled — Telegram Bot Bridge
// Forwards messages between Telegram and the Addled Python backend.

const { AddledWSClient } = require('../shared/ws-client');

// Configuration — set via environment variables or config file
const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';

async function main() {
  if (!TELEGRAM_TOKEN) {
    console.error('[Telegram Bot] TELEGRAM_BOT_TOKEN not set. Exiting.');
    process.exit(1);
  }

  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  await ws.connect();

  // Listen for state changes
  ws.onNotification('state.changed', (params) => {
    console.log(`[Telegram Bot] Character state: ${params.state}`);
  });

  console.log('[Telegram Bot] Bridge ready. Waiting for messages...');
  console.log('[Telegram Bot] Full Telegram bot implementation coming in Phase 6.');

  // Keep alive
  process.on('SIGINT', () => {
    console.log('[Telegram Bot] Shutting down...');
    ws.disconnect();
    process.exit(0);
  });
}

main().catch(console.error);
