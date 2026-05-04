"""Small OTA transfer verification module.

This file is intentionally simple. It gives early OTA tests a harmless payload
that can be staged and verified before live firmware replacement is enabled.
"""

OTA_TEST_NAME = "nodus-ota-transfer-check"
OTA_TEST_VERSION = 1


def describe():
    """Return a stable string for manual OTA transfer verification."""
    return "{}:{}".format(OTA_TEST_NAME, OTA_TEST_VERSION)
