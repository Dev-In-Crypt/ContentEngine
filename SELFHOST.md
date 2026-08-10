# Self-Hosting on a $5 VPS

Run the public multi-tenant site on your own server with Docker Compose. This is
the cheapest path (~$5/mo for the VPS; Postgres + storage run in containers at no
extra cost). You manage the box; you get automatic HTTPS out of the box.

**Stack:** the app + Postgres + Caddy (reverse proxy that auto-provisions a
Let's Encrypt TLS certificate). All defined in `docker-compose.yml`.

## 1. Get a VPS + a domain

- Rent a small VPS (e.g. **Hetzner CX22 ~€4/mo**, or a $5–6 droplet). Ubuntu is fine.
- Point a domain (or subdomain) at it: add a **DNS A-record** → the VPS's public IP.
- Make sure ports **80** and **443** are open (needed for HTTPS certificates).

## 2. Install Docker on the VPS

```bash
curl -fsSL https://get.docker.com | sh
```

## 3. Get the code + configure

```bash
git clone https://github.com/Dev-In-Crypt/ContentEngine.git
cd ContentEngine
cp compose.env.example .env
nano .env        # fill in DOMAIN, SECRET_KEY, DB_PASSWORD, ADMIN_EMAILS, PUBLIC_BASE_URL
```

Generate a strong `SECRET_KEY`:

```bash
openssl rand -base64 48
```

> **⚠️ SECRET_KEY is permanent.** It signs logins **and** encrypts users' stored
> API keys. Never change it after go-live — rotating it logs everyone out and
> makes every stored credential undecryptable.

## 4. Launch

```bash
docker compose up -d --build
```

First boot builds the image, starts Postgres, runs migrations, and Caddy fetches
the TLS certificate (takes ~30s). Then:

```bash
curl https://your-domain.com/health      # -> {"status":"ok"}
```

Open `https://your-domain.com/` → the **login / register** screen.

## 5. Use it

- Register (email + password) → **⚙️ Settings** → paste your own keys (OpenRouter
  for generation; Instagram token + user id + imgbb for IG publishing; X keys for X).
- Generate, schedule, publish — the server does 24/7 scheduled publishing with each
  user's own keys, even when your PC is off.

## Operations

```bash
docker compose logs -f app          # app logs
docker compose pull && docker compose up -d --build   # update to latest code (git pull first)
docker compose down                 # stop (data survives in named volumes)
```

**Automatic database backups.** The `backup` service dumps Postgres **daily**
(gzipped) into the `backups` volume and prunes anything older than
`BACKUP_KEEP_DAYS` (default 7). Nothing to schedule — it runs on `up -d`.

```bash
# list dumps
docker compose exec backup ls -lh /backups
# copy the latest dump to the host
docker compose cp backup:/backups ./db-backups
# restore a dump into the running DB
gunzip -c ./db-backups/insta_YYYYMMDD_HHMMSS.sql.gz | docker compose exec -T db psql -U insta insta
```

The dumps live in a Docker volume **on the same disk as the database they protect**.
That covers a bad migration or a dropped table; it does not cover a dead machine.
Pull them off the box:

```bash
pwsh scripts/pull-prod-backup.ps1              # Windows: copies the newest pair here
pwsh scripts/pull-prod-backup.ps1 -KeepDays 90 # keep a longer history than the server does
```

The script takes the newest **pair** — the database dump and the uploads archive
written by the same run — because picking the newest of each independently can
straddle two runs and pair a database with media from a different moment. It
verifies both after the copy by decompressing them: gzip carries a CRC, so a
transfer that stopped halfway fails there rather than the night it is needed.

Nothing on the server is deleted. Local copies older than `-KeepDays` (30 by
default) are pruned, which is the point of a second copy: the server keeps 7 days,
this one keeps as many as you ask for.

**Verify the restore, not just the file.** A dump nobody has ever restored is a
belief, not a backup. Into a scratch database, so the live one is never at risk:

```bash
gunzip -c /var/lib/docker/volumes/instacontentengine_backups/_data/insta_YYYYMMDD_HHMMSS.sql.gz \
  | docker compose exec -T db psql -U insta -d restoretest -v ON_ERROR_STOP=1 -q
# ...compare row counts against the live database, then:
docker compose exec -T db psql -U insta -d postgres -c "DROP DATABASE restoretest;"
```

(Create it first with `CREATE DATABASE restoretest;`. Never restore a dump into
`insta` to "test" it — the dumps are taken with `--clean --if-exists`, so they
drop what they find.)

**Uploads** (generated slides, raw images, reels) live in the `uploads` volume.
A daily in-app job removes files for deleted posts automatically; back the rest up with:

```bash
docker run --rm -v instacontentengine_uploads:/u -v "$PWD":/b alpine tar czf /b/uploads_$(date +%F).tgz -C /u .
```

**⚠️ Back up your `.env` too — especially `SECRET_KEY`.** It signs every login token
AND derives the key that decrypts every user's stored API keys. Lose it and all
stored keys become unreadable and everyone is logged out. Keep a copy of `.env`
somewhere safe and separate from the server.

(Admins listed in `ADMIN_EMAILS` can also use the in-app **Backup/Restore** under ⚙️ Settings.)

**Uptime monitoring.** Point a free external monitor (e.g. UptimeRobot) at
`https://<your-domain>/health` — it returns `{"status":"ok"}`. You'll get an alert
if the site goes down. Set `SENTRY_DSN` in `.env` to also capture backend errors.

## Notes

- **You maintain the server**: OS security updates, backups, uptime. That's the
  trade for the low cost — a managed host (see `DEPLOY.md` for Render) does this
  for you at higher price.
- Postgres runs as a container here (free); the app auto-normalizes its URL to the
  async driver. To use a managed Postgres instead, just point `DATABASE_URL` at it.
- **Schema migrations run automatically on startup** (Alembic). The first deploy
  onto your already-populated DB is auto-detected and *stamped* (adopted) rather
  than rebuilt, so no manual step is needed — just `docker compose up -d --build`.
- Single VPS = single instance, which is exactly what the app expects.
