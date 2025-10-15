#!/bin/bash

# Script to help set up kubectl configuration for GitLab CI
# Run this script on your k3s master node (k3s.ahurix.com)

set -e

echo "🚀 Setting up kubectl configuration for GitLab CI deployment"
echo "============================================================"

# Check if running on k3s master
if ! command -v k3s &> /dev/null; then
    echo "❌ k3s command not found. Please run this script on the k3s master node."
    exit 1
fi

# Get the k3s config
echo "📋 Retrieving k3s configuration..."
K3S_CONFIG="/etc/rancher/k3s/k3s.yaml"

if [ ! -f "$K3S_CONFIG" ]; then
    echo "❌ k3s configuration file not found at $K3S_CONFIG"
    exit 1
fi

# Create a copy and modify it
TEMP_CONFIG="/tmp/k3s-external.yaml"
cp "$K3S_CONFIG" "$TEMP_CONFIG"

# Get the external IP
EXTERNAL_IP=$(hostname -I | awk '{print $1}')
echo "🌐 Detected external IP: $EXTERNAL_IP"

# Replace the server URL
sed -i "s|https://127.0.0.1:6443|https://$EXTERNAL_IP:6443|g" "$TEMP_CONFIG"

echo "✅ Modified configuration for external access"

# Base64 encode for GitLab CI
echo ""
echo "📝 Base64 encoded configuration for GitLab CI:"
echo "=============================================="
base64 -w 0 "$TEMP_CONFIG"
echo ""
echo ""

echo "🔧 Next steps:"
echo "1. Copy the base64 string above"
echo "2. Go to your GitLab project → Settings → CI/CD → Variables"
echo "3. Add a new variable:"
echo "   - Key: KUBE_CONFIG_DEV (for development)"
echo "   - Key: KUBE_CONFIG_PROD (for production)"
echo "   - Value: [paste the base64 string]"
echo "   - Type: Variable"
echo "   - Flags: ✅ Protected, ✅ Masked"
echo ""
echo "⚠️  Note: Make sure your firewall allows access to port 6443 from GitLab runners"

# Clean up
rm "$TEMP_CONFIG"

echo "✨ Setup complete!"