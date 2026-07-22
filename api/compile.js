const WANDBOX_URL = "https://wandbox.org/api/compile.json";

function escapeC(str) {
  return str
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\n/g, "\\n")
    .replace(/\t/g, "\\t");
}

function buildWrappedSource(code, argv, lang) {
  const argsArr = ["./prog", ...argv];
  const argcVal = argsArr.length;
  const argvEntries = argsArr.map(a => `    "${escapeC(a)}"`).join(",\n");
  const argvDecl =
    `static const char *__argv_data[] = {\n${argvEntries}\n};\n` +
    `static const int __argc_val = ${argcVal};\n`;

  const mainSig = /int\s+main\s*\([^)]*\)/;
  let matched = false;
  const wrapped = code.replace(mainSig, (m) => {
    matched = true;
    const params = m.slice(m.indexOf("("));
    return "int __user_main" + params;
  });
  if (!matched) {
    return { source: code, wrapped: false };
  }

  if (lang === "cpp") {
    const wrapper =
      argvDecl +
      "\nint main() {\n" +
      "    return __user_main(__argc_val, const_cast<char**>(__argv_data));\n}\n";
    return { source: wrapped + "\n\n" + wrapper, wrapped: true };
  } else {
    const wrapper =
      argvDecl +
      "\nint main(void) {\n" +
      "    return __user_main(__argc_val, (char **)__argv_data);\n}\n";
    return { source: wrapped + "\n\n" + wrapper, wrapped: true };
  }
}

function pickCompiler(lang) {
  if (lang === "cpp") return "gcc-13.2.0";
  return "gcc-13.2.0-c";
}

export default async function handler(req, res) {
  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const { code, lang = "c", args = [] } = req.body || {};

  if (!code || typeof code !== "string") {
    return res.status(400).json({ error: "Missing or invalid 'code' field" });
  }

  const argv = Array.isArray(args) ? args.map(String) : [];
  const { source, wrapped } = buildWrappedSource(code, argv, lang);
  const compiler = pickCompiler(lang);
  const argc = argv.length + 1;
  const fullArgv = ["./prog", ...argv];

  const compilerArgs = lang === "cpp" ? "-std=c++17" : "-std=c11";

  try {
    const wandRes = await fetch(WANDBOX_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        compiler,
        code: source,
        "compiler-arguments": compilerArgs,
        options: "warnings",
      }),
    });

    const data = await wandRes.json();

    const compileErr = (data.compiler_error || "").trim();
    const hasCompileFail = compileErr.length > 0 || data.status === "CE";

    if (hasCompileFail) {
      return res.status(200).json({
        success: false,
        compile_error: compileErr || "Compilation failed",
        compile_output: (data.compiler_output || "").trim(),
        stdout: "",
        stderr: "",
        code: -1,
        signal: data.signal || null,
        argc,
        argv: fullArgv,
      });
    }

    const progOut = data.program_output || "";
    const progErr = data.program_error || "";
    const signal = data.signal || null;

    let stderr = "";
    if (progErr.trim()) stderr += progErr.trim();
    if (signal) stderr += (stderr ? "\n" : "") + "Signal: " + signal;

    const exitCode = data.status !== undefined ? parseInt(data.status) || 0 : 0;

    return res.status(200).json({
      success: true,
      compile_error: "",
      compile_output: (data.compiler_output || "").trim(),
      stdout: progOut,
      stderr,
      code: exitCode,
      signal,
      argc,
      argv: fullArgv,
    });
  } catch (err) {
    return res.status(500).json({
      success: false,
      error: "Compilation service unavailable. Please try again.",
      detail: err.message,
    });
  }
}
