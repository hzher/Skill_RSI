import {
  appendFileSync, closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync, openSync,
  readFileSync, readSync, realpathSync, renameSync, rmSync, writeFileSync,
} from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

export const name = 'clbench-submit';
export const inject = ['tools', 'systemPrompt'];

function physical(path) {
  let ancestor = resolve(path);
  const suffix = [];
  while (!existsSync(ancestor)) {
    const parent = dirname(ancestor);
    if (parent === ancestor) throw new Error(`Cannot resolve path ${path}`);
    suffix.unshift(relative(parent, ancestor));
    ancestor = parent;
  }
  return join(realpathSync(ancestor), ...suffix);
}

const MEMORY_TOOLS = new Set(['read', 'write', 'edit']);
const MAX_MEMORY_BYTES = 4 * 1024 * 1024;

function inside(root, path) {
  const part = relative(root, path);
  return part !== '..' && !part.startsWith(`..${sep}`) && !isAbsolute(part);
}

function recorder(config) {
  const directory = config.captureDirectory;
  const collector = randomUUID();
  const counts = {};
  const errors = {};
  const activeCalls = new Set();
  const run = (source, fn) => {
    try {
      return fn();
    } catch (error) {
      const key = `${source}:${error?.code ?? error?.name ?? 'Error'}`;
      errors[key] = (errors[key] ?? 0) + 1;
      return undefined;
    }
  };
  const save = (path, content) => {
    const pending = `${path}.${randomUUID()}.tmp`;
    try {
      writeFileSync(pending, content, { flag: 'wx', mode: 0o600 });
      renameSync(pending, path);
    } finally {
      rmSync(pending, { force: true });
    }
  };
  const append = (path, row) => appendFileSync(path, `${JSON.stringify(row)}\n`, { mode: 0o600 });
  const event = (source, kind, sessionId, fields) => {
    if (!directory) return;
    const seq = counts[source] = (counts[source] ?? 0) + 1;
    run(source, () => append(join(directory, `${source}.jsonl`), {
      schema_version: 1, source_seq: seq, collector_id: collector, timestamp: new Date().toISOString(),
      kind, session_id: sessionId ?? null, runtime_id: config.runtimeId, ...fields,
    }));
  };
  const health = disposed => directory && run('plugin_health', () => save(join(directory, 'plugin_health.json'),
    JSON.stringify({
      collector_id: collector, disposed, counts, errors, open_calls: [...activeCalls],
      memory_max_snapshot_bytes: MAX_MEMORY_BYTES,
    })));
  for (const sub of ['objects', 'responses']) {
    if (directory) run(sub, () => mkdirSync(join(directory, sub), { recursive: true, mode: 0o700 }));
  }
  health(false);
  return { run, save, event, health, append, activeCalls };
}

function contentObject(capture, directory, content, representation) {
  if (content.length > MAX_MEMORY_BYTES) return { status: 'oversize', bytes: content.length, representation };
  const sha256 = createHash('sha256').update(content).digest('hex');
  const ref = join('objects', sha256);
  const file = join(directory, ref);
  if (!existsSync(file)) capture.save(file, content);
  return { status: 'captured', sha256, path: `capture/${ref}`, bytes: content.length, representation };
}

function snapshot(capture, directory, root, requested) {
  const outcome = capture.run('memory_snapshot', () => {
    const path = physical(requested);
    if (path === root || !inside(root, path)) return { status: 'outside_scope' };
    let fd;
    try {
      fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    } catch (error) {
      if (error?.code === 'ENOENT') return { status: 'absent' };
      throw error;
    }
    try {
      const info = fstatSync(fd);
      if (!info.isFile()) return { status: 'non_regular' };
      if (info.nlink !== 1) return { status: 'hardlink_untrusted' };
      const linked = lstatSync(path);
      if (linked.dev !== info.dev || linked.ino !== info.ino || !inside(root, realpathSync(path))) {
        return { status: 'target_changed_before_read' };
      }
      if (info.size > MAX_MEMORY_BYTES) return { status: 'oversize', bytes: info.size };
      const data = Buffer.alloc(info.size + 1);
      let size = 0;
      while (size < data.length) {
        const received = readSync(fd, data, size, data.length - size);
        if (!received) break;
        size += received;
      }
      const after = fstatSync(fd);
      if (size !== info.size || after.mtimeMs !== info.mtimeMs || after.ctimeMs !== info.ctimeMs) {
        return { status: 'changed_during_read' };
      }
      return contentObject(capture, directory, data.subarray(0, size), 'file_bytes');
    } finally {
      closeSync(fd);
    }
  });
  return outcome ?? { status: 'capture_error' };
}

