# Lesson 1: Development tools on Linux and macOS

[Lesson index](README.md) · [Next: Hardware](02-pico2w-hardware.md)

## What you will learn

Prepare an editor, host Python environment, build tools, and serial terminal.
Explain which tools run on your computer and which code runs on the Pico.
Allow 60–90 minutes, plus downloads. Hardware is not needed for this lesson.

The course uses a **Raspberry Pi Pico 2 W**, **CircuitPython 9.2.8**, one
**BME280**, and **S1**. XIAO ESP32-S3 Sense is extra credit outside the core
lessons. Keep one terminal tab for host commands and a second for the board's
serial console. Blocks marked `sh` run on your computer; REPL examples run on
CircuitPython only after connecting to the board.

## 1. Install the host tools

Use Visual Studio Code as the editor and its integrated terminal or your
system terminal: Terminal on macOS, or the desktop terminal on Linux. `screen`
is the serial program running inside that terminal; the repository's
[run_screen](../../scripts/run_screen) wraps it with reconnection logic.

On Ubuntu/Debian, install these packages from a host terminal:

```sh
sudo apt update
sudo apt install git python3 python3-venv python3-pip rsync screen curl ripgrep unzip
```

Check `python3 --version`; the host exercises need Python 3.11 or later. Older
Linux releases may need the instructor's Python environment. Other distributions
use their own package manager and equivalent packages. Install VS Code using
Microsoft's [Linux installation guide](https://code.visualstudio.com/docs/setup/linux).

On macOS, install VS Code from the
[macOS guide](https://code.visualstudio.com/docs/setup/mac). Move it to
Applications and use its Command Palette command **Shell Command: Install
'code' command in PATH** if you want `code .` in a terminal. Install
[Homebrew](https://brew.sh/) through its official instructions if it is not
already available, then:

```sh
brew install git python rsync screen ripgrep
```

Follow Homebrew's shell/PATH instructions for your Mac. Use `python3 --version`
and `command -v screen` to check what the terminal actually resolves. Do not
replace macOS system Python. `curl` and `unzip` are normally already available.

## 2. Clone and create a host environment

If you already have the course checkout, open that folder and skip cloning.
Otherwise:

```sh
git clone https://github.com/mot-yelraf/cPyNodus_II.git
cd cPyNodus_II
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pytest ruff
code .
```

Use the instructor's assigned revision. Record it with `git rev-parse HEAD`.
In each new host terminal, activate `.venv` again. Host dependencies belong in
this environment, not in `CIRCUITPY/lib`. CircuitPython's `board`, `busio`, and
`digitalio` modules exist on the microcontroller; a normal host interpreter
cannot execute hardware firmware by clicking VS Code's Python Run button.

## 3. Configure VS Code

Open the repository folder, not the mounted board drive. In the Extensions view,
install these by publisher/ID:

| Extension | ID | Course use |
| --- | --- | --- |
| Python, Microsoft | `ms-python.python` | Host interpreter selection and pytest integration |
| Pylance, Microsoft | `ms-python.vscode-pylance` | Python navigation and completion; normally installed with Python |
| Ruff, Astral | `charliermarsh.ruff` | Lint diagnostics using the repository configuration |

See Microsoft's [Python guide](https://code.visualstudio.com/docs/languages/python)
and [Pylance description](https://marketplace.visualstudio.com/items?itemName=ms-python.vscode-pylance),
and Astral's [Ruff extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff).
Git and Markdown preview are already part of VS Code. A CircuitPython-specific
uploader is optional and is not required for this course; deployment uses the
project scripts and serial access uses `run_screen`.

Select **Python: Select Interpreter** and choose `.venv/bin/python`. Use
**Python: Configure Tests**, select pytest, and choose `tests`. Treat missing
CircuitPython import diagnostics as a host/device boundary to investigate;
installing similarly named PyPI packages does not turn the laptop into a Pico.
Avoid automatic whole-repository formatting while working through a small lab.

## 4. Prepare the firmware build inputs

Read [nodus_mpy.sh](../../scripts/nodus_mpy.sh). It requires a host-executable
`mpy-cross` that reports **CircuitPython 9.2.8** and **mpy v6.3**, plus a
complete compatible 9.x MPY library bundle. A generic MicroPython compiler or
a newer CircuitPython compiler is not an interchangeable substitute.

The instructor should supply a tested compiler for each host OS/CPU and the
course bundle. CircuitPython's [9.2.8 release](https://github.com/adafruit/circuitpython/releases/tag/9.2.8)
and [library downloads](https://circuitpython.org/libraries) are the upstream
sources. Prefer the course-pinned 9.x bundle; a newest download is not evidence
of compatibility with the project's tested dependencies.

Set these example variables to the actual extracted paths, keeping the quotes:

```sh
export MPY_CROSS_PICO2W="/absolute/path/to/course/mpy-cross"
export NODUS_LIB_SOURCE="/absolute/path/to/course/9.x-bundle/lib"
"$MPY_CROSS_PICO2W" --version
bash scripts/nodus_mpy.sh --help
bash scripts/deploy_nodus.sh --help
bash scripts/run_screen --help
```

Do not paste the placeholder paths unchanged. Use `--lib-source` when building
in Lesson 4: the script's default library path refers to the maintainer's local
layout and need not exist on a student's computer. The script validates all
entries in `LIB_MANIFEST`, including web and MQTT dependencies, even for this
BME280 exercise. If a compiler/bundle is unavailable, finish the host lessons
and obtain the course build inputs before attempting deployment.

## Acceptance and reference observations

Run `python -m pytest tests/test_board_profile.py tests/test_runtime_config.py`.
Submit tool versions, Git revision, selected interpreter, test results, and
compiler version or an explicit missing-tool note. Explain the difference
between host Python, CircuitPython UF2, application MPY files, and driver MPY files.

Expected: VS Code opens the checkout, tests run under `.venv`, and script help
works without a connected board. No firmware is flashed during this lesson.
If a command is missing, inspect PATH and package installation. If pytest cannot
import the project, confirm you are running from the repository root.
