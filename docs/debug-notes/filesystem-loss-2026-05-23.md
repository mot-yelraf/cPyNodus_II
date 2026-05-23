# Filesystem Loss Incident - 2026-05-23

## Incident

`lux-wf5ama` had a severe crash while it was simply running. After the crash,
the device filesystem no longer showed the deployed firmware code. The only
files visible on the device were the default TOML files.

The exact corruption trigger is unknown. Do not treat this as confirmed
firmware-caused filesystem corruption without more evidence.

## MQTT Evidence

Broker logs for `nodus/lux-wf5ama` showed repeated retained `/meta` publishes
on `v0.26.143.9` before the filesystem-loss event. The pasted sample includes
identity cycles at roughly:

- `2026-05-23T11:49:28-0600`
- `2026-05-23T11:51:30-0600`
- `2026-05-23T11:53:31-0600`
- `2026-05-23T11:55:33-0600`
- `2026-05-23T11:57:34-0600`
- `2026-05-23T11:59:35-0600`
- `2026-05-23T12:01:37-0600`
- `2026-05-23T12:03:38-0600`
- `2026-05-23T12:05:40-0600`
- `2026-05-23T12:07:42-0600`
- `2026-05-23T12:09:50-0600`

Each `/meta` publish was followed by heartbeat and availability messages. That
pattern is consistent with repeated startup/identity replay cycles, and the
operator observed the freeze/corruption window around `12:07` local time. The
last pasted pre-reflash broker message was availability at
`2026-05-23T12:09:55-0600`.

After the device was nuked and reflashed, broker logs resumed at
`2026-05-23T13:25:24-0600` with `version="v0.26.143.10"`. The first post-reflash
payload used timestamp `946684820`, then normal timestamps resumed by
`2026-05-23T13:26:24-0600`, consistent with booting before NTP sync and then
recovering time.

## Recovery

The device was nuked, reflashed, redeployed, and returned to service. It is up
and running again after the reflash.

## Notes

- Device: `lux-wf5ama`
- Observed symptom: deployed code disappeared from CIRCUITPY.
- Remaining files: default TOML files only.
- Pre-reflash firmware in broker logs: `v0.26.143.9`.
- Post-reflash firmware in broker logs: `v0.26.143.10`.
- Broker-visible silence: after `12:09:55` until `13:25:24` local time in the
  pasted sample.
- Cause: unknown.
- Current status: recovered after full reflash.

## Follow-Up Signals To Watch

- Repeated filesystem loss on `lux-wf5ama` or another lux-profile device.
- Any preceding `runtime action=reset`, `runtime action=reload`, or
  CircuitPython filesystem error.
- Whether the app filesystem was writable at the time of failure.
- Host-side USB mount or deploy activity near the crash time.
- Power instability or brownout signs before the device disappeared.