// DSH owns the agent loop, session history, compaction, and tool execution.
export function apply(ctx, config) {
  const capture = recorder(config);
  const attempts = new Map();
  const positions = new Map();
  const activeMemory = new Map();
  const memoryCalls = new Map();
  const states = new Map();
  const skillReceipts = new Set();
  let skillSection;
  if (config.skillPath) {
    const bytes = readFileSync(config.skillPath);
    if (createHash('sha256').update(bytes).digest('hex') !== config.skillSha256) {
      throw new Error('Frozen skill differs from its approved hash');
    }
    skillSection = `Approved comparison skill:\n${bytes.toString('utf8')}`;
    ctx.systemPrompt.section({
      name: 'skill-rsi:comparison',
      order: ctx.systemPrompt.getSectionOrder('DEPLOYMENT_PERSONA_SUFFIX') + 1,
      text: skillSection,
      interpolate: false,
    });
  }
  const memoryRoot = config.captureDirectory
    ? capture.run('memory_root', () => physical(config.memoryRoot))
    : undefined;
  const queryIdentity = sessionId => capture.run('active_query', () => {
    if (!existsSync(config.controlFile)) return null;
    const query = JSON.parse(readFileSync(config.controlFile, 'utf8'));
    return query.active === true && query.sessionId === sessionId ? query.requestId : null;
  }) ?? null;
  const refresh = agent => {
    if (!existsSync(config.controlFile)) return;
    const query = JSON.parse(readFileSync(config.controlFile, 'utf8'));
    if (query.sessionId !== agent.session.id || agent.session.header?.origin === 'subagent') return;
    const previous = states.get(agent.session.id);
    const sameQuery = previous?.requestId === query.requestId;
    if (sameQuery && previous.resultFile === query.resultFile) return;
    previous?.dispose();
    const state = { requestId: query.requestId, resultFile: query.resultFile,
      submitted: false, steps: sameQuery ? previous.steps : 0,
      restrictions: previous?.restrictions ?? [] };
    if (config.toolsMode === 'memory' && !previous) {
      const allowed = ctx.tools.definitions(agent).filter(tool =>
        MEMORY_TOOLS.has(tool.name)
        && tool.parameters?.properties?.file_path?.type === 'string',
      ).map(tool => tool.name);
      const root = physical(config.memoryRoot);
      state.restrictions.push(agent.ctx.tools.restrict({ allow: allowed }));
      state.restrictions.push(agent.ctx.tools.guard(exec => {
        if (exec.name === 'clbench_submit') return;
        if (!allowed.includes(exec.name)) return 'Use clbench_submit for benchmark environment actions';
        try {
          const path = physical(resolve(dirname(root), exec.arguments.file_path));
          if (!inside(root, path)) {
            return 'Native file operations must stay within workspace/memory';
          }
        } catch (error) { return String(error?.message ?? error); }
      }));
    }
    state.dispose = agent.ctx.tools.register({
      name: 'clbench_submit',
      description: 'Submit exactly one action for the current benchmark query and finish this turn. Await the environment feedback before taking another action.',
      parameters: query.schema,
      output: {
        schema: { type: 'boolean' },
        render: () => [{ type: 'text', text: 'Action submitted. Await the environment feedback.' }],
      },
      async execute(args, exec) {
        if (state.submitted) throw new Error('An action has already been submitted for this query');
        exec.signal.throwIfAborted();
        writeFileSync(query.resultFile, JSON.stringify({
          requestId: query.requestId, sessionId: agent.session.id,
          toolCallId: exec.callId, action: args,
        }), { flag: 'wx', mode: 0o600 });
        state.submitted = true;
        exec.concludeTurn();
        return true;
      },
    });
    states.set(agent.session.id, state);
  };
  ctx.on('agent/created', ({ agent }) => refresh(agent));
  ctx.on('agent/inbox/inserted', ({ agent }) => refresh(agent));
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    const state = states.get(agent.session.id);
    if (state && !state.submitted && ++state.steps > config.maxSteps) {
      throw new Error(`No benchmark action submitted within ${config.maxSteps} native steps`);
    }
    return next();
  });
  ctx.on('agent/disposed', ({ agent }) => {
    const state = states.get(agent.session.id);
    state?.dispose();
    for (const dispose of state?.restrictions ?? []) dispose();
    states.delete(agent.session.id);
    attempts.delete(agent.session.id);
    positions.delete(agent.session.id);
  });
  ctx.on('session/event', (session, event) => {
    const { turn, step } = event.data ?? {};
    if (event.type === 'turn/start' || event.type === 'step/start') positions.set(session.id, { turn, step });
    if (event.type === 'step/end') positions.set(session.id, { turn });
    if (event.type === 'turn/end') positions.delete(session.id);
  });
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const sessionId = agent.session.id;
    if (frame.type === 'start') {
      attempts.set(sessionId, { id: frame.attemptId, turn: frame.turn, step: frame.step });
    } else if (frame.type === 'end') {
      const active = attempts.get(sessionId);
      if (active?.id !== frame.attemptId) return;
      attempts.delete(sessionId);
      if (active.call) capture.event('calls', 'model_output', sessionId, {
        ...active.call, native_event_type: frame.outcome?.eventType ?? null,
        native_event_seq: frame.outcome?.seq ?? null, native_status: frame.outcome?.kind ?? null,
      });
    }
  });
  ctx.on('tools/execute', async (exec, next) => {
    if (!config.captureDirectory || !MEMORY_TOOLS.has(exec.name) || !exec.agent) return next();
    const sessionId = exec.agent.session.id;
    const operation = capture.run('memory_target', () => {
      if (!memoryRoot || physical(config.memoryRoot) !== memoryRoot) {
        throw new Error('Memory scope moved');
      }
      const root = memoryRoot;
      const requested = resolve(dirname(root), exec.arguments.file_path);
      const path = physical(requested);
      if (path === root || !inside(root, path)) return undefined;
      const peers = activeMemory.get(path) ?? new Set();
      const row = {
        operation_id: randomUUID(), query_request_id: queryIdentity(sessionId),
        tool_call_id: exec.callId, tool_name: exec.name, scope_path: relative(root, path),
        arguments: exec.arguments, observation_started_at: new Date().toISOString(),
      };
      const entry = { root, requested, path, row, peers, concurrent: peers.size > 0 };
      for (const peer of peers) peer.concurrent = true;
      peers.add(entry);
      activeMemory.set(path, peers);
      memoryCalls.set(exec.token, entry);
      capture.event('memory_operations', 'memory_before', sessionId, {
        ...row, before: snapshot(capture, config.captureDirectory, root, requested),
        proposed: exec.name === 'read' ? null : capture.run('memory_proposed', () => contentObject(
          capture, config.captureDirectory, Buffer.from(JSON.stringify(exec.arguments)), 'native_tool_arguments',
        )) ?? { status: 'capture_error' },
      });
      return entry;
    });
    try {
      return await next();
    } finally {
      if (operation) capture.run('memory_after', () => {
        const { root, requested, path, row, peers } = operation;
        const after = snapshot(capture, config.captureDirectory, root, requested);
        peers.delete(operation);
        if (!peers.size) activeMemory.delete(path);
        capture.event('memory_operations', 'memory_after', sessionId, {
          ...row, after, concurrent: operation.concurrent,
          observation_ended_at: new Date().toISOString(),
        });
      });
    }
  });
  ctx.on('tools/result', (exec, result) => {
    if (!config.captureDirectory || !MEMORY_TOOLS.has(exec.name) || !exec.agent) return;
    const sessionId = exec.agent.session.id;
    const operation = memoryCalls.get(exec.token);
    if (operation) {
      memoryCalls.delete(exec.token);
      capture.event('memory_operations', 'memory_result', sessionId, {
        ...operation.row, status: result.isError ? 'failed' : 'completed',
        canonical_value: result.value ?? null, rendered_content: result.content, error: result.error ?? null,
        readback: exec.name === 'read' && !result.isError ? capture.run('memory_readback', () => contentObject(
          capture, config.captureDirectory, Buffer.from(JSON.stringify(result.value)), 'native_read_result',
        )) ?? { status: 'capture_error' } : null,
      });
    } else {
      const path = capture.run('memory_path', () => exec.arguments?.file_path) ?? null;
      capture.event('memory_operations', 'memory_rejected', sessionId, {
        operation_id: randomUUID(), query_request_id: queryIdentity(sessionId),
        tool_call_id: exec.callId, tool_name: exec.name, requested_path: path,
        status: result.isError ? 'rejected' : 'outside_observed_scope', error: result.error ?? null,
      });
    }
  });
  // Include auxiliary calls such as compaction in the same usage ledger.
  ctx.on('llm/stream', async function* (options, next) {
    const row = {
      callId: randomUUID(), sessionId: options.sessionId,
      provider: options.provider, model: options.model, purpose: options.purpose,
      startedAt: new Date().toISOString(),
    };
    const active = attempts.get(options.sessionId);
    const nativeAttempt = options.purpose === undefined && active && !active.call ? active : undefined;
    const position = nativeAttempt ?? positions.get(options.sessionId);
    const requestPath = `request-${row.callId}.json`;
    const responsePath = `capture/responses/${row.callId}.jsonl`;
    const fields = {
      llm_call_id: row.callId, query_request_id: queryIdentity(row.sessionId),
      turn_id: position?.turn ?? null, step_id: position?.step ?? null,
      position_linkage: nativeAttempt ? 'native_assistant_attempt' : position ? 'session_scope' : null,
      native_attempt_id: nativeAttempt?.id ?? null, request_path: requestPath,
      response_path: config.captureDirectory ? responsePath : null, purpose: row.purpose ?? null,
      provider: row.provider, model: row.model,
    };
    if (nativeAttempt) nativeAttempt.call = fields;
    capture.activeCalls.add(row.callId);
    capture.event('calls', 'model_start', row.sessionId, { ...fields, status: 'started' });
    const request = Object.fromEntries([
      'provider', 'model', 'system', 'systemPrompt', 'messages', 'tools', 'toolHistory',
      'temperature', 'maxTokens', 'reasoningEffort', 'stop', 'purpose', 'sessionId',
    ].filter(key => options[key] !== undefined).map(key => [key, options[key]]));
    const runtime = dirname(config.usageFile);
    const recorded = capture.run('request', () => {
      capture.save(join(runtime, requestPath), JSON.stringify(request));
      return true;
    });
    const skillObserved = recorded && skillSection && !skillReceipts.has(row.sessionId) && nativeAttempt &&
      capture.run('skill_receipt', () => request.messages?.some(message => message.role === 'system' &&
        message.content.some(block => block.type === 'text' && block.text.includes(skillSection))));
    if (skillObserved) {
      skillReceipts.add(row.sessionId);
      capture.event('calls', 'skill_loaded', row.sessionId, {
        ...fields, sha256: config.skillSha256, method: 'system_prompt_section', status: 'observed_native_input',
      });
    }
    const writer = { chunks: [], bytes: 0, count: 0, failed: false };
    const flush = () => {
      if (!config.captureDirectory || !writer.chunks.length || writer.failed) return;
      writer.failed = capture.run('response', () => {
        appendFileSync(join(runtime, responsePath), writer.chunks.join(''), { mode: 0o600 });
        return true;
      }) !== true;
      writer.chunks.length = writer.bytes = 0;
    };
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') row.usage = chunk.usage;
        if (chunk.type === 'finish') row.finish = chunk.reason;
        if (config.captureDirectory && !writer.failed) {
          writer.failed = capture.run('response_chunk', () => {
            const line = `${JSON.stringify({ index: writer.count++, time_ms: Date.now(), chunk })}\n`;
            writer.chunks.push(line);
            writer.bytes += Buffer.byteLength(line);
            return true;
          }) !== true;
          if (writer.bytes >= 16384 || chunk.type === 'finish') flush();
        }
        yield chunk;
      }
    } catch (error) {
      row.error = String(error?.message ?? error);
      throw error;
    } finally {
      flush();
      capture.run('usage', () => capture.append(config.usageFile, row));
      const status = row.error || row.finish?.kind === 'error' ? 'failed'
        : row.finish?.kind === 'aborted' ? 'cancelled'
          : row.finish ? 'completed' : 'unterminated';
      capture.event('calls', 'model_end', row.sessionId, {
        ...fields, status, finish: row.finish ?? null, usage: row.usage ?? null,
        error: row.error ?? null, response_chunks: writer.count, response_capture_failed: writer.failed,
      });
      capture.activeCalls.delete(row.callId);
    }
  });
  ctx.effect(() => () => {
    try {
      for (const state of states.values()) {
        state.dispose();
        for (const dispose of state.restrictions) dispose();
      }
      states.clear();
    } finally {
      capture.health(true);
    }
  }, 'clbench-submit');
}
