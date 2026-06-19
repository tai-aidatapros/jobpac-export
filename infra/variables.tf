# ---------------------------------------------------------------------------
# Variables for the JobPac Export Fargate infrastructure
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-southeast-2"
}

variable "project_name" {
  description = "Project name prefix for resource naming"
  type        = string
  default     = "jobpac-export"
}

variable "environment" {
  description = "Environment name (e.g., prod, staging, dev)"
  type        = string
  default     = "prod"
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_id" {
  description = "VPC ID where Fargate tasks will run (must have route to on-prem via site-to-site VPN)"
  type        = string
  default     = "vpc-09665980de9df9cab"
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for Fargate tasks. Leave empty to use the subnets created by this module."
  type        = list(string)
  default     = []
}

variable "route_table_id" {
  description = "Route table ID to associate with the private subnets (must have route to VPN gateway)"
  type        = string
  default     = "rtb-08f4310958a251a2b"
}

variable "vpn_gateway_id" {
  description = "Virtual Private Gateway ID for Site-to-Site VPN routes to on-premises"
  type        = string
  default     = "vgw-051bc5684227443e2"
}

variable "onprem_cidr_block" {
  description = "CIDR block of the on-premises network (for security group egress to JobPac DB)"
  type        = string
  default     = "10.128.13.0/24"
}

variable "jobpac_db_port" {
  description = "Port the JobPac database listens on"
  type        = number
  default     = 446 # AS/400 JDBC (jt400) default port
}

# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

variable "create_s3_bucket" {
  description = "Set to true to create a new S3 bucket; false to use an existing one"
  type        = bool
  default     = false
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket for CSV exports"
  type        = string
  default     = "nwci-pbi-bucket"
}

variable "s3_prefix" {
  description = "S3 key prefix for uploaded CSVs"
  type        = string
  default     = "jobpac-export/"
}

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

variable "schedule_expression" {
  description = "EventBridge Scheduler cron expression (UTC). Example: cron(0 16 * * ? *) = 2 AM AEST"
  type        = string
  default     = "cron(0 16 * * ? *)"
}

variable "schedule_timezone" {
  description = "IANA timezone for the schedule expression"
  type        = string
  default     = "Australia/Brisbane"
}

# ---------------------------------------------------------------------------
# Task resources
# ---------------------------------------------------------------------------

variable "task_cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU)"
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 2048
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

variable "odbc_secret_name" {
  description = "Name of the Secrets Manager secret containing ODBC credentials"
  type        = string
  default     = "prod/jobpac/db2"
}

variable "email_secret_name" {
  description = "Name of the Secrets Manager secret containing email/SMTP credentials (only for SMTP backend)"
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

variable "notification_backend" {
  description = "Notification backend: ses, sns, or smtp"
  type        = string
  default     = "ses"

  validation {
    condition     = contains(["ses", "sns", "smtp"], var.notification_backend)
    error_message = "notification_backend must be one of: ses, sns, smtp"
  }
}

variable "notification_recipients" {
  description = "Comma-separated list of email recipients for notifications"
  type        = string
  default     = "tai@aidatapros.com"
}

variable "sns_topic_arn" {
  description = "SNS topic ARN (only required when notification_backend = sns)"
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention period in days"
  type        = number
  default     = 30
}

variable "ephemeral_storage_gib" {
  description = "Ephemeral storage allocated to the Fargate task in GiB (min 21, max 200)"
  type        = number
  default     = 100
}
