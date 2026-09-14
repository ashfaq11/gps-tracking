# Gateway on Oracle Cloud Always Free

A real VM with a permanent public IPv4 — what the TCP gateway needs and what
serverless cannot give it. Always Free resources do not expire when the
30-day trial credits do.

## Pick a shape

| Shape | Free allowance | Notes |
|---|---|---|
| **VM.Standard.A1.Flex** (Ampere, Arm) | 4 OCPU + 24 GB across instances | best choice; `python:3.13-slim` has arm64 images |
| VM.Standard.E2.1.Micro (AMD) | 2 instances, 1 OCPU + 1 GB each | fallback when A1 capacity is unavailable |

1 OCPU and 1 GB handles a few thousand idle sockets comfortably — these
devices send a few hundred bytes every 10–30 seconds.

If you hit **"Out of host capacity"** on A1, that region has no free Ampere
left. Retry later, pick another availability domain, or use E2.1.Micro.

## 1. Create the instance

- Image: **Ubuntu 22.04 or 24.04**
- Under *Show advanced options → Initialization script*, paste
  [`cloud-init.yaml`](cloud-init.yaml)
- Assign a **public IPv4**, then reserve it: *Networking → Reserved public
  IPs*. An ephemeral IP changes on stop/start, and it is the address baked
  into every tracker's SMS config.

## 2. Open port 5023 — in BOTH places

This is the step that goes wrong. OCI has two independent firewalls and the
port must be open in each.

**a) Cloud: VCN → Subnet → Security List → Add Ingress Rule**

| Field | Value |
|---|---|
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `5023` |

**b) Host: iptables on the VM.** Ubuntu images on OCI ship an INPUT chain
that REJECTs everything but SSH. `cloud-init.yaml` handles this; on an
existing instance run:

```bash
sudo iptables -I INPUT 1 -p tcp --dport 5023 -m conntrack --ctstate NEW -j ACCEPT
sudo netfilter-persistent save
```

On Oracle Linux instead:

```bash
sudo firewall-cmd --permanent --add-port=5023/tcp && sudo firewall-cmd --reload
```

If the security list is open but the host firewall is not, connections hang
and then time out with no log line on the gateway — because the packets never
reach it.

## 3. Deploy

```bash
sudo apt-get install -y git
git clone <your-repo> /opt/gps-tracking
cd /opt/gps-tracking

sudo nano /etc/gps-gateway.env      # set PG_DSN to the Supabase SESSION pooler (5432)
sudo docker compose -f deploy/oci/docker-compose.yml --env-file /etc/gps-gateway.env up -d --build
sudo docker logs -f gps-gateway
```

Expect:

```
GPS ingestion gateway ready (GT06 over plain TCP)
  LISTEN  tcp://0.0.0.0:5023
```

Prefer no Docker? Use [`gps-gateway.service`](gps-gateway.service) with a
virtualenv instead.

## 4. Verify from your laptop

```bash
python3 tools/simulate_device.py --host <your-public-ip> --port 5023 --pings 3
```

CRC-valid ACKs mean the whole path works. Then confirm the rows arrived:

```sql
SELECT device_id, latitude, longitude, received_at
FROM device_locations ORDER BY id DESC LIMIT 5;
```

If the simulator hangs instead of connecting, it is the firewall — check step
2b before anything else.

## 5. Point real hardware at it

```
SERVER,0,<your-reserved-ip>,5023,0#
```

## Operating notes

- **Updates:** `git pull && sudo docker compose -f deploy/oci/docker-compose.yml up -d --build`.
  Every live device socket drops on restart; trackers reconnect on their own,
  but there is a gap in fixes.
- **Logs:** capped at 3 × 10 MB by the compose file, so the free tier's small
  boot volume cannot fill up.
- **Egress:** 10 TB/month free. Position frames are tiny; you will not get
  close.
- **Idle reclamation:** Oracle can reclaim *idle* Always Free compute. A
  gateway holding device connections is not idle, but if you run zero devices
  for weeks, expect a reclamation notice.
- **Exposure:** the gateway trusts the IMEI in the login packet. On a public
  IP, anyone who finds the port can impersonate a device. Restrict the
  ingress CIDR if your SIM provider gives you a fixed range, and add an IMEI
  allowlist before this carries anything that matters.
