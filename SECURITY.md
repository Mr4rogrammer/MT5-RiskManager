# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Contact:** Krishna Kumar Eswaran — [krishnakumar.eswaran@zohocorp.com](mailto:krishnakumar.eswaran@zohocorp.com)

Please do **not** open a public GitHub issue for security vulnerabilities. Instead, email the maintainer directly with:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Timeline

- **Acknowledgment**: Within 48 hours of receiving the report.
- **Assessment**: Within 7 days, the vulnerability will be assessed and prioritized.
- **Fix & Disclosure**: A fix will be developed and released as soon as possible. Public disclosure will follow after the fix is deployed.

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| main    | ✅ Yes             |

## Security Best Practices

When deploying this project:

- Never commit `.env` files or credentials to version control.
- Use strong, unique passwords for all services (PostgreSQL, Traefik, MT5 broker).
- Keep Docker images and dependencies up to date.
- Restrict network access to sensitive services (Redis, PostgreSQL) to internal networks only.
- Enable HTTPS via Traefik with valid SSL certificates.
- Regularly review and rotate API keys and broker credentials.
