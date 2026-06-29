# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "ecr_repository_url" {
  description = "ECR repository URL for pushing Docker images"
  value       = aws_ecr_repository.main.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  description = "ECS cluster ARN"
  value       = aws_ecs_cluster.main.arn
}

output "task_definition_arn" {
  description = "ECS task definition ARN"
  value       = aws_ecs_task_definition.main.arn
}

output "s3_bucket_name" {
  description = "S3 bucket name for CSV exports"
  value       = var.create_s3_bucket ? aws_s3_bucket.export[0].id : var.s3_bucket_name
}

output "log_group_name" {
  description = "CloudWatch Logs group name"
  value       = aws_cloudwatch_log_group.main.name
}

output "scheduler_name" {
  description = "EventBridge Scheduler schedule name"
  value       = aws_scheduler_schedule.main.name
}

output "scheduler_arn" {
  description = "EventBridge Scheduler schedule ARN"
  value       = aws_scheduler_schedule.main.arn
}

# ---------------------------------------------------------------------------
# Useful commands (displayed after terraform apply)
# ---------------------------------------------------------------------------

output "public_subnet_id" {
  description = "Public subnet ID used by Fargate tasks (has VPN + IGW routes)"
  value       = aws_subnet.public_a.id
}

output "task_security_group_id" {
  description = "Security group ID attached to Fargate tasks"
  value       = aws_security_group.task.id
}

output "manual_run_command" {
  description = "AWS CLI command to manually trigger the Fargate task"
  value       = <<-EOT
    aws ecs run-task \
      --cluster ${aws_ecs_cluster.main.name} \
      --task-definition ${aws_ecs_task_definition.main.family} \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${aws_subnet.public_a.id}],securityGroups=[${aws_security_group.task.id}],assignPublicIp=ENABLED}" \
      --region ${var.aws_region}
  EOT
}

output "docker_push_commands" {
  description = "Commands to build and push the Docker image to ECR"
  value       = <<-EOT
    # Authenticate Docker with ECR
    aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${aws_ecr_repository.main.repository_url}

    # Build and push (--platform required when building on Apple Silicon for x86_64 Fargate)
    docker build --platform linux/amd64 -t ${var.project_name} .
    docker tag ${var.project_name}:latest ${aws_ecr_repository.main.repository_url}:latest
    docker push ${aws_ecr_repository.main.repository_url}:latest
  EOT
}
