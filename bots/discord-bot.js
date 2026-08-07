// Addled — Discord Bot Bridge
// Forwards messages between Discord and the Addled Python backend.

const { AddledWSClient } = require('../shared/ws-client');

const DISCORD_TOKEN = process.env.DISCORD_BOT_TOKEN || '';
const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';

async function main() {
  if (!DISCORD_TOKEN) {
    console.error('[Discord Bot] DISCORD_BOT_TOKEN not set. Exiting.');
    process.exit(1);
  }

  const ws = new AddledWSClient(WS_URL);
  await ws.connect();

  console.log('[Discord Bot] Bridge ready. Full Discord bot implementation coming in Phase 6.');
  console.log('[Discord Bot] Will use discord.js v14 for slash commands and mention triggers.');

  process.on('SIGINT', () => {
    console.log('[Discord Bot] Shutting down...');
    ws.disconnect();
    process.exit(0);
  });
}

main().catch(console.error);
