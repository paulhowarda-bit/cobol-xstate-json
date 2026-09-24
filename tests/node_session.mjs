// One long-lived Node process for the XState integration tests (upstream ledger item 54).
//
// Each request is one JSON line on stdin, {op, path}; each reply is one JSON line on
// stdout, {code, stdout, stderr} - the three things a `node driver.mjs` spawn gave the
// test, so every assertion and failure message reads exactly as before. A driver that
// calls process.exit(n) ends ITS case with code n, not this process; anything else it
// throws is exit code 1 with the stack on stderr, which is what node itself does.
//
//   run:   import the driver module (its top-level code is the case)
//   check: parse the module without linking or running it - `node --check`
import readline from 'node:readline';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';
import { format } from 'node:util';

class Exit { constructor(code) { this.code = code; } }

const reply = process.stdout.write.bind(process.stdout);
const realExit = process.exit;

async function runCase(op, path) {
  let out = '', err = '';
  const saved = {
    write: process.stdout.write, log: console.log, info: console.info,
    error: console.error, warn: console.warn,
  };
  process.stdout.write = (chunk) => { out += String(chunk); return true; };
  console.log = console.info = (...a) => { out += format(...a) + '\n'; };
  console.error = console.warn = (...a) => { err += format(...a) + '\n'; };
  process.exit = (code = 0) => { throw new Exit(code); };
  let code = 0;
  try {
    if (op === 'check') new vm.SourceTextModule(readFileSync(path, 'utf8'), { identifier: path });
    else await import(pathToFileURL(path).href);
  } catch (e) {
    if (e instanceof Exit) code = e.code;
    else { code = 1; err += (e && e.stack) ? e.stack + '\n' : String(e) + '\n'; }
  } finally {
    process.stdout.write = saved.write;
    Object.assign(console, { log: saved.log, info: saved.info, error: saved.error, warn: saved.warn });
    process.exit = realExit;
  }
  return { code, stdout: out, stderr: err };
}

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  const { op, path } = JSON.parse(line);
  reply(JSON.stringify(await runCase(op, path)) + '\n');
}
