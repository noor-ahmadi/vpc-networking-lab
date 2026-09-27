# VPC Networking Lab

Building a small VPC with Linux namespaces, virtual Ethernet links, and routing, then recreating it in AWS. The [local lab](docs/local-lab.md) gives three subnets their own routing tables, checks connectivity and ARP, and tests that traffic cannot escape through an unrelated router route. The external fixture, NAT, and AWS version come next.
