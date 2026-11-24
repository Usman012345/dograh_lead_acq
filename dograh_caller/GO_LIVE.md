# Going live: scraper → `https://ai-sdr.grobots.ai` → dograh caller

```
 dograh caller (any machine)                    scraper server vmi3720154 (79.143.180.241)
 ───────────────────────────                    ─────────────────────────────────────────────────────
 dashboard_server.py (or pull_leads.py)         nginx 1.24 (already there for scrapper.grobots.ai)
   GET https://ai-sdr.grobots.ai/leads/next ──►   + new site ai-sdr.grobots.ai, Let's Encrypt cert
   [?limit=1..1000]  Bearer <token>               │ proxy_pass http://127.0.0.1:8090, Host passed on
                                                    ▼
   ◄── up to limit (default 300) leads ───────  lead-streamer.service (127.0.0.1:8090)
   only after Start Batch / Enter; stops when done  │
                                                    ▼
                                                PostgreSQL "dataops" (container dataops_db, 127.0.0.1:5432)
```

Do the steps in order; each ends with a check. Nothing here restarts the scraper,
its containers, or the other nginx site.

---

## 1. Streamer on the scraper server

Copy the `lead_streamer` folder **with its `.env`** to the server (`.env` is git-ignored, so copy it
by hand, e.g. `scp -r lead_streamer root@79.143.180.241:~/`), then:

```bash
cd ~/lead_streamer && sudo bash install.sh
```

**Check:** the output includes `All checks passed.` and `OK   leads: … with a phone`, and ends
with `health: {"status":"alive","database":"ok"}`.

## 2. nginx site (next to the existing one)

```bash
sudo nginx -t                                   # must pass BEFORE you change anything
sudo cp ~/lead_streamer/nginx-lead-streamer.conf /etc/nginx/sites-available/ai-sdr.grobots.ai
sudo ln -s /etc/nginx/sites-available/ai-sdr.grobots.ai /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx    # reload, never restart
```

**Check (works before DNS moves):**

```bash
curl -s -H 'Host: ai-sdr.grobots.ai' http://127.0.0.1/health
# {"status":"alive","database":"ok"}     <- the streamer, through nginx
curl -s https://scrapper.grobots.ai/ -o /dev/null -w '%{http_code}\n'
# 200                                     <- the scraper's own site is untouched
```

If the first command prints anything else (an nginx 404 page, or the scraper backend's
`{"status":"healthy","service":"DataOps AI Backend"…}`), the new site is not active and the
scraper's default site answered: check the symlink and `server_name`, then
`sudo nginx -t && sudo systemctl reload nginx` again.

## 3. Old push setup off (scraper server)

The earlier `pg-sender` pushed to `ai-sdr.grobots.ai/ingest`; it is replaced by the pull.

```bash
sudo systemctl disable --now pg-sender
sudo sed -i '/[[:space:]]ai-sdr\.grobots\.ai$/d' /etc/hosts   # it pinned the name to the mail server
getent hosts ai-sdr.grobots.ai                                 # must NOT print 167.88.38.179
```

## 4. DNS

At GoDaddy (DNS for `grobots.ai`): change the **A** record `ai-sdr` from `167.88.38.179` to
**`79.143.180.241`**. There is no AAAA record for it; don't add one. TTL is 600 s.

**Check (after ~10 minutes):**

```bash
nslookup ai-sdr.grobots.ai 8.8.8.8        # Address: 79.143.180.241
```

## 5. Certificate (scraper server)

```bash
sudo certbot plugins 2>/dev/null | grep -q nginx || sudo apt install -y python3-certbot-nginx
sudo certbot --nginx -d ai-sdr.grobots.ai --redirect
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew --dry-run               # renewal works (certbot's timer renews it)
```

Port 80 on this server is reachable from the internet (checked 2026-11-20), which certbot needs.

**Check from anywhere:**

```bash
curl -s https://ai-sdr.grobots.ai/health                                   # {"status":"alive","database":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://ai-sdr.grobots.ai/leads/next   # 401 (token required)
```

No `-k`: the certificate must be valid as is.

## 6. Dograh caller

