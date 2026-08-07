// Addled — WhatsApp Bot Bridge
// Forwards messages between WhatsApp and the Addled Python backend.

const { AddledWSClient } = require('../shared/ws-client');

const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';

async function main() {
  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  await ws.connect();

  console.log('[WhatsApp Bot] Bridge ready. Full WhatsApp bot implementation coming in Phase 6.');
  console.log('[WhatsApp Bot] Will use Baileys for WhatsApp Web multi-device API.');

  process.on('SIGINT', () => {
    console.log('[WhatsApp Bot] Shutting down...');
    ws.disconnect();
    process.exit(0);
  });
}

main().catch(console.error);
