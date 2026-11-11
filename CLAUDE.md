# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Project

JobPac Data Export — a scheduled ECS Fargate task (Python) that pulls tables from an
on-prem AS/400 (IBM i / DB2) JobPac database over JDBC (`jt400`), writes them to S3 as
CSV, and sends an SES email only when the run (or any table) fails. Runs on a cron schedule (EventBridge
Scheduler), Monday–Friday 2 AM Australia/Brisbane. See `README.md` for the general
architecture, config, and local dev instructions.

Infrastructure is Terraform-managed (`infra/`), state in
`s3://nwci-pbi-bucket/jobpac-export/terraform.tfstate`, AWS account `118439969456`,
region `ap-southeast-2`, profile `NWCI-Prod`.

---

## Current situation: temporary VPN bridge (as of 2026-08-25)

### Why this exists

The task is meant to reach the on-prem DB2 server over the account's Site-to-Site VPN
(`vgw-051bc5684227443e2` / `vpn-013c1a33af035de66`). The AWS side of that tunnel is up,
but on-prem routing/firewall to the actual DB subnet (`10.128.13.0/24`) was not yet
live — the tunnel's route table only had a BGP-propagated route for the transit network
(`10.220.210.0/24`), not the DB subnet itself, and a direct test confirmed the DB was
unreachable via that path.

