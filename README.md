# JobPac Data Export

Automated export of JobPac database tables to S3 as CSV files, running on AWS Fargate with EventBridge Scheduler.

> **Migrated from**: `TaskJobPac.ps1` + `JobPacGetCurrentData.ps1` (Windows PowerShell / Task Scheduler)

## Architecture

```
EventBridge Scheduler (cron)
    │
    ▼
ECS Fargate Task (Python 3.12)
    ├── ODBC → JobPac DB (via Site-to-Site VPN)
    ├── CSV  → S3 Bucket
    ├── Logs → CloudWatch
    └── Email → SES / SNS / SMTP
```

## Quick Start

### Prerequisites

- AWS CLI configured with appropriate permissions
- Docker installed locally
- Terraform >= 1.5 (for infrastructure)
- Python 3.12+ (for local development)

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt
pip install pytest

# Run tests
pytest tests/ -v

# Run locally (requires ODBC driver + DB access)
export JOBPAC_SECRET_NAME="jobpac/odbc-creds"
export S3_BUCKET="your-bucket"
export NOTIFICATION_RECIPIENTS="tai@aidatapros.com"
export NOTIFICATION_BACKEND="ses"
python -m src.main
```

### Deploy Infrastructure

```bash
cd infra/

# Initialize Terraform
terraform init

# Review the plan
terraform plan \
  -var="vpc_id=vpc-xxxx" \
  -var='private_subnet_ids=["subnet-aaa","subnet-bbb"]'

# Apply
terraform apply \
  -var="vpc_id=vpc-xxxx" \
  -var='private_subnet_ids=["subnet-aaa","subnet-bbb"]'
```

### Build & Push Docker Image

```bash
# Get ECR login
aws ecr get-login-password --region ap-southeast-2 | \
  docker login --username AWS --password-stdin <account-id>.dkr.ecr.ap-southeast-2.amazonaws.com

# Build and push
docker build -t jobpac-export .
docker tag jobpac-export:latest <ecr-url>:latest
docker push <ecr-url>:latest
```

### Manual Test Run

```bash
aws ecs run-task \
  --cluster jobpac-export \
  --task-definition jobpac-export \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=DISABLED}" \
  --region ap-southeast-2
```

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `JOBPAC_SECRET_NAME` | ✅ | — | Secrets Manager secret name for ODBC creds |
| `S3_BUCKET` | ✅ | — | Target S3 bucket |
| `S3_PREFIX` | | `current/` | S3 key prefix |
| `NOTIFICATION_BACKEND` | | `ses` | `ses`, `sns`, or `smtp` |
| `NOTIFICATION_RECIPIENTS` | | — | Comma-separated email list |
| `AWS_REGION` | | `ap-southeast-2` | AWS region |
| `TABLES_PATH` | | bundled `config/tables.csv` | Path to table list |
| `EMAIL_SECRET_NAME` | | — | Secrets Manager secret for SMTP creds |
| `SNS_TOPIC_ARN` | | — | SNS topic ARN (for SNS backend) |

### Secrets Manager Format

**ODBC credentials** (`jobpac/odbc-creds`):
```json
{
  "dsn": "JPData",
  "database": "JDNWCDTA01",
  "username": "NWCODBC",
  "password": "your-password"
}
```

**Email credentials** (optional, for SMTP backend):
```json
{
  "smtp_server": "smtp.office365.com",
  "smtp_port": 587,
  "username": "sender@example.com",
  "password": "your-password",
  "from_address": "sender@example.com"
}
```

## Project Structure

```
jobpac-export/
├── src/
│   ├── __init__.py      # Package marker
│   ├── main.py          # Orchestrator entry point
│   ├── config.py        # Configuration & secrets loading
│   ├── exporter.py      # ODBC → CSV extraction
│   ├── uploader.py      # CSV → S3 upload
│   └── notifier.py      # Email notifications
├── config/
│   └── tables.csv       # List of tables to export
├── tests/
│   ├── test_exporter.py
│   ├── test_uploader.py
│   └── test_notifier.py
├── infra/
│   ├── main.tf          # Terraform resources
│   ├── variables.tf     # Terraform variables
│   └── outputs.tf       # Terraform outputs
├── .github/workflows/
│   └── deploy.yml       # CI/CD pipeline
├── Dockerfile
├── .dockerignore
├── requirements.txt
└── README.md
```

## S3 Output Layout

Each run creates a timestamped directory, plus a `latest` pointer:

```
s3://jobpac-dump-prod/
  current/
    2026-06-15T07-00-00/
      TABLE_A.csv
      TABLE_B.csv
      JobPacTableProcessingInfo.csv
    latest/              ← always points to most recent run
      TABLE_A.csv
      TABLE_B.csv
      JobPacTableProcessingInfo.csv
```
