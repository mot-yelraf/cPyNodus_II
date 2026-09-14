# Lesson 10: Memory and cooperative I/O

[Previous](09-network-failures-and-recovery.md) · [Index](README.md) · [Next](11-testing-and-observability.md)

## What you will learn

Identify allocation lifetime, explain bounded work and cooperative scheduling,
and distinguish a heap guard from a resource leak. Allow 90–120 minutes.

## Preparation and source map

Use a host checkout. A `nodusweb` board and serial capture are optional. Read
[architecture](../architecture.md). Inspect
[web_runtime.py](../../cpynodus_ii/features/web_runtime.py),
[app.py](../../cpynodus_ii/app.py), and
[mqtt_client.py](../../cpynodus_ii/core/mqtt_client.py). Search for the timeout event and `setblocking` to locate the accepted-client
response implementation within the web runtime.

## Walkthrough

An async function cooperates only when execution reaches a yielding operation.
A blocking socket call can stall unrelated work even inside an async loop.
Bounded chunks, explicit socket behavior, and elapsed-time limits matter more
than the presence of the `async` keyword alone.

The web path collects garbage before admitting HTML rendering and requires at
least 10 KB free after collection. A protective 503 is different from a crash.
Large contiguous strings may still fail even when a rough free-byte total
looks sufficient. Page families are loaded on demand and evicted to preserve
heap. The MQTT slow-chunk diagnostics are capped at four lines per packet and
only emitted for chunks taking at least 250 ms; diagnostic work also costs time
and allocations.

## Lab

1. Find the HTML admission guard, renderer cache eviction, successful-response
   cleanup, and response timeout cleanup. For each, record the function and
   the object or resource whose lifetime it bounds.
2. Read one timeout test in
   [test_web_runtime.py](../../tests/test_web_runtime.py). Trace the fake socket's
   behavior through the response path. Identify where other work can run.
3. Write an allocation table for three objects: the latest sensor snapshot,
   rendered HTML body, and accepted client socket. Include creator, consumers,
   intended lifetime, and cleanup owner. Do not assume GC closes every resource
   at the instant your code stops referencing it.
4. Predict the difference between an abandoned client and two consecutive send
   timeouts. Verify the listener-restart behavior in code and tests.
5. Run:

   ```sh
   pytest tests/test_web_runtime.py tests/test_web_services.py tests/test_mqtt_client_adapter.py
   ```

6. Optional board observation: issue one bounded `/setup` request, wait for its
   serial cleanup events, then issue a second. Record status, response size,
   elapsed time, and available heap checkpoints. Use Lesson 4's curl approach;
   do not run parallel requests or a stress loop for this lab.

## Acceptance and debugging

Submit the allocation table, one traced timeout case, predictions checked
against source, and test results. Hardware evidence must identify board,
firmware, profile, request pacing, and serial events. Two requests are an
observation, not a leak or soak-test proof.

If a page returns 503, find the admission or rendering failure before changing
thresholds. If the host test passes, remember its fake socket cannot reproduce
native Pico radio behavior. Do not replace explicit socket cleanup with GC on
the strength of a host-only result.

## Reference answer and reflection

The sensor snapshot persists for consumers until replaced; a rendered document
is temporary but needs contiguous space; accepted clients require explicit
close on completion or failure. Two consecutive response-send timeouts restart
the HTTP listener. This does not imply rebuilding the whole station network.

How could extra logging worsen a timing-sensitive failure? Why can reducing a
single peak allocation help more than reducing total allocations over a minute?
