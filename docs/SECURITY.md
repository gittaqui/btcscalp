# Security and operator controls

- Use a dedicated spot account with a limited balance. No margin, borrowing or derivatives. Do not share the account with manual trades or another bot.
- Use account-scoped time-based-nonce keys. Entry key: Trader, Require Heartbeat on. Protection key: Trader, Require Heartbeat off. No withdrawal permissions on either key. Apply IP allowlisting where the account supports it.
- Keep sandbox and production keys completely separate. `.env` is ignored by Git; production secrets belong in environment injection or a secret manager. Do not paste secrets into issues, PRs, screenshots or generated reports.
- Generated logs redact configured key/secret/token/webhook values. Signed payloads and response bodies are not logged. Do not enable HTTP wire logging in production.
- Lock down SSH before funding: individual public keys, no password authentication, no root login, and restricted administrator access. Verify a second login before closing the original session.
- Bind dashboard/metrics to loopback. The Compose mapping is `127.0.0.1:8080:8080`. Access remotely over SSH. Use a random token of at least 32 characters and rotate it after operator access changes. TLS/authentication is required if an external proxy is introduced.
- Dashboard state-changing operations require an authorization header and confirmation for resume, flatten, kill and reset. No risk-parameter editor is exposed over HTTP. Configuration changes require stopped/flat operation and new evidence.
- Run the application as a non-root user. Containers have no Linux capabilities and use read-only roots. Do not mount Docker's socket, SSH keys or the host root into a container.
- Install security updates during flat maintenance windows; avoid unexpected live reboots. Keep clocks synchronized and disks monitored. Restart policies preserve kill latches and do not approve new exposure after an unclean live restart.
- Back up SQLite using its online backup API; encrypt/access-control backups and test restore. Do not restore a stale database over a funded live runtime. Reconcile orders, fills and both BTC/USD balances first.
- The guardian and runtime share an advisory execution lock. File permissions, API key settings and evidence artifacts are operator-controlled trust boundaries, not a defense against a malicious host administrator.
- Do not enable alerts to a public channel containing account balances or personal information. The default alert messages omit secrets and dollar amounts. Configure a private operator webhook and test delivery before live use.
- Native stops and heartbeat cancellation limit certain failure modes but do not guarantee a maximum monetary loss. Price gaps, minimum order sizes, API outages and exchange failures can defeat a timely exit.

No withdrawal endpoint, browser automation, scraped interface or private/undocumented exchange API is included.
