#!/bin/sh
set -e

DNS_RESOLVER=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)
export DNS_RESOLVER="${DNS_RESOLVER:-10.96.0.10}"
export BACKEND_URL="${BACKEND_URL:-http://backend:8000}"

envsubst '${DNS_RESOLVER} ${BACKEND_URL}' < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf

echo "Nginx starting with resolver=$DNS_RESOLVER backend=$BACKEND_URL"
exec nginx -g 'daemon off;'
