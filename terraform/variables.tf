variable "aws_region" {
  description = "AWS region for the project."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used to identify project resources."
  type        = string
  default     = "self-healing-ec2"
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
  default     = "t3.micro"
}

variable "check_interval_minutes" {
  description = "Minutes between health checks."
  type        = number
  default     = 5

  validation {
    condition     = var.check_interval_minutes >= 1 && var.check_interval_minutes <= 60
    error_message = "The check interval must be between 1 and 60 minutes."
  }
}
