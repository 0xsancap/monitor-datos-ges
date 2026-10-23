#!/usr/bin/env bash
# Genera un certificado autofirmado para servir /metrics por HTTPS.
# Uso: ./generar-certificado.sh <ip-publica-o-nombre-dns-de-esta-vm>
#
# Prometheus confiará en este certificado concreto (sin CA), así que hay que
# copiar el .crt a la VM del monitor como prometheus/secrets/sappo_ca.crt.
set -euo pipefail

CN="${1:?indica la IP pública o el nombre DNS con el que Prometheus llegará a esta VM}"
cd "$(dirname "$0")"
mkdir -p certs

case "$CN" in
  *[!0-9.]*) SAN="DNS:$CN" ;;
  *)         SAN="IP:$CN" ;;
esac

openssl req -x509 -newkey rsa:2048 -nodes -days 1095 \
  -keyout certs/sappo-exporter.key -out certs/sappo-exporter.crt \
  -subj "/CN=$CN" -addext "subjectAltName=$SAN"

# El contenedor se ejecuta como 'nobody' y necesita leer la clave
chmod 644 certs/sappo-exporter.key

echo
echo "Certificado válido 3 años para $SAN."
echo "Copia certs/sappo-exporter.crt a la VM del monitor como prometheus/secrets/sappo_ca.crt"
