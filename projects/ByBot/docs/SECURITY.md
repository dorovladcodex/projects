# Security policy

## v1 threat boundary

Live trading is explicitly disabled. The accepted v1 modes are `DATA_ONLY`,
`PAPER`, and `BYBIT_DEMO`; `LIVE` is rejected during configuration validation.
Shadow live is observation-only and must not submit orders.

The main assets to protect are API credentials, Telegram credentials, LLM
credentials, market/news integrity, decision records, PostgreSQL data, and the
deployment host.

## Secrets

- Never commit `.env`, API keys, tokens, database passwords, or exported logs
  containing secrets.
- Keep production and demo credentials completely separate.
- For local development, use `.env` with restricted user permissions.
- For GCP, use Secret Manager or a root-readable deployment environment file;
  do not bake secrets into images or source control.
- Rotate credentials after suspected disclosure and periodically in deployed
  environments.
- Redact authorization headers, query signatures, tokens, and full exception
  payloads before logging.

## Bybit controls

- Phase 1 uses mocks and does not require a Bybit key.
- Phase 3A may use Bybit demo read-only keys for private account data only.
- DATA_ONLY and shadow-live public data should use no authenticated key.
- BYBIT_DEMO must use demo-only endpoints and demo-only credentials.
- Never grant withdrawal permission.
- Apply the narrowest API permissions and IP allowlisting available.
- `BYBIT_ENABLE_TRADING=true` is rejected during configuration validation in
  Phase 3A.
- The application must reject production trading endpoints in v1.
- A configured credential must not imply permission to execute.

## Application controls

- Keep the mode allowlist in validated configuration.
- Preserve the independent `RiskManager` as final authority.
- Default to `NO_TRADE` on stale data, classifier failure, low confidence,
  provider instability, wide spread, or insufficient expected edge.
- Require a stop loss and limit to one position and 1x-2x leverage.
- Never allow the LLM to create execution commands or override deterministic
  controls.
- Validate all external data and cap lengths, timestamps, and numeric ranges.
- Add authentication before exposing the dashboard beyond localhost.

## LLM classifier boundary

- Mock classification remains the default; real LLM use is explicit through
  `NEWS_CLASSIFIER_MODE=llm`.
- Send only normalized filtered-news title, truncated summary, asset hint,
  source, publication timestamp, and importance.
- Never include Bybit keys, authorization headers, account balances, positions,
  orders, private market data, logs, raw HTML, or arbitrary application state.
- Treat article text as untrusted data and instruct the provider to ignore
  embedded instructions, links, and commands.
- Validate strict structured output and fail closed to neutral, zero-confidence,
  non-tradeable classifications on any provider/schema/budget failure.
- Do not log complete provider requests/responses or authorization metadata.
- Mock fallback is local-test-only, explicit, visibly marked, and non-tradeable.
- Recalculate trade eligibility at classifier, ingestion, test-endpoint, and
  signal boundaries; never trust a provider-supplied eligibility flag.

## Host and network controls

- Do not expose PostgreSQL port 5432 publicly.
- Do not expose the FastAPI development server directly to the internet.
- Restrict SSH to IAP or known administrator addresses and require key-based
  authentication.
- Use TLS at a managed load balancer or hardened reverse proxy before any
  remote dashboard access.
- Run containers as a non-root user in the production image and keep the host
  and container runtime patched.
- Allow outbound traffic only to required news, LLM, Telegram, and approved
  Bybit endpoints when practical.

## Supply chain and repository

- Require pull-request review for changes to configuration, execution, risk,
  deployment, or dependencies.
- Pin and routinely review dependency versions before production deployment.
- Run tests and security scanning in CI.
- Protect the default branch and prevent secret commits with automated scans.
- Build immutable images and record the Git commit and image digest deployed.

## Audit and incident handling

Record mode changes, classifications, signals, risk decisions, skipped trades,
paper/demo orders, position changes, provider failures, and operator actions.
Use UTC timestamps and protect audit records from routine deletion.

On suspected compromise: stop the service, revoke affected credentials,
preserve logs and database snapshots, identify the deployed image/configuration,
and resume only in `DATA_ONLY` after review.

## Future live trading

Live execution is outside v1. It requires a separate threat model, architecture,
credential path, approval process, kill switch, reconciliation design, security
review, and release. No environment-variable change may promote v1 to live.
