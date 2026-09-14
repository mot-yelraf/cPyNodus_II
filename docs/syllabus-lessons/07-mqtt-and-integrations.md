# Lesson 7: MQTT and integrations

[Previous](06-sensor-data-flow.md) · [Index](README.md) · [Next](08-switches-and-local-automation.md)

## What you will learn

Trace queued publications to the adapter, distinguish retained snapshots from
new events, and validate a topic contract at the broker. Allow 120 minutes.

## Preparation

Complete Lessons 5–6. For hardware work, use one instructor-provisioned sensor
node in `sensorius`, `weewx`, or `homeassistant`, an isolated classroom broker,
and a configured MQTT subscriber. Obtain the broker address and device/sensor
IDs from the operator. Read [MQTT](../mqtt.md) and the canonical
[Sensorius contract](../sensorius_contract.md). The lab is read-only at the broker.

## Walkthrough

[MQTTTransport](../../cpynodus_ii/core/mqtt.py) represents queued application
work; [publish_cycle.py](../../cpynodus_ii/features/publish_cycle.py) prepares
publications using [payloads.py](../../cpynodus_ii/features/payloads.py).
`sync_transport_to_client` in
[mqtt_client.py](../../cpynodus_ii/core/mqtt_client.py) drains work through the
client/socket adapter. Queue acceptance and a local send return are not proof
of receipt. Observe the broker separately.

Retained `meta` is a startup identity snapshot. `meta/switch` holds detailed
channel information. `meta/patch` describes accepted runtime mutations without
replacing the full retained startup snapshot. Sensor `/data` uses metric names
and units defined by the contract; temperature values are Celsius.

## Lab

1. Predict the schema, identity, timestamp, and retain behavior for sensor data,
   device heartbeat, and device metadata. Verify each against source.
2. Have the operator configure subscriber authentication. On an isolated broker
   that permits this connection, the basic command is:

   ```sh
   mosquitto_sub -h BROKER_HOST -t 'nodus/DEVICE_ID/#' -v
   ```

   Replace placeholders and subscribe separately to `nodus/SENSOR_ID/#` if its
   ID differs. Use the classroom subscriber's configured authentication/TLS
   options when required; this bare command does not configure them.
3. Record arrival times and topic/payload pairs for startup metadata, heartbeat,
   and two sensor publications. Allow the configured publish interval between
   samples. Redact credential-bearing metadata before submitting captures.
4. Start a second subscriber. Identify which observations arrive immediately
   from retained state. Use a subscriber that exposes the retain flag if needed;
   `-v` shows topic and payload, not that flag. Do not label retained replay a
   newly generated sensor event solely from its arrival time.
5. Trace one observed payload through the code and run:

   ```sh
   pytest tests/test_mqtt_client_adapter.py tests/test_command_intake.py tests/test_publish_cycle.py
   ```

## Acceptance and debugging

Submit your prediction table, redacted broker capture, matching serial excerpt,
and test results. Pass when the evidence distinguishes queueing, sending,
receipt, and retained replay. A host-only variant uses the existing payload and
adapter tests and marks broker delivery unverified.

If there is no traffic, check profile, broker target, topic identity, credentials,
and subscription filter. A successful MQTT CONNECT does not establish the
operational checkpoint: startup publishes and subscriptions must also drain.

## Reference answer and reflection

Data uses `nodus-sensor-data/v1`, with readings under `values` and a numeric
`timestamp`. Metadata is retained; command-correlated patches are non-retained.
An immediate retained heartbeat supplies last-known state, not proof that its
publisher is still alive. The retained offline Last Will helps expose abrupt
loss. Never infer MQTT success from `published=1` alone.

How would your report distinguish a disconnected publisher from a subscriber
using the wrong filter? Why is QoS acknowledgement a different observation
from a socket accepting bytes?
