import os
import re
import pty
import select
import subprocess
import tempfile
import json
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pygdbmi.gdbcontroller import GdbController

app = Flask(__name__, static_folder="static")
CORS(app)

MAX_STEPS = 500
STEP_TIMEOUT = 5
COMPILE_TIMEOUT = 10


def read_inferior(master_fd):
    out = b""
    while select.select([master_fd], [], [], 0)[0]:
        try:
            out += os.read(master_fd, 4096)
        except OSError:
            break
    return out.decode(errors="replace")


def get_stopped(resp):
    for r in resp:
        if r.get("type") == "notify" and r.get("message") == "stopped":
            return r["payload"]
    return None


def parse_argv_value(raw_value):
    m = re.search(r'"((?:[^"\\]|\\.)*)"', raw_value)
    if m:
        return m.group(1)
    return raw_value


def compile_code(code, lang):
    tmpdir = tempfile.mkdtemp(prefix="cctutor_")
    ext = ".cpp" if lang == "cpp" else ".c"
    src_path = os.path.join(tmpdir, "prog" + ext)
    bin_path = os.path.join(tmpdir, "prog")

    with open(src_path, "w") as f:
        f.write(code)

    compiler = "g++" if lang == "cpp" else "gcc"
    result = subprocess.run(
        [compiler, "-g", "-O0", "-o", bin_path, src_path],
        capture_output=True, text=True, timeout=COMPILE_TIMEOUT
    )

    return tmpdir, src_path, bin_path, result


def extract_var_value(val_str):
    val_str = val_str.strip()
    if val_str.startswith("{"):
        inner = val_str[1:-1].strip()
        parts = []
        depth = 0
        current = ""
        for ch in inner:
            if ch == "{":
                depth += 1
                current += ch
            elif ch == "}":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        return "{" + ", ".join(parts) + "}"
    return val_str


