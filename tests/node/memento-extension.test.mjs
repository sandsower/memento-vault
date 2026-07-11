import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, chmod } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const repoRoot = resolve(__dirname, '..', '..');
const configModuleUrl = pathToFileURL(join(repoRoot, 'extensions', 'memento-config.js')).href;
const workerModuleUrl = pathToFileURL(join(repoRoot, 'extensions', 'memento-process-worker.mjs')).href;
const extensionPath = join(repoRoot, 'extensions', 'memento.ts');
const uiPath = join(repoRoot, 'extensions', 'memento-ui.ts');
const workerPath = join(repoRoot, 'extensions', 'memento-process-worker.mjs');

async function withCleanPiBridgeEnv(fn) {
  const names = [
    'HOME',
    'MEMENTO_PI_ENABLED',
    'MEMENTO_PI_BRIEFING',
    'MEMENTO_PI_PROMPT_RECALL',
    'MEMENTO_PI_TOOL_CONTEXT',
    'MEMENTO_PI_AUTO_CAPTURE',
    'MEMENTO_PI_CAPTURE_QUEUE',
    'MEMENTO_PI_PROCESS_QUEUE',
    'MEMENTO_PI_PROCESS_QUEUE_ON_SESSION_CLOSE',
    'MEMENTO_PI_QUEUE_SUMMARIES',
    'MEMENTO_PI_PROCESS_QUEUE_MAX_CAPTURES',
    'MEMENTO_PI_PROCESS_QUEUE_MODEL',
    'MEMENTO_PI_MAX_INJECTED_CHARS',
    'MEMENTO_PI_MAX_TOOL_CONTEXT_PER_SESSION',
  ];
  const previous = new Map(names.map((name) => [name, process.env[name]]));
  for (const name of names) delete process.env[name];
  try {
    return await fn();
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

test('memento-config layers defaults, home config, project config, package config, and env', async () => {
  await withCleanPiBridgeEnv(async () => {
    const root = await mkdtemp(join(tmpdir(), 'memento-config-test-'));
    const home = join(root, 'home');
    const project = join(root, 'project');
    await mkdir(join(home, '.config', 'memento-vault'), { recursive: true });
    await mkdir(join(project, '.pi'), { recursive: true });
    await writeFile(
      join(home, '.config', 'memento-vault', 'pi-bridge.json'),
      JSON.stringify({ memento: { piBridge: { briefing: false, toolContext: false, autoCapture: false, processQueueMaxCaptures: 9 } } }),
    );
    await writeFile(join(project, '.pi', 'settings.json'), JSON.stringify({ piBridge: { autoCapture: true, processQueueOnSessionClose: true } }));
    await writeFile(join(project, 'package.json'), JSON.stringify({ memento: { piBridge: { promptRecall: false, processQueueModel: null } } }));

    process.env.HOME = home;
    process.env.MEMENTO_PI_TOOL_CONTEXT = 'true';
    process.env.MEMENTO_PI_QUEUE_SUMMARIES = 'true';
    process.env.MEMENTO_PI_MAX_INJECTED_CHARS = '1234';
    const { loadConfig } = await import(`${configModuleUrl}?case=config-layering-${Date.now()}`);
    const payload = loadConfig(project);

    assert.equal(payload.config.briefing, false);
    assert.equal(payload.config.promptRecall, false);
    assert.equal(payload.config.toolContext, true);
    assert.equal(payload.config.autoCapture, true);
    assert.equal(payload.config.processQueueOnSessionClose, true);
    assert.equal(payload.config.queueSummaries, true);
    assert.equal(payload.config.processQueueMaxCaptures, 9);
    assert.equal(payload.config.processQueueModel, null);
    assert.equal(payload.config.maxInjectedChars, 1234);
    assert.equal(payload.sources[0], 'defaults');
    assert.match(payload.sources[1], /pi-bridge\.json$/);
    assert.match(payload.sources[2], /\.pi[/\\]settings\.json$/);
    assert.match(payload.sources[3], /package\.json$/);
    assert.equal(payload.sources[4], 'environment');
  });
});

test('memento TypeScript extension wires lifecycle events, tools, queue behavior, and deferred worker spawn', async () => {
  const source = await readFile(extensionPath, 'utf8');
  for (const eventName of [
    'session_start',
    'before_agent_start',
    'tool_result',
    'agent_end',
    'session_before_compact',
    'session_compact',
    'session_shutdown',
  ]) {
    assert.match(source, new RegExp(`pi\\.on\\("${eventName}"`), `${eventName} should be registered`);
  }
  for (const toolName of ['memento_status', 'memento_session_context', 'memento_search', 'memento_queue', 'memento_process']) {
    assert.match(source, new RegExp(`name:\\s*"${toolName}"`), `${toolName} tool should be registered`);
  }
  assert.match(source, /registerCommand\("memento-capture"/);
  assert.match(source, /registerCommand\("memento"/);
  assert.match(source, /memento_process requires explicit selection/);
  assert.match(source, /withProcessLimit\(processArgsFromParams/);
  assert.match(source, /config\.processQueueMaxCaptures/);
  assert.match(source, /config\.queueSummaries/);
  assert.match(source, /--include-generated-summaries/);
  assert.match(source, /includeGeneratedSummaries/);
  assert.match(source, /join\(__dirname, "memento-process-worker\.mjs"\)/);
  assert.match(source, /pi\.exec\("node", \[worker, \.\.\.workerArgs\]/);
  assert.match(source, /processQueueModel \? \["--processor-model", config\.processQueueModel/);
  assert.match(source, /--max-injected-chars/);
  assert.match(source, /async function runLifecycle[\s\S]*const boundedArgs[\s\S]*--max-injected-chars/);
  const runJsonSource = source.slice(source.indexOf('async function runJson'), source.indexOf('async function runLifecycle'));
  assert.doesNotMatch(runJsonSource, /boundedArgs|--max-injected-chars/);
  assert.doesNotMatch(source, /capText\(briefing\.content/);
  assert.doesNotMatch(source, /capText\(recall\.content/);
  assert.doesNotMatch(source, /capText\(toolContext\.content/);

  const uiSource = await readFile(uiPath, 'utf8');
  assert.match(uiSource, /groupStatus === "running" && group\.log_tail/, 'running groups should render a live log tail');
  assert.match(uiSource, /inspected\.log_tail/, 'inspected groups should render a larger log tail');
  assert.match(uiSource, /group\.log_error/, 'groups should render missing or unavailable log errors');
  assert.match(uiSource, /function formatLogTail/, 'log-tail formatting should stay factored and bounded');
});

test('curator result parser accepts sentinel-wrapped JSON and classifies malformed output', async () => {
  const { RESULT_START, RESULT_END, parseCuratorResult } = await import(`${workerModuleUrl}?case=parse-${Date.now()}`);
  const good = parseCuratorResult('group-1', `${RESULT_START}\n{"group_id":"group-1","processed_capture_ids":["cap-1"],"status":"processed","created":[],"skipped_duplicates":[]}\n${RESULT_END}`);
  assert.equal(good.state, 'success');
  assert.equal(good.protocol, 'sentinel');
  assert.deepEqual(good.parsed.processed_capture_ids, ['cap-1']);

  assert.equal(parseCuratorResult('group-1', '').state, 'no_output');
  assert.equal(parseCuratorResult('group-1', '{"group_id":"group-1"}').state, 'malformed_output');
  assert.equal(parseCuratorResult('group-1', `${RESULT_START}{"group_id":"group-1"}`).state, 'partial_write');
  assert.match(parseCuratorResult('group-1', `${RESULT_START}{"group_id":"other","processed_capture_ids":[],"status":"processed"}${RESULT_END}`).error, /group_id/);
  assert.match(parseCuratorResult('group-1', `${RESULT_START}{"group_id":"group-1","processed_capture_ids":[],"status":"bogus"}${RESULT_END}`).error, /valid status/);
});

async function writeFakeBridge(root, runBase) {
  const fakeBridge = join(root, 'fake-bridge.mjs');
  await writeFile(fakeBridge, `
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const args = process.argv.slice(2);
const base = process.env.MEMENTO_TEST_RUN_BASE;
if (!base) throw new Error('missing MEMENTO_TEST_RUN_BASE');
const runDir = join(base, 'run-1');
const group = {
  group_id: 'group-1',
  capture_ids: ['cap-1'],
  session_id: 'session-1',
  project: 'project-1',
  branch: 'branch-1',
  cwd: base,
  input_json: join(runDir, 'group-1-input.json'),
  input_markdown: join(runDir, 'group-1-input.md'),
  result_json: join(runDir, 'group-1-result.json'),
  log_markdown: join(runDir, 'group-1-log.md'),
  transcript: { mode: 'included' },
};
if (args.includes('process-start')) {
  mkdirSync(runDir, { recursive: true });
  writeFileSync(group.input_json, JSON.stringify({ captures: [{ id: 'cap-1', metadata: { vault_path: join(base, 'vault') } }] }));
  writeFileSync(group.input_markdown, '# Group input\\n');
  writeFileSync(join(runDir, 'manifest.json'), JSON.stringify({
    created_at: '2026-06-30T00:00:00Z',
    selected_capture_count: 1,
    group_count: 1,
    groups: [group],
  }));
  console.log(JSON.stringify({ run_id: 'run-1', run_dir: runDir, selected_capture_count: 1 }));
} else if (args.includes('process-finalize')) {
  const result = JSON.parse(readFileSync(group.result_json, 'utf8'));
  console.log(JSON.stringify({
    dequeued: result.processed_capture_ids.length,
    remaining: 0,
    groups: [{ group_id: group.group_id, status: result.status, dequeued_capture_ids: result.processed_capture_ids }],
  }));
} else {
  console.log(JSON.stringify({ error: 'unexpected fake bridge args', args }));
  process.exitCode = 2;
}
`);
  return fakeBridge;
}

test('process worker can run deterministic fake-curator flow through process-start and process-finalize', async () => {
  const root = await mkdtemp(join(tmpdir(), 'memento-worker-test-'));
  const fakeBin = join(root, 'bin');
  const runBase = join(root, 'runs');
  await mkdir(fakeBin, { recursive: true });
  await mkdir(runBase, { recursive: true });

  const fakeBridge = await writeFakeBridge(root, runBase);
  const fakePython = join(fakeBin, 'python3');
  await writeFile(fakePython, `#!/usr/bin/env bash\nexec node ${JSON.stringify(fakeBridge)} "$@"\n`);
  await chmod(fakePython, 0o755);

  const result = spawnSync('node', [workerPath, '--fake-curator', 'processed', '--id', 'cap-1'], {
    cwd: repoRoot,
    encoding: 'utf8',
    env: { ...process.env, PATH: `${fakeBin}:${process.env.PATH}`, MEMENTO_TEST_RUN_BASE: runBase },
    timeout: 30_000,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.run_id, 'run-1');
  assert.equal(payload.dequeued, 1);
  assert.deepEqual(payload.processed_groups, ['group-1']);

  const resultJson = JSON.parse(await readFile(join(runBase, 'run-1', 'group-1-result.json'), 'utf8'));
  assert.equal(resultJson.group_id, 'group-1');
  assert.deepEqual(resultJson.processed_capture_ids, ['cap-1']);
  assert.equal(resultJson.status, 'processed_no_notes');
  assert.equal(resultJson.discard_reason, 'fake curator test adapter did not create notes');
  const progress = JSON.parse(await readFile(join(runBase, 'run-1', 'progress.json'), 'utf8'));
  assert.equal(progress.status, 'finalized');
  assert.equal(progress.groups[0].status, 'processed_no_notes');
  assert.ok(existsSync(join(runBase, 'run-1', 'group-1-log.md')));
});

test('process worker streams real curator stdout and stderr into the group log', async () => {
  const root = await mkdtemp(join(tmpdir(), 'memento-worker-stream-test-'));
  const fakeBin = join(root, 'bin');
  const runBase = join(root, 'runs');
  await mkdir(fakeBin, { recursive: true });
  await mkdir(runBase, { recursive: true });

  const fakeBridge = await writeFakeBridge(root, runBase);
  const fakePython = join(fakeBin, 'python3');
  await writeFile(fakePython, `#!/usr/bin/env bash\nexec node ${JSON.stringify(fakeBridge)} "$@"\n`);
  await chmod(fakePython, 0o755);
  const fakePi = join(fakeBin, 'pi');
  await writeFile(fakePi, `#!/usr/bin/env bash\necho "curator stdout before result"\necho "curator stderr line" >&2\ncat <<'JSON'\n<<<MEMENTO_PROCESS_RESULT_START>>>\n{"group_id":"group-1","processed_capture_ids":["cap-1"],"status":"processed","created":[{"title":"Note","path":"notes/note.md"}],"skipped_duplicates":[]}\n<<<MEMENTO_PROCESS_RESULT_END>>>\nJSON\n`);
  await chmod(fakePi, 0o755);

  const result = spawnSync('node', [workerPath, '--id', 'cap-1'], {
    cwd: repoRoot,
    encoding: 'utf8',
    env: { ...process.env, PATH: `${fakeBin}:${process.env.PATH}`, MEMENTO_TEST_RUN_BASE: runBase },
    timeout: 30_000,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.dequeued, 1);

  const resultJson = JSON.parse(await readFile(join(runBase, 'run-1', 'group-1-result.json'), 'utf8'));
  assert.equal(resultJson.status, 'processed');
  assert.deepEqual(resultJson.created, [{ title: 'Note', path: 'notes/note.md' }]);
  const log = await readFile(join(runBase, 'run-1', 'group-1-log.md'), 'utf8');
  assert.match(log, /Streaming stdout\/stderr/);
  assert.match(log, /\[stdout\] curator stdout before result/);
  assert.match(log, /\[stderr\] curator stderr line/);
});
