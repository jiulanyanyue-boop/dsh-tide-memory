// tools/zstd_frames.mjs
// 解压 DSH 的 session.v4.jsonl.zstd：文件是「多帧 zstd 顺序拼接」，
// Node 的一次性 API 只解第一帧，所以这里按 magic 切帧后逐帧解压。
//
// 用法： node tools/zstd_frames.mjs <file.zst> > out.jsonl

import fs from 'node:fs';
import zlib from 'node:zlib';

if (typeof zlib.zstdDecompressSync !== 'function') {
  console.error(
    `this Node (${process.version}) lacks zstd support; ` +
      'need Node >= 22.15 / 23.8 (DSH bundles Node 24)',
  );
  process.exit(3);
}

const file = process.argv[2];
if (!file) {
  console.error('usage: node tools/zstd_frames.mjs <file.zst>');
  process.exit(2);
}

const buf = fs.readFileSync(file);
const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const positions = [];
let at = buf.indexOf(MAGIC);
while (at !== -1) {
  positions.push(at);
  at = buf.indexOf(MAGIC, at + 1);
}
positions.push(buf.length);

const chunks = [];
let failed = 0;
for (let i = 0; i < positions.length - 1; i += 1) {
  try {
    chunks.push(zlib.zstdDecompressSync(buf.subarray(positions[i], positions[i + 1])));
  } catch (err) {
    failed += 1;
    console.error(`frame ${i} failed: ${err.message}`);
  }
}

process.stdout.write(Buffer.concat(chunks));
if (failed > 0) console.error(`frames failed: ${failed}/${positions.length - 1}`);
