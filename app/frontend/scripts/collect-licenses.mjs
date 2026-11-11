import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstat, mkdir, readFile, readdir, realpath, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = join(root, 'dist-staging');
const modules = await realpath(join(root, 'node_modules'));
const within = (base, path) => {
  const value = relative(base, path);
  return value === '' || !isAbsolute(value) && value !== '..' && !value.startsWith(`..${process.platform === 'win32' ? '\\' : '/'}`);
};

if (!(await lstat(join(output, 'index.html'))).isFile()) {
  throw new Error('Build the workbench before collecting distribution licenses.');
}

const inventory = execFileSync('npm', ['ls', '--parseable', '--all', '--omit=dev'], {
  cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
}).trim().split(/\r?\n/).filter(Boolean);
const sections = [];
const seen = new Set();
for (const path of inventory) {
  if (resolve(path) === root) continue;
  const directory = await realpath(path);
  if (!within(modules, directory)) throw new Error('Runtime dependency escapes the installed package tree.');
  const metadata = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
  if (typeof metadata.name !== 'string' || typeof metadata.version !== 'string') {
    throw new Error('Incomplete runtime dependency identity.');
  }
  const identity = `${metadata.name}@${metadata.version}`;
  if (seen.has(identity)) continue;
  seen.add(identity);
  const names = (await readdir(directory)).filter(name => /^(?:licen[cs]e|copying|notice)(?:[.\-_]|$)/i.test(name)).sort();
  const texts = [];
  for (const name of names) {
    const candidate = join(directory, name);
    const info = await lstat(candidate);
    if (info.isSymbolicLink()) throw new Error(`License file must not be a symlink: ${identity}`);
    if (!info.isFile()) continue;
    const text = await readFile(candidate, 'utf8');
    if (text.trim()) texts.push(`${name}\n${text.trim()}`);
  }
  if (!texts.length) throw new Error(`No distributable license text found: ${identity}`);
  const license = typeof metadata.license === 'string' ? metadata.license : metadata.license?.type || 'See included notices';
  sections.push({ identity, license, texts });
}
if (!sections.length) throw new Error('No runtime dependency licenses were collected.');
sections.sort((a, b) => a.identity.localeCompare(b.identity, 'en'));
const body = [
  'ContextCore / Senrelay browser runtime dependency licenses',
  'Generated from the installed locked runtime graph. Build tools are not included.',
  'These notices retain their respective terms; the ContextCore license does not relicense dependencies.',
  '',
  ...sections.flatMap(item => [`===== ${item.identity} (${item.license}) =====`, ...item.texts, '']),
].join('\n');
await mkdir(output, { recursive: true });
await writeFile(join(output, 'THIRD_PARTY_LICENSES.txt'), `${body}\n`, 'utf8');
const digest = createHash('sha256').update(`${body}\n`).digest('hex');
process.stdout.write(`Collected ${sections.length} runtime license notices (${digest.slice(0, 12)}).\n`);
