#!/bin/bash
# FRP 安装脚本 — Servermanager 端

set -e

FRP_VERSION="0.58.1"
FRP_ARCH="linux_amd64"

echo "=== Installing FRP v${FRP_VERSION} ==="

# 下载并安装 frp
cd /tmp
wget -q "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
tar -xzf "frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
sudo cp "frp_${FRP_VERSION}_${FRP_ARCH}/frpc" /usr/local/bin/
sudo chmod +x /usr/local/bin/frpc
rm -rf "frp_${FRP_VERSION}_${FRP_ARCH}"

# 创建配置目录
sudo mkdir -p /etc/frp

# 安装 systemd 服务
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sudo cp "${SCRIPT_DIR}/frpc-containers.service" /etc/systemd/system/

# 重新加载 systemd
sudo systemctl daemon-reload

echo "=== FRP installed successfully ==="
echo ""
echo "Next steps:"
echo "1. Configure environment variables in your start.sh:"
echo "   export FRP_ENABLED=true"
echo "   export FRP_SERVER_ADDR='your-vps-ip'"
echo "   export FRP_SERVER_PORT=7000"
echo "   export FRP_TOKEN='your-secret-token'"
echo ""
echo "2. Start the service:"
echo "   sudo systemctl enable frpc-containers"
echo "   sudo systemctl start frpc-containers"
echo ""
echo "3. Start Servermanager:"
echo "   python main.py"
