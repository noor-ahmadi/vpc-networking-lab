# VPC Networking Lab

Building a small VPC with Linux namespaces, virtual Ethernet links, and routing, then recreating it in AWS. The [local lab](docs/local-lab.md) has three subnet routing tables, a simulated internet edge, private NAT, and an Nginx proxy calling a private Python app and isolated PostgreSQL database. Checks capture both NAT translations and verify five repeatable [faults and repairs](docs/failures.md). The [AWS version](docs/aws.md) has live service, NAT, isolation, and teardown checks, plus three [cloud fault and repair exercises](docs/aws-failures.md) with packet evidence.
