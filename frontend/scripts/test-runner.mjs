import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { __suites, __resetSuites, vi } from "vitest";
import { cleanup } from "@testing-library/react";

const frontendRoot = path.resolve(import.meta.dirname, "..");

globalThis.window = globalThis;

const storageMap = new Map();
Object.defineProperty(globalThis, "localStorage", {
  value: {
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: (k) => storageMap.delete(k),
    clear: () => storageMap.clear(),
  },
  configurable: true,
  writable: true,
});

const rawArgs = process.argv.slice(2).filter((a) => a !== "--run" && !a.startsWith("-"));

function findTestFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  let files = [];
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      files.push(...findTestFiles(full));
    } else if (e.isFile() && /\.test\.tsx?$/.test(e.name)) {
      files.push(full);
    }
  }
  return files.sort();
}

let targetFiles = [];
if (rawArgs.length > 0) {
  for (const arg of rawArgs) {
    const resolved = path.resolve(frontendRoot, arg);
    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
      targetFiles.push(resolved);
    } else {
      throw new Error(`Test file not found: ${arg}`);
    }
  }
} else {
  targetFiles = findTestFiles(path.join(frontendRoot, "src"));
}

let totalPassed = 0;
let totalFailed = 0;

for (const file of targetFiles) {
  __resetSuites();

  await import(pathToFileURL(file).href);

  const relName = path.relative(frontendRoot, file);
  for (const suite of __suites) {
    for (const t of suite.tests) {
      cleanup();
      vi.restoreAllMocks();
      globalThis.localStorage.clear();
      try {
        for (const be of t.beforeEach) await be();
        await t.fn();
        for (const ae of t.afterEach) await ae();
        console.log(` ✓ ${relName} > ${t.name}`);
        totalPassed++;
      } catch (err) {
        console.error(` ✗ ${relName} > ${t.name}`);
        console.error(`   ${err?.stack || err}`);
        totalFailed++;
      }
    }
  }
}

console.log(`\nTest Files: ${targetFiles.length} | Tests: ${totalPassed} passed, ${totalFailed} failed`);
if (totalFailed > 0 || totalPassed === 0) {
  process.exit(1);
}
