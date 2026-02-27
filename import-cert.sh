#!/usr/bin/env bash
# Re-encode client cert with legacy encryption so Chromium can import it.
set -euo pipefail

P12_IN="$HOME/client_certificate-1.p12"
P12_OUT="$HOME/client_certificate-legacy.p12"
PEM_TMP="/tmp/client_cert_tmp.pem"

echo "=== Extract cert (enter your EXISTING p12 password) ==="
openssl pkcs12 -in "$P12_IN" -out "$PEM_TMP" -nodes

echo ""
echo "=== Re-encode (enter a NEW password — you'll use this when importing) ==="
openssl pkcs12 -export -in "$PEM_TMP" -out "$P12_OUT" -legacy

rm -f "$PEM_TMP"
echo ""
echo "Done! Now run:"
echo "  flatpak run org.chromium.Chromium chrome://settings/certificates"
echo "Import ~/client_certificate-legacy.p12 under 'Your certificates'"
