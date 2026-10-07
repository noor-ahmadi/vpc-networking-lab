# Recorded demonstration

[Watch the silent terminal video](https://github.com/noor-ahmadi/vpc-networking-lab/releases/download/v1.0.0/vpc-networking-lab.webm)
or replay [the original recording](../demo/vpc-networking.cast) locally:

```sh
asciinema play demo/vpc-networking.cast
```

This 88-second demonstration was recorded October 6, 2026, in a disposable
Ubuntu 24.04 container with its
external network disabled. The commands, request results, packet captures,
and delays come from one actual run of [demo/run.sh](../demo/run.sh).
The video renders that output at its recorded timing; there is no narration
or simulated command output. The original uses the
[asciicast v2 format](https://docs.asciinema.org/manual/asciicast/v2/).

The sequence shows the topology and actual policy routes, fetches a real
PostgreSQL row through Nginx and the private app, captures one fresh connection
at both SNAT points and its reply, then removes the private external route.
A healthy outside fixture is still reachable from itself, the app's new
external connection fails, and the database-backed request stays healthy.
Repair restores a fresh translated connection before services and namespaces
are removed. Assertions stop the script if these outcomes differ.

The final section displays the **earlier October 5 AWS evidence file**. It
is labeled as saved evidence in the recording. No AWS deployment or live
cloud fault is part of this recording. The two prior cloud runs and their
independent cleanup are detailed in [the AWS evidence](../evidence/aws-faults-2026-10-05.md).

## Repeat the live sequence

Use a disposable Linux environment with the [local dependencies](local-lab.md)
installed and no running lab, then:

```sh
sudo bash demo/run.sh
```

The script starts and stops its own services, and removes only the lab it
created. It uses the existing traffic/service check helpers. CI also executes
this sequence in a separate network stack, alongside the full two-cycle
integration suite and offline AWS checks. No recording software or AWS
credentials are needed to repeat the commands.

For a new terminal recording with asciinema installed:

```sh
sudo asciinema rec --command 'bash demo/run.sh' demo/new-run.cast
```
