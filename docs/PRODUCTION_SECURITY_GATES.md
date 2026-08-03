# Production security gates

The controls below are **BLOCKING** for a production launch. A dedicated Docker
bridge is useful service separation, but it is not an L4 egress policy: Docker
normally NATs bridge traffic through the host.

## One-shot maintenance egress

Before running either maintenance profile, install and retain an audited
host/container egress policy for `brx-sync-maintenance-net` with default deny.
Allow only:

- DNS to the approved resolver when name resolution is required;
- TCP 5432 to the private, allowlisted RDS destination used by the job.

The jobs receive credentials through their environment and do not need Internet,
Redis, API, worker, SSM or KMS data-plane access. Capture evidence from the same
host that RDS:5432 succeeds while public HTTPS, the runtime Docker subnets and the
Redis/API/worker addresses fail. Re-run this proof after every network or firewall
change. Do not execute a production migration without the evidence attached to
the change record.

Prefer a scheduler/network implementation with a dedicated security group and
explicit RDS destination over mutable IP rules. If host firewall rules are used,
account for Docker's `DOCKER-USER`/forwarding behavior and RDS address changes.

## Redis trust boundary

The current local Redis has no TLS or ACL authentication. An untrusted process in
an attached sibling container could manipulate rate limits, deduplication, leases
or queued work. Before launch, replace it with managed private Redis using TLS,
ACL credentials from a dedicated secret and a security group limited to the Sync
workloads. At minimum, configure a rotated Redis ACL password and `rediss://` with
certificate verification; do not print the resulting `REDIS_URL` in deployment or
application logs.

Record a failover/restore exercise and verify the `noeviction` capacity alarm,
connection limit, authentication failure alert and keyspace isolation before the
gate is signed off.
