# Security Policy

## Supported versions

FastClaw Python is pre-release software. Security fixes are applied to the
latest revision on the `main` branch.

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in a public issue. Use the
repository's **Security** tab to submit a private vulnerability report. Include
the affected version or commit, reproduction steps, impact, and any suggested
mitigation.

Maintainers will acknowledge a complete report within five business days and
will coordinate remediation and disclosure with the reporter.

## Provider credential storage

Provider API keys are encrypted with AES-GCM and bound to their stable config
record ID. The database contains only versioned ciphertext. The master key is
loaded from `FASTCLAW_MASTER_KEY` or a data-root `master.key` file. POSIX hosts
restrict the file to mode `0600`; Windows stores a current-user DPAPI-protected
payload and does not treat POSIX mode bits as an ACL. A DPAPI key file is not
portable to another Windows account or device, so cross-device deployments must
share `FASTCLAW_MASTER_KEY` through a secret manager. The key must never be
committed or stored beside database backups. Browser/API responses contain only
masked credentials, and production browser traffic must use HTTPS.

## Outbound web fetch policy

The built-in `web_fetch` tool accepts only HTTP(S) URLs without embedded
credentials. Every redirect is resolved independently, and every returned IP
must be globally routable. The Runtime pins the validated IPs at the TCP layer
while preserving the original hostname for HTTP Host, TLS SNI, and certificate
validation. Unpinned connections, Unix sockets, and environment proxies are
denied by the dedicated web-fetch transport.
