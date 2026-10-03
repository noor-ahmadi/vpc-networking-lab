output "stage" {
  value = var.bootstrap_database ? "database bootstrap: temporary external access" : "database isolated: no external route or new TCP egress"
}

output "proxy_ip" {
  value = aws_eip.lab["web"].public_ip
}

output "nat_ip" {
  value = aws_eip.lab["nat"].public_ip
}

output "instance_ids" {
  value = { for role, instance in aws_instance.workload : role => instance.id }
}

output "resource_inventory" {
  description = "Save privately before destroy, then verify these IDs were removed."
  value = {
    region              = var.region
    vpc_id              = aws_vpc.lab.id
    internet_gateway_id = aws_internet_gateway.lab.id
    nat_gateway_id      = aws_nat_gateway.lab.id
    elastic_ip_ids      = { for role, address in aws_eip.lab : role => address.id }
    instance_ids        = { for role, instance in aws_instance.workload : role => instance.id }
    root_volume_ids     = { for role, instance in aws_instance.workload : role => instance.root_block_device[0].volume_id }
  }
}
