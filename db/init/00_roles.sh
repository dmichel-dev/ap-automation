#!/bin/sh
# Runs once, on the first start of an empty Postgres volume.
# Creates: the n8n role + database, and the ap_app role owning the "ap" schema.
# Tables are created by the next init file(s).
set -eu

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname postgres \
  -v n8n_db="$N8N_DB_NAME" -v n8n_user="$N8N_DB_USER" -v n8n_pw="$N8N_DB_PASSWORD" \
  -v ap_user="$AP_DB_USER" -v ap_pw="$AP_DB_PASSWORD" <<'SQL'
CREATE ROLE :"n8n_user" LOGIN PASSWORD :'n8n_pw';
CREATE ROLE :"ap_user"  LOGIN PASSWORD :'ap_pw';
CREATE DATABASE :"n8n_db" OWNER :"n8n_user";
REVOKE ALL ON DATABASE :"n8n_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"n8n_db" TO :"n8n_user", :"ap_user";
SQL

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$N8N_DB_NAME" \
  -v ap_user="$AP_DB_USER" <<'SQL'
CREATE SCHEMA ap AUTHORIZATION :"ap_user";
ALTER ROLE :"ap_user" SET search_path = ap;
SQL
