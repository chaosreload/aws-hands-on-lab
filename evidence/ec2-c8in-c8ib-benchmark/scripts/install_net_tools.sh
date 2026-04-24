#!/bin/bash
# Install network benchmark tools on each host
set -e
sudo dnf install -y iperf3 git make gcc gcc-c++ kernel-headers 1>/dev/null 2>&1

# Build sockperf from source (small, reliable)
if [ ! -f /usr/local/bin/sockperf ]; then
  cd /tmp
  git clone --depth 1 https://github.com/Mellanox/sockperf.git 2>&1 | tail -3
  cd sockperf
  ./autogen.sh 2>&1 | tail -3 || true
  sudo dnf install -y automake libtool 1>/dev/null 2>&1
  ./autogen.sh
  ./configure --prefix=/usr/local > /dev/null
  make -j$(nproc) > /dev/null 2>&1
  sudo make install > /dev/null
fi

which iperf3 sockperf
echo "Network tools installed"
