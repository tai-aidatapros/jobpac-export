# =============================================================================
# JobPac Data Export — AWS Fargate + EventBridge Scheduler
# =============================================================================
#
# This Terraform configuration provisions all AWS resources needed to run the
# JobPac data export Python application as a scheduled Fargate task.
#
# Architecture:
#   EventBridge Scheduler → ECS Fargate Task → ODBC (via Site-to-Site VPN)
#                                            → S3 (CSV output)
#                                            → SES/SNS/SMTP (notifications)
#                                            → CloudWatch Logs
# =============================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment and configure for remote state:
  backend "s3" {
     bucket  = "nwci-pbi-bucket"
     key     = "jobpac-export/terraform.tfstate"
     region  = "ap-southeast-2"
     profile = "NWCI-Prod"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = "NWCI-Prod"

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# Subnets (carved from 10.100.1.0/24 — two /26s across two AZs)
# ---------------------------------------------------------------------------

resource "aws_subnet" "private_a" {
  vpc_id            = var.vpc_id
  cidr_block        = "10.100.1.0/26"
  availability_zone = data.aws_availability_zones.available.names[0]

  tags = { Name = "${var.project_name}-private-a" }
}

resource "aws_subnet" "private_b" {
  vpc_id            = var.vpc_id
  cidr_block        = "10.100.1.64/26"
  availability_zone = data.aws_availability_zones.available.names[1]

  tags = { Name = "${var.project_name}-private-b" }
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = var.route_table_id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = var.route_table_id
}

# Routes to on-premises via Site-to-Site VPN
resource "aws_route" "onprem_vpc" {
  route_table_id         = var.route_table_id
  destination_cidr_block = "10.0.0.0/16"
  gateway_id             = var.vpn_gateway_id
}

resource "aws_route" "onprem_jobpac_db" {
  route_table_id         = var.route_table_id
  destination_cidr_block = "10.128.13.0/24"
  gateway_id             = var.vpn_gateway_id
}

# ---------------------------------------------------------------------------
# Internet Gateway + public subnet (outbound internet for SES — no NAT cost)
# ---------------------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  vpc_id = var.vpc_id

  tags = { Name = "${var.project_name}-igw" }
}

resource "aws_subnet" "public_a" {
  vpc_id                  = var.vpc_id
  cidr_block              = "10.100.1.128/26"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project_name}-public-a" }
}

resource "aws_route_table" "public" {
  vpc_id = var.vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project_name}-public" }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# ECR Repository
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "main" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Lifecycle policy — keep only the last 10 images
resource "aws_ecr_lifecycle_policy" "main" {
  repository = aws_ecr_repository.main.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# ---------------------------------------------------------------------------
# S3 Bucket (optional — controlled by var.create_s3_bucket)
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "export" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = var.s3_bucket_name
}

resource "aws_s3_bucket_versioning" "export" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = aws_s3_bucket.export[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "export" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = aws_s3_bucket.export[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "export" {
  count                   = var.create_s3_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.export[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# ECS Cluster
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = var.project_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------------------------------------------------------------------------
# IAM — Task Execution Role (for ECS agent: pull images, write logs)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name = "${var.project_name}-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------------
# IAM — Task Role (for application code: S3, Secrets Manager, SES)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name = "${var.project_name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  name = "${var.project_name}-task-policy"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — write CSV exports
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_name}",
          "arn:aws:s3:::${var.s3_bucket_name}/*",
        ]
      },
      # Secrets Manager — read ODBC and email credentials
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = concat(
          ["arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.odbc_secret_name}-*"],
          var.email_secret_name != "" ? ["arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.email_secret_name}-*"] : []
        )
      },
      # SES — send notification emails (if using SES backend)
      {
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail",
        ]
        Resource = ["*"]
      },
      # SNS — publish notifications (if using SNS backend)
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_topic_arn != "" ? [var.sns_topic_arn] : ["*"]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Security Group
# ---------------------------------------------------------------------------

resource "aws_security_group" "task" {
  name_prefix = "${var.project_name}-"
  description = "Security group for JobPac export Fargate task"
  vpc_id      = var.vpc_id

  # Egress to on-prem DB via site-to-site VPN
  egress {
    description = "ODBC to JobPac DB (on-prem via VPN)"
    from_port   = var.jobpac_db_port
    to_port     = var.jobpac_db_port
    protocol    = "tcp"
    cidr_blocks = [var.onprem_cidr_block]
  }

  # Egress to AWS services (S3, SES, Secrets Manager, CloudWatch)
  egress {
    description = "HTTPS to AWS services"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Egress for SMTP (only if using Office 365 SMTP backend)
  egress {
    description = "SMTP to Office 365 (port 587)"
    from_port   = 587
    to_port     = 587
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-task"
  }
}

# ---------------------------------------------------------------------------
# VPC Endpoints (ECR, S3, CloudWatch Logs — required for private subnet access)
# ---------------------------------------------------------------------------

locals {
  endpoint_subnets = length(var.private_subnet_ids) > 0 ? var.private_subnet_ids : [aws_subnet.private_a.id, aws_subnet.private_b.id]
}

resource "aws_security_group" "vpc_endpoints" {
  name_prefix = "${var.project_name}-endpoints-"
  description = "Allow HTTPS from Fargate task to VPC interface endpoints"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from Fargate task"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  tags = {
    Name = "${var.project_name}-endpoints"
  }
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-ecr-api" }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-ecr-dkr" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [var.route_table_id]

  tags = { Name = "${var.project_name}-s3" }
}

resource "aws_vpc_endpoint" "cloudwatch_logs" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-logs" }
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-secretsmanager" }
}

# ---------------------------------------------------------------------------
# ECS Task Definition
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "main" {
  family                   = var.project_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  container_definitions = jsonencode([{
    name      = var.project_name
    image     = "${aws_ecr_repository.main.repository_url}:latest"
    essential = true

    environment = [
      { name = "JOBPAC_SECRET_NAME", value = var.odbc_secret_name },
      { name = "S3_BUCKET", value = var.s3_bucket_name },
      { name = "S3_PREFIX", value = var.s3_prefix },
      { name = "NOTIFICATION_BACKEND", value = var.notification_backend },
      { name = "NOTIFICATION_RECIPIENTS", value = var.notification_recipients },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "EMAIL_SECRET_NAME", value = var.email_secret_name },
      { name = "SNS_TOPIC_ARN", value = var.sns_topic_arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.main.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

# ---------------------------------------------------------------------------
# IAM — EventBridge Scheduler Execution Role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "${var.project_name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "scheduler.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.project_name}-scheduler-policy"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.main.arn]
        Condition = {
          ArnLike = {
            "ecs:cluster" = aws_ecs_cluster.main.arn
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [
          aws_iam_role.execution.arn,
          aws_iam_role.task.arn,
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler
# ---------------------------------------------------------------------------

resource "aws_scheduler_schedule" "main" {
  name       = var.project_name
  group_name = "default"

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.main.arn
      launch_type         = "FARGATE"
      platform_version    = "LATEST"
      task_count          = 1

      network_configuration {
        subnets          = local.endpoint_subnets
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }
}
