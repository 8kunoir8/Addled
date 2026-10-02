// Addled — Discord Bot Bridge
// Forwards messages between Discord and the Addled Python backend via discord.js.

const { Client, GatewayIntentBits, REST, Routes, SlashCommandBuilder } = require('discord.js');
const { AddledWSClient } = require('./shared/ws-client');

const DISCORD_TOKEN = process.env.DISCORD_BOT_TOKEN || '';
const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const ALLOWED_CHANNELS = (process.env.DISCORD_ALLOWED_CHANNELS || '').split(',').filter(Boolean);

const { ActionRowBuilder, ButtonBuilder, ButtonStyle } = require('discord.js');

// How a pending approval reads in the channel. Mirrors the dashboard card.
function approvalPrompt(req) {
  const what = req?.kind === 'tool' ? 'tool' : req?.kind === 'skill' ? 'skill' : 'action';
  const lines = [`🔐 **Permission needed**`,
                 `The ${what} \`${req?.name || req?.action_type || 'action'}\` needs your approval.`];
  if (req?.command) lines.push('', `\`\`\`\n${String(req.command).slice(0, 300)}\n\`\`\``);
  if (!req?.grantable) {
    lines.push('', '_This one always asks first, so it cannot be remembered._');
  }
  return lines.join('\n');
}

// One row of buttons for the first pending request. A second request waits for
// the next turn: two rows in one channel invites approving the wrong thing.
function approvalRow(pending) {
  const first = Array.isArray(pending) ? pending[0] : null;
  if (!first?.approval_id) return null;
  const id = String(first.approval_id).slice(0, 90);
  const row = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`ap:${id}`).setLabel('Allow once')
      .setStyle(ButtonStyle.Primary),
  );
  if (first.grantable) {
    row.addComponents(
      new ButtonBuilder().setCustomId(`al:${id}`).setLabel('Always allow')
        .setStyle(ButtonStyle.Secondary));
  }
  row.addComponents(
    new ButtonBuilder().setCustomId(`dn:${id}`).setLabel('Deny')
      .setStyle(ButtonStyle.Danger));
  return row;
}

// The shared answer path, used by the button handler. Kept in one function so
// the three verbs cannot drift apart.
async function answerApproval(ws, verb, approvalId) {
  if (verb === 'al') {
    // The name is not in the button; the backend resolves it from its record
    // of this approval, so it cannot be altered in transit.
    const r = await ws.send('approvals.alwaysAllow', { approvalId });
    return r?.success
      ? '♾️ Always allowed — it will not ask again.'
      : `⚠️ ${r?.error || 'Could not save that.'}`;
  }
  if (verb === 'ap') {
    const r = await ws.send('action.approve', { approvalId });
    return r?.success ? '✅ Allowed — running it now.'
      : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
  }
  const r = await ws.send('action.deny', { approvalId });
  return r?.success ? '⛔ Denied — it was not run.'
    : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
}

// A question the model asked, described for the channel. Shares its wording
// with the dashboard card so the same prompt reads the same everywhere.
function questionPrompt(q) {
  const lines = ['❓ **Addled needs to know**', '', String(q?.question || '').slice(0, 900)];
  if (q?.context) lines.push('', `_${String(q.context).slice(0, 200)}_`);
  if (q?.ttl) lines.push('', `_Waiting for ${q.ttl}. It carries on when you answer._`);
  return lines.join('\n');
}

// Buttons for a question's choices, or null when it is open-ended.
//
// The option *index* travels, not its text: Discord allows 100 characters where
// Telegram allows 64, but an option can be longer than either, and a truncated
// label would ask the user to choose between two identical stubs. The bridge
// holds the wording from the same reply, so the index is enough.
function questionRows(q) {
  const options = Array.isArray(q?.options) ? q.options.filter((o) => String(o).trim()) : [];
  if (!q?.question_id || options.length === 0) return [];
  const id = String(q.question_id).slice(0, 80);
  const rows = [];
  // Discord allows five buttons per row, so six choices become two rows.
  for (let i = 0; i < Math.min(options.length, 6); i += 5) {
    const row = new ActionRowBuilder();
    options.slice(i, i + 5).forEach((label, offset) => {
      row.addComponents(new ButtonBuilder()
        .setCustomId(`qa:${id}:${i + offset}`)
        .setLabel(String(label).slice(0, 80))
        .setStyle(ButtonStyle.Primary));
    });
    rows.push(row);
  }
  rows.push(new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`qs:${id}`).setLabel('Skip')
      .setStyle(ButtonStyle.Secondary)));
  return rows;
}

