#!/bin/bash
# Network benchmark - expects SERVER_IP as arg (private IP of peer)
# Runs on CLIENT side. Peer needs iperf3 -s running.
set -e
SERVER_IP=$1
OUT=/tmp/net_results
mkdir -p $OUT

INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
echo "[$(date -u +%FT%TZ)] Starting network tests on $INSTANCE_TYPE from $(hostname -I) -> $SERVER_IP"

# Verify server reachable
ping -c 2 -W 2 $SERVER_IP || { echo "ERR: cannot ping $SERVER_IP"; exit 1; }

# Test 1: TCP single stream (3 runs)
echo "[$(date -u +%FT%TZ)] T1: iperf3 single stream TCP"
for run in 1 2 3; do
  iperf3 -c $SERVER_IP -t 30 -J > $OUT/iperf3_single_run${run}.json 2>&1
  sleep 2
done

# Test 2: TCP multi stream P=16 (3 runs)
echo "[$(date -u +%FT%TZ)] T2: iperf3 multi stream TCP P=16"
for run in 1 2 3; do
  iperf3 -c $SERVER_IP -t 30 -P 16 -J > $OUT/iperf3_multi16_run${run}.json 2>&1
  sleep 2
done

# Test 3: TCP multi stream P=32 (saturate 150G link)
echo "[$(date -u +%FT%TZ)] T3: iperf3 multi stream TCP P=32"
for run in 1 2 3; do
  iperf3 -c $SERVER_IP -t 30 -P 32 -J > $OUT/iperf3_multi32_run${run}.json 2>&1
  sleep 2
done

# Test 4: sockperf ping-pong latency (200 msg/s, 2 min)
echo "[$(date -u +%FT%TZ)] T4: sockperf ping-pong latency"
for run in 1 2 3; do
  sockperf ping-pong -i $SERVER_IP -p 11111 -t 30 --full-rtt --msg-size 64 > $OUT/sockperf_pp_run${run}.txt 2>&1
  sleep 1
done

echo "[$(date -u +%FT%TZ)] All network tests done"
ls -la $OUT
