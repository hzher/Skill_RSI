// Cordis request-local decoration adapted from Skill_RSI_hzh/plugins/clbench-submit/index.mjs (MIT).
import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, mkdirSync, realpathSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { baselineState, registerBaseline, seeToolResult } from './baseline.mjs';

export const name = 'skill-rsi';
export const inject = ['tools', 'llm'];

const MAX_BRIDGE_BYTES = 2 * 1024 * 1024;
const sha = text => createHash('sha256').update(text).digest('hex');

function invoke(config, input, mode = 'hook') {
  try {
    const [python, ...args] = config.argv;
    if (mode !== 'hook') args[2] = mode;
    const bytes = JSON.stringify(input);
    if (Buffer.byteLength(bytes) > MAX_BRIDGE_BYTES) return {};
    const reply = spawnSync(python, args, {
      cwd: config.project, input: bytes, encoding: 'utf8', timeout: 3500, maxBuffer: MAX_BRIDGE_BYTES,
    });
    return reply.status === 0 ? JSON.parse(reply.stdout) : {};
  } catch {
    return {};
  }
}

function makeRecorder(config) {
  const directory = config.observe ? config.captureDirectory : null;
  let seq = 0;
  let dropped = 0;
  const writes = (path, bytes, append = false) => {
    if (!directory) return;
    try {
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      if (append) appendFileSync(path, bytes, { mode: 0o600 });
      else writeFileSync(path, bytes, { mode: 0o600 });
    } catch {
      dropped++;
    }
  };
  return {
    enabled: !!directory,
    event(stream, kind, session_id, fields = {}) {
      if (!directory) return;
      writes(join(directory, stream + '.jsonl'), JSON.stringify({
        schema_version: 1, source_seq: ++seq, timestamp: Date.now(), kind, session_id, ...fields,
      }) + '\n', true);
    },
    request(id, options) {
      if (!directory) return null;
      const path = `request-${id}.json`;
      const publicFields = ['provider', 'model', 'system', 'systemPrompt', 'messages', 'tools', 'toolHistory',
        'temperature', 'maxTokens', 'reasoningEffort', 'stop', 'purpose', 'sessionId'];
      try {
        const value = Object.fromEntries(publicFields.filter(key => options[key] !== undefined).map(key => [key, options[key]]));
        writes(join(directory, '..', path), JSON.stringify(value));
        return path;
      } catch {
        dropped++;
        return null;
      }
    },
    finish() {
      if (directory) writes(join(directory, 'plugin_health.json'), JSON.stringify({
        closed: true, dropped_records: dropped, recording_closed: dropped === 0,
      }));
    },
  };
}

function dispositions(options) {
  const messages = options.messages;
  const lastAssistant = messages.findLastIndex(message => message.role === 'assistant');
  return messages.slice(lastAssistant + 1).flatMap((message, relative) => {
    const block = message.content?.length === 1 && message.content[0];
    if (message.role !== 'tool' || message.isError || !message.toolCallId ||
        block?.type !== 'text' || typeof block.text !== 'string') return [];
    return [{ position: lastAssistant + 1 + relative, message, block }];
  });
}

function replace(message, block, text) {
  return Object.freeze({ ...message, content: Object.freeze([Object.freeze({ ...block, text })]) });
}

function appendContext(messages, texts) {
  const target = messages.findIndex(message => message.role === 'system' &&
    message.content?.some(block => block.type === 'text' && typeof block.text === 'string'));
  if (target === -1) return false;
  const message = messages[target];
  const content = [...message.content];
  const position = content.findIndex(block => block.type === 'text' && typeof block.text === 'string');
  const block = content[position];
  content[position] = Object.freeze({ ...block, text: `${block.text}\n\n${texts.join('\n')}` });
  messages[target] = Object.freeze({ ...message, content: Object.freeze(content) });
  return true;
}

