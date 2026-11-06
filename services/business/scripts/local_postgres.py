"""Manage the project's Windows portable PostgreSQL; never installs a service.

Usage: .venv/Scripts/python.exe services/business/scripts/local_postgres.py prepare
Other actions: start, status, stop. No drop/reset action is provided.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import urllib.request
from urllib.parse import quote
import uuid
import zipfile

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[3]
LOCAL = ROOT / ".local"
DATA = LOCAL / "postgres-data"
BIN_ROOT = LOCAL / "postgres-17.11"
BIN = BIN_ROOT / "pgsql/bin"
MARKER = LOCAL / "postgres-project.json"
SECRET = LOCAL / "postgres-admin.json"
CONFIG = LOCAL / "business.json"
PORT = 55432
ADMIN = "llm_cluster_admin"
VERSION = "17.11"
ARCHIVE = "postgresql-17.11-4-windows-x64-binaries.zip"
DOWNLOAD = "https://get.enterprisedb.com/postgresql/" + ARCHIVE
# SHA-256 of the exact EDB archive fetched through PostgreSQL's official Windows entry.
SHA256 = "b9424ee7bc60b52450ff910a3630225df32e633f3cb29c1d126d9299d59aea28"


def local_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved != path:
        raise RuntimeError("Refusing a redirected/outside project path.")
    return resolved


def save_new(path: Path, value: dict) -> None:
    local_path(path)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def load(path: Path) -> dict:
    local_path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def validate() -> dict:
    marker = load(MARKER)
    if (marker.get("project") != "llm-business" or
            marker.get("root") != str(ROOT) or marker.get("data") != str(DATA) or
            marker.get("port") != PORT or marker.get("version") != VERSION):
        raise RuntimeError("Project cluster marker does not match this workspace.")
    local_path(DATA)
    if (DATA / "PG_VERSION").read_text().strip() != "17":
        raise RuntimeError("Unexpected PostgreSQL data major version.")
    return marker


def command(name: str, *args: str, accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    local_path(BIN)
    if name == "pg_ctl" and "start" in args:
        # A detached Windows server can inherit pipe handles and keep communicate()
        # blocked after pg_ctl exits. Its diagnostic output already goes to postgres.log.
        result = subprocess.run(
            [str(BIN / (name + ".exe")), *map(str, args)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode not in accepted:
            raise RuntimeError("pg_ctl start failed; inspect .local/postgres.log.")
        return result
    result = subprocess.run(
        [str(BIN / (name + ".exe")), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode not in accepted:
        # PostgreSQL command arguments never contain a password; initdb uses a private file.
        raise RuntimeError(f"{name} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def download() -> None:
    archive = local_path(LOCAL / "downloads" / ARCHIVE)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        partial = local_path(archive.with_suffix(".part"))
        with urllib.request.urlopen(DOWNLOAD, timeout=60) as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target)
        partial.replace(archive)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise RuntimeError("PostgreSQL archive checksum mismatch; nothing extracted.")
    if BIN.exists():
        version = command("postgres", "--version").stdout.strip()
        if version != "postgres (PostgreSQL) " + VERSION:
            raise RuntimeError("Existing PostgreSQL binaries have an unexpected version.")
        return
    local_path(BIN_ROOT)
    if BIN_ROOT.exists():
        raise RuntimeError("Incomplete PostgreSQL binaries; inspect before recovery.")
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            # Exclude bundled graphical applications and developer components.
            if not entry.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                continue
            local_path(BIN_ROOT / entry.filename)
            source.extract(entry, BIN_ROOT)
    print("Prepared PostgreSQL", VERSION, "binaries under", BIN_ROOT)


def initialize() -> None:
    if DATA.exists():
        validate()
        return
    if MARKER.exists() or SECRET.exists() or CONFIG.exists():
        raise RuntimeError("Partial local cluster setup exists; inspect it before retrying.")
    instance_id = str(uuid.uuid4())
    passwords = {name: secrets.token_urlsafe(36) for name in ("admin", "dev", "test")}
    save_new(SECRET, passwords)
    pwfile = local_path(LOCAL / "initdb-password.tmp")
    try:
        with pwfile.open("x", encoding="ascii") as stream:
            stream.write(passwords["admin"] + "\n")
        command("initdb", "-D", str(DATA), "-U", ADMIN, "--auth=scram-sha-256",
                "--encoding=UTF8", "--locale=C", "--pwfile=" + str(pwfile))
    finally:
        # Only this exact one-use password file is removed; never recurse into a data directory.
        if pwfile.exists():
            local_path(pwfile).unlink()
    with (DATA / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write("\n# llm-business local development cluster\n"
                     "listen_addresses = '127.0.0.1'\nport = 55432\n"
                     "max_connections = 30\nshared_buffers = '32MB'\n"
                     "password_encryption = 'scram-sha-256'\n"
                     "log_statement = 'none'\nlog_min_error_statement = 'panic'\n")
    save_new(MARKER, {"project": "llm-business", "instance_id": instance_id,
                      "root": str(ROOT), "data": str(DATA), "port": PORT, "version": VERSION})
    print("Initialized project-only PostgreSQL data at", DATA)


def admin_connect() -> psycopg.Connection:
    connection = psycopg.connect(host="127.0.0.1", port=PORT, dbname="postgres", user=ADMIN,
                                 password=load(SECRET)["admin"], connect_timeout=5,
                                 autocommit=True)
    actual = connection.execute("SHOW data_directory").fetchone()[0]
    if Path(actual).resolve() != DATA:
        connection.close()
        raise RuntimeError("Connected PostgreSQL data directory is not the project cluster.")
    return connection


def running() -> bool:
    return command("pg_ctl", "-D", str(DATA), "status", accepted=(0, 3)).returncode == 0


def start() -> None:
    validate()
    if not running():
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", PORT))
            except OSError:
                raise RuntimeError("Project PostgreSQL port is occupied; no process was stopped.") from None
        command("pg_ctl", "-D", str(DATA), "-l", str(LOCAL / "postgres.log"), "-w", "start")
    with admin_connect() as connection:
        print("PostgreSQL", connection.execute("SHOW server_version").fetchone()[0],
              "ready at 127.0.0.1:" + str(PORT))


def provision() -> None:
    marker = validate()
    passwords = load(SECRET)
    with admin_connect() as connection:
        for purpose in ("dev", "test"):
            role = "llm_business_" + purpose
            dbname = role
            label = "llm-business:" + marker["instance_id"] + ":" + purpose
            existing_role = connection.execute(
                "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            if existing_role is not None and existing_role[0] != label:
                raise RuntimeError("Refusing to change an unknown existing database role.")
            if existing_role is None:
                connection.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(passwords[purpose])))
                connection.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(role), sql.Literal(label)))
            existing_db = connection.execute(
                "SELECT shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba) "
                "FROM pg_database WHERE datname = %s", (dbname,),
            ).fetchone()
            if existing_db is not None and existing_db != (label, role):
                raise RuntimeError("Refusing to change an unknown existing database.")
            if existing_db is None:
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    sql.Identifier(dbname), sql.Identifier(role)))
                connection.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(dbname), sql.Literal(label)))
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(dbname)))
            connection.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), sql.Identifier(role)))
    config = {
        ("database_url" if purpose == "dev" else "test_database_url"):
            "postgresql://llm_business_" + purpose + ":" + quote(passwords[purpose], safe="") +
            "@127.0.0.1:" + str(PORT) + "/llm_business_" + purpose
        for purpose in ("dev", "test")
    }
    if CONFIG.exists():
        if load(CONFIG) != config:
            raise RuntimeError("Existing business.json differs; refusing to overwrite credentials.")
    else:
        save_new(CONFIG, config)
    for purpose in ("dev", "test"):
        key = "database_url" if purpose == "dev" else "test_database_url"
        with psycopg.connect(config[key]) as connection:
            name, superuser = connection.execute(
                "SELECT current_database(), rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if name != "llm_business_" + purpose or superuser:
                raise RuntimeError("Application database/role verification failed.")
    print("Verified llm_business_dev/test with separate non-superuser roles; credentials saved locally.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "status", "stop"))
    action = parser.parse_args().action
    if os.name != "nt":
        raise SystemExit("This portable PostgreSQL helper targets Windows x64 only.")
    local_path(LOCAL)
    if action == "prepare":
        LOCAL.mkdir(exist_ok=True)
        download()
        initialize()
        start()
        provision()
    elif action == "start":
        start()
    elif action == "status":
        validate()
        if running():
            with admin_connect() as connection:
                print("Project PostgreSQL is running:", connection.execute("SHOW server_version").fetchone()[0])
        else:
            print("Project PostgreSQL is stopped.")
    else:
        validate()
        if running():
            # Both the local marker and the live server data directory must match before stopping.
            with admin_connect():
                pass
            command("pg_ctl", "-D", str(DATA), "-m", "fast", "-w", "stop")
        print("Project PostgreSQL is stopped; all data was preserved.")


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error as exc:
        # SQL text/connection strings/passwords must never be echoed in a setup report.
        raise SystemExit(f"PostgreSQL operation failed ({type(exc).__name__}); inspect local setup.") from None
