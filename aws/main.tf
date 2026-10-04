terraform {
  required_version = "= 1.16.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "vpc-networking-lab" }
  }
}

locals {
  subnets = {
    public   = "10.0.1.0/24"
    private  = "10.0.2.0/24"
    isolated = "10.0.3.0/24"
  }
  workloads = {
    web = { subnet = "public", ip = "10.0.1.10" }
    app = { subnet = "private", ip = "10.0.2.10" }
    db  = { subnet = "isolated", ip = "10.0.3.10" }
  }
  user_data = {
    for role in keys(local.workloads) : role => templatefile("${path.module}/bootstrap.sh.tftpl", {
      role        = role
      db_password = var.db_password
      server      = filebase64("${path.module}/../app/server.py")
      seed        = filebase64("${path.module}/../app/seed.sql")
    })
  }
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "vpc-networking-lab" }
}

resource "aws_subnet" "lab" {
  for_each                = local.subnets
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = each.value
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false
  tags                    = { Name = "vpc-lab-${each.key}" }
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "vpc-lab-edge" }
}

resource "aws_eip" "lab" {
  for_each = toset(["web", "nat"])
  domain   = "vpc"
  tags     = { Name = "vpc-lab-${each.key}" }
}

resource "aws_nat_gateway" "lab" {
  availability_mode = "zonal"
  connectivity_type = "public"
  allocation_id     = aws_eip.lab["nat"].id
  subnet_id         = aws_subnet.lab["public"].id
  private_ip        = "10.0.1.20"
  depends_on        = [aws_internet_gateway.lab]
  tags              = { Name = "vpc-lab-nat" }
}

# The VPC's main table stays local-only; every subnet has an explicit association.
resource "aws_route_table" "lab" {
  for_each = local.subnets
  vpc_id   = aws_vpc.lab.id
  tags     = { Name = "vpc-lab-${each.key}" }
}

resource "aws_route_table_association" "lab" {
  for_each       = local.subnets
  subnet_id      = aws_subnet.lab[each.key].id
  route_table_id = aws_route_table.lab[each.key].id
}

resource "aws_route" "public" {
  route_table_id         = aws_route_table.lab["public"].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.lab.id
}

resource "aws_route" "private" {
  route_table_id         = aws_route_table.lab["private"].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.lab.id
}

resource "aws_route" "database_bootstrap" {
  count                  = var.bootstrap_database ? 1 : 0
  route_table_id         = aws_route_table.lab["isolated"].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.lab.id
}

resource "aws_key_pair" "operator" {
  key_name_prefix = "vpc-lab-"
  public_key      = trimspace(var.ssh_public_key)
}

resource "aws_instance" "workload" {
  for_each      = local.workloads
  ami           = var.ami_id
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.lab[each.value.subnet].id
  private_ip    = each.value.ip
  # Web inherits disabled subnet auto-addressing; this computed flag becomes
  # true when its separately managed EIP is attached.
  associate_public_ip_address = each.key == "web" ? null : false
  vpc_security_group_ids      = [aws_security_group.workload[each.key].id]
  key_name                    = aws_key_pair.operator.key_name
  user_data                   = local.user_data[each.key]
  user_data_replace_on_change = true
  credit_specification {
    cpu_credits = "standard"
  }
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true
  }
  depends_on = [
    aws_route.public, aws_route.private, aws_route.database_bootstrap,
    aws_route_table_association.lab,
    aws_vpc_security_group_ingress_rule.access,
    aws_vpc_security_group_egress_rule.access,
  ]
  tags = { Name = "vpc-lab-${each.key}" }
}

resource "aws_eip_association" "web" {
  instance_id   = aws_instance.workload["web"].id
  allocation_id = aws_eip.lab["web"].id
}
