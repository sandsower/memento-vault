#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const repoRoot = resolve(__dirname, '..');

function runBridge(args) {
  const result = spawnSync('python3', ['-m', 'memento.pi_bridge', ...args], {
    cwd: repoRoot,
    encoding: 'utf8',
    env: process.env,
  });
  if (result.status !== 0) {
    throw new Error(`pi_bridge failed (${result.status}): ${result.stderr}`);
  }
  return JSON.parse(result.stdout);
}

function argValue(args, flag) {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : undefined;
}

async function fakeCurator(group, mode) {
  const resultPath = group.result_json;
  const captureIds = group.capture_ids ?? [];
  if (mode === 'processed') {
    const vaultPath = JSON.parse(readFileSync(resolve(group.input_json), 'utf8')).captures?.[0]?.metadata?.vault_path;
    void vaultPath;
  }
  writeFileSync(resultPath, JSON.stringify({
    group_id: group.group_id,
    processed_capture_ids: captureIds,
    status: 'processed_no_notes',
    created: [],
    skipped_duplicates: [],
    discard_reason: 'fake curator test adapter did not create notes',
  }, null, 2));
  writeFileSync(group.log_markdown, `# Fake curator\n\nProcessed ${captureIds.length} capture(s).\n`);
}

function mementoSkillFallback() {
  return `Capture durable session knowledge as atomic Memento Vault notes. Search first to avoid duplicates. Use memento_capture for each durable idea. Notes should cover one decision, discovery, pattern, bugfix, or tool insight. Sanitize secrets. Skip raw transcript fragments, command chatter, and non-durable details. Zero notes is acceptable when there is no reusable future context.`;
}

async function realCurator(group) {
  const input = readFileSync(group.input_markdown, 'utf8');
  const curatorCwd = group.cwd || repoRoot;
  const prompt = `/skill:memento\n\n${mementoSkillFallback()}\n\nYou are processing a queued pi session group for Memento. Create zero or more curated notes using the existing memento_capture tool. Do not write raw transcript notes. Do not edit the queue. Preserve the original project/cwd/branch/session metadata from the input packet in captured note bodies when relevant. Final answer must be ONLY a JSON object with this shape:\n{\n  "group_id": ${JSON.stringify(group.group_id)},\n  "processed_capture_ids": ${JSON.stringify(group.capture_ids ?? [])},\n  "status": "processed" | "processed_no_notes",\n  "created": [{"title":"...","path":"notes/..."}],\n  "skipped_duplicates": [{"title":"...","existing_path":"notes/..."}],\n  "discard_reason": "required when processed_no_notes"\n}\n\nInput packet:\n\n${input}`;
  const args = [
    '-p',
    '--no-session',
    '--no-builtin-tools',
    '--tools',
    'memento_status,memento_search,memento_get,memento_capture',
    '-e',
    resolve(__dirname, 'memento.ts'),
  ];
  const model = process.env.MEMENTO_PI_PROCESS_QUEUE_MODEL || group.processor_model;
  if (model) args.push('--model', model);
  args.push(prompt);
  const result = spawnSync('pi', args, {
    cwd: curatorCwd,
    encoding: 'utf8',
    env: {
      ...process.env,
      MEMENTO_PI_AUTO_CAPTURE: 'false',
      MEMENTO_PI_CAPTURE_QUEUE: 'false',
      MEMENTO_PI_PROCESSOR: 'true',
    },
    maxBuffer: 50 * 1024 * 1024,
  });
  writeFileSync(group.log_markdown, `# Curator output\n\n## stdout\n\n${result.stdout}\n\n## stderr\n\n${result.stderr}\n`);
  if (result.status !== 0) throw new Error(`pi curator failed (${result.status}): ${result.stderr}`);
  const match = result.stdout.match(/\{[\s\S]*"processed_capture_ids"[\s\S]*\}/m);
  if (!match) throw new Error('curator did not return result JSON');
  writeFileSync(group.result_json, match[0]);
}

async function main() {
  const args = process.argv.slice(2);
  const fakeMode = argValue(args, '--fake-curator');
  const processorModel = argValue(args, '--processor-model');
  const bridgeArgs = args.filter((arg, index) => !['--fake-curator', '--processor-model'].includes(arg) && !['--fake-curator', '--processor-model'].includes(args[index - 1]));
  const start = runBridge(['queue', 'process-start', '--owner-pid', String(process.pid), ...bridgeArgs]);
  if (start.error || !start.run_id) {
    console.log(JSON.stringify(start));
    return;
  }
  const manifest = JSON.parse(readFileSync(resolve(start.run_dir, 'manifest.json'), 'utf8'));
  const processed = [];
  for (const group of manifest.groups ?? []) {
    try {
      if (processorModel) group.processor_model = processorModel;
      if (fakeMode) await fakeCurator(group, fakeMode);
      else await realCurator(group);
      processed.push(group.group_id);
    } catch (error) {
      const message = String(error?.message ?? error);
      writeFileSync(group.result_json, JSON.stringify({
        group_id: group.group_id,
        processed_capture_ids: group.capture_ids ?? [],
        status: 'failed',
        created: [],
        skipped_duplicates: [],
        error: message,
      }, null, 2));
      writeFileSync(group.log_markdown, `# Curator failure\n\n${message}\n\n${error?.stack ?? ''}\n`);
    }
  }
  const finalized = runBridge(['queue', 'process-finalize', '--run-id', start.run_id]);
  console.log(JSON.stringify({ ...finalized, run_id: start.run_id, run_dir: start.run_dir, processed_groups: processed }, null, 2));
}

main().catch((error) => {
  console.log(JSON.stringify({ error: String(error?.message ?? error), stack: error?.stack }, null, 2));
  process.exitCode = 1;
});
