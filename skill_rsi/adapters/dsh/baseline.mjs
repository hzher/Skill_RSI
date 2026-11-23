// Manual focus/restore semantics adapted from Skill_RSI_hzh/plugins/clbench-submit/context-{view,edit}.mjs (MIT).
const MAX_SOURCES = 24;
const MAX_BYTES = 512 * 1024;
const sourceSpec = { type: 'string', minLength: 1 };
const lineSpec = { type: 'integer', minimum: 1 };

function focused(text, start, end) {
  const lines = text.split('\n');
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start || end > lines.length) {
    return null;
  }
  const kept = lines.slice(start - 1, end).join('\n');
  const rendered = (start > 1 ? '[…]\n' : '') + kept + (end < lines.length ? '\n[…]' : '');
  return Buffer.byteLength(rendered) < Buffer.byteLength(text) ? rendered : null;
}

export function baselineState() {
  return { entries: new Map(), projected: new Map(), version: 0 };
}

export function seeToolResult(state, callId, text) {
  if (state.entries.get(callId) === text || Buffer.byteLength(text) > MAX_BYTES) return;
  state.entries.set(callId, text);
  if (state.entries.size > MAX_SOURCES) {
    const oldest = state.entries.keys().next().value;
    state.entries.delete(oldest);
    state.projected.delete(oldest);
  }
  state.version++;
}

export function registerBaseline(agent, state) {
  const register = (suffix, description, properties, required, handle) => agent.ctx.tools.register({
    name: 'skill_rsi_baseline_' + suffix,
    description,
    parameters: { type: 'object', properties, required, additionalProperties: false },
    output: { schema: { type: 'string' }, render: (_args, text) => [{ type: 'text', text }] },
    async execute(args, call) {
      call.signal?.throwIfAborted();
      return JSON.stringify(handle(args));
    },
  });
  const original = callId => {
    if (!state.entries.has(callId)) throw new Error('Identifier is outside the available tool result history.');
    return state.entries.get(callId);
  };
  const current = ({ version }) => {
    if (version !== state.version) throw new Error('Tool view version changed. Refresh the catalog.');
  };
  const versionSpec = { type: 'integer', minimum: 0 };
  const disposers = [
    register('list', 'List recent tool results and the current view version.', {}, [], () => ({
      version: state.version,
      results: Array.from(state.entries, ([call_id, text]) => ({
        call_id, lines: text.split('\n').length, focused: state.projected.has(call_id),
        preview: text.split('\n').filter(line => line.trim()).slice(-2).map(line => line.slice(0, 160)),
      })),
    })),
    register('read', 'Read original lines from a recent tool result. Return at most 40 lines and 12 KiB.',
      { call_id: sourceSpec, start_line: lineSpec, end_line: lineSpec }, ['call_id', 'start_line', 'end_line'], args => {
        const lines = original(args.call_id).split('\n');
        const { start_line: start, end_line: end } = args;
        if (start > end || end > lines.length) throw new Error('Line range is outside the source.');
        const through = Math.min(start + 39, end);
        const text = lines.slice(start - 1, through).join('\n');
        if (Buffer.byteLength(text) > 12288) throw new Error('Choose a smaller range of lines.');
        return { text, start_line: start, end_line: through, next_line: through < end ? through + 1 : null };
      }),
    register('focus', 'Shorten later primary requests to an inclusive range of original result lines.',
      { call_id: sourceSpec, start_line: lineSpec, end_line: lineSpec, version: versionSpec },
      ['call_id', 'start_line', 'end_line', 'version'], args => {
        current(args);
        const view = focused(original(args.call_id), args.start_line, args.end_line);
        if (view === null) throw new Error('Choose an in-bounds range that shortens the output.');
        state.projected.set(args.call_id, view);
        return { version: ++state.version, call_id: args.call_id, focused: true };
      }),
    register('restore', 'Restore a recent tool result to its original view.',
      { call_id: sourceSpec, version: versionSpec }, ['call_id', 'version'], args => {
        current(args);
        original(args.call_id);
        if (state.projected.delete(args.call_id)) state.version++;
        return { version: state.version, call_id: args.call_id, focused: false };
      }),
  ];
  return () => disposers.forEach(dispose => dispose());
}
