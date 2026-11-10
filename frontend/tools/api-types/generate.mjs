import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString } from 'openapi-typescript';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const output = new URL('../../src/api/schema.d.ts', import.meta.url);
const fallback = join(homedir(), '.local', 'bin', 'uv.exe');
const uv = process.platform === 'win32' && existsSync(fallback) ? fallback : 'uv';
const exported = spawnSync(uv, [
  'run', '--offline', '--locked', 'python', '-m', 'app.api.export_schema',
], { cwd: join(root, 'backend'), encoding: 'utf8' });
if (exported.error || exported.status !== 0) {
  throw new Error(`OpenAPI export failed: ${exported.error?.message ?? exported.stderr}`);
}
const contents = '// Generated from FastAPI OpenAPI. Run npm run api:generate; do not edit.\n'
  + astToString(await openapiTS(JSON.parse(exported.stdout)));
if (process.argv.includes('--check')) {
  const current = existsSync(output) ? readFileSync(output, 'utf8').replaceAll('\r\n', '\n') : '';
  if (current !== contents) {
    throw new Error('API types are out of date. Run npm run api:generate.');
  }
  console.log('API types match FastAPI OpenAPI.');
} else {
  writeFileSync(output, contents);
  console.log('Generated frontend/src/api/schema.d.ts');
}