export function apply(ctx, config) {
  const recorder = makeRecorder(config);
  const sessions = new Map();
  const pending = new Map();
  const toolInputs = new Map();
  const baselines = new Map();
  const effects = new WeakMap();
  let marks;
  try {
    marks = createRequire(realpathSync(process.argv[1]))('@deepseek-ai/dsh-llm');
  } catch {
    // The installed DSH runtime must supply request branding before replacement is enabled.
  }
  const writable = typeof marks?.isAgentLoopRequest === 'function' && typeof marks?.markAgentLoopRequest === 'function';
  const supported = {
    session_start: [],
    ...(writable && {
      tool_result: ['read_origin', 'replace_tool_result', 'retrieve_original'],
      before_action: ['read_origin', 'append_context'],
    }),
  };
  function dispatch(session, boundary, values = {}) {
    return invoke(config, { cwd: config.project, session_id: session, boundary, supported_nodes: supported, ...values });
  }
  function matches(agent) {
    const cwd = agent.session.header?.cwd ?? agent.session.cwd ?? process.cwd();
    try {
      const normalized = realpathSync(resolve(cwd));
      const project = realpathSync(config.project);
      return normalized === project;
    } catch {
      return false;
    }
  }
  ctx.on('agent/created', ({ agent }) => {
    if (!matches(agent)) return;
    const id = agent.session.id;
    const disposeExpand = agent.ctx.tools.register({
      name: 'skill_rsi_expand',
      description: 'Read the unmodified text behind a Skill RSI view handle, optionally by inclusive line range.',
      parameters: { type: 'object', properties: {
        handle: { type: 'string' }, start_line: { type: 'integer', minimum: 1 }, end_line: { type: 'integer', minimum: 1 },
      }, required: ['handle'], additionalProperties: false },
      output: { schema: { type: 'string' }, render: (_args, text) => [{ type: 'text', text }] },
      async execute(args) {
        const result = invoke(config, { ...args, session_id: id }, 'expand');
        if (typeof result.text !== 'string') throw new Error('Original text is not available in this project.');
        return result.text;
      },
    });
    const manual = writable && config.baselineTools === true ? baselineState() : null;
    if (manual) baselines.set(id, manual);
    const disposeBaseline = manual && registerBaseline(agent, manual);
    sessions.set(id, () => {
      disposeExpand();
      disposeBaseline?.();
    });
    dispatch(id, 'session_start', { event_id: randomUUID() });
  });
  ctx.on('agent/disposed', ({ agent }) => {
    const id = agent.session.id;
    sessions.get(id)?.();
    sessions.delete(id);
    baselines.delete(id);
    pending.delete(id);
    toolInputs.delete(id);
  });
  ctx.on('session/event', (session, event) => {
    if (!sessions.has(session.id)) return;
    if (event.type === 'turn/end') {
      dispatch(session.id, 'turn_end', { event_id: String(event.seq ?? randomUUID()), turn_id: event.data?.turn });
      pending.delete(session.id);
      toolInputs.delete(session.id);
    }
  });
  ctx.on('tools/execute', async (exec, next) => {
    const id = exec.agent?.session.id;
    if (!sessions.has(id) || !exec.callId || exec.name?.startsWith('skill_rsi_')) return next();
    const byCall = toolInputs.get(id) ?? new Map();
    toolInputs.set(id, byCall);
    byCall.set(exec.callId, { name: exec.name, args: exec.arguments });
    const reply = dispatch(id, 'before_action', {
      event_id: exec.callId, call_id: exec.callId, tool_name: exec.name, tool_input: exec.arguments,
    });
    const notes = (reply.actions ?? []).filter(row => row.kind === 'append_context' && typeof row.text === 'string');
    if (notes.length) pending.set(id, [...(pending.get(id) ?? []), ...notes].slice(-20));
    return next();
  });
  if (writable) {
    const llm = ctx.llm;
    const original = llm.prepareCall;
    const wrapper = async function (...args) {
      const prepared = await original.apply(this, args);
      return Object.freeze({ ...prepared, stream(options) {
        const session = options.sessionId;
        if (options.purpose !== undefined || !sessions.has(session) || !marks.isAgentLoopRequest(options) ||
            !Array.isArray(options.messages)) return prepared.stream(options);
        const mapped = [...options.messages];
        const applied = [];
        let changed = false;
        const eligible = dispositions(options);
        const manual = baselines.get(session);
        if (manual) {
          for (const { message, block } of eligible) seeToolResult(manual, message.toolCallId, block.text);
          for (let index = 0; index < mapped.length; index++) {
            const message = mapped[index];
            const block = message.content?.length === 1 && message.content[0];
            const text = manual.projected.get(message.toolCallId);
            if (message.role !== 'tool' || block?.type !== 'text' || !text ||
                manual.entries.get(message.toolCallId) !== block.text) continue;
            mapped[index] = replace(message, block, text);
            changed = true;
          }
        }
        const byCall = toolInputs.get(session);
        for (const { position, message, block } of eligible) {
          const identity = message.toolCallId;
          if (!byCall?.has(identity)) continue;
          const input = byCall.get(identity);
          byCall.delete(identity);
          if (manual?.projected.has(identity)) continue;
          const reply = dispatch(session, 'tool_result', {
            event_id: message.id ?? identity, call_id: identity, tool_name: input.name, tool_input: input.args,
            input_version: sha(block.text), payload: { text: block.text, success: true },
          });
          const action = (reply.actions ?? []).find(row => row.kind === 'replace_tool_result' && row.target === identity &&
            typeof row.text === 'string' && Buffer.byteLength(row.text) < Buffer.byteLength(block.text));
          if (!action) continue;
          mapped[position] = replace(message, block, action.text);
          changed = true;
          applied.push({ tool_call_id: identity, operation_key: action.operation_key, previous: sha(block.text),
            projected: sha(action.text), effective_scope: 'next_main_request' });
        }
        const notes = pending.get(session);
        if (notes?.length && appendContext(mapped, notes.map(row => row.text))) {
          pending.delete(session);
          changed = true;
          applied.push(...notes.map(row => ({ tool_call_id: row.target, operation_key: row.operation_key, context_appended: true, effective_scope: 'next_main_request' })));
        }
        if (!changed) return prepared.stream(options);
        const request = marks.markAgentLoopRequest(Object.freeze({ ...options, messages: Object.freeze(mapped) }));
        effects.set(request, applied);
        return prepared.stream(request);
      } });
    };
    ctx.effect(() => {
      const own = Object.getOwnPropertyDescriptor(llm, 'prepareCall');
      llm.prepareCall = wrapper;
      return () => {
        if (Object.getOwnPropertyDescriptor(llm, 'prepareCall')?.value !== wrapper) return;
        if (own) Object.defineProperty(llm, 'prepareCall', own);
        else delete llm.prepareCall;
      };
    }, 'skill-rsi:request-view');
  }
  ctx.on('llm/stream', async function* (options, next) {
    const { sessionId, purpose, provider, model } = options;
    if (!sessions.has(sessionId)) {
      yield* next();
      return;
    }
    const id = randomUUID();
    const request_path = recorder.request(id, options);
    const delivered = effects.get(options) ?? [];
    recorder.event('calls', 'model_start', sessionId, { llm_call_id: id, status: 'started', request_path,
      purpose: purpose ?? null, provider, model, view_receipts: delivered });
    if (delivered.length) invoke(config, { receipts: delivered.map(row => ({
      ...row, session_id: sessionId, request_ref: { llm_call_id: id, request_path },
    })) }, 'receipt');
    let usage = null;
    let finish = null;
    let failed = false;
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage;
        if (chunk.type === 'finish') finish = chunk.reason;
        yield chunk;
      }
    } catch (error) {
      failed = true;
      throw error;
    } finally {
      recorder.event('calls', 'model_end', sessionId, { llm_call_id: id,
        status: failed || finish?.kind === 'error' ? 'failed' : finish ? 'completed' : 'unterminated',
        request_path, finish, usage });
    }
  });
  ctx.effect(() => () => recorder.finish(), 'skill-rsi:capture');
}
