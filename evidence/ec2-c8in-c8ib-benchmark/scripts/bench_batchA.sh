#!/bin/bash
# Batch A benchmark - CPU / memory / crypto
# Runs on: c8in.8xlarge, c6in.8xlarge, c8i.8xlarge, c7i.8xlarge
set -e
OUT=/tmp/bench_results
mkdir -p $OUT
HOST=$(hostname)
INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)

echo "[$(date -u +%FT%TZ)] Starting benchmarks on $INSTANCE_TYPE ($INSTANCE_ID)"

# Install tools
sudo dnf install -y sysbench openssl git make gcc gcc-c++ kernel-headers 1>/dev/null 2>&1 || true

# Save system info
lscpu > $OUT/lscpu.txt
cat /proc/cpuinfo | head -30 > $OUT/cpuinfo_head.txt
sudo dmidecode -t 4 2>/dev/null > $OUT/dmidecode_cpu.txt || true
uname -a > $OUT/uname.txt
cat /etc/os-release > $OUT/os-release.txt

NPROC=$(nproc)
echo "vCPU count: $NPROC"

# Test 1: sysbench CPU (single + full threads)
echo "[$(date -u +%FT%TZ)] Test 1: sysbench CPU"
for run in 1 2 3; do
  sysbench cpu --cpu-max-prime=30000 --threads=1 --time=30 run > $OUT/sysbench_cpu_t1_run${run}.txt 2>&1
  sysbench cpu --cpu-max-prime=30000 --threads=$NPROC --time=60 run > $OUT/sysbench_cpu_tfull_run${run}.txt 2>&1
done

# Test 2: OpenSSL AES-256-GCM
echo "[$(date -u +%FT%TZ)] Test 2: openssl speed AES"
for run in 1 2 3; do
  openssl speed -evp aes-256-gcm -seconds 10 -bytes 8192 > $OUT/openssl_aes_run${run}.txt 2>&1
done

# Test 3: STREAM memory benchmark
echo "[$(date -u +%FT%TZ)] Test 3: STREAM"
if [ ! -f /tmp/stream.c ]; then
  curl -s -o /tmp/stream.c https://www.cs.virginia.edu/stream/FTP/Code/stream.c
fi
# Array size ~80M elements (~1.8GB total, fits in RAM, doesn't fit in cache)
gcc -O3 -fopenmp -mcmodel=medium -DSTREAM_ARRAY_SIZE=80000000 -DNTIMES=20 /tmp/stream.c -o /tmp/stream
for run in 1 2 3; do
  OMP_NUM_THREADS=$NPROC /tmp/stream > $OUT/stream_run${run}.txt 2>&1
done

echo "[$(date -u +%FT%TZ)] All batch A tests complete"
ls -la $OUT
