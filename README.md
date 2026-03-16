# AWS Hands-on Lab 🧪

从概念到实操的 AWS 技术指南。

🔗 **在线阅读**: [https://chaosreload.github.io/aws-hands-on-lab/](https://chaosreload.github.io/aws-hands-on-lab/)

## 内容分类

- **AI/ML** — Bedrock、SageMaker、Q 等
- **Compute** — EC2、Lambda、ECS、EKS 等
- **Networking** — VPC、CloudFront、Route 53 等
- **Storage** — S3、EBS、EFS 等
- **Database** — RDS、DynamoDB、Aurora 等
- **Security** — IAM、KMS、Security Hub 等

## 本地开发

```bash
pip install mkdocs-material
mkdocs serve    # http://localhost:8000
```

## 写文章

1. 复制 `docs/TEMPLATE.md` 到对应分类目录
2. 在 `mkdocs.yml` 的 `nav` 中添加条目
3. Push 到 `main`，GitHub Actions 自动部署

## License

MIT