def capture_trace(tmpdir, src_path, bin_path, argv_list, code=""):
    SRC_BASENAME = os.path.basename(src_path)

    def safe_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    def eval_gdb_expr(expr, timeout=3):
        resp = gdbmi.write(f"-data-evaluate-expression {expr}", timeout_sec=timeout)
        for r in resp:
            if r.get("type") == "result" and r.get("message") == "done":
                payload = r.get("payload", {})
                if isinstance(payload, dict) and "value" in payload:
                    return payload["value"]
        return None

    gdbmi = GdbController(
        command=["gdb", "--nx", "--quiet", "--interpreter=mi3", bin_path],
    )

    try:
        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
    except Exception:
        master = None
        slave = None
        slave_name = None

    try:
        gdbmi.write("set pagination off", timeout_sec=2)
        gdbmi.write("set confirm off", timeout_sec=2)
        gdbmi.write("set print pretty off", timeout_sec=2)
        gdbmi.write("set print array off", timeout_sec=2)
        gdbmi.write("set unwindonsignal on", timeout_sec=2)

        if slave_name:
            gdbmi.write(f"-inferior-tty-set {slave_name}", timeout_sec=2)
            os.close(slave)
            slave = None

        gdbmi.write("-break-insert main", timeout_sec=2)
        if argv_list:
            gdbmi.write("-exec-arguments " + " ".join(argv_list), timeout_sec=2)
        resp = gdbmi.write("-exec-run", timeout_sec=5)

        stopped = get_stopped(resp)
        if not stopped:
            for r in resp:
                if r.get("type") == "result" and r.get("message") == "error":
                    return {"success": False, "error": r.get("payload", "Failed to start program")}
            return {"success": False, "error": "Failed to start program"}

        argv_resolved = []
        func_name = stopped.get("frame", {}).get("func", "")
        if "main" in func_name:
            frame_args = stopped.get("frame", {}).get("args", [])
            argc_val = None
            argv_name = None
            for a in frame_args:
                name = a.get("name", "")
                if name in ("argc", "ac"):
                    argc_val = safe_int(a.get("value", "0"))
                elif name in ("argv", "av", "v", "args"):
                    argv_name = name
            if argc_val is None:
                argc_raw = eval_gdb_expr("argc")
                if argc_raw is not None:
                    argc_val = safe_int(argc_raw)
                elif frame_args:
                    argc_val = safe_int(frame_args[0].get("value", "0"))
                else:
                    argc_val = 0
            if argv_name is None:
                argv_raw = eval_gdb_expr("argv")
                if argv_raw is not None:
                    argv_name = "argv"
                elif len(frame_args) > 1:
                    argv_name = frame_args[1].get("name", "argv")
                else:
                    argv_name = "argv"
            for i in range(argc_val):
                val = eval_gdb_expr(f'(({argv_name})[{i}])')
                if val is not None:
                    argv_resolved.append(parse_argv_value(val))
                elif i < len(argv_list):
                    argv_resolved.append(argv_list[i])
                else:
                    argv_resolved.append("")
        else:
            resp2 = gdbmi.write("-exec-continue", timeout_sec=5)
            stopped = get_stopped(resp2)
            if not stopped:
                for r in resp2:
                    if r.get("type") == "notify" and r.get("message") in ("exited", "thread-exited"):
                        return {
                            "success": True,
                            "source_lines": code.split("\n"),
                            "argv": argv_resolved,
                            "steps": [{
                                "line": 0,
                                "func": "???",
                                "output_delta": "",
                                "output_so_far": "",
                                "finished": True,
                                "frames": [],
                            }]
                        }

        def capture_step_state(stopped):
            frames = []
            if stopped.get("reason") in ("exited-normally", "exited"):
                return frames

            stack_resp = gdbmi.write("-stack-list-frames", timeout_sec=3)
            stack_data = None
            for r in stack_resp:
                if r.get("type") == "result" and r.get("message") == "done":
                    stack_data = r.get("payload", {})

            frame_list = []
            if isinstance(stack_data, dict) and "stack" in stack_data:
                frame_list = stack_data["stack"]

            for frame_info in frame_list:
                level = safe_int(frame_info.get("level", 0))
                frame_func = frame_info.get("func", "???")
                frame_file = frame_info.get("file", "")
                frame_line = safe_int(frame_info.get("line", 0))

                gdbmi.write(f"-stack-select-frame {level}", timeout_sec=2)
                var_resp = gdbmi.write("-stack-list-variables --simple-values", timeout_sec=3)

                vars_list = []
                for r in var_resp:
                    if r.get("type") == "result" and r.get("message") == "done":
                        payload = r.get("payload", {})
                        if isinstance(payload, dict) and "variables" in payload:
                            for v in payload["variables"]:
                                val = extract_var_value(str(v.get("value", "")))
                                vars_list.append({
                                    "name": v.get("name", "?"),
                                    "type": v.get("type", "?"),
                                    "value": val,
                                })

                frames.append({
                    "level": level,
                    "func": frame_func,
                    "line": frame_line,
                    "file": frame_file,
                    "vars": vars_list,
                })

            return frames

        source_lines = code.split("\n")
        trace = []
        total_output = ""
        step_num = 0
        prev_line = -1
        prev_func = "main"

        def add_trace_step(stopped, output_delta, total_out, frames_data, finished=False):
            nonlocal prev_line, prev_func
            frame_file = stopped.get("frame", {}).get("file", "")
            frame_basename = os.path.basename(frame_file) if frame_file else ""
            line = safe_int(stopped.get("frame", {}).get("line", 0))
            func = stopped.get("frame", {}).get("func", "???")

            if finished:
                display_line = prev_line if prev_line > 0 else 0
                display_func = prev_func
            elif frame_basename == SRC_BASENAME and line:
                prev_line = line
                prev_func = func
                display_line = line
                display_func = func
            elif frame_basename == SRC_BASENAME:
                display_line = line
                display_func = func
            else:
                display_line = prev_line if prev_line > 0 else 0
                display_func = prev_func

            trace.append({
                "line": display_line,
                "func": display_func,
                "output_delta": output_delta,
                "output_so_far": total_out,
                "finished": finished,
                "frames": frames_data,
            })

        frames_data = capture_step_state(stopped)
        if frames_data:
            frame_file = stopped.get("frame", {}).get("file", "")
            frame_basename = os.path.basename(frame_file) if frame_file else ""
            line = safe_int(stopped.get("frame", {}).get("line", 0))
            func = stopped.get("frame", {}).get("func", "???")
            if frame_basename == SRC_BASENAME and line:
                prev_line = line
                prev_func = func
            trace.append({
                "line": line if frame_basename == SRC_BASENAME else 0,
                "func": stopped.get("frame", {}).get("func", "???"),
                "output_delta": "",
                "output_so_far": "",
                "finished": False,
                "frames": frames_data,
            })

        while step_num < MAX_STEPS:
            resp = gdbmi.write("-exec-step", timeout_sec=STEP_TIMEOUT)
            stopped = get_stopped(resp)
            output = read_inferior(master) if master else ""

            guard = 0
            while (stopped
                   and os.path.basename(stopped.get("frame", {}).get("file", "")) != SRC_BASENAME
                   and stopped.get("reason") not in ("exited-normally", "exited")
                   and guard < 50):
                resp2 = gdbmi.write("-exec-finish", timeout_sec=STEP_TIMEOUT)
                output += read_inferior(master) if master else ""
                s2 = get_stopped(resp2)
                if s2 is None:
                    break
                stopped = s2
                guard += 1

            if not stopped:
                break

            reason = stopped.get("reason", "")
            total_output += output
            step_num += 1

            if reason in ("exited-normally", "exited"):
                frames_data = capture_step_state(stopped)
                add_trace_step(stopped, output, total_output, frames_data, finished=True)
                break

            if reason == "breakpoint-hit":
                frame_file = stopped.get("frame", {}).get("file", "")
                frame_basename = os.path.basename(frame_file) if frame_file else ""
                if frame_basename != SRC_BASENAME:
                    resp3 = gdbmi.write("-exec-continue", timeout_sec=STEP_TIMEOUT)
                    stopped2 = get_stopped(resp3)
                    output2 = read_inferior(master) if master else ""
                    total_output += output2
                    if stopped2:
                        stopped = stopped2
                        output = output2
                        if stopped.get("reason") in ("exited-normally", "exited"):
                            frames_data = capture_step_state(stopped)
                            add_trace_step(stopped, output, total_output, frames_data, finished=True)
                            break

            frame_file = stopped.get("frame", {}).get("file", "")
            frame_basename = os.path.basename(frame_file) if frame_file else ""

            if frame_basename == SRC_BASENAME:
                frames_data = capture_step_state(stopped)
                add_trace_step(stopped, output, total_output, frames_data)

        if step_num >= MAX_STEPS and trace:
            trace[-1]["finished"] = True

        return {
            "success": True,
            "source_lines": source_lines,
            "argv": argv_resolved,
            "steps": trace,
        }

    finally:
        try:
            gdbmi.write("-gdb-exit", timeout_sec=2)
        except Exception:
            pass
        try:
            gdbmi.exit()
        except Exception:
            pass
        if master:
            try:
                os.close(master)
            except Exception:
                pass
        if slave:
            try:
                os.close(slave)
            except Exception:
                pass


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


@app.route("/api/trace", methods=["POST"])
def trace():
    data = request.get_json()
    code = data.get("code", "")
    lang = data.get("lang", "c")
    argv_list = data.get("args", [])

    if not code.strip():
        return jsonify({"success": False, "error": "No code provided"})

    tmpdir = None
    try:
        tmpdir, src_path, bin_path, result = compile_code(code, lang)

        if result.returncode != 0:
            return jsonify({"success": False, "error": result.stderr})

        trace_result = capture_trace(tmpdir, src_path, bin_path, argv_list, code)
        return jsonify(trace_result)

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Compilation timed out"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        if tmpdir:
            import shutil
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    import os as _os
    port = int(_os.environ.get("PORT", 5000))
    debug = _os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
