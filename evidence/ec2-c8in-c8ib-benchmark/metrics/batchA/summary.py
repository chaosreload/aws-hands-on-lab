#!/usr/bin/env python3
"""Compute median and stddev from benchmark results."""
import statistics as st

def stats(vals):
    if not vals: return (0, 0, 0)
    return (st.median(vals), st.mean(vals), st.stdev(vals) if len(vals) > 1 else 0)

# Matrix single-thread
data = {
  'matrix_t1': {
    'c8in.8xlarge': [6065.128851, 6040.396640, 6030.130170],
    'c6in.8xlarge': [3906.325541, 3884.393363, 3917.593555],
    'c8i.8xlarge':  [6010.587342, 6004.052686, 6012.854864],
    'c7i.8xlarge':  [4665.121061, 4655.792674, 4687.989419],
  },
  'matrix_tfull': {
    'c8in.8xlarge': [116517.955870, 116397.364734, 116239.286538],
    'c6in.8xlarge': [76997.298877, 77490.994950, 78044.308083],
    'c8i.8xlarge':  [118864.320318, 115697.765593, 115606.796561],
    'c7i.8xlarge':  [103148.766921, 102287.124349, 102722.478701],
  },
  'prime_tfull': {
    'c8in.8xlarge': [37531.274344, 37522.425398, 37535.599828],
    'c6in.8xlarge': [32878.363818, 32838.740596, 32839.728016],
    'c8i.8xlarge':  [37512.037999, 37493.249284, 37521.869362],
    'c7i.8xlarge':  [32946.036479, 32733.232461, 32974.225455],
  },
  'aes256gcm_Kbps': {
    'c8in.8xlarge': [13192722.84, 13196979.40, 13242281.08],
    'c6in.8xlarge': [8879786.05, 8803692.54, 8860435.25],
    'c8i.8xlarge':  [13178731.72, 13183586.30, 13157425.97],
    'c7i.8xlarge':  [12260642.82, 12254303.85, 12196067.74],
  },
  'sha256_multi_Kbps': {
    'c8in.8xlarge': [39209461.35, 39205899.47, 39212023.81],
    'c6in.8xlarge': [29311051.37, 29312077.00, 29301696.10],
    'c8i.8xlarge':  [39165630.87, 39160692.74, 39169310.72],
    'c7i.8xlarge':  [35085923.12, 35132273.46, 35229525.61],
  },
  'stream_triad_MBps': {
    'c8in.8xlarge': [178204.6, 179172.0, 179491.5],
    'c6in.8xlarge': [119887.2, 119077.1, 118584.4],
    'c8i.8xlarge':  [171443.9, 171890.4, 168926.5],
    'c7i.8xlarge':  [134860.6, 134220.0, 134556.4],
  },
  'stream_copy_MBps': {
    'c8in.8xlarge': [178149.4, 178742.5, 178796.1],
    'c6in.8xlarge': [117952.1, 119381.6, 119315.3],
    'c8i.8xlarge':  [169199.8, 171103.3, 172018.9],
    'c7i.8xlarge':  [121225.4, 120914.1, 122218.9],
  },
}

BASELINE = 'c6in.8xlarge'
print(f"{'Test':<22}{'c8in.8xlarge':>18}{'c6in.8xlarge':>18}{'c8i.8xlarge':>18}{'c7i.8xlarge':>18}  c8in vs c6in")
print("-"*110)
for test, rows in data.items():
    line = f"{test:<22}"
    baseline_med = st.median(rows[BASELINE])
    for inst in ['c8in.8xlarge','c6in.8xlarge','c8i.8xlarge','c7i.8xlarge']:
        med, mean, stdev = stats(rows[inst])
        pct = (stdev/mean)*100 if mean else 0
        line += f" {med:>12.1f} ±{pct:4.1f}%"
    pct_gain = (st.median(rows['c8in.8xlarge']) - baseline_med) / baseline_med * 100
    line += f"  {pct_gain:+5.1f}%"
    print(line)
