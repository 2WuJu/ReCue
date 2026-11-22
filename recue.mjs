#!/usr/bin/env node
// Shared entry point: no shell-specific environment activation or global Python.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = dirname(fileURLToPath(import.meta.url));
const windows = process.platform === 'win32';
const supportsWorker = windows || process.platform === 'darwin';
const usage = `Usage: node recue.mjs [launch|setup|start|stop] [--service all|api|worker]

setup   Install locked Python/npm dependencies and build the frontend (network required).
launch  Start ReCue, preparing dependencies and model configuration on first use (default).
start   Run in the foreground using uv --offline --locked (default service: all).
stop    Request shutdown; use the same --service and RECUE_DATA_DIR as start.

Transcription supports Windows 10+ and macOS. On Linux use --service api
for API/UI development.`;

function run(command, args, cwd = root, onSignal) {
  return new Promise((accept, reject) => {
    const child = spawn(command, args, { cwd, stdio: 'inherit', windowsHide: true });
    const shutdown = () => onSignal?.();
    // The supervisor owns service shutdown. A marker also works on Windows,
    // where sending SIGTERM directly would forcefully terminate it.
    if (onSignal) {
      process.on('SIGINT', shutdown);
      process.on('SIGTERM', shutdown);
    }
    const cleanup = () => {
      process.off('SIGINT', shutdown);
      process.off('SIGTERM', shutdown);
    };
    child.once('error', error => {
      cleanup();
      reject(new Error(`Cannot run ${command}: ${error.message}. Check that it is installed and on PATH.`));
    });
    child.once('exit', (code, signal) => {
      cleanup();
      if (code === 0) accept();
      else reject(new Error(`${command} exited with ${signal ?? code}`));
    });
  });
}

function uvCommand() {
  const fallback = join(homedir(), '.local', 'bin', windows ? 'uv.exe' : 'uv');
  // Prefer PATH, including a user-selected installation, over the fallback.
  return new Promise(accept => {
    const probe = spawn('uv', ['--version'], { stdio: 'ignore', windowsHide: true });
    probe.once('error', () => accept(existsSync(fallback) ? fallback : 'uv'));
    probe.once('exit', code => accept(code === 0 ? 'uv' : (existsSync(fallback) ? fallback : 'uv')));
  });
}

function npm(args) {
  // Windows npm is a .cmd file. Only fixed commands pass through cmd.exe;
  // user arguments and paths are never interpolated into shell text.
  return windows
    ? run(process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', `npm ${args.join(' ')}`], join(root, 'frontend'))
    : run('npm', args, join(root, 'frontend'));
}

async function main() {
  const { values, positionals } = parseArgs({
    options: { service: { type: 'string' }, help: { type: 'boolean', short: 'h' } },
    allowPositionals: true,
  });
  if (values.help) return console.log(usage);
  const [action = 'launch'] = positionals;
  if (positionals.length > 1 || !['setup', 'launch', 'start', 'stop'].includes(action)) throw new Error(usage);
  const service = values.service ?? 'all';
  if (!['all', 'api', 'worker'].includes(service)) throw new Error('Service must be all, api or worker.');
  if (action === 'setup' && values.service) throw new Error('setup does not accept --service.');
  const configuredData = process.env.RECUE_DATA_DIR;
  if (configuredData && !isAbsolute(configuredData)) throw new Error('RECUE_DATA_DIR must be an absolute path.');
  const runtime = join(configuredData || join(root, 'data'), 'runtime');
  const requestStop = () => {
    if (!existsSync(runtime)) return console.log('No service runtime found.');
    writeFileSync(join(runtime, `${service}.stop`), 'stop');
    console.log(`Stop requested for launcher '${service}'; wait for the start terminal to exit.`);
  };
  if (action === 'stop') return requestStop();
  if (action !== 'setup' && !supportsWorker && service !== 'api') {
    throw new Error('Transcription requires Windows 10+ or macOS. Use node recue.mjs --service api for API/UI development on this platform.');
  }
  // Keep Windows executable lookup from implicitly searching the working directory.
  if (windows) process.env.NoDefaultCurrentDirectoryInExePath = '1';
  const uv = await uvCommand();
  const python = join(root, '.venv', windows ? 'Scripts/python.exe' : 'bin/python');
  const needsSetup = !existsSync(python) || !existsSync(join(root, 'frontend', 'dist', 'index.html'));
  if (action === 'setup' || (action === 'launch' && needsSetup)) {
    await run(uv, ['python', 'install']);
    await run(uv, ['sync', '--locked']);
    await npm(['ci']);
    await npm(['run', 'build']);
    if (action === 'setup') return console.log('Setup complete. Run: node recue.mjs');
  }
  if (!existsSync(python)) throw new Error('Python environment missing. Run node recue.mjs setup first.');
  if (action === 'launch' && service !== 'api' && !process.env.RECUE_PROCESSING_CONFIG) {
    const config = join(root, 'processing.local.json');
    if (!existsSync(config)) {
      await run(python, ['-X', 'utf8', '-m', 'app.model_setup'], join(root, 'backend'));
    }
    process.env.RECUE_PROCESSING_CONFIG = config;
    console.log(`使用转写配置 ${config}`);
  }
  mkdirSync(runtime, { recursive: true });
  await run(uv, ['run', '--offline', '--locked', 'python', '-m', 'app.run', '--service', service],
    join(root, 'backend'), requestStop);
}

main().catch(error => {
  console.error(`ReCue: ${error.message}`);
  process.exitCode = 1;
});
