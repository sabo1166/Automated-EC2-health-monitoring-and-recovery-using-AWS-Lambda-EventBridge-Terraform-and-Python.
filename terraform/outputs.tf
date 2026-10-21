output "initial_website_url" {
  description = "Public URL of the initial Nginx instance."
  value       = "http://${aws_instance.web.public_ip}"
}

output "initial_instance_id" {
  description = "ID of the initial EC2 instance."
  value       = aws_instance.web.id
}

output "lambda_function_name" {
  description = "Self-healing Lambda function name."
  value       = aws_lambda_function.healer.function_name
}

output "health_check_schedule" {
  description = "EventBridge health-check schedule."
  value       = aws_cloudwatch_event_rule.health_check.schedule_expression
}

output "dynamodb_lock_table" {
  description = "Table used to prevent concurrent replacements."
  value       = aws_dynamodb_table.lock.name
}
