# Nodus WeeWX skin files

This directory is the complete `Nodus` WeeWX skin bundle. It arranges
metric cards alphabetically, omits cards whose current observation is
unavailable, and reloads the generated report in the browser every 60 seconds.
When the optional Nodus automation service is enabled, it also shows each
configured switch rule's channel state, current decision, last confirmed
action, and error status.

For a fresh installation, copy `index.html.tmpl`, `skin.conf`, and `style.css`
into the host's `Nodus` skin directory. When updating a customized
installation, keep its existing `style.css` unless the supplied default style
is wanted.

On a package-installed WeeWX host the destination is commonly:

```text
/etc/weewx/skins/Nodus/
```

After copying the files, restart WeeWX or run the `Nodus` report manually
from a directory readable by the `weewx` service account.

See `docs/weewx.md` for complete Nodus provisioning, MQTTSubscribe, schema,
report, transfer, validation, and troubleshooting instructions.
