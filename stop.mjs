import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const launcher = fileURLToPath(new URL('./recue.mjs', import.meta.url));
const result = spawnSync(process.execPath, [launcher, 'stop', ...process.argv.slice(2)], {
  stdio: 'inherit',
  windowsHide: true,
});

if (result.error) console.error(`ReCue: ${result.error.message}`);
process.exitCode = result.status ?? 1;
