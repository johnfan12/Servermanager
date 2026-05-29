#!/bin/bash
# FRP install helper for the simplified Servermanager node.

set -e

FRP_VERSION="0.58.1"
FRP_ARCH="linux_amd64"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_binary_safe() {
  local src="$1"
  local dst="$2"
  local tmp_dst="${dst}.new"
  sudo install -m 0755 "$src" "$tmp_dst"
  sudo mv -f "$tmp_dst" "$dst"
}

echo "=== Installing FRP v${FRP_VERSION} ==="

cd /tmp
wget -q "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
tar -xzf "frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
install_binary_safe "frp_${FRP_VERSION}_${FRP_ARCH}/frpc" "/usr/local/bin/frpc"
rm -rf "frp_${FRP_VERSION}_${FRP_ARCH}"

sudo mkdir -p /etc/frp

echo "=== FRP installed successfully ==="
echo ""
echo "Next steps:"
echo "1. Configure SIMPLE_FRP_* variables in Servermanager/.env"
echo "   (SIMPLE_FRP_SERVER_ADDR / SIMPLE_FRP_SERVER_PORT / SIMPLE_FRP_TOKEN)"
echo ""
echo "2. Configure the node API tunnel if the node is behind NAT:"
echo "   SIMPLE_API_FRP_ENABLED=true"
echo "   SIMPLE_API_REMOTE_PORT=18881"
echo ""
echo "3. Start simplified Servermanager:"
echo "   ./start.sh"
