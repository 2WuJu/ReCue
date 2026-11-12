#!/usr/bin/env node
// Shared entry point: no shell-specific environment activation or global Python.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const windows = process.platform === 'win32';
const usage = `Usage: node scripts/recue.mjs <setup|start|stop> [--service all|api|worker]

setup   Install locked Python/npm dependencies and build the frontend (network required).
start   Run in the foreground using uv --offline --locked (default service: all).
stop    Request shutdown; use the same --service and RECUE_DATA_DIR as start.

The entry point works on Windows, macOS and Linux. Transcription currently requires
Windows 10+. On macOS/Linux use start --service api for API/UI development only.`;

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
  const [action] = positionals;
  if (positionals.length !== 1 || !['setup', 'start', 'stop'].includes(action)) throw new Error(usage);
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
  if (action === 'start' && !windows && service !== 'api') {
    throw new Error('Transcription requires Windows 10+ (Job Object containment). Use start --service api for API/UI development on this platform.');
  }
  const uv = await uvCommand();
  if (action === 'setup') {
    await run(uv, ['python', 'install']);
    await run(uv, ['sync', '--locked']);
    await npm(['ci']);
    await npm(['run', 'build']);
    console.log('Setup complete. Run: node scripts/recue.mjs start');
  } else {
    const python = join(root, '.venv', windows ? 'Scripts/python.exe' : 'bin/python');
    if (!existsSync(python)) throw new Error('Python environment missing. Run node scripts/recue.mjs setup first.');
    mkdirSync(runtime, { recursive: true });
    await run(uv, ['run', '--offline', '--locked', 'python', '-m', 'app.run', '--service', service],
      join(root, 'backend'), requestStop);
  }
}

main().catch(error => {
  console.error(`ReCue: ${error.message}`);
  process.exitCode = 1;
});
