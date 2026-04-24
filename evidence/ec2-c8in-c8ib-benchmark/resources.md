# EC2 C8in/C8ib Benchmark — Resources

All resources tagged: `Owner=archie-benchmark` `Task=c8in-c8ib`

## Infrastructure (Step 0, created 2026-04-18T09:28-09:29Z)

| Resource | ID/Name | Notes |
|---|---|---|
| VPC | vpc-03794af1a9a1e6685 | 10.200.0.0/16, name `c8-bench-vpc` |
| Subnet | subnet-0b3dfba8036fdab73 | us-east-1c, 10.200.1.0/24, auto-assign public IP |
| IGW | igw-0cc108c6b6ed5a8b0 | Attached to VPC |
| Route Table | rtb-016159aa92e9fdf84 | 0.0.0.0/0 → IGW |
| Security Group | sg-02a7d833363622659 | name `c8-bench-sg`; SSH from 18.136.118.151/32 only; self-ref all protocols for cluster |
| Placement Group | c8-benchmark-pg | strategy=cluster |
| Key Pair | ec2-benchmark-2026-04 | PEM on dev-server `~/.ssh/` 0600 |
| AMI | ami-098e39bafa7e7303d | AL2023 latest x86_64 (us-east-1) |

## Instances (launch/terminate log)

_Populated during batches A–D._

## Cleanup Status

_Populated after Step 7._
