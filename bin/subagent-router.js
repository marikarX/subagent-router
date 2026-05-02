#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const root = path.resolve(__dirname, "..");
const src = path.join(root, "src");
const args = ["-m", "subagent_router.cli", ...process.argv.slice(2)];
const env = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${src}${path.delimiter}${process.env.PYTHONPATH}`
    : src,
};
const candidates = [
  process.env.PYTHON,
  process.env.PYTHON3,
  "python3",
  "python",
].filter(Boolean);

let found = false;
for (const python of candidates) {
  const versionResult = spawnSync(python, ["-c", "import sys; exit(0 if sys.version_info[:2] >= (3, 11) else 1)"], { stdio: "pipe" });
  if (versionResult.status !== 0) continue;
  found = true;
  const result = spawnSync(python, args, { stdio: "inherit", env });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

if (!found) {
  console.error("Python 3.11+ is required but not found. Install Python 3.11 or later and ensure it is on your PATH.");
  process.exit(1);
}
