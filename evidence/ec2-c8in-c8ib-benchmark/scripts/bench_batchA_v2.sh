#!/bin/bash
# Batch A benchmark v2 - uses stress-ng (available natively) instead of sysbench
set -e
OUT=/tmp/bench_results
mkdir -p $OUT
INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)

echo "[$(date -u +%FT%TZ)] Starting benchmarks on $INSTANCE_TYPE ($INSTANCE_ID)"

# Install tools
sudo dnf install -y stress-ng openssl git make gcc gcc-c++ kernel-headers 1>/dev/null 2>&1

lscpu > $OUT/lscpu.txt
cat /proc/cpuinfo | head -60 > $OUT/cpuinfo_head.txt
sudo dmidecode -t 4 2>/dev/null > $OUT/dmidecode_cpu.txt || true
uname -a > $OUT/uname.txt
cat /etc/os-release > $OUT/os-release.txt

NPROC=$(nproc)
echo "vCPU count: $NPROC"

# Test 1: stress-ng matrix (CPU integer+float, good SIMD stress)
# Use --metrics to get bogo ops/s
echo "[$(date -u +%FT%TZ)] Test 1a: stress-ng matrix single-thread"
for run in 1 2 3; do
  stress-ng --matrix 1 --metrics --timeout 30s --yaml $OUT/stress_matrix_t1_run${run}.yml > $OUT/stress_matrix_t1_run${run}.txt 2>&1
done

echo "[$(date -u +%FT%TZ)] Test 1b: stress-ng matrix all-thread"
for run in 1 2 3; do
  stress-ng --matrix $NPROC --metrics --timeout 60s --yaml $OUT/stress_matrix_tfull_run${run}.yml > $OUT/stress_matrix_tfull_run${run}.txt 2>&1
done

echo "[$(date -u +%FT%TZ)] Test 1c: stress-ng cpu (integer prime) all-thread"
for run in 1 2 3; do
  stress-ng --cpu $NPROC --cpu-method prime --metrics --timeout 60s --yaml $OUT/stress_prime_tfull_run${run}.yml > $OUT/stress_prime_tfull_run${run}.txt 2>&1
done

# Test 2: OpenSSL AES-256-GCM
echo "[$(date -u +%FT%TZ)] Test 2: openssl speed AES-256-GCM"
for run in 1 2 3; do
  openssl speed -evp aes-256-gcm -seconds 10 -bytes 8192 > $OUT/openssl_aes_run${run}.txt 2>&1
done

# Test 2b: OpenSSL multi-threaded SHA-256 using -multi
echo "[$(date -u +%FT%TZ)] Test 2b: openssl speed -multi sha256"
for run in 1 2 3; do
  openssl speed -multi $NPROC -seconds 5 -evp sha256 > $OUT/openssl_sha256_multi_run${run}.txt 2>&1
done

# Test 3: STREAM memory benchmark
echo "[$(date -u +%FT%TZ)] Test 3: STREAM"
if [ ! -f /tmp/stream.c ]; then
  curl -s -o /tmp/stream.c https://www.cs.virginia.edu/stream/FTP/Code/stream.c
fi
gcc -O3 -fopenmp -mcmodel=medium -DSTREAM_ARRAY_SIZE=80000000 -DNTIMES=20 /tmp/stream.c -o /tmp/stream
for run in 1 2 3; do
  OMP_NUM_THREADS=$NPROC /tmp/stream > $OUT/stream_run${run}.txt 2>&1
done

echo "[$(date -u +%FT%TZ)] All batch A tests complete"
ls -la $OUT
