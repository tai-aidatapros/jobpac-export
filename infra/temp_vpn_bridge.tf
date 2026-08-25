# =============================================================================
# TEMPORARY — JobPac Export VPN client bridge
#
# Stopgap while the on-prem side of the Site-to-Site VPN is still being
# finished. Routes the Fargate task's on-prem DB traffic through Jobpac-EC2
# (running a VPN client) instead of the VGW. Fully additive: does not modify
# the existing public route table, the VGW, or the Customer Gateway/VPN
# Connection.
#
# Decommission plan: delete this file, remove its route table ID from
# aws_vpc_endpoint.s3.route_table_ids in main.tf, run `terraform apply`, then
# revert the manual changes on Jobpac-EC2 (re-enable source_dest_check,
# detach the bridge_temp security group and instance profile - the original
# sg-04ad480df46a87f0e and its lack of an IAM role were never removed, so
# nothing needs reattaching there).
# =============================================================================

resource "aws_subnet" "bridge_temp" {
  vpc_id                  = var.vpc_id
  cidr_block              = "10.100.1.192/26"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name    = "${var.project_name}-bridge-temp"
    Purpose = "jobpac-export-temp-vpn-bridge"
  }
}

# ---------------------------------------------------------------------------
# Route table — only this table gets a route to the on-prem DB CIDR via the
# bridge instance's ENI. The existing public route table (which routes that
# same CIDR to the VGW) is left untouched.
# ---------------------------------------------------------------------------

resource "aws_route_table" "bridge_temp" {
  vpc_id = var.vpc_id

  tags = {
    Name    = "${var.project_name}-bridge-temp"
    Purpose = "jobpac-export-temp-vpn-bridge"
  }
}

resource "aws_route_table_association" "bridge_temp" {
  subnet_id      = aws_subnet.bridge_temp.id
  route_table_id = aws_route_table.bridge_temp.id
}

# Internet access for SES / Secrets Manager / CloudWatch Logs / ECR pulls —
# mirrors the existing public subnet's IGW route. Reuses the existing IGW as
# a target only; does not modify it or any existing route table.
resource "aws_route" "bridge_temp_igw" {
  route_table_id         = aws_route_table.bridge_temp.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

# The DB route: on-prem CIDR points at Jobpac-EC2's ENI instead of the VGW.
resource "aws_route" "bridge_temp_onprem_jobpac_db" {
  route_table_id         = aws_route_table.bridge_temp.id
  destination_cidr_block = var.onprem_cidr_block
  network_interface_id   = "eni-063e3e5850ec553e6"
}

# ---------------------------------------------------------------------------
# Security group for the bridge instance (Jobpac-EC2), attached ALONGSIDE the
# existing sg-04ad480df46a87f0e (an instance can have multiple SGs; effective
# rules are the union). sg-04ad480df46a87f0e itself is never modified or
# detached, so its existing SSH/ICMP ingress and HTTPS egress keep covering
# the instance unchanged. This group only adds what's unique to the bridge
# role, to avoid duplicating rules already granted by the group that stays.
# ---------------------------------------------------------------------------

resource "aws_security_group" "bridge_temp" {
  name_prefix = "${var.project_name}-bridge-temp-"
  description = "Temporary VPN client bridge for JobPac export (Jobpac-EC2)"
  vpc_id      = var.vpc_id

  ingress {
    description = "jt400 JDBC from the Fargate task temp bridge subnet, to be forwarded on-prem"
    from_port   = 449
    to_port     = 449
    protocol    = "tcp"
    cidr_blocks = [aws_subnet.bridge_temp.cidr_block]
  }

  ingress {
    description = "jt400 JDBC (Database Host Server) from the Fargate task temp bridge subnet"
    from_port   = 8471
    to_port     = 8471
    protocol    = "tcp"
    cidr_blocks = [aws_subnet.bridge_temp.cidr_block]
  }

  ingress {
    description = "jt400 JDBC (Sign-On Server) from the Fargate task temp bridge subnet"
    from_port   = 8476
    to_port     = 8476
    protocol    = "tcp"
    cidr_blocks = [aws_subnet.bridge_temp.cidr_block]
  }

  egress {
    description = "OpenVPN to remote VPN server (vpnaccess.nwciqld.com:8443/udp)"
    from_port   = 8443
    to_port     = 8443
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP (apt/package updates) - not covered by sg-04ad480df46a87f0e"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS - not covered by sg-04ad480df46a87f0e"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-bridge-temp"
    Purpose = "jobpac-export-temp-vpn-bridge"
  }
}

# ---------------------------------------------------------------------------
# IAM role + instance profile for Jobpac-EC2, scoped to read only the
# prod/jobpac/openvpn-config secret. Jobpac-EC2 currently has no IAM role at
# all; this attaches a new one without touching anything else on the
# instance.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "bridge_temp" {
  name = "${var.project_name}-bridge-temp"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })

  tags = {
    Purpose = "jobpac-export-temp-vpn-bridge"
  }
}

resource "aws_iam_role_policy" "bridge_temp_secret" {
  name = "${var.project_name}-bridge-temp-openvpn-secret"
  role = aws_iam_role.bridge_temp.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:prod/jobpac/openvpn-config-IK4dd6",
        "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:prod/jobpac/openvpn-authpass-H5uOEe",
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "bridge_temp" {
  name = "${var.project_name}-bridge-temp"
  role = aws_iam_role.bridge_temp.name

  tags = {
    Purpose = "jobpac-export-temp-vpn-bridge"
  }
}
