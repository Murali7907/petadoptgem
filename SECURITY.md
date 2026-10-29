# Security Policy

## Supported Versions

Only the latest release on the primary branches is actively maintained and supported with security patches:

| Branch / Version | Supported          |
| ---------------- | ------------------ |
| `main`           | :white_check_mark: |
| `develop`        | :white_check_mark: |
| < 1.0.0          | :x:                |

## Reporting a Vulnerability

We take the security and integrity of the KindHeart Pet Adoption platform very seriously. If you discover a vulnerability or security flaw, please report it privately:

1. **GitHub Private Vulnerability Reporting**:
   - Go to the repository's **Security** tab on GitHub.
   - Click on **Report a vulnerability** to open an advisory draft.
   - Include clear steps to reproduce, affected endpoints, proof-of-concept payloads, and impact assessment.

2. **Direct Security Contact**:
   - If GitHub Private Vulnerability Reporting is unavailable, you may contact the repository maintainer privately via GitHub (`Murali7907`).

Please **DO NOT** open public GitHub issues, discussions, or pull requests for suspected security vulnerabilities.

### What to Expect
- **Acknowledgement**: Within 48 hours of initial report receipt.
- **Triage & Assessment**: Confirmation and severity triage within 5 business days.
- **Patch & Disclosure**: Coordinated patch development, verification on `develop`, deployment to `main`, and public release advisory.

## Credential Safety & Zero Secret Policy

> [!CAUTION]
> **No real credentials, API secrets, database passwords, or private encryption keys belong in this repository.**

- All secrets, tokens, credentials, and API keys must be provided exclusively via environment variables or secret management services (e.g. AWS Secrets Manager, HashiCorp Vault, GitHub Actions Secrets).
- The repository `.env.example` file contains sanitized dummy placeholders only.
- Continuous Integration (`.github/workflows/ci.yml`) runs strict secret scanning and security checks (`python manage.py check --deploy`).
