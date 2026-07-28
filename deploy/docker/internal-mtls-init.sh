#!/bin/sh
set -eu

# Generate the local development/service PKI once. The CA private key is kept
# only in this init container's private volume; runtime services receive the CA
# certificate and only the leaf certificate/key they need.
umask 077

CA_PRIVATE_DIR=${GETOFFLINE_MTLS_CA_PRIVATE_DIR:-/run/getoffline-mtls/ca-private}
CA_PUBLIC_DIR=${GETOFFLINE_MTLS_CA_PUBLIC_DIR:-/run/getoffline-mtls/ca}
API_DIR=${GETOFFLINE_MTLS_API_DIR:-/run/getoffline-mtls/api}
FRONTEND_DIR=${GETOFFLINE_MTLS_FRONTEND_DIR:-/run/getoffline-mtls/frontend}
CLI_DIR=${GETOFFLINE_MTLS_CLI_DIR:-/run/getoffline-mtls/cli}
CERT_DAYS=${GETOFFLINE_MTLS_CERT_DAYS:-825}
API_SAN=${GETOFFLINE_MTLS_API_SAN:-DNS:api,DNS:localhost,IP:127.0.0.1}
FRONTEND_SAN=${GETOFFLINE_MTLS_FRONTEND_SAN:-DNS:localhost,IP:127.0.0.1}

mkdir -p "$CA_PRIVATE_DIR" "$CA_PUBLIC_DIR" "$API_DIR" "$FRONTEND_DIR" "$CLI_DIR"

ca_key="$CA_PRIVATE_DIR/ca.key"
ca_cert="$CA_PRIVATE_DIR/ca.crt"
ca_regenerated=0
ca_needs_regeneration=0
if [ ! -s "$ca_key" ] || [ ! -s "$ca_cert" ]; then
    ca_needs_regeneration=1
elif ! openssl x509 -in "$ca_cert" -noout -text 2>/dev/null | grep -q "CA:TRUE"; then
    ca_needs_regeneration=1
elif ! openssl x509 -in "$ca_cert" -noout -text 2>/dev/null | grep -q "Certificate Sign"; then
    ca_needs_regeneration=1
fi
if [ "$ca_needs_regeneration" -eq 1 ]; then
    rm -f "$ca_key" "$ca_cert"
    openssl genrsa -out "$ca_key" 4096
    openssl req -x509 -new -nodes -sha256 \
        -key "$ca_key" \
        -days "$CERT_DAYS" \
        -subj "/CN=GetOffline Internal CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$ca_cert"
    ca_regenerated=1
fi

if [ "$ca_regenerated" -eq 1 ]; then
    rm -f "$API_DIR"/* "$FRONTEND_DIR"/* "$CLI_DIR"/*
fi

cp "$ca_cert" "$CA_PUBLIC_DIR/ca.crt"
chmod 0644 "$CA_PUBLIC_DIR/ca.crt"

issue_certificate() {
    name="$1"
    common_name="$2"
    output_dir="$3"
    extended_usage="$4"
    san="$5"
    key="$output_dir/$name.key"
    cert="$output_dir/$name.crt"
    csr="$output_dir/$name.csr"
    extensions="$output_dir/$name.extensions"

    if [ -s "$key" ] && [ -s "$cert" ]; then
        chmod 0600 "$key"
        chmod 0644 "$cert"
        return
    fi

    rm -f "$key" "$cert" "$csr" "$extensions"
    openssl genrsa -out "$key" 2048
    openssl req -new -sha256 \
        -key "$key" \
        -subj "/CN=$common_name" \
        -out "$csr"
    {
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature,keyEncipherment"
        echo "extendedKeyUsage=$extended_usage"
        if [ -n "$san" ]; then
            echo "subjectAltName=$san"
        fi
    } > "$extensions"
    openssl x509 -req -sha256 \
        -in "$csr" \
        -CA "$ca_cert" \
        -CAkey "$ca_key" \
        -CAcreateserial \
        -days "$CERT_DAYS" \
        -out "$cert" \
        -extfile "$extensions"
    rm -f "$csr" "$extensions" "$CA_PRIVATE_DIR/ca.srl"
    chmod 0600 "$key"
    chmod 0644 "$cert"
}

issue_certificate api api "$API_DIR" serverAuth "$API_SAN"
issue_certificate health health "$API_DIR" clientAuth ""
issue_certificate frontend frontend "$FRONTEND_DIR" clientAuth ""
issue_certificate frontend-server frontend "$FRONTEND_DIR" serverAuth "$FRONTEND_SAN"
issue_certificate cli cli "$CLI_DIR" clientAuth ""

echo "Internal mTLS certificates are ready."
