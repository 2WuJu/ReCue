#!/usr/bin/env node
// Shared entry point: no shell-specific environment activation or global Python.
import { spawn, spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, networkInterfaces } from 'node:os';
import { dirname, isAbsolute, join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = dirname(fileURLToPath(import.meta.url));
const windows = process.platform === 'win32';
const supportsWorker = windows || process.platform === 'darwin';
const usage = `Usage: node recue.mjs [launch|setup|configure|start|stop] [--service all|api|worker]

setup   Install locked Python/npm dependencies and build the frontend (network required).
launch  Start ReCue, preparing dependencies and model configuration on first use (default).
configure  Choose local-only or LAN access for the next launch.
start   Run in the foreground using uv --offline --locked (default service: all).
stop    Request shutdown; use the same --service and RECUE_DATA_DIR as start.

Transcription supports Windows 10+ and macOS. On Linux use --service api
for API/UI development.`;

function readNetworkMode(path) {
  try {
    const mode = readFileSync(path, 'utf8').trim();
    return ['local', 'lan'].includes(mode) ? mode : undefined;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
}

async function configureNetwork(path, currentMode) {
  const defaultMode = currentMode ?? 'local';
  const input = createInterface({ input: process.stdin, output: process.stdout, crlfDelay: Infinity });
  input.on('SIGINT', () => input.close());
  console.log('ReCue 访问设置');
  console.log('  1. 仅本机使用');
  console.log('  2. 允许局域网访问（能连上 ReCue 的设备可以查看、上传和删除媒体）');
  console.log(`输入 1 或 2 后按回车，直接回车${currentMode ? '保留当前设置' : '选择仅本机使用'}。`);
  try {
    for await (const line of input) {
      const answer = line.trim();
      const mode = { '': defaultMode, '1': 'local', '2': 'lan' }[answer];
      if (mode !== 'local' && mode !== 'lan') {
        console.log('请输入 1 或 2。');
        continue;
      }
      mkdirSync(dirname(path), { recursive: true });
      const temporary = `${path}.${randomUUID()}.tmp`;
      try {
        writeFileSync(temporary, `${mode}\n`, { flag: 'wx' });
        renameSync(temporary, path);
      } finally {
        rmSync(temporary, { force: true });
      }
      console.log(`已保存：${mode === 'lan' ? '允许局域网访问' : '仅本机使用'}。`);
      return mode;
    }
    console.log('已取消访问设置。');
  } finally {
    input.close();
  }
}

function lanInterfaces() {
  const labels = new Map();
  const mac = process.platform === 'darwin';
  if (mac) {
    const { stdout } = spawnSync('/usr/sbin/networksetup', ['-listallhardwareports'], {
      encoding: 'utf8', timeout: 2000,
    });
    for (const [, label, device] of (stdout ?? '').matchAll(/^Hardware Port: (.+)\r?\nDevice: (\S+)/gm)) {
      labels.set(device, label);
    }
  }
  const interfaces = new Map();
  for (const [name, addresses] of Object.entries(networkInterfaces())) {
    if (mac && name.startsWith('utun')) continue;
    for (const { address, internal, family } of addresses) {
      if (internal || family !== 'IPv4') continue;
      const [first, second] = address.split('.').map(Number);
      if (first === 10 || (first === 172 && second >= 16 && second <= 31) || (first === 192 && second === 168)) {
        interfaces.set(address, labels.get(name) ?? name);
      }
    }
  }
  return [...interfaces];
}

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
  if (positionals.length > 1 || !['setup', 'configure', 'launch', 'start', 'stop'].includes(action)) throw new Error(usage);
  const service = values.service ?? 'all';
  if (!['all', 'api', 'worker'].includes(service)) throw new Error('Service must be all, api or worker.');
  if (['setup', 'configure'].includes(action) && values.service) throw new Error(`${action} does not accept --service.`);
  const configuredData = process.env.RECUE_DATA_DIR;
  if (configuredData && !isAbsolute(configuredData)) throw new Error('RECUE_DATA_DIR must be an absolute path.');
  const data = configuredData || join(root, 'data');
  const runtime = join(data, 'runtime');
  const networkModePath = join(data, 'network-mode');
  if (action === 'configure') {
    const mode = await configureNetwork(networkModePath, readNetworkMode(networkModePath));
    if (mode && process.env.RECUE_HOST) console.log('下次启动前请清除 RECUE_HOST，否则会沿用手动指定的地址。');
    else if (mode) console.log('下次运行 node recue.mjs 时生效。');
    return;
  }
  const requestStop = () => {
    if (!existsSync(runtime)) return console.log('No service runtime found.');
    writeFileSync(join(runtime, `${service}.stop`), 'stop');
    console.log(`Stop requested for launcher '${service}'; wait for the start terminal to exit.`);
  };
  if (action === 'stop') return requestStop();
  if (action !== 'setup' && !supportsWorker && service !== 'api') {
    throw new Error('Transcription requires Windows 10+ or macOS. Use node recue.mjs --service api for API/UI development on this platform.');
  }
  let networkMode;
  if (action !== 'setup' && service !== 'worker' && !process.env.RECUE_HOST) {
    networkMode = readNetworkMode(networkModePath);
    if (!networkMode && process.stdin.isTTY && process.stdout.isTTY) {
      networkMode = await configureNetwork(networkModePath);
      if (!networkMode) return;
    }
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
  const args = ['run', '--offline', '--locked', 'python', '-X', 'utf8', '-m', 'app.run', '--service', service];
  if (networkMode === 'lan') args.push('--lan-interfaces', JSON.stringify(lanInterfaces()));
  await run(uv, args, join(root, 'backend'), requestStop);
}

main().catch(error => {
  console.error(`ReCue: ${error.message}`);
  process.exitCode = 1;
});
