---
name: trust-boundaries
description: Follow untrusted data from source to sink and check every trust boundary the change touches
categories: [vulnerability]
---

# Trust boundaries

A vulnerability is attacker-controlled data reaching a sink that trusts it, or a check that should
stop the attacker and does not. Work from both ends.

## Sources (where outside data enters)

Request parameters, headers, cookies and bodies; uploaded files and archives; CLI arguments and
environment variables of a service; messages from queues and other services; database rows written
by users; webhooks; anything read from a URL.

## Sinks (where data becomes action)

- **Command execution**: shell strings, `eval`, template engines, dynamic imports.
- **Queries**: SQL, NoSQL filters, LDAP, XPath built by string formatting.
- **Files and paths**: path joins with user input (`../`), archive extraction (zip slip), symlinks.
- **Network**: server-side requests to user-chosen URLs (SSRF), redirects to user-chosen URLs.
- **Deserialisation**: pickle, YAML `load`, Java serialisation, PHP `unserialize`.
- **Output**: HTML without escaping (XSS), headers built from input (header injection), logs that
  record secrets.

## Checks that must hold

- **Authentication** on every new route or handler, not just the old ones.
- **Authorisation**: the object being read or changed belongs to the caller (IDOR). Check the
  query, not the UI.
- **Secrets**: no keys, tokens or passwords in code, logs, error messages or URLs.
- **Crypto**: no home-made crypto, no MD5/SHA1 for passwords, constant-time comparison of tokens,
  real randomness (`secrets`, `crypto/rand`) for tokens.

## Reporting

Name the source, the sink and the path between them with file:line for each hop. Give the CWE. A
sink that only trusted code can reach is not a vulnerability; say why you think the source is
reachable. Put a minimal input that triggers it in `reproduction`.
