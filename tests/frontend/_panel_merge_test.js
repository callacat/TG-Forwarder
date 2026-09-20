// 面板 fetchLive 增量合并逻辑冒烟（数据流防闪烁核心）。
// 从真实 index.html 提取主 <script>，在 vm 沙箱执行后直接调用 app() 暴露的
// changedMerge/deepEqual，断言「无变化返回原引用 = Alpine 零重绘」「有变化才换对象」。
'use strict';

const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const index = process.argv[2];
if (!index) {
  console.error('用法: node _panel_merge_test.js <index.html>');
  process.exit(2);
}

const html = fs.readFileSync(index, 'utf8');
const blocks = html.match(/<script>([\s\S]*?)<\/script>/g) || [];
const main = blocks.find((b) => b.includes('function app()'));
if (!main) {
  console.error('未找到包含 app() 的主脚本块');
  process.exit(2);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(main.replace(/^<script>/, '').replace(/<\/script>$/, ''), sandbox);
const data = vm.runInContext('app()', sandbox);

const old = { a: 1, b: 'x', nested: { d: 2 }, arr: [1, 2, 3] };

// 1) 内容完全一致 → 返回原引用（Alpine 不触发重绘 → 不闪烁）
assert.strictEqual(
  data.changedMerge(old, { a: 1, b: 'x', nested: { d: 2 }, arr: [1, 2, 3] }),
  old,
  '无变化应返回原引用'
);

// 2) 标量变化 → 新引用 + 目标值更新，其余保持
const r2 = data.changedMerge(old, { a: 2, b: 'x', nested: { d: 2 }, arr: [1, 2, 3] });
assert.notStrictEqual(r2, old, '有变化应返回新引用');
assert.strictEqual(r2.a, 2);
assert.strictEqual(r2.b, 'x');

// 3) 嵌套对象内容变化（deepEqual 生效）
const r3 = data.changedMerge(old, { a: 1, b: 'x', nested: { d: 99 }, arr: [1, 2, 3] });
assert.notStrictEqual(r3, old);
assert.strictEqual(r3.nested.d, 99);

// 4) null / 无效输入 → 原引用
assert.strictEqual(data.changedMerge(old, null), old);
assert.strictEqual(data.changedMerge(undefined, null), undefined);

// 5) 初始空对象首轮填充
const r5 = data.changedMerge({}, { a: 1 });
assert.notStrictEqual(r5, {});
assert.strictEqual(r5.a, 1);

console.log('panel merge logic OK');