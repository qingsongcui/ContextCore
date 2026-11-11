import fs from 'node:fs';
import path from 'node:path';
import { parse } from '@babel/parser';
import traverseModule from '@babel/traverse';
import { en } from '../src/lib/messages.en.js';

const traverse = traverseModule.default || traverseModule;
const root = path.join(process.cwd(), 'src');
const cjk = /[\u4e00-\u9fff]/;
const placeholders = value => [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort().join(',');
const allowChineseValues = new Set(['· 续境']);
const allowUnwrapped = new Set(['中']);

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full);
    return /\.(jsx|js)$/.test(entry.name) ? [full] : [];
  });
}

const keys = new Set();
const failures = [];
for (const file of walk(root)) {
  const rel = path.relative(root, file);
  if (rel === 'lib/messages.en.js' || rel === 'lib/constants.js') continue;
  const source = fs.readFileSync(file, 'utf8');
  let ast;
  try {
    ast = parse(source, { sourceType: 'module', plugins: ['jsx'] });
  } catch (error) {
    failures.push(`${rel}: cannot parse (${error.message})`);
    continue;
  }
  traverse(ast, {
    CallExpression(pathRef) {
      const { callee, arguments: args } = pathRef.node;
      if (callee.type === 'Identifier' && callee.name === 't' && args[0]?.type === 'StringLiteral') keys.add(args[0].value);
    },
    JSXText(pathRef) {
      const value = pathRef.node.value.trim();
      if (value && cjk.test(value) && !allowUnwrapped.has(value)) failures.push(`${rel}:${pathRef.node.loc.start.line} unwrapped JSX text: ${value}`);
    },
    TemplateLiteral(pathRef) {
      const value = pathRef.node.quasis.map(quasi => quasi.value.cooked).join('');
      if (cjk.test(value)) failures.push(`${rel}:${pathRef.node.loc.start.line} untranslated template literal: ${value}`);
    },
    StringLiteral(pathRef) {
      const value = pathRef.node.value;
      if (cjk.test(value)) keys.add(value);
    },
  });
}

for (const key of keys) {
  if (!Object.prototype.hasOwnProperty.call(en, key)) failures.push(`missing English copy: ${key}`);
}
for (const [key, value] of Object.entries(en)) {
  if (!keys.has(key)) failures.push(`unused English copy: ${key}`);
  if (cjk.test(value) && !allowChineseValues.has(value)) failures.push(`Chinese left in English value: ${key} => ${value}`);
  if (placeholders(key) !== placeholders(value)) failures.push(`placeholder mismatch: ${key} => ${value}`);
}

if (failures.length) {
  console.error(`i18n check failed (${failures.length})`);
  failures.forEach(item => console.error(' -', item));
  process.exit(1);
}
console.log(`i18n check passed: ${keys.size} keys, ${Object.keys(en).length} English strings, no unwrapped Chinese.`);