On the machine that runs the caller (its `.env` holds `LEAD_SOURCE_TOKEN`, equal to the
streamer's `STREAM_TOKEN`):

```bash
bash install.sh            # Windows: .venv\Scripts\python pull_leads.py --check
```

**Check:** `All checks passed. Nothing was taken from the streamer.` Then start the dashboard (prints `Lead source (reverse proxy): https://ai-sdr.grobots.ai` and `Batch runner is idle` until you click **Start Batch**):

```bash
tmux new -s caller
.venv/bin/python dashboard_server.py
```

In the dashboard **Start Lead Batch Run** dialog, you can choose `1`–`1,000` leads per batch (`?limit=<batch_size>`) and `1`–`100` batches; starting a batch clears the live view to show only that job's leads (while previous batches remain accessible from the top-bar **Batches** dropdown), and the runner stops automatically once finished. (Or use `.venv/bin/python pull_leads.py` in the terminal — it waits for you to press **Enter** before every ping, including the first, and pulls directly from the reverse proxy `https://ai-sdr.grobots.ai`.)

Results: `output/call_results.xlsx` (every lead), `output/transferred_calls.xlsx` (only transferred calls), and `output/pipeline_tracker.xlsx` (live per-lead dashboard state).

## 7. Mail server cleanup (optional, after step 6 works)

`ai-sdr.grobots.ai` on the mail server (CyberPanel site + `dict-receiver` on port 8010) is no
longer used. Its certificate can't renew there once DNS points elsewhere, so CyberPanel will
complain later. When convenient:

```bash
sudo systemctl disable --now dict-receiver
```

and delete only the website `ai-sdr.grobots.ai` in CyberPanel (Websites → Delete). Do not
touch `dograh.grobots.ai` or other sites; use `lswsctrl reload`, never `systemctl restart lsws`.

---

## If `pull_leads.py --check` fails

| It says | Meaning | Fix |
|---|---|---|
| `answered HTTP 200 {'status': 'ok'} - that is not the lead streamer` | the name still reaches the mail server's old receiver | DNS not moved yet / cached; wait for the TTL, check `nslookup` |
| `… {'status': 'healthy', 'service': 'DataOps AI Backend' …}` | nginx answered with the scraper's site | step 2: site not enabled or wrong `server_name` |
| `TLS/certificate problem` (certificate for `scrapper.grobots.ai`) | certbot hasn't run for `ai-sdr` | step 5 |
| Intermittent `TLS/certificate problem` or `404` (~70% fail, ~30% succeed) | stale nginx workers or duplicate nginx process still serving `scrapper.grobots.ai`'s certificate after `reload` (fails at TLS handshake before any token is sent or leads are marked sent) | inspect `ps aux \| grep nginx` and `sudo ss -tulpn \| grep -E ':80\|:443'`, then `sudo nginx -t && sudo systemctl restart nginx` |
| `HTTP 421` | nginx doesn't pass the name on | keep `proxy_set_header Host $host;` in the site |
| `HTTP 404` | request didn't reach the streamer | step 2 |
| nginx `502 Bad Gateway` | streamer not running | `systemctl status lead-streamer`, `journalctl -u lead-streamer -n 30` |
| `503 … database_unavailable` | streamer can't reach PostgreSQL | `docker ps` (dataops_db up?), `.env` `DATABASE_URL` |
| `503 … schema_mismatch` | wrong database or scraper migrations missing | `/opt/lead_streamer/.venv/bin/python /opt/lead_streamer/lead_streamer.py --check` |
| `token: rejected (HTTP 401)` | tokens differ | `LEAD_SOURCE_TOKEN` (caller) = `STREAM_TOKEN` (streamer); restart the streamer after changing it |

## What was tested before go-live (2026-11-20)

- Streamer against PostgreSQL 17.7 with the scraper's own schema: batches 300/300/50, no
  duplicates, phone-less leads never sent, updated leads re-sent once.
- Caller end to end with a fake Dograh: 650 leads → one result row each, campaigns per
  service, retries, skipped leads, transfers file, Enter required before every ping.
- The production path: `https://ai-sdr.grobots.ai` with certificate verification and the
  streamer's host check on, through a TLS proxy that forwards like this nginx site; an untrusted
  certificate and a proxy that drops the Host header were both refused.
- A kill / dropped SSH session mid-campaign pauses the Dograh campaigns and keeps the batch.

Not testable from outside the servers: the real DNS change, certbot on the scraper server,
and the server's data itself (step 1's `--check` covers that).
