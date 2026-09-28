import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = process.cwd();
const streamSource = readFileSync(join(root, 'app', 'lib', 'agent-stream.ts'), 'utf8');
const detailSource = readFileSync(join(root, 'app', 'skills', 'detail', 'page.tsx'), 'utf8');

assert.match(streamSource, /execute_skill_script/);
assert.match(streamSource, /技能脚本/);
assert.match(streamSource, /sandbox\.mode/);
assert.match(streamSource, /stdout_truncated/);
assert.match(detailSource, /skill-sandbox-note/);
assert.match(detailSource, /安全沙箱/);

console.log('skill sandbox UI metadata checks passed');
