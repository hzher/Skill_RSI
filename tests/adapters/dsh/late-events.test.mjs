import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { apply } from '../../../skill_rsi/adapters/dsh/plugin.mjs';

test('late session events attach once and retain project isolation', async () => {
  const root = mkdtempSync(join(tmpdir(), 'skill-rsi-late-event-'));
  try {
    const project = join(root, 'project'), other = join(root, 'other');
    mkdirSync(project); mkdirSync(other);
    const registration = join(root, 'registration.json');
    writeFileSync(registration, JSON.stringify({
      observe: true, bridge_python: process.execPath, registration_id: 'fixture',
    }));
    const bridge = join(root, 'bridge.cjs');
    writeFileSync(bridge, `
      const fs = require('fs');
      const mode = process.argv.find(value => ['capture', 'desktop', 'hook'].includes(value));
      const row = JSON.parse(fs.readFileSync(0, 'utf8'));
      fs.appendFileSync(${JSON.stringify(join(root, 'bridge.jsonl'))}, JSON.stringify({mode, row}) + '\\n');
      console.log(JSON.stringify(mode === 'capture' ? {capture_directory: ${JSON.stringify(join(root, 'capture'))}} : {}));
    `);
    const runtime = join(root, 'node_modules', '@deepseek-ai', 'dsh-llm');
    mkdirSync(runtime, { recursive: true });
    writeFileSync(join(runtime, 'package.json'), JSON.stringify({main: 'index.cjs'}));
    writeFileSync(join(runtime, 'index.cjs'), 'exports.isAgentLoopRequest=()=>true;exports.markAgentLoopRequest=x=>x;');
    writeFileSync(join(root, 'package.json'), '{}');
    const handlers = new Map(), cleanup = [];
    const ctx = {
      on(name, callback) { handlers.set(name, callback); },
      effect(callback) { const dispose = callback(); if (typeof dispose === 'function') cleanup.push(dispose); },
      llm: { async prepareCall() { return { stream() {} }; } },
    };
    apply(ctx, {project, desktop: true, desktopDigest: 'fixture', registrationPath: registration,
      brandingAnchor: join(root, 'package.json'), argv: [process.execPath, bridge, '--unused', 'hook', '--owner', 'fixture']});
    const session = {id: 'already-running', header: {cwd: project}};
    handlers.get('session/event')({id: 'other-project', header: {cwd: other}}, {type: 'user/message'});
    let calls = readFileSync(join(root, 'bridge.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(calls.filter(call => call.mode === 'capture').length, 0);
    handlers.get('session/event')(session, {seq: 1, type: 'user/message', data: {text: 'late query'}});
    handlers.get('session/event')(session, {seq: 2, type: 'compaction/end', data: {summary: 'preserved'}});
    let registrations = 0;
    const agent = {session, ctx: {tools: {register() { registrations++; return () => {}; }}}};
    for (const id of ['tool-1', 'tool-2']) {
      await handlers.get('tools/execute')({agent, callId: id, name: 'shell', arguments: {}}, async () => ({text: 'result'}));
    }
    handlers.get('agent/created')({agent});
    handlers.get('agent/created')({agent});
    assert.equal(registrations, 1);
    for await (const chunk of handlers.get('llm/stream')({sessionId: session.id, messages: []}, async function* () {
      yield {type: 'text', text: 'reply'};
      yield {type: 'finish', reason: {kind: 'completed'}};
    })) {}
    calls = readFileSync(join(root, 'bridge.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(calls.filter(call => call.mode === 'capture').length, 1);
    const events = readFileSync(join(root, 'capture', 'session-events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(events.filter(row => row.kind === 'session_attached').length, 1);
    assert.deepEqual(events.filter(row => row.kind === 'session_event').map(row => row.event.type), ['user/message', 'compaction/end']);
    assert.match(readFileSync(join(root, 'capture', 'calls.jsonl'), 'utf8'), /model_end/);
    handlers.get('agent/disposed')({agent});
    for (const dispose of cleanup.reverse()) dispose();
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
});
