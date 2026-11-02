# Security Policy

## Reporting a vulnerability

**Do not report security vulnerabilities in public GitHub issues, pull requests or
discussions.** This repository is public: anything posted there is visible to everyone before
a fix is available.

Report it privately instead, through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/deapi-ai/mcp-server-deapi/security/advisories/new)).

Please include:
- the affected version (`deapi_mcp.__version__` or the git tag);
- what an attacker can do, and under which configuration;
- steps to reproduce, or a proof of concept;
- any suggested fix.

Do not include real deAPI API keys or other credentials in a report. Use a revoked or test key.

We will acknowledge your report and keep you informed until the issue is resolved. Please give us a reasonable time to release a fix before disclosing it publicly.

## Supported versions

Security fixes go into the latest release. Upgrade to the newest tag to receive them.

| Version | Supported |
|---------|-----------|
| latest 0.5.x | Yes |
| older | No |

## Scope

This repository: the MCP server, its OAuth endpoints, the tools and the optional webhook
receiver. Issues in the deAPI platform itself (`api.deapi.ai`) or in the
[deAPI Python SDK](https://github.com/deapi-ai/deapi-python-sdk) can be reported the same way;
we route them to the right team.
