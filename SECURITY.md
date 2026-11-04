# Security policy

## Reporting a vulnerability

Please open a private report through GitHub's **Security → Report a vulnerability** on this repository. Do not open a public issue for security problems.

## Design rules this project keeps

- No secrets in the repository. All credentials are mounted files read at runtime; CI and a pre-commit hook run gitleaks on every change.
- State-changing actions (add, delete, cleanup) never run on the model's say-so. They need an inline-button confirmation enforced in code: a random nonce, an expiry, and a check that the same Telegram user who asked is the one who confirms.
- Text from external services (titles, overviews, file names) is untrusted data and is passed to the model only as structured tool results.
- Filesystem access is limited to configured allowed roots, resolved with `realpath`; symlinks, traversal and the roots themselves are refused.
- `DRY_RUN` is on by default.
- If a secret is ever committed, published history is not rewritten to hide it: rotate the secret first.
