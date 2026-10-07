resource "aws_security_group" "workload" {
  for_each    = local.workloads
  name_prefix = "vpc-lab-${each.key}-"
  description = "VPC lab ${each.key} service and administration access"
  vpc_id      = aws_vpc.lab.id
  tags        = { Name = "vpc-lab-${each.key}" }
}

locals {
  ingress = {
    operator_http = { role = "web", port = 80, cidr = var.operator_cidr, peer = null }
    operator_ssh  = { role = "web", port = 22, cidr = var.operator_cidr, peer = null }
    app_http      = { role = "app", port = 8080, cidr = null, peer = "web" }
    database_sql  = { role = "db", port = 5432, cidr = null, peer = "app" }
    app_ssh       = { role = "app", port = 22, cidr = null, peer = "web" }
    database_ssh  = { role = "db", port = 22, cidr = null, peer = "web" }
  }
  egress = merge({
    web_app      = { role = "web", port = 8080, cidr = null, peer = "app" }
    app_database = { role = "app", port = 5432, cidr = null, peer = "db" }
    web_app_ssh  = { role = "web", port = 22, cidr = null, peer = "app" }
    web_db_ssh   = { role = "web", port = 22, cidr = null, peer = "db" }
    web_http     = { role = "web", port = 80, cidr = "0.0.0.0/0", peer = null }
    web_https    = { role = "web", port = 443, cidr = "0.0.0.0/0", peer = null }
    app_http     = { role = "app", port = 80, cidr = "0.0.0.0/0", peer = null }
    app_https    = { role = "app", port = 443, cidr = "0.0.0.0/0", peer = null }
    }, var.bootstrap_database ? {
    db_http  = { role = "db", port = 80, cidr = "0.0.0.0/0", peer = null }
    db_https = { role = "db", port = 443, cidr = "0.0.0.0/0", peer = null }
  } : {})
}

# Standalone rules preserve group references without opening all-protocol egress.
resource "aws_vpc_security_group_ingress_rule" "access" {
  for_each                     = local.ingress
  security_group_id            = aws_security_group.workload[each.value.role].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  cidr_ipv4                    = each.value.cidr
  referenced_security_group_id = each.value.peer == null ? null : aws_security_group.workload[each.value.peer].id
  description                  = each.key
}

resource "aws_vpc_security_group_egress_rule" "access" {
  for_each                     = local.egress
  security_group_id            = aws_security_group.workload[each.value.role].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  cidr_ipv4                    = each.value.cidr
  referenced_security_group_id = each.value.peer == null ? null : aws_security_group.workload[each.value.peer].id
  description                  = each.key
}
