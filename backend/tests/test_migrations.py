"""Schema is managed by Alembic (main._run_migrations). A fresh DB gets the
baseline; a pre-existing DB (tables but no alembic_version) is auto-stamped so
the first deploy onto the live prod DB doesn't try to recreate existing tables."""
import sqlite3

import main


def test_sync_db_url_strips_async_drivers():
    assert main._sync_db_url("sqlite+aiosqlite:///./x.db") == "sqlite:///./x.db"
    assert main._sync_db_url("postgresql+asyncpg://u:p@h/d") == "postgresql://u:p@h/d"
    assert main._sync_db_url("postgres://u:p@h/d") == "postgresql://u:p@h/d"


def test_migrations_build_full_schema_on_fresh_db(tmp_path):
    db = tmp_path / "fresh.db"
    main._run_migrations(f"sqlite:///{db}")
    tables = {r[0] for r in sqlite3.connect(db).execute(
        "select name from sqlite_master where type='table'")}
    # baseline created the core tables + alembic's bookkeeping
    assert {"users", "posts", "slides", "user_credentials", "alembic_version"} <= tables
    # Business tables (Phase 2) created by an incremental revision
    assert {"workspaces", "sources", "source_snapshots", "leads"} <= tables
    # Business Phase 4-5 tables
    assert {"brand_rules", "audit_entries"} <= tables
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(users)")}
    assert "token_version" in cols          # the newest column is in the baseline
    assert "logo_path" in cols              # added by an incremental revision
    assert "post_presets" in cols           # added by an incremental revision
    assert "account_type" in cols           # added by an incremental revision
    post_cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(posts)")}
    assert {"lead_id", "workspace_id", "source_kind"} <= post_cols  # Business links
    assert {"claim_check", "ai_caption"} <= post_cols               # Business Phase 4-5
    ws_cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(workspaces)")}
    assert {"max_per_day", "max_per_week"} <= ws_cols               # Business Phase 6
    lead_cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(leads)")}
    assert "sensitive" in lead_cols                                 # Business Phase 6
    assert "managed_accounts" in tables                             # Phase 7
    assert "managed_account_id" in post_cols                        # Phase 7
    assert "active_account_id" in cols                              # Phase 7 (users)
    cred_cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(user_credentials)")}
    assert "elevenlabs_api_key_enc" in cred_cols                    # Reels R1 (voiceover)
    # Media library: standalone assets, plus the links from the two places that
    # can consume one. The links are nullable and SET NULL on delete — a post
    # keeps its own copy of the bytes when the library asset goes away.
    assert "media_assets" in tables
    asset_cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(media_assets)")}
    assert {"kind", "status", "source", "provider", "model", "prompt",
            "file_path", "mime", "duration_sec", "provider_task_id",
            "parent_asset_id", "cost_usd"} <= asset_cols
    slide_cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(slides)")}
    assert "media_asset_id" in slide_cols
    assert "video_asset_id" in post_cols


def test_migrations_autostamp_preexisting_db(tmp_path):
    """A DB that already has the schema but no alembic_version must be stamped,
    not rebuilt (upgrade would otherwise fail creating existing tables)."""
    db = tmp_path / "existing.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email TEXT)")
    con.commit()
    con.close()

    main._run_migrations(f"sqlite:///{db}")   # must not raise despite users existing

    tables = {r[0] for r in sqlite3.connect(db).execute(
        "select name from sqlite_master where type='table'")}
    assert "alembic_version" in tables        # adopted
    ver = sqlite3.connect(db).execute("select version_num from alembic_version").fetchone()
    assert ver is not None                    # stamped at head


def test_migrations_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    main._run_migrations(f"sqlite:///{db}")
    main._run_migrations(f"sqlite:///{db}")   # second run is a no-op, not an error


def test_migrations_do_not_silence_existing_loggers(tmp_path):
    """Alembic's fileConfig disables every pre-existing logger by default. Since
    migrations run inside app startup, that muted uvicorn and all services/*
    logging for the whole process — the container logged its banner and nothing
    else, so a failed generation's traceback was unreadable."""
    import logging
    log = logging.getLogger("services.some_module")
    main._run_migrations(f"sqlite:///{tmp_path / 'logs.db'}")
    assert not log.disabled
    assert not logging.getLogger("uvicorn.error").disabled
