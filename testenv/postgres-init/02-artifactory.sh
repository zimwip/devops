#!/bin/bash
# Creates the Artifactory database and user on first postgres init.
# Runs inside the postgres container; ARTIFACTORY_DB_PASSWORD is injected
# via the postgres service environment in docker-compose.yml.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE USER artifactory WITH PASSWORD '$ARTIFACTORY_DB_PASSWORD';
    CREATE DATABASE artifactory OWNER artifactory;
EOSQL
