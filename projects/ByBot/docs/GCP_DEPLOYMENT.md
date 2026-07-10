# Planned GCP deployment

## Status and safety boundary

This document describes a future deployment phase; it does not authorize a
deployment today. ByBot v1 may run on GCP only in `DATA_ONLY`, `PAPER`, or
`BYBIT_DEMO`. Live trading remains explicitly disabled. Shadow live is
observation-only and submits no orders.

The current `docker-compose.yml` is development-oriented: it exposes FastAPI
and PostgreSQL ports and contains local database defaults. Do not use it as-is
on an internet-facing VM.

## Target topology

- One dedicated Compute Engine VM in a selected region and private VPC.
- Docker Engine with the API and PostgreSQL containers managed by Compose.
- Persistent disk for PostgreSQL and encrypted backups.
- Secret Manager or a tightly permissioned environment file for secrets.
- Cloud Logging/Monitoring for logs, uptime, disk, CPU, memory, and alerts.
- IAP or a restricted administrative path for SSH.
- No public PostgreSQL endpoint.
- No public FastAPI endpoint until authentication and TLS are implemented.

Managed Cloud SQL is preferable once persistence becomes operationally
important, but a VM-local PostgreSQL container is acceptable for a controlled
paper-stage pilot with tested backups.

## Required deployment hardening

Before the first GCP deployment:

1. Add a production Compose override that removes the PostgreSQL host-port
   mapping and binds any temporary API port to localhost only.
2. Replace development database credentials and move secrets outside Git.
3. Run the application container as a non-root user.
4. Pin container image versions and deploy an immutable application image.
5. Add PostgreSQL migrations, persistent-volume ownership checks, backups, and
   restore tests.
6. Add structured logs, health monitoring, retention, and alerting.
7. Add dashboard authentication and TLS before remote access.
8. Confirm `BOT_MODE=DATA_ONLY` and verify `/status` reports
   `live_trading=false`.

## Planned rollout

### 1. Provision

- Create a least-privilege GCP project/service account.
- Create the VPC, firewall rules, VM, static internal addressing, and persistent
  disk.
- Permit SSH only through IAP or a documented administrator source.
- Enable OS Login, automatic security updates, monitoring, and logging.

### 2. Install and configure

- Install Docker Engine and the Compose plugin from trusted repositories.
- Clone the protected repository at a reviewed commit.
- Fetch secrets at deployment time; never copy a developer `.env`.
- Validate the resolved Compose configuration before startup.
- Record the commit, image digest, configuration version, and operator.

### 3. Start in DATA_ONLY

- Start PostgreSQL and the API.
- Verify `/health`, `/status`, logs, time synchronization, disk persistence,
  restart behavior, and backups.
- Run without authenticated Bybit credentials.
- Observe at least one agreed stability window before changing mode.

### 4. Promote to PAPER

- Review data completeness, stale-data blocks, signal audit records, and alerts.
- Change only `BOT_MODE` to `PAPER`, redeploy, and verify status again.
- Confirm every order and position is simulated locally.

### 5. Promote to BYBIT_DEMO

- Complete a separate readiness review.
- Add demo-only credentials from the secret store and enforce demo endpoints.
- Test idempotency, stop behavior, reconciliation, provider outages, restarts,
  and the operational kill procedure.
- Do not add production trading credentials.

### 6. Shadow live

- Use public production feeds with hypothetical execution only.
- Keep authenticated production trading credentials absent.
- Compare paper assumptions with observed spreads, latency, liquidity, funding,
  and slippage while retaining `live_trading=false`.

## Deployment verification

For every deployment, verify:

```text
GET /health  -> status == "ok"
GET /status  -> mode is allowlisted
GET /status  -> live_trading == false
```

Also verify tests passed for the deployed commit, the database is not publicly
reachable, backups are current, alerts reach operators, and the previous image
can be restored.

## Rollback

Stop the application, preserve database and logs, restore the previous reviewed
image and matching configuration, and restart in `DATA_ONLY`. Reconcile all
paper/demo state before any later promotion.

## Explicitly out of scope

- Production Bybit order submission
- Production trading credentials
- Automated promotion from demo or shadow mode
- Any mechanism that changes v1 into a live-trading system
