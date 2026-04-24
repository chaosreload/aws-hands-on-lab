# EC2 C8in / C8ib Benchmark — Raw Evidence

Supporting data for article: [EC2 C8in/C8ib 实测：Granite Rapids CPU/内存/加密性能深度分析](../../docs/compute/ec2-c8in-c8ib-benchmark.md)

## Contents

- `metrics/batchA/` — CPU / Memory / Crypto benchmarks, 4-model comparison (c8in.8xl / c6in.8xl / c8i.8xl / c7i.8xl), **complete with 3-run raw outputs**
- `scripts/` — Launch + benchmark + summary bash scripts
- `resources.md` — Infrastructure resource IDs (terminated after testing)

## Batches Executed

| Batch | Scope | Status |
|---|---|---|
| A | CPU / Memory / Crypto (stress-ng + OpenSSL + STREAM) | ✅ Completed 2026-04-18 |
| B | Network TCP bandwidth (c8in.24xl × 2 vs c6in.24xl × 2) | ❌ Not executed |
| C | EFA collective (c8in.48xl × 2) | ❌ Not executed |
| D | EBS fio + Redis (c8ib.24xl vs c6in.24xl) | ❌ Not executed |

Network / EBS / EFA sections in the article are **specification analysis based on AWS public docs**, not live measurements. This is disclosed in the article.

## Reproduce

```bash
# From an AL2023 instance with SSH access between cluster nodes:
bash scripts/bench_batchA_v2.sh
python3 metrics/batchA/summary.py
```

All tests: 3 runs, 30s each after 30s warm-up, median reported with stddev.
