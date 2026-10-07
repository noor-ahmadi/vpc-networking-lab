# Documentation addresses and a dummy public key/password are offline fixtures.
mock_provider "aws" {
  mock_resource "aws_vpc" {
    defaults = { id = "vpc-00000000000000001" }
  }
  mock_resource "aws_key_pair" {
    defaults = { key_name = "vpc-lab-offline" }
  }
}

override_resource {
  target = aws_security_group.workload["web"]
  values = { id = "sg-00000000000000001" }
}
override_resource {
  target = aws_security_group.workload["app"]
  values = { id = "sg-00000000000000002" }
}
override_resource {
  target = aws_security_group.workload["db"]
  values = { id = "sg-00000000000000003" }
}

# The real provider reports this computed flag true after the web EIP attaches.
override_resource {
  target = aws_instance.workload["web"]
  values = { associate_public_ip_address = true }
}

variables {
  ami_id             = "ami-00000000000000001"
  operator_cidr      = "198.51.100.10/32"
  ssh_public_key     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA offline-test"
  db_password        = "000000000000000000000000000000000000000000000000"
  bootstrap_database = false
}

run "isolated_topology" {
  command = apply
  assert {
    condition = (
      aws_vpc.lab.cidr_block == "10.0.0.0/16" && length(aws_subnet.lab) == 3 &&
      toset([for subnet in aws_subnet.lab : subnet.cidr_block]) == toset(["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]) &&
      alltrue([for subnet in aws_subnet.lab : subnet.availability_zone == "us-east-2a" && !subnet.map_public_ip_on_launch])
    )
    error_message = "Keep three distinct subnets in one AZ with automatic public addressing disabled."
  }
  assert {
    condition = (
      length(aws_route_table_association.lab) == 3 &&
      alltrue([for role, association in aws_route_table_association.lab :
        association.subnet_id == aws_subnet.lab[role].id && association.route_table_id == aws_route_table.lab[role].id
      ]) &&
      aws_route.public.gateway_id == aws_internet_gateway.lab.id &&
      aws_route.private.nat_gateway_id == aws_nat_gateway.lab.id &&
      length(aws_route.database_bootstrap) == 0
    )
    error_message = "Associate each subnet explicitly; only public/private have an external default in the final stage."
  }
  assert {
    condition = (
      length(aws_eip.lab) == 2 && aws_nat_gateway.lab.availability_mode == "zonal" &&
      aws_nat_gateway.lab.private_ip == "10.0.1.20" &&
      aws_nat_gateway.lab.subnet_id == aws_subnet.lab["public"].id &&
      aws_nat_gateway.lab.allocation_id == aws_eip.lab["nat"].id &&
      aws_eip_association.web.instance_id == aws_instance.workload["web"].id
    )
    error_message = "Keep one public zonal NAT at .20 and exactly two Elastic IP allocations."
  }
  assert {
    condition = (
      length(aws_instance.workload) == 3 &&
      aws_instance.workload["web"].private_ip == "10.0.1.10" &&
      aws_instance.workload["app"].private_ip == "10.0.2.10" &&
      aws_instance.workload["db"].private_ip == "10.0.3.10" &&
      alltrue([for role, instance in aws_instance.workload :
        (role == "web" || !instance.associate_public_ip_address) && instance.ami == var.ami_id &&
        instance.subnet_id == aws_subnet.lab[local.workloads[role].subnet].id &&
        instance.metadata_options[0].http_tokens == "required" &&
        instance.root_block_device[0].encrypted && instance.root_block_device[0].delete_on_termination &&
        instance.credit_specification[0].cpu_credits == "standard"
      ])
    )
    error_message = "Keep three private addresses, IMDSv2, encrypted disposable disks, and standard CPU credits."
  }
  assert {
    condition = (
      length(aws_vpc_security_group_ingress_rule.access) == 6 &&
      aws_vpc_security_group_ingress_rule.access["operator_http"].cidr_ipv4 == var.operator_cidr &&
      aws_vpc_security_group_ingress_rule.access["operator_ssh"].cidr_ipv4 == var.operator_cidr &&
      aws_vpc_security_group_ingress_rule.access["app_http"].referenced_security_group_id == aws_security_group.workload["web"].id &&
      aws_vpc_security_group_ingress_rule.access["database_sql"].referenced_security_group_id == aws_security_group.workload["app"].id &&
      aws_vpc_security_group_ingress_rule.access["database_sql"].from_port == 5432 &&
      aws_vpc_security_group_ingress_rule.access["app_http"].from_port == 8080
    )
    error_message = "Restrict external ingress to the operator and service ingress to the preceding workload group."
  }
  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.access) == 8 &&
      alltrue([for rule in aws_vpc_security_group_egress_rule.access : rule.ip_protocol == "tcp" && rule.from_port == rule.to_port]) &&
      alltrue([for rule in aws_vpc_security_group_egress_rule.access : rule.security_group_id != aws_security_group.workload["db"].id]) &&
      aws_vpc_security_group_egress_rule.access["app_database"].referenced_security_group_id == aws_security_group.workload["db"].id &&
      aws_vpc_security_group_egress_rule.access["app_database"].from_port == 5432
    )
    error_message = "The final database must have no new TCP egress; preserve explicit app-to-database access."
  }
  assert {
    condition = (
      !strcontains(nonsensitive(local.user_data["web"]), nonsensitive(var.db_password)) &&
      strcontains(nonsensitive(local.user_data["app"]), "EnvironmentFile=/etc/vpc-lab.env") &&
      strcontains(nonsensitive(local.user_data["db"]), "10.0.2.10/32 scram-sha-256")
    )
    error_message = "Keep the password off the proxy and configure the app environment and TCP SCRAM database policy."
  }
}

run "database_bootstrap" {
  command = apply
  variables {
    bootstrap_database = true
  }
  assert {
    condition = (
      length(aws_route.database_bootstrap) == 1 &&
      aws_route.database_bootstrap[0].nat_gateway_id == aws_nat_gateway.lab.id &&
      length(aws_vpc_security_group_egress_rule.access) == 10 &&
      aws_vpc_security_group_egress_rule.access["db_http"].from_port == 80 &&
      aws_vpc_security_group_egress_rule.access["db_https"].from_port == 443
    )
    error_message = "Bootstrap needs its explicit temporary NAT route and only HTTP/HTTPS database egress."
  }
}

run "remove_bootstrap" {
  command = plan
  assert {
    condition = (
      length(aws_route.database_bootstrap) == 0 && length(aws_vpc_security_group_egress_rule.access) == 8 &&
      aws_instance.workload["db"].id == run.database_bootstrap.instance_ids["db"]
    )
    error_message = "Remove the bootstrap route/rules while retaining the existing database instance."
  }
}

run "reject_broad_operator" {
  command = plan
  variables {
    operator_cidr = "0.0.0.0/0"
  }
  expect_failures = [var.operator_cidr]
}

run "reject_password_injection" {
  command = plan
  variables {
    db_password = "invalid'password"
  }
  expect_failures = [var.db_password]
}

run "reject_wrong_az" {
  command = plan
  variables {
    availability_zone = "us-east-1a"
  }
  expect_failures = [var.availability_zone]
}
