# C/C++ Tutor

A step-by-step execution visualizer for C and C++ programs, inspired by [Python Tutor](https://pythontutor.com). Uses real compilation (`gcc`/`g++`) and GDB's Machine Interface for actual debugging — not a fake interpreter.

## Features

- **Real compilation and debugging** — compiles with `gcc -g -O0` / `g++ -g -O0`, traces with GDB MI3
- **First-class argc/argv support** — enter command-line arguments and watch them resolved in the trace
- **Call stack visualization** — see all stack frames with local variables at each step
- **Variable change highlighting** — variables that changed since the previous step are highlighted
- **Console output** — accumulates stdout character-by-character (works with raw `write()` calls)
- **Step controls** — First / Prev / Next / Last buttons plus a scrubber slider
- **Keyboard shortcuts** — Arrow keys, `n`/`p`, Home/End, Ctrl+Enter to run
- **C and C++ support** — toggle between C and C++ compilation
- **Library-skipping** — automatically steps through glibc internals (like `write()` wrappers) and shows clean transitions back to user code

## Prerequisites

- Python 3.8+
- GDB (`apt-get install gdb` on Debian/Ubuntu, `yum install gdb` on RHEL/CentOS)
- `libc6-dbg` (for GDB to step through libc with debug info — usually installed with gdb)

## Setup

```bash
# Clone the repo
git clone <repo-url> && cd CCTUTOR

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py
```

Open http://localhost:5000 in your browser.

## API

### `POST /api/trace`

Request body:
```json
{
  "code": "int main(int argc, char *argv[]) { ... }",
  "lang": "c",
  "args": ["hello", "world"]
}
```

- `code` — source code string
- `lang` — `"c"` or `"cpp"`
- `args` — command-line arguments (becomes `argv[1]`, `argv[2]`, etc.)

Response (success):
```json
{
  "success": true,
  "source_lines": ["..."],
  "argv": ["/path/to/prog", "hello", "world"],
  "steps": [
    {
      "line": 5,
      "func": "main",
      "output_delta": "h",
      "output_so_far": "h",
      "finished": false,
      "frames": [
        {
          "level": 0,
          "func": "main",
          "line": 5,
          "vars": [
            {"name": "i", "type": "int", "value": "0"}
          ]
        }
      ]
    }
  ]
}
```

Response (compile error):
```json
{
  "success": false,
  "error": "<gcc/g++ stderr>"
}
```

## Architecture

1. Frontend submits source code + argv to the Flask backend
2. Backend writes source to a temp file, compiles with gcc/g++
3. GDB's Machine Interface (`--interpreter=mi3`) drives execution via `pygdbmi`
4. After each `-exec-step`, library internals are skipped via `-exec-finish` until control returns to user code
5. At each user-code stop, the full call stack and variables are captured
6. The complete trace is returned as JSON and rendered in the browser

### Key design decisions

- **PTY for inferior stdout** — the traced program gets its own pty so its stdout doesn't corrupt the GDB MI stream
- **Library skipping** — after each step, if GDB lands in glibc (e.g., inside `write()`), repeated `-exec-finish` calls return to user code; accumulated output is preserved
- **Batch trace** — the entire trace is captured server-side before returning to the client (no streaming), since traced programs terminate quickly

## Safety

- Each request compiles and runs in an isolated temp directory, cleaned up after
- Step limit of 500 steps guards against infinite loops
- GDB timeout of 5 seconds per step
- Compilation timeout of 10 seconds

## Example

Try this in the editor — set argv to `alice bob`:

```c
#include <unistd.h>

int str_len(char *s) {
    int n = 0;
    while (s[n]) n++;
    return n;
}

void reverse(char *s) {
    int i = 0, j = str_len(s) - 1;
    while (i < j) {
        char t = s[i]; s[i] = s[j]; s[j] = t;
        i++; j--;
    }
}

int main(int argc, char *argv[]) {
    int i;
    for (i = 1; i < argc; i++) {
        reverse(argv[i]);
        write(1, argv[i], str_len(argv[i]));
        write(1, " ", 1);
    }
    write(1, "\n", 1);
    return 0;
}
```

**Note on C++ `std::cout`:** buffered I/O may not appear in the output panel
until the buffer is flushed. Use `std::endl` or `std::cout.flush()` to ensure
output is visible at each step.
