# Security Policy

## Security limitations

This project is intended for hobbyist and educational use, and its security posture is weak by design. Wi-Fi credentials in `settings.toml` for Nodus devices that are 'onboarded' from Sensorius (System Settings > Add Device) are obfuscated with a reversible device-local cipher (not strong encryption). If you choose to manually add the wifi credentials to the settings.toml, these will remain in clear text. 

Care should be taken if the plan is to deploy this firmware in environments where exposure of Wi-Fi credentials or device access would be unacceptable.

This project is not hardened against physical access to the device filesystem.
If an attacker has access to the CIRCUITPY drive, credentials may be recoverable.

OTA firmware packages require an RSA-2048/SHA-256 signature over the exact
manifest bytes. Devices store only the trusted public key; release private keys
must remain outside this repository and outside deployed devices. OTA package
transport remains plain HTTP on the local network, so signing protects package
authenticity and integrity but does not provide transport confidentiality.

## Reporting a vulnerability

No formal security audit has been performed.

If you discover a security issue, please report it privately.

- Email: mot.yelraf@gmail.com

Please include:

- A clear description of the issue
- Steps to reproduce
- Any relevant logs or screenshots

We will acknowledge reports within 7 days and provide a timeline for fixes when possible.
