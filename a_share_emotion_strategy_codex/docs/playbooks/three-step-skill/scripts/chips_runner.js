'use strict';
// Execute only the vendored, fingerprint-checked AKShare kernel, never input code.
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const crypto = require('crypto');
const root = path.join(__dirname, '..', 'vendor');
const model = JSON.parse(fs.readFileSync(path.join(root, 'model.json'), 'utf8'));
const kernel = fs.readFileSync(path.join(root, 'cyq.js'), 'utf8');
if (crypto.createHash('sha256').update(kernel).digest('hex') !== model.kernel_sha256) throw Error('Kernel fingerprint mismatch');
const text = fs.readFileSync(0, 'utf8');
if (Buffer.byteLength(text) > 1000000) throw Error('Input too large');
const bars = JSON.parse(text);
if (!Array.isArray(bars) || bars.length !== 210) throw Error('Exactly 210 bars required');
const context = vm.createContext({bars, result: null});
vm.runInContext(kernel, context, {timeout: 1000});
// Progressive history uses only each prefix, never later observations. Earlier
// points are warm-up estimates in this one window, not 210-bar rolling signals.
vm.runInContext('result = bars.map((b,i) => ({date:b.date, fraction:CYQCalculator(i,bars).benefitPart}));', context, {timeout: 10000});
process.stdout.write(JSON.stringify(context.result));
