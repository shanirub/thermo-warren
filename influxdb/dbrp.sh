#!/bin/sh
# Stage 12. Declares the explicit DBRP mapping (database / retention-policy
# mapping) that exposes bucket $INFLUXDB_BUCKET under a v1-style database name,
# so InfluxQL can read it. InfluxQL rather than Flux is a locked decision: it
# survives a future move to InfluxDB 3 and Flux does not.
#
# Runs as a one-shot compose service, the same shape as `topology`: it must exit
# 0 before `grafana` starts. Compose runs it on every `up`, so it is idempotent.
#
# InfluxDB 2.x does NOT require this mapping to exist. Verified against the
# running 2.9 instance at stage 12: it synthesises a read-only VIRTUAL mapping
# for any bucket that has no explicit one, and InfluxQL already worked through
# that. The mapping is declared here anyway, so the database name is stated in
# version control rather than inherited from a 2.x convenience.
#
# Reads INFLUX_HOST, INFLUX_ORG and INFLUX_TOKEN straight from the environment.
# Compose maps those by hand from the INFLUXDB_-prefixed keys -- see the `dbrp`
# service in compose.yaml for why that is the single deliberate exception to
# stage 10's prefix rule.

set -eu

# The grep pattern is the whole trick, and was verified both ways at stage 12:
# `list --json` INCLUDES virtual mappings, each carrying "virtual": true, while
# an explicit mapping carries "virtual": false. Matching on the database name
# alone would always find the virtual mapping and so never create anything.
if influx v1 dbrp list --db "$INFLUXDB_BUCKET" --json | grep -q '"virtual": false'; then
    echo "dbrp: explicit mapping for database '$INFLUXDB_BUCKET' already present"
    exit 0
fi

# `create` takes --bucket-id, not a bucket name, so the id is looked up first.
# --hide-headers leaves the id in field 1 of a tab-separated line, which is
# cut's default delimiter. jq is NOT in this image (Debian 12 base); grep, sh
# and curl are.
bucket_id=$(influx bucket list --name "$INFLUXDB_BUCKET" --hide-headers | cut -f1)
if [ -z "$bucket_id" ]; then
    echo "dbrp: bucket '$INFLUXDB_BUCKET' does not exist" >&2
    exit 1
fi

# --rp autogen deliberately reuses the retention-policy name InfluxDB gave the
# virtual mapping this replaces, so a query written against the virtual one goes
# on working unchanged.
#
# --default is explicit although this is the only retention policy on the
# database: it is what lets a query name the database bare, rather than having
# to qualify it as "telemetry"."autogen".
influx v1 dbrp create \
    --db "$INFLUXDB_BUCKET" \
    --bucket-id "$bucket_id" \
    --rp autogen \
    --default

echo "dbrp: created explicit mapping for database '$INFLUXDB_BUCKET' -> bucket $bucket_id"