// Which question this channel was last shown buttons for, and what they said.
// An open-ended question has no buttons, so its answer arrives as ordinary
// text — without this the bot could not tell an answer from a new instruction.
const openQuestions = new Map();

function rememberQuestion(channelId, q) {
  if (!q?.question_id) return;
  openQuestions.set(String(channelId), {
    question_id: q.question_id,
    question: q.question || '',
    options: Array.isArray(q.options) ? q.options : [],
  });
}

function takeQuestion(channelId) {
  const key = String(channelId);
  const held = openQuestions.get(key) || null;
  openQuestions.delete(key);
  return held;
}

// Answering resumes the work the question was blocking, exactly as the
// dashboard does: settle it, then send the answer as the next turn.
//
// The `chat.send` belongs HERE and not in the caller. Sending only
// `question.answer` settles the question without the model ever learning the
// answer — it asked, the user answered, and the work stayed stopped. Keeping
// both calls in one function is what makes that impossible to half-do.
async function answerQuestion(ws, conversation, question, answer, reply) {
  const settled = await ws.send('question.answer',
    { question_id: question.question_id, answer,
      source: 'discord', conversation: String(conversation || '') });
  if (!settled?.success) return settled;
  const asked = question.question ? `"${question.question}"` : 'a question';
  const resumed = await ws.send('chat.send', {
    message:
      `Answering your question ${asked}: ${answer}\n\n` +
      `This is my answer to the question you asked. Continue the task you were ` +
      `working on when you asked it, using this.`,
    source: 'discord',
    conversation: String(conversation || ''),
  });
  const text = resumed?.response || 'No response';
  if (typeof reply === 'function') await reply(text);
  // `settled` is what callers branch on, and success means the QUESTION was
  // answered — the resume is a follow-up whose failure must not be reported as
  // a refused answer, or the question would be re-armed with a dead id.
  return settled;
}

