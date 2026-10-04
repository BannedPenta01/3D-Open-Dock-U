#!/bin/bash
set -e

# Robust Postgres Initialization Script
# This ensures all game-specific databases exist before services start.

databases="friends super_mario_maker pikmin3 splatoon super_smash_bros_wiiu pokken_tournament mario_kart_8 website"

echo "[Postgres-Init] Starting database initialization..."

# The postgres user to use for creation (matches POSTGRES_USER env in compose)
PG_USER=${POSTGRES_USER:-postgres_pretendo}
export PGUSER="$PG_USER"

# Wait for local postgres system to be ready to accept commands
# We try 'postgres', 'template1', and the user database as maintenance databases
until psql -d postgres -c "SELECT 1" > /dev/null 2>&1 || psql -d template1 -c "SELECT 1" > /dev/null 2>&1 || psql -d "$PG_USER" -c "SELECT 1" > /dev/null 2>&1; do
  echo "[Postgres-Init] Waiting for database system to boot..."
  sleep 2
done

# Determine which maintenance database to use for the rest of the script
if psql -d postgres -c "SELECT 1" > /dev/null 2>&1; then
    MNT_DB="postgres"
elif psql -d template1 -c "SELECT 1" > /dev/null 2>&1; then
    MNT_DB="template1"
elif psql -d "$PG_USER" -c "SELECT 1" > /dev/null 2>&1; then
    MNT_DB="$PG_USER"
else
    echo "[Postgres-Init] FATAL: Could not find a valid maintenance database (tried postgres, template1, $PG_USER)."
    exit 1
fi

echo "[Postgres-Init] Using maintenance database: $MNT_DB"

for db in $databases; do
    # Check if database exists
    if ! psql -d "$MNT_DB" -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
        echo "[Postgres-Init] Creating database: $db"
        psql -d "$MNT_DB" -c "CREATE DATABASE \"$db\""
    else
        echo "[Postgres-Init] Database $db already exists. Skipping."
    fi
done

echo "[Postgres-Init] Initialization complete."
