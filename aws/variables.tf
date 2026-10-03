variable "region" {
  type        = string
  default     = "us-east-2"
  description = "AWS region for the short-lived lab."
}

variable "availability_zone" {
  type        = string
  default     = "us-east-2a"
  description = "One enabled Availability Zone in the chosen region."
  validation {
    condition     = can(regex("^${var.region}[a-z]$", var.availability_zone))
    error_message = "Choose a standard Availability Zone in region."
  }
}

variable "ami_id" {
  type        = string
  description = "Pinned Canonical Ubuntu 24.04 amd64 server AMI in region."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply an explicit regional Ubuntu 24.04 AMI ID."
  }
}

variable "operator_cidr" {
  type        = string
  description = "Operator's current public IPv4 /32; only this client gets web and SSH ingress."
  validation {
    condition     = can(cidrnetmask(var.operator_cidr)) && can(regex("^[0-9.]+/32$", var.operator_cidr))
    error_message = "operator_cidr must be one IPv4 address with /32, not a wider range."
  }
}

variable "ssh_public_key" {
  type        = string
  description = "Existing RSA or Ed25519 public key; keep its private key on the operator's machine."
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]+={0,2}( .*)?$", trimspace(var.ssh_public_key)))
    error_message = "Supply an OpenSSH RSA or Ed25519 public key."
  }
}

variable "db_password" {
  type        = string
  sensitive   = true
  description = "Generated 48-character lowercase hex demo password, supplied outside Git. Stored in local state and app/DB user data."
  validation {
    condition     = can(regex("^[0-9a-f]{48}$", var.db_password))
    error_message = "Generate the demo password with Python secrets.token_hex(24)."
  }
}

variable "bootstrap_database" {
  type        = bool
  description = "Explicitly choose true for the first apply, then false only after DB cloud-init and a real query succeed."
}
