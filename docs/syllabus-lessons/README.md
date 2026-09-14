# cPyNodus II educational series

This series is for sophomore-level college students and above studying embedded
systems, IoT, or software systems design. Thirteen lessons progress from a new
development workstation and unprogrammed board to a tested extension project.

## Course hardware and software

The primary fixture is a **Raspberry Pi Pico 2 W running CircuitPython 9.2.8**,
one **BME280 on I2C bus 0 at 0x76**, one **S1 output**, and a **GP14 RW selector**.
Use Linux or macOS, Visual Studio Code with Python/Pylance/Ruff, and a terminal
running `screen` through the repository's `scripts/run_screen` helper.
Begin in the `nodusweb` profile; introduce MQTT after local hardware is working.

The XIAO ESP32-S3 Sense (`xesp32s3`) remains supported by the project, but its
implementation is **extra credit outside these core lessons**. It is not an
alternative wiring or firmware target within the Pico2 W lab instructions.

## Prerequisites

- Basic Python functions, classes, exceptions, and modules.
- Introductory low-voltage circuit knowledge and supervised breadboard practice.
- A Linux/macOS computer on which the course tools can be installed.
- Basic IP-network concepts by the time you reach the MQTT lesson.

Lesson 1 introduces the toolchain and host/device distinction. Lessons 2–4
teach wiring, filesystem modes, CircuitPython installation, serial access,
REPL use, and deployment. No previous CircuitPython or MQTT setup is assumed.

## Learning sequence

| Lesson | Topic | Learning outcome | Suggested evidence |
| --- | --- | --- | --- |
| [1](01-development-toolchain.md) | Development toolchain | Set up VS Code, Linux/macOS host tools, pytest, screen, and build inputs. | Tool versions and passing host smoke tests. |
| [2](02-pico2w-hardware.md) | Pico2 W hardware and RW selector | Wire BME280 at 0x76, S1, and GP14; explain ROFS versus RWFS. | Inspected wiring and filesystem ownership table. |
| [3](03-circuitpython-and-repl.md) | CircuitPython and serial REPL | Flash 9.2.8, discover /dev endpoints, connect run_screen, and probe I2C. | Version identity, REPL output, and 0x76 scan. |
| [4](04-deploy-and-first-observations.md) | Deploy and first observations | Build/copy Nodus, bootstrap in RWFS, and observe BME280/S1. | Build/deploy record, sensor samples, switch and persistence observations. |
| [5](05-startup-and-configuration.md) | Startup and configuration | Trace entrypoints, profile selection, and service dependencies. | Annotated execution graph and configuration tests. |
| [6](06-sensor-data-flow.md) | Sensor data flow | Trace BME280 calibration and derived metrics using a driver double. | Data-flow diagram and a focused host test. |
| [7](07-mqtt-and-integrations.md) | MQTT and integrations | Explain topics, retained messages, and broker-visible delivery. | Redacted broker capture and matching serial evidence. |
| [8](08-switches-and-local-automation.md) | Switches and local automation | Explain S1 gating, ownership, state persistence, and rule timing. | Low-voltage fixture timeline and cleanup. |
| [9](09-network-failures-and-recovery.md) | Network failures and recovery | Distinguish policy, retries, soft reload, and hard reset. | Synthetic-time test and a source-supported reset trace. |
| [10](10-memory-and-cooperative-io.md) | Memory and cooperative I/O | Explain allocation lifetime, heap guards, and socket cleanup. | Resource ownership table and timeout analysis. |
| [11](11-testing-and-observability.md) | Testing and observability | Match claims to host, board, broker, and persistence evidence. | Tests and a reproducible evidence matrix. |
| [12](12-signed-ota-updates.md) | Signed OTA updates | Audit Pico2 W packages and trace verification/apply/rollback. | Host failure-case analysis and package inspection. |
| [13](13-extension-project.md) | Extension project | Deliver a small tested feature or host tool with documented scope. | Design, patch, tests, and appropriate validation evidence. |

Lessons 1–4 are the practical foundation and should be completed in order.
Lessons 5–8 connect observations to source and protocols; Lessons 9–13 develop
reliability, testing, update, and extension skills. Each lesson includes time
estimates, acceptance criteria, debugging guidance, and expected observations
or reference answers.

## Using the lessons

Run host shell commands from the repository root. Keep a separate terminal tab
for the serial console. Python typed at the board's `>>>` prompt runs on
CircuitPython; pytest and the build/deploy scripts run on Linux/macOS.

The student/operator performs the documented board writes. Automated agents
must not copy, edit, or deploy onto `/Volumes/CIRCUITPY`. ROFS/RWFS labels describe
**application** filesystem access: ROFS exposes the drive for host editing;
RWFS hides the drive and allows application persistence. See Lesson 2 before
changing the jumper and Lesson 4 before first bootstrap.

Students without hardware can complete source and host exercises while marking
physical results unverified. Lessons 6 and 9 contain starter tests with TODOs
and reference answers. Keep exercise files outside the deployed firmware.
Record the Git revision, tool/runtime versions, profile, and actual results.
Redact credentials and do not present expected outputs as measured evidence.

Budget additional time for tool downloads, compiler setup, and troubleshooting.
Use each lesson's acceptance criteria; the extension project includes a rubric.
Review stability-sensitive behavior proposals with the operator before editing
runtime code. Supervise hardware fault injection and OTA transfer separately.

## Extra credit: XIAO ESP32-S3 Sense

After completing the Pico2 W work, propose a port of the same BME280/S1 fixture
to `xesp32s3`. Begin with the existing [XIAO bring-up guide](../xiao_esp32s3_circuitpython_bringup.md),
[board pinouts](../pinout.md), and target build instructions. Use that target's
verified CircuitPython 10.2.1, matching compiler/libraries, and its own pin map.
Do not copy Pico GP numbers, UF2, or MPY build artifacts to the XIAO.

Submit a mapping of differences in wiring, RW selector, USB/serial behavior,
bootstrap, and resource observations, plus target-specific validation. These
lessons do not supply the XIAO implementation steps or assume equivalent
hardware results from the Pico tests.

## Technical references

Lessons explain the firmware; the existing contract documents remain the source
of truth for current behavior:

- [Project overview](../README.md) and [user guide](../user_guide.md)
- [Architecture and recovery](../architecture.md)
- [Configuration and profiles](../configuration.md)
- [MQTT overview](../mqtt.md) and [Sensorius contract](../sensorius_contract.md)
- [Hardware pinout](../pinout.md) and [extension guide](../extending.md)
- [Local automations](../automations.md) and [OTA updates](../ota.md)
- [Contributing](../CONTRIBUTING.md) and [test apparatus](../../testApparatus/)

Host-side pytest does not execute CircuitPython on a board. Network, socket,
and reboot claims require hardware evidence; MQTT delivery requires a broker
capture in addition to console output. External tool-installation references
are linked within the opening lessons; course firmware versions are pinned to
the repository's verified targets.
