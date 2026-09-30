# VPC Networking Lab

Building a small VPC with Linux namespaces, virtual Ethernet links, and routing, then recreating it in AWS. The [local lab](docs/local-lab.md) has three subnet routing tables, a simulated internet edge, private NAT, and an Nginx proxy calling a private Python app and isolated PostgreSQL database. Checks exercise the service access rules and capture both NAT translations. The AWS version is next.