As a stopgap, traffic is routed instead through an existing EC2 instance (`Jobpac-EC2`,
`i-03e129838d814b4f7`) running an OpenVPN client — the same VPN credentials used for
local/manual access to the DB — acting as a NAT bridge for the Fargate task. This is
**temporary and additive only**: nothing about the original Site-to-Site VPN path (VGW,
Customer Gateway, VPN Connection, or the existing public route table's on-prem routes)
was touched. Once the real Site-to-Site VPN is confirmed working end-to-end, the bridge
gets decommissioned and traffic reverts to native VGW routing.

### What's been done

All new resources live in `infra/temp_vpn_bridge.tf`, tagged `Purpose =
jobpac-export-temp-vpn-bridge`, so the whole bridge is one file to delete at teardown
(plus the two follow-up edits noted below).

- **New subnet** `subnet-0a97fa38922fc00a6` (`10.100.1.192/26`, ap-southeast-2a) — the
  only unused /26 left in the VPC's `10.100.1.0/24` block.
- **New route table** `rtb-0ae5216fe6a9ef8ef`, associated with that subnet only:
  - `0.0.0.0/0` → the existing IGW (reused as a target only, not modified)
  - `10.128.13.0/24` → `Jobpac-EC2`'s ENI (`eni-063e3e5850ec553e6`)

  This is the core trick: the existing public route table already routes
  `10.128.13.0/24` to the VGW, and a route table can't hold two routes to the same
  destination — so rather than touch that (forbidden), the Fargate task runs in this
  new subnet/route table instead, where the same destination points at the bridge.
- **S3 gateway endpoint** (`vpce-04d7a166aed0f00cd`) extended to include this new route
  table alongside the two it already had — so S3 traffic from the bridge subnet stays
  off the public internet, same as the original public subnet.
- **New security group** `sg-00887205897db4152` on `Jobpac-EC2`, added *alongside* its
  original `sg-04ad480df46a87f0e` (not replacing it — an instance can have multiple
  SGs, and the original stays completely untouched). Only carries what's unique to the
  bridge role: JDBC ingress (449/8471/8476) from the bridge subnet, and egress for
  OpenVPN (UDP 8443) + HTTP/DNS (needed for `apt`/package installs — HTTPS/443 wasn't
  duplicated since the original SG already covers it).
- **New IAM role/instance profile** `jobpac-export-bridge-temp`, attached to
  `Jobpac-EC2` (which had no IAM role before), scoped to `secretsmanager:GetSecretValue`
  on exactly two secrets: `prod/jobpac/openvpn-config` (the `.ovpn` client config) and
  `prod/jobpac/openvpn-authpass` (the VPN login username/password `auth-user-pass`
  file it references). Chosen over reusing an existing role because none of the
  project's existing roles trust `ec2.amazonaws.com`, and reusing the task role would
  have handed the bridge instance permissions (S3 write, SES send, the DB credentials
  secret) it has no business having.
- **`sg-04ad480df46a87f0e` (the shared task SG) updated**: `jobpac_db_ports` in
  `variables.tf` changed from `[449, 8471]` to `[449, 8471, 8476]` — see the port
  discovery below. This is the one change to a resource actually shared with
  production, and it's a strict addition (existing egress rules untouched).
- **On `Jobpac-EC2` itself** (not Terraform-managed — it predates this project,
  hand-created, so managed via direct AWS CLI / SSH instead of importing into state):
  - `source_dest_check` disabled (required to forward traffic not addressed to itself)
  - OpenVPN client installed, config/credentials fetched from the two secrets above at
    `/etc/openvpn/client/jobpac.conf` + `/etc/openvpn/client/pass.txt`
  - `net.ipv4.ip_forward=1` (persisted via `/etc/sysctl.d/99-jobpac-bridge.conf`)
  - `iptables -t nat -A POSTROUTING -o tun0 -j MASQUERADE` (persisted via
    `netfilter-persistent`) — see reasoning below
  - A manually-added route, `10.100.1.192/26 via 10.100.1.129 dev ens5` — see reasoning
    below. **Not yet persisted across reboot** (see Pending).
- **Live scheduler cut over**: `aws_scheduler_schedule.main`'s network configuration in
  `main.tf` now points at `aws_subnet.bridge_temp.id` instead of `aws_subnet.public_a.id`
  (marked with a comment at the call site). Cron expression, task definition, security
  group, and retry policy are all unchanged. This was held back deliberately until the
  bridge was proven end-to-end with a manual test run.

### Non-obvious bugs hit and fixed along the way (useful if this breaks again)

1. **`ip-win32 dynamic` in the `.ovpn` config** — a Windows-only OpenVPN directive;
   the config was exported for a Windows client. Commented out for the Linux client.
2. **Routing black hole**: OpenVPN pushes a route for the *entire* `10.100.1.0/24`
   supernet into the tunnel (the on-prem/Sophos side apparently expects the whole VPC
   block to be VPN-only). That's more specific than the default route but less
   specific than `Jobpac-EC2`'s own subnet route (`10.100.1.128/26`), so it silently
   swallowed return traffic addressed to the Fargate task's subnet
   (`10.100.1.192/26`) into the tunnel instead of sending it back out to the VPC. Fixed
   with a more-specific manual route (see above) — confirmed via `tcpdump` on both
   `ens5` and `tun0` showing forwarded SYNs with no matching route back.
3. **On-prem source-IP filtering**: forwarding worked (packets confirmed reaching
   on-prem via `tcpdump`), but got no response at all. A direct `nc` test *from*
   `Jobpac-EC2` worked, using source IP `10.81.234.3` (auto-selected, the tunnel's own
   address) — but the Fargate task's real IP (something in `10.100.1.192/26`) got
   silently dropped. On-prem (Sophos) apparently trusts specific VPN pool IPs, not the
   whole pool by network. Fixed with the MASQUERADE rule so forwarded traffic appears
   to originate from the bridge's own tunnel IP.
4. **Tunnel IP mismatch with the known-good IP**: the user's own local VPN client
   reliably gets `10.81.234.131`; the bridge's first connection got a different pool
   IP (`10.81.234.3`) and still failed the same way even after the masquerade fix.
   Restarting the OpenVPN client got it reassigned `10.81.234.131` (same cert/identity
   → same persistent pool IP) — but this turned out not to be the actual fix (see #5);
   both IPs hit the identical failure signature once decoded.
5. **The real fix — missing Sign-On Server port**: packet payloads showed the Fargate
   task's JDBC driver sending the literal ASCII string `as-signon` to port 449 (the
   AS/400 "Central Server"/port-mapper) and getting back a 5-byte reply decoding to
   `0x211C` = **8476** — the standard unsecure AS/400 Sign-On Server port. The
   project's `jobpac_db_ports` var only had `[449, 8471]` (Central Server + Database
   Host Server) — missing Sign-On Server, which the jt400 driver contacts *before* the
   database server, regardless of what port the connection string names. Adding 8476
   to both `sg-04ad480df46a87f0e` (task SG) and the bridge SG's ingress fixed it — JDBC
   connected cleanly on the next test. This is a real, permanent fix that will matter
   again once traffic reverts to the native VGW path — the same port would have been
   missing there too.

