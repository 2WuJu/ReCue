import { createHash } from 'node:crypto';
import { createReadStream, existsSync } from 'node:fs';
import { stat, writeFile } from 'node:fs/promises';
import { basename, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';

async function filePath(lines, prompt) {
  while (true) {
    process.stdout.write(prompt);
    const answer = await lines.next();
    if (answer.done) throw new Error('输入已结束，请重新运行启动脚本。');
    const input = answer.value.trim().replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1');
    const path = resolve(input.replace(/^\/([a-z])\//i, process.platform === 'win32' ? '$1:/' : '/$1/'));
    if (input && (await stat(path).catch(() => null))?.isFile()) return path;
    console.log('找不到该文件，请输入完整文件路径。');
  }
}

async function digest(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

export async function configureProcessing(root) {
  if (process.env.RECUE_PROCESSING_CONFIG) return;
  const config = join(root, 'processing.local.json');
  if (!existsSync(config)) {
    const terminal = createInterface({ input: process.stdin, crlfDelay: Infinity });
    try {
      const lines = terminal[Symbol.asyncIterator]();
      const cli = process.platform === 'win32' ? 'whisper-cli.exe' : 'whisper-cli';
      const executable = await filePath(lines, `${cli} 在哪里？ `);
      const model = await filePath(lines, '多语言 GGML 模型文件在哪里？ ');
      console.log('正在计算文件哈希…');
      const settings = {
        version: 1, backend: 'cpu', executable, model,
        executable_sha256: await digest(executable), model_sha256: await digest(model),
        engine_version: 'unrecorded', engine_commit: 'unrecorded',
        model_revision: 'unrecorded', model_name: basename(model), ffprobe: 'ffprobe',
      };
      await writeFile(config, JSON.stringify(settings, null, 2) + '\n', { flag: 'wx' });
    } finally {
      terminal.close();
    }
  }
  process.env.RECUE_PROCESSING_CONFIG = config;
  console.log(`使用转写配置 ${config}`);
}
