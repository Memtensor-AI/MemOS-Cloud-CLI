#!/usr/bin/env node

"use strict";

const { spawn } = require("child_process");
const { existsSync } = require("fs");
const path = require("path");

const exeName = process.platform === "win32" ? "memos.exe" : "memos";
const binaryPath = path.join(__dirname, "..", "bin", exeName);

if (!existsSync(binaryPath)) {
  console.error("MemOS CLI binary is not installed.");
  console.error("Reinstall the package to download the platform binary.");
  process.exit(1);
}

// Force UTF-8 on the child so the frozen Python CLI doesn't fall back to the
// Windows active code page (CP936 / GBK on Chinese systems), which corrupts
// CJK output.
//
// A falsy check — not `=== undefined` — is deliberate: users who set
// `PYTHONUTF8=` (empty string) in their shell semantically mean "not
// configured", and an empty string is falsy in JS. Using `=== undefined` would
// wrongly treat the empty value as an explicit override and skip the default
// (fix #1). Same reasoning for `PYTHONIOENCODING`.
const childEnv = { ...process.env };
if (!childEnv.PYTHONUTF8) {
  childEnv.PYTHONUTF8 = "1";
}
if (!childEnv.PYTHONIOENCODING) {
  childEnv.PYTHONIOENCODING = "utf-8";
}

const child = spawn(binaryPath, process.argv.slice(2), {
  stdio: "inherit",
  env: childEnv,
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(typeof code === "number" ? code : 1);
});

child.on("error", (error) => {
  console.error(`Failed to start MemOS CLI binary: ${error.message}`);
  process.exit(1);
});
