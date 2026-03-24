#!/bin/bash
# FRP 安装脚本 — Servermanager 端

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

# 创建配置目录
sudo mkdir -p /etc/frp
sudo mkdir -p /etc/frp/containers

# 安装 systemd 服务
sudo cp "${SCRIPT_DIR}/frpc-containers.service" /etc/systemd/system/
sudo cp "${SCRIPT_DIR}/frpc-container@.service" /etc/systemd/system/
sudo cp "${SCRIPT_DIR}/frpc-api.service" /etc/systemd/system/

# 重新加载 systemd
sudo systemctl daemon-reload

echo "=== FRP installed successfully ==="
echo ""
echo "Next steps:"
echo "1. Configure FRP_* variables in Servermanager/.env"
echo "   (FRP_SERVER_ADDR / FRP_SERVER_PORT / FRP_TOKEN)"
echo ""
echo "2. Start the services:"
echo "   sudo systemctl enable frpc-api"
echo "   sudo systemctl start frpc-api"
echo ""
echo "3. Container SSH tunnels now run in per-instance mode via"
echo "   frpc-container@<container>.service, managed automatically by Servermanager."
echo "   Do NOT keep legacy frpc-containers service running in parallel."
echo ""
echo "4. Starting Servermanager will now sync /etc/frp/frpc-api.ini from .env"
echo "   and restart frpc-api automatically when the token/port changes."
echo ""
echo "5. Start Servermanager:"
echo "   ./start.sh"
