#!/bin/bash
# Ubuntu Host Setup for Homelab k0s Cluster

ASSET_ROOT="https://raw.githubusercontent.com/zacheryph/k8s-gitops/refs/heads/main"
UBUNTU_ASSET_ROOT="${ASSET_ROOT}/config/ubuntu/"

# Copy SSH Keys
echo "** Copying SSH Keys"
cp /home/context/.ssh/authorized_keys /root/.ssh/

# Set System Files
echo "** Placing System Files"
curl -sL \
  "${UBUNTU_ASSET_ROOT}/etc/modules-load.d/k0s.conf" \
  -o /etc/modules-load.d/k0s.conf

curl -sL \
  "${UBUNTU_ASSET_ROOT}/etc/sysctl.d/k0s.conf" \
  -o /etc/sysctl.d/k0s.conf

# Update & Install Packages
apt update
apt upgrade --yes
apt install --yes nfs-common


# Reboot =)
echo "** Rebooting"
reboot
