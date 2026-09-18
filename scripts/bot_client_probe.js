// Probe for the bot bridge's request timeouts. Run by scripts/check_bot_chat.py.
//
// The bridge used to cap every request at 30s. A real chat turn — a local
// model, tool rounds, plus the background compaction job the backend runs after
// every turn — takes minutes, so the bridge abandoned work the backend was
// still doing and threw the answer away. These checks pin the fix: chat gets a
// long budget, everything else keeps a short one, and a dropped connection
// fails in-flight work immediately instead of at the timeout.

const path = require('path');
const { WebSocketServer } = require('ws');

const {
  AddledWSClient, CHAT_TIMEOUT_MS, DEFAULT_TIMEOUT_MS,
} = require(path.join(__dirname, '..', 'bots', 'shared', 'ws-client.js'));

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
}

async function main() {
  check('default request timeout is still 30s',
    DEFAULT_TIMEOUT_MS === 30000, DEFAULT_TIMEOUT_MS);
  // The dashboard allows 180s per request for exactly this reason; the bridge
  // must not be the tighter of the two.
  check('chat budget is at least the dashboard\'s 180s',
    CHAT_TIMEOUT_MS >= 180000, CHAT_TIMEOUT_MS);

  const server = new WebSocketServer({ port: 0 });
  await new Promise((resolve) => server.once('listening', resolve));
  const port = server.address().port;

  server.on('connection', (sock) => {
    sock.on('message', (data) => {
      let msg;
      try { msg = JSON.parse(data.toString()); } catch { return; }
      if (msg.method === 'system.status') {
        sock.send(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: { ok: true } }));
      }
      // chat.send is deliberately never answered.
    });
  });

  const client = new AddledWSClient('ws://127.0.0.1:' + port);
  await client.connect();

  const status = await client.send('system.status');
  check('a quick method still resolves', status && status.ok === true);

  // An explicit budget must still be honoured, and the error must say which
  // method and how long — a bare "timed out" is what made this hard to place.
  const started = Date.now();
  let err = null;
  try {
    await client.send('chat.send', {}, { timeout: 400 });
  } catch (e) {
    err = e;
  }
  const took = Date.now() - started;
  check('an explicit timeout still rejects', !!err);
  check('the rejection names the method',
    err && err.message.includes('chat.send'), err && err.message);
  check('the rejection says how long it waited',
    err && /timed out after [\d.]+s/.test(err.message), err && err.message);
  check('the explicit budget was the one used', took < 5000, took + 'ms');

  // Losing the connection must settle in-flight work at once. Previously every
  // pending request sat out its full timeout and then blamed a timeout, which
  // hid the real cause.
  const dropStarted = Date.now();
  const pending = client.send('chat.send', {}, { timeout: 600000 });
  pending.catch(() => {});
  setTimeout(() => {
    for (const sock of server.clients) sock.terminate();
  }, 50);

  let dropErr = null;
  try {
    await pending;
  } catch (e) {
    dropErr = e;
  }
  const dropTook = Date.now() - dropStarted;
  check('a dropped connection fails in-flight work',
    !!dropErr, dropErr && dropErr.message);
  check('and fails it immediately rather than timing out',
    dropTook < 5000, dropTook + 'ms');

  client.disconnect();
  server.close();

  for (const r of results) {
    console.log((r.ok ? 'ok  ' : 'FAIL') + '  ' + r.name + (r.detail ? '  [' + r.detail + ']' : ''));
  }
  const failed = results.filter((r) => !r.ok);
  console.log(failed.length ? 'probe: ' + failed.length + ' failed' : 'probe: all passed');
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error('probe crashed:', e && e.stack ? e.stack : e);
  process.exit(2);
});