async function main() {
  if (!DISCORD_TOKEN) {
    console.error('[Discord] DISCORD_BOT_TOKEN environment variable is required.');
    console.error('[Discord] Create an app at https://discord.com/developers/applications');
    process.exit(1);
  }

  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  try {
    await ws.connect();
  } catch (e) {
    console.error('[Discord] Backend not reachable:', e.message);
  }

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.DirectMessages,
    ],
  });

  // Register slash commands on ready
  client.once('ready', async () => {
    console.log(`[Discord] Logged in as ${client.user.tag}`);

    const commands = [
      new SlashCommandBuilder().setName('chat').setDescription('Chat with Addled')
        .addStringOption(o => o.setName('message').setDescription('Your message').setRequired(true)),
      new SlashCommandBuilder().setName('goal').setDescription('Create a background goal')
        .addStringOption(o => o.setName('description').setDescription('Goal description').setRequired(true)),
      new SlashCommandBuilder().setName('status').setDescription('Check Addled status'),
      new SlashCommandBuilder().setName('sleep').setDescription('Put Addled to sleep'),
      new SlashCommandBuilder().setName('wake').setDescription('Wake Addled up'),
    ].map(cmd => cmd.toJSON());

    try {
      const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
      await rest.put(Routes.applicationCommands(client.user.id), { body: commands });
      console.log('[Discord] Slash commands registered');
    } catch (e) {
      console.error('[Discord] Failed to register commands:', e.message);
    }

    client.user.setActivity('your desktop');
  });

  // Handle slash commands
  client.on('interactionCreate', async (interaction) => {
    // A button before anything else. This handler used to return immediately
    // for anything that was not a chat-input command, which would have made an
    // approval button do nothing at all.
    if (interaction.isButton()) {
      const [verb, refA, refB] = String(interaction.customId || '').split(':');
      const channelId = String(interaction.channelId || '');
      // Reply inside Discord's 3-second window, then edit with the outcome.
      await interaction.deferUpdate().catch(() => {});

      // A tap on a question's choice, or its Skip. Handled before the approval
      // verbs, which would reject these as unknown.
      if (verb === 'qa' || verb === 'qs') {
        const held = openQuestions.get(channelId);
        if (!held || held.question_id !== refA) {
          await interaction.editReply({
            content: '⚠️ That question is no longer open here.', components: [],
          }).catch(() => {});
          return;
        }
        const label = verb === 'qs' ? null : (held.options[Number(refB)] ?? '');
        try {
          if (verb === 'qs') {
            const r = await ws.send('question.dismiss',
              { question_id: refA, source: 'discord',
                conversation: channelId });
            takeQuestion(channelId);
            await interaction.editReply({
              content: r?.success ? '⏭️ Skipped.'
                : `⚠️ ${r?.error || 'It is no longer open.'}`,
              components: [],
            });
          } else {
            if (!label) {
              await interaction.editReply({
                content: '⚠️ That choice is not available.', components: [],
              }).catch(() => {});
              return;
            }
            await interaction.editReply({ content: `✍️ ${label}`, components: [] });
            const settled = await answerQuestion(ws, channelId, held, label,
              (msg) => interaction.followUp(msg));
            if (!settled?.success) {
              // Only claim success when the backend said so. "✅ Answered" for
              // a refused answer tells the user the work resumed when it did
              // not. The question goes back so the tap can be retried.
              rememberQuestion(channelId, held);
              await interaction.editReply({
                content: `⚠️ ${settled?.error || 'That question is no longer open.'}`,
                components: [],
              }).catch(() => {});
            } else {
              takeQuestion(channelId);
              await interaction.editReply({
                content: `✅ Answered: ${label}`, components: [],
              }).catch(() => {});
            }
          }
        } catch (e) {
          await interaction.editReply({
            content: `⚠️ Failed: ${e.message}`, components: [],
          }).catch(() => {});
        }
        return;
      }

      if (!refA || !['ap', 'al', 'dn'].includes(verb)) return;
      try {
        const outcome = await answerApproval(ws, verb, refA);
        await interaction.editReply({ content: outcome, components: [] });
      } catch (e) {
        await interaction.editReply({
          content: `⚠️ Failed: ${e.message}`, components: [],
        }).catch(() => {});
      }
      return;
    }
    if (!interaction.isChatInputCommand()) return;

    const { commandName } = interaction;

    switch (commandName) {
      case 'chat': {
        await interaction.deferReply();
        const message = interaction.options.getString('message', true);
        // A reply can take minutes on a local model, so show that it is still
        // working rather than a frozen deferred reply.
        let elapsed = 0;
        const ticker = setInterval(() => {
          elapsed += 15;
          interaction.editReply(`\uD83E\uDD14 Thinking... (${elapsed}s)`).catch(() => {});
        }, 15000);
        try {
          // `source` is what the chat page labels the bubble with, and
          // `conversation` is what lets an approval be answered from *this*
          // channel rather than any other. The backend announces the turn to
          // every other surface, so a message asked here appears in the app.
          const r = await ws.send('chat.send', {
            message,
            source: 'discord',
            conversation: String(interaction.channelId || ''),
          });
          const response = r?.response || 'No response';
          clearInterval(ticker);
          const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
          const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
          // A question is shown as its own message below, so the approval row
          // is not attached to the reply when a question is waiting: the reply
          // already restates the question, and two sets of buttons for two
          // different decisions in one channel invites answering the wrong one.
          const row = questions.length ? null : approvalRow(pending);
          if (response.length <= 2000) {
            await interaction.editReply(row
              ? { content: response, components: [row] }
              : { content: response });
          } else {
            await interaction.editReply(response.slice(0, 1997) + '...');
            // Send rest as follow-up
            for (let i = 1997; i < response.length; i += 2000) {
              await interaction.followUp(response.slice(i, i + 2000));
            }
          }
          if (row) {
            await interaction.followUp({
              content: approvalPrompt(pending[0]), components: [row],
            });
          }
          if (questions.length) {
            const q = questions[0];
            rememberQuestion(interaction.channelId, q);
            await interaction.followUp({
              content: questionPrompt(q), components: questionRows(q),
            });
          }
        } catch (e) {
          clearInterval(ticker);
          await interaction.editReply(`\u2757 Error: ${e.message}`);
        }
        break;
      }
      case 'goal': {
        await interaction.deferReply();
        const desc = interaction.options.getString('description', true);
        try {
          const r = await ws.send('goal.create', { title: desc, priority: 'normal' });
          await interaction.editReply(`\u2705 Goal created: **${desc}**\n\uD83C\uDD94 \`${r?.goalId || '?'}\``);
        } catch (e) {
          await interaction.editReply(`\u2757 Failed: ${e.message}`);
        }
        break;
      }
      case 'status': {
        await interaction.deferReply();
        try {
          const r = await ws.send('system.status', {});
          await interaction.editReply(
            `\uD83D\uDFE2 **Addled Status**\n` +
            `Engine: ${r?.engineState || '?'}\n` +
            `Provider: ${r?.provider || '?'}\n` +
            `Character: ${r?.characterState || '?'}`
          );
        } catch (e) {
          await interaction.editReply(`\u26A0\uFE0F Addled unreachable: ${e.message}`);
        }
        break;
      }
      case 'sleep': {
        await interaction.deferReply();
        try { await ws.send('character.setState', { state: 'sleeping' }); await interaction.editReply('\uD83D\uDE34 Addled sleeping'); }
        catch (e) { await interaction.editReply(`\u2757 ${e.message}`); }
        break;
      }
      case 'wake': {
        await interaction.deferReply();
        try { await ws.send('character.setState', { state: 'idle' }); await interaction.editReply('\uD83D\uDC4B Addled awake'); }
        catch (e) { await interaction.editReply(`\u2757 ${e.message}`); }
        break;
      }
    }
  });

  // Handle @mentions in guild messages
  client.on('messageCreate', async (msg) => {
    if (msg.author.bot) return;
    if (ALLOWED_CHANNELS.length > 0 && !ALLOWED_CHANNELS.includes(msg.channelId)) return;

    const isMentioned = msg.mentions.has(client.user);
    const isDM = !msg.guild;

    if (!isMentioned && !isDM) return;

    // Remove the @mention from the text
    const text = msg.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
    if (!text) {
      await msg.reply('\uD83D\uDC4B Hi! Use `/chat <message>` or just mention me with your question.');
      return;
    }

    // An open question with no choices takes its answer as ordinary text, so
    // this comes BEFORE the turn is sent: otherwise the answer would be handled
    // as a fresh instruction while the question sat unanswered.
    const open = openQuestions.get(String(msg.channelId || ''));
    if (open) {
      takeQuestion(msg.channelId);
      await msg.channel.sendTyping();
      try {
        const settled = await answerQuestion(ws, msg.channelId, open, text,
          (m) => msg.reply(m));
        if (!settled?.success) {
          // Refused — expired, or answered elsewhere. The message must NOT be
          // dropped: it is a turn the user meant to send, so it is forwarded as
          // one. Doing nothing is the worst outcome, because nothing on screen
          // says anything went wrong.
          await msg.reply(`⚠️ ${settled?.error || 'That question is no longer open.'}`
            + '\nSending your message as a normal request instead…');
          await handleTurn(msg, text);
        }
      } catch (e) {
        // A thrown failure is the same case: keep the question so it can be
        // retried, rather than losing it with the user's text.
        rememberQuestion(msg.channelId, open);
        await msg.reply(`\u26A0\uFE0F Could not send that: ${e.message}`);
      }
      return;
    }

    await handleTurn(msg, text);
  });

  /**
   * One ordinary turn, from a mention or a DM.
   *
   * Extracted so the two callers that need it — the normal path and the
   * "answer was refused, forward the message instead" path — share one
   * implementation. A second copy would drift, and the fallback is exactly the
   * path least likely to be exercised by hand.
   */
  async function handleTurn(msg, text) {
    await msg.channel.sendTyping();
    try {
      const r = await ws.send('chat.send', {
        message: text,
        source: 'discord',
        conversation: String(msg.channelId || ''),
      });
      const response = r?.response || 'No response';
      const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
      const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
      const row = questions.length ? null : approvalRow(pending);
      if (response.length <= 2000) {
        await msg.reply(row
          ? { content: response, components: [row] }
          : { content: response });
      } else {
        await msg.reply(response.slice(0, 1997) + '...');
        const channel = msg.channel;
        for (let i = 1997; i < response.length; i += 2000) {
          await channel.send(response.slice(i, i + 2000));
        }
      }
      if (row) {
        await msg.channel.send({
          content: approvalPrompt(pending[0]), components: [row],
        });
      }
      if (questions.length) {
        const q = questions[0];
        rememberQuestion(msg.channelId, q);
        await msg.channel.send({
          content: questionPrompt(q), components: questionRows(q),
        });
      }
    } catch (e) {
      await msg.reply(`\u2757 Addled: ${e.message}`);
    }
  }

  // Login
  await client.login(DISCORD_TOKEN);

  process.on('SIGINT', () => {
    console.log('[Discord] Shutting down...');
    ws.disconnect();
    client.destroy();
    process.exit(0);
  });
}

main().catch(err => { console.error('[Discord] Fatal:', err); process.exit(1); });
