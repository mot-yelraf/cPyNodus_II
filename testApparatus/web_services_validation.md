# NodusWeb service regression validation

Firmware under test: `v0.26.289.3`. Deploy using the operator's normal process;
the agent must not write to `/Volumes/CIRCUITPY`. Keep socket-control changes
deferred until these checks establish the behavior of this revision.

## Read-only checks

1. Confirm the new boot version, `profile=nodusweb`, filesystem mode, device IP,
   and active serial capture. Avoid simultaneous browser and curl tests.
2. Run the paced host sweep described in [profile_webui.md](profile_webui.md).
   Compare the complete response lengths and timings with the prior run:
   `/setup` completed twice in 1.25–1.39 seconds on the reported device.
3. In the operator's browser, navigate Status, Network twice, Time, Profile &
   MQTT, Sensor Display, Calibration, and Info sequentially. Repeat after several
   minutes. Correlate requests with
   serial heap, timeout cleanup, listener restart, and web-health records.
4. With browser network tools, confirm only one Status request is in flight,
   no refresh requests start while the tab is hidden, and returning resumes
   polling. Repeat with a delayed or interrupted response. Record the browser
   used; Chromium does not establish Safari behavior.

The combined Setup form previously measured 8953 bytes in the host fixture.
The split fixture measures Network 6030, Time 6245, Profile & MQTT 6578, and
Sensor Display 7526 bytes. Extra navigation links add 119 bytes to each of the
other pages. These are response-size checks, not board heap measurements. Compare
post-GC free heap and repeated navigation against the prior firmware; do not
infer lack of memory degradation from host tests alone.

## Operator-controlled mutation and interruption checks

These steps change settings, switch state, or connection behavior. Use a test
device and restore the original values afterward.

- In ROFS, save Sensor Display and Time edits: confirm volatile feedback and
  the live result. Submit an SSID/profile edit: confirm `settings_not_writable`,
  no applied restart-required setting, and no automatic reboot. Mixed batches
  may apply live-safe entries while reporting the unsaved restart fields.
- In RWFS, save a harmless setting, confirm persistence, and explicitly test
  Save & Restart. Check the saved value after reboot. Also verify that a
  failed/partial save does not trigger an automatic restart.
- Confirm each page submits only its own fields. Saving Time or Sensor Display
  must not submit Network, MQTT, or Profile values. Confirm the other pages'
  values are unchanged and both passwords remain password inputs.
- Submit `/config` with `{"updates":[null]}`, invalid numeric values, or more
  than 32 entries: expect HTTP 400 and unchanged settings. Follow each with a
  successful `/current-data` or `/setup` GET.
- Send a declared body larger than 4096 bytes: expect connection closure and
  a body-limit diagnostic, then confirm another GET succeeds.
- Interrupt a Setup download. If a response-send errno 32 occurs, confirm
  `web_poll_recovered`, continued ready health, and a successful subsequent GET.
  A disconnect that produces another errno does not validate the errno-32 case.
- On a switch-equipped test device, verify five-second manual guarding and
  automation ownership still hold. Confirm a slow refresh cannot overlap a
  switch request, and an interrupted switch response refreshes state without
  resending the command.

Record firmware, board/CircuitPython version, browser, fixture conditions,
response timings/sizes, post-GC heap, and new recovery/error events. Host tests
and MPY compilation do not establish RF/socket behavior or absence of on-device
degradation.