### Pending / not yet done

- **The manual route on `Jobpac-EC2` (`10.100.1.192/26 via ens5`) is not persisted.**
  It was added live via `ip route add` and will be lost on reboot. Needs a permanent
  home — e.g. a netplan static route, or an OpenVPN `route-up` script — before this
  bridge can be trusted across an instance restart.
- **First scheduled run through the bridge hasn't happened yet.** The cutover was
  applied today (2026-08-25); the next scheduled trigger is 2 AM Brisbane. Worth
  checking `/ecs/jobpac-export` CloudWatch logs after that run.
- **Decommission, once the real Site-to-Site VPN is confirmed working on-prem**:
  1. Revert `aws_scheduler_schedule.main`'s subnet back to `aws_subnet.public_a.id` in
     `main.tf` (comment already marks the spot).
  2. Delete `infra/temp_vpn_bridge.tf` and remove the `aws_route_table.bridge_temp.id`
     entry from `aws_vpc_endpoint.s3.route_table_ids` in `main.tf`, then
     `terraform apply`.
  3. On `Jobpac-EC2`: re-enable `source_dest_check`, detach the
     `jobpac-export-bridge-temp` instance profile, remove the MASQUERADE iptables rule
     and the manual route, optionally stop the OpenVPN client
     (`systemctl disable --now openvpn-client@jobpac`). `sg-04ad480df46a87f0e` was
     never removed from the instance, so nothing needs reattaching there.
  4. Decide separately whether to keep `Jobpac-EC2` around for other manual testing —
     it predates this project and isn't Terraform-managed.
  5. `jobpac_db_ports` (now `[449, 8471, 8476]`) should stay as-is — it's a genuine fix,
     unrelated to the bridge itself.

### In progress at time of writing

- A manual test export (`aws ecs run-task` against `subnet-0a97fa38922fc00a6`) was
  still running as of this writing — 8/12 tables completed successfully through the
  bridge (`JCODECCP`, `PEMPMEMP`, `CLAIMHSHS`, `CLAIMHIST`, `JHSJTHTP`, `CCREDCRP`,
  `JHSJBHJP`, `JLABCLBP`), each uploaded to
  `s3://nwci-pbi-bucket/jobpac-export/2026-08-25T09-40-50/`. One table (`JHSJBHJP`)
  took ~41 minutes — worth keeping an eye on whether that's normal for that table's
  size or a sign the bridge's `t3.micro` + VPN path is a bandwidth bottleneck for large
  tables.

### Key resource IDs (bridge-related)

| Resource | ID |
|---|---|
| Bridge instance | `Jobpac-EC2` / `i-03e129838d814b4f7` (public IP `3.25.213.45`, SSH key `jobpac-ec2-key`) |
| Bridge subnet | `subnet-0a97fa38922fc00a6` (`10.100.1.192/26`) |
| Bridge route table | `rtb-0ae5216fe6a9ef8ef` |
| Bridge security group | `sg-00887205897db4152` |
| Original task security group (unchanged, still attached to both the task and the bridge instance) | `sg-04ad480df46a87f0e` |
| Bridge IAM role/instance profile | `jobpac-export-bridge-temp` |
| VPN config secret | `prod/jobpac/openvpn-config` |
| VPN auth secret | `prod/jobpac/openvpn-authpass` |
| VPN server | `vpnaccess.nwciqld.com:8443/udp` (Sophos appliance) |
| Bridge's tunnel IP | `10.81.234.131` (same pool as local VPN clients; persists per-certificate on reconnect) |
| Production DB endpoint | `10.128.13.219` (ports 449, 8471, 8476) |
