# Security policy

## Reporting a vulnerability

E-mail **velaris.app.os@gmail.com** with the subject line starting
**`SECURITY:`**. Please don't open a public issue, discussion or pull request
for a vulnerability.

Include what you can:

- the affected version (`version:` in `velaris.yaml`);
- what an attacker can do, and what they need first (an account? which role? a
  tenant?);
- the steps or the request that shows it;
- whether you know of it being used.

You don't need a complete exploit. A clear description is enough.

## What we do, and when

| | Within |
|---|---|
| Acknowledge your report | 3 working days |
| Tell you whether we can reproduce it, and how serious we think it is | 10 working days |
| Release a fix for a critical or high-severity issue | 30 days, sooner when it is being used |
| Release a fix for a medium or low-severity issue | the next planned release |

We will keep you informed, credit you in the release notes if you want, and
agree a disclosure date with you. We ask you to give us until the fix is
released before you publish.

## Scope

In scope: this repository, everything it installs (the services, Studio, the
gateway, the scripts, the Helm chart), and the default configuration it
generates.

Out of scope: a server whose operator changed the generated secrets to weak
ones or exposed internal ports; denial of service by volume; social engineering;
third-party services you connect (your AI provider, your SMTP server).

## Safe harbour

Research done in good faith, on your own installation, that does not touch
anyone else's data or service, will not be pursued. If you are unsure whether
something is in bounds, ask first.

## Supported versions

Security fixes are released for the **current minor version** (3.1.x today).
An installation takes them with `./update-velaris.sh`. A release that carries a
security fix says so in its notes (`releases/notes/`) and in `CHANGELOG.md`.
