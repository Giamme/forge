#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const root = path.resolve(__dirname, '..');
const forgeScript = path.join(root, 'forge');

function isWindowsBashStub(candidate) {
  const low = candidate.replace(/\//g, '\\').toLowerCase();
  return low.includes('\\system32\\bash.exe') || low.includes('\\syswow64\\bash.exe') || low.includes('\\windowsapps\\');
}

function findBash() {
  if (process.platform !== 'win32') {
    return fs.existsSync('/bin/bash') ? '/bin/bash' : 'bash';
  }
  for (const key of ['FORGE_BASH', 'BASH']) {
    const val = (process.env[key] || '').trim();
    if (val && fs.existsSync(val) && !isWindowsBashStub(val)) {
      return val;
    }
  }
  const candidates = [];
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    const p = path.join(dir, 'bash.exe');
    if (fs.existsSync(p) && !isWindowsBashStub(p)) {
      candidates.push(p);
    }
  }
  for (const envRoot of [process.env.ProgramFiles, process.env['ProgramFiles(x86)'], process.env.LocalAppData]) {
    if (!envRoot) continue;
    candidates.push(path.join(envRoot, 'Git', 'bin', 'bash.exe'));
    candidates.push(path.join(envRoot, 'Git', 'usr', 'bin', 'bash.exe'));
    candidates.push(path.join(envRoot, 'Programs', 'Git', 'bin', 'bash.exe'));
  }
  candidates.push('C:\\Program Files\\Git\\bin\\bash.exe', 'C:\\Program Files (x86)\\Git\\bin\\bash.exe');
  for (const c of candidates) {
    if (fs.existsSync(c)) {
      return c;
    }
  }
  console.error('forge: Git Bash (bash.exe) not found. Install Git for Windows or set FORGE_BASH.');
  process.exit(127);
}

const bash = findBash();
const child = spawn(bash, [forgeScript, ...process.argv.slice(2)], {
  stdio: 'inherit',
  windowsHide: false,
});

const forwardedSignals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
const handlers = new Map();
for (const sig of forwardedSignals) {
  const fn = () => {
    try {
      child.kill(sig);
    } catch (_err) {
      // Child may already have exited.
    }
  };
  try {
    process.on(sig, fn);
    handlers.set(sig, fn);
  } catch (_err) {
    // Signal not supported on this platform.
  }
}

function cleanupSignals() {
  for (const [sig, fn] of handlers.entries()) {
    process.removeListener(sig, fn);
  }
}

child.on('error', (err) => {
  cleanupSignals();
  console.error(`forge: failed to launch ${bash}: ${err.message}`);
  process.exit(127);
});

child.on('exit', (code, signal) => {
  cleanupSignals();
  if (signal) {
    try {
      process.kill(process.pid, signal);
      return;
    } catch (_err) {
      process.exit(128);
    }
  }
  process.exit(code == null ? 1 : code);
});
