# Lead Streamer

Standalone HTTP pull service (`Python 3.10+` + `psycopg` v3) that reads eligible leads from PostgreSQL and serves up to `300` leads per request (`BATCH_SIZE`, max 1,000) to the dograh caller, tracking delivery state solely in `lead_stream.sent_leads`.

This directory is completely self-contained and can be copied anywhere on a server or removed from the repository without affecting any other component.

## 0. Quick install (scraper server)

`.env` in this folder is already filled in for the scraper's database (`dataops`
on `127.0.0.1:5432`) and holds the token the dograh caller uses. Copy the whole
folder to the server, then:

```bash
sudo bash install.sh          # user, /opt/lead_streamer, venv, database check, systemd, health check
```

Check the database and schema at any time, without changing anything:

```bash
/opt/lead_streamer/.venv/bin/python /opt/lead_streamer/lead_streamer.py --check
```

If tables or columns the streamer reads are missing (wrong database, scraper
migrations not applied), `--check` names them, and `/leads/next` answers
`503 {"error":"schema_mismatch"}` until it is fixed.

The steps below are what `install.sh` does, for doing it by hand.

## 1. Install

```bash
sudo mkdir -p /opt/lead_streamer && sudo cp -r lead_streamer/* /opt/lead_streamer/
cd /opt/lead_streamer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Configure

Create `.env` from the template and lock down permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Generate a `STREAM_TOKEN` (minimum 32 characters):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Edit `.env` and set `DATABASE_URL` and `STREAM_TOKEN`. `PUBLIC_BASE_URL` is set to `https://ai-sdr.grobots.ai` at the top of `lead_streamer.py`; a `PUBLIC_BASE_URL` line in `.env` overrides it.

### Least-privilege PostgreSQL setup (optional)

If the runtime database role does not have `CREATE` privilege on the database, run this SQL once as a DBA:

```sql
CREATE SCHEMA IF NOT EXISTS lead_stream;

CREATE TABLE IF NOT EXISTS lead_stream.sent_leads (
    lead_id      varchar(100) PRIMARY KEY,
    sent_version integer      NOT NULL,
    sent_at      timestamptz  NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA lead_stream TO lead_streamer_user;
GRANT SELECT, INSERT, UPDATE ON lead_stream.sent_leads TO lead_streamer_user;
GRANT SELECT ON public.leads, public.organizations, public.contacts, public.phones, public.emails, public.locations TO lead_streamer_user;
```

## 3. Run

### Local / manual run

```bash
.venv/bin/python lead_streamer.py
```

### Production (systemd + nginx)

Create a dedicated system user and set ownership of `/opt/lead_streamer`, then install the systemd service:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lead-streamer
sudo chown -R lead-streamer:lead-streamer /opt/lead_streamer
sudo cp lead-streamer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lead-streamer
journalctl -u lead-streamer -f
```

Add the nginx site from `nginx-lead-streamer.conf` (next to the scraper's existing site), move the DNS record, then run certbot. The exact steps and checks are in `../GO_LIVE.md`.

## 4. API & Ping Examples

```bash
# Liveness (no token required)
curl -s https://ai-sdr.grobots.ai/health

# Progress stats (token required)
curl -s -H "Authorization: Bearer $STREAM_TOKEN" https://ai-sdr.grobots.ai/stats

# Pull next batch of up to 300 leads (or request 1..1000 via ?limit=)
curl -s -H "Authorization: Bearer $STREAM_TOKEN" https://ai-sdr.grobots.ai/leads/next
curl -s -H "Authorization: Bearer $STREAM_TOKEN" "https://ai-sdr.grobots.ai/leads/next?limit=50"
```

## 5. Verify & Operations

```bash
# Service status and today's logs
systemctl status lead-streamer
journalctl -u lead-streamer --since today | tail -50
```

To re-send leads without restarting:

```sql
-- Re-send a single lead
DELETE FROM lead_stream.sent_leads WHERE lead_id = '<lead id>';

-- Re-send everything sent today
DELETE FROM lead_stream.sent_leads WHERE sent_at >= date_trunc('day', now());

-- Start over completely
TRUNCATE lead_stream.sent_leads;
```

To remove the streamer completely:

```bash
sudo systemctl disable --now lead-streamer
```

```sql
DROP SCHEMA lead_stream CASCADE;
```
