const express = require("express");
const cors = require("cors");
const { execFile, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");

const app = express();
app.use(cors());
app.use(express.json({ limit: "1mb" }));

const TIMEOUT_MS = 10000;
const MAX_SOURCE = 50000;

function cleanup(tmpDir) {
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch {}
}

app.post("/api/compile", (req, res) => {
  const { code, lang = "c", args = [] } = req.body || {};

  if (!code || typeof code !== "string") {
    return res.status(400).json({ error: "Missing code" });
  }
  if (code.length > MAX_SOURCE) {
    return res.status(400).json({ error: "Code too large" });
  }

  const isCpp = lang === "cpp";
  const compiler = isCpp ? "g++" : "gcc";
  const ext = isCpp ? "cpp" : "c";
  const stdFlag = isCpp ? "-std=c++17" : "-std=c11";

  const id = crypto.randomBytes(8).toString("hex");
  const tmpDir = path.join(os.tmpdir(), "cctutor_" + id);
  const srcFile = path.join(tmpDir, `prog.${ext}`);
  const binFile = path.join(tmpDir, "prog");

  fs.mkdirSync(tmpDir, { recursive: true });
  fs.writeFileSync(srcFile, code);

  const compileArgs = [
    stdFlag, "-Wall", "-Wextra", "-O2",
    "-o", binFile, srcFile,
  ];

  execFile(compiler, compileArgs, { timeout: TIMEOUT_MS }, (compErr, compStdout, compStderr) => {
    if (compErr) {
      cleanup(tmpDir);
      return res.json({
        success: false,
        compile_error: (compStderr || compErr.message || "").trim(),
        compile_output: (compStdout || "").trim(),
        stdout: "",
        stderr: "",
        code: -1,
        signal: null,
        argc: 1,
        argv: ["./prog"],
      });
    }

    const argv = Array.isArray(args) ? args.map(String) : [];
    const fullArgv = ["./prog", ...argv];

    const proc = spawn(binFile, argv, {
      cwd: tmpDir,
      timeout: TIMEOUT_MS,
      env: { ...process.env, LANG: "en_US.UTF-8" },
    });

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => { stdout += d.toString(); });
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    proc.on("close", (exitCode, signal) => {
      cleanup(tmpDir);
      res.json({
        success: true,
        compile_error: "",
        compile_output: (compStdout || "").trim(),
        stdout,
        stderr,
        code: exitCode ?? -1,
        signal: signal || null,
        argc: fullArgv.length,
        argv: fullArgv,
      });
    });

    proc.on("error", (err) => {
      cleanup(tmpDir);
      res.json({
        success: false,
        compile_error: "",
        compile_output: "",
        stdout: "",
        stderr: err.message,
        code: -1,
        signal: null,
        argc: 1,
        argv: ["./prog"],
      });
    });
  });
});

app.get("/api/health", (req, res) => {
  execFile("gcc", ["--version"], (err, stdout) => {
    const version = (stdout || "").split("\n")[0] || "unknown";
    res.json({ status: "ok", compiler: version });
  });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`CCTutor backend running on port ${PORT}`);
  execFile("gcc", ["--version"], (_, stdout) => {
    console.log((stdout || "").split("\n")[0]);
  });
});
