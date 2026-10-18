# Keeping the AWS bill small

The whole backend is **one EC2 instance**: the API, the GT06 gateway and
Postgres side by side. The dashboard is on Cloudflare (free), maps come
from OpenFreeMap (free) and alerts need no paid push service. So the bill is
basically the instance, its disk, its public IP and data sent out.
Everything below keeps it that way.

## Already done in code

| Saving | How |
|---|---|
| **Data out** (≈ $0.11/GB in Mumbai after the free 100 GB/month) | The API gzips every response over 1 KB (`GZipMiddleware` in `api/main.py`). Fix lists and reports are repetitive JSON and shrink 5–10×. |
| **Disk that grows forever** | `purge_old_locations()` (`sql/schema.sql`), run nightly by `deploy/aws/maintenance.sh`. It keeps 180 days of positions by default (`LOCATION_RETENTION_DAYS`, minimum 90, `0` = keep all) and always keeps each vehicle's last position. A tracker reporting every 10–30 s adds about 0.5 GB a year, so 100 vehicles would otherwise add about 50 GB a year. |
| **Index size** | The purge uses a BRIN index on `received_at`: a few KB, not GBs. |
| **Backups without RDS** | Nightly `pg_dump`, compressed, 7 days kept on the disk. Set `BACKUP_S3_URI` for an off-box copy in S3 Infrequent Access (cents a month). |
| **Docker/app logs** | Errors only by default. Daily files are kept 30 days, and the json-file driver is capped at 3×10 MB. |
| **Gateway writes** | One `COPY` per batch, not an `INSERT` per packet, so a small instance keeps up. |

## Do once in the AWS console (I can't reach the account from here)

1. **Set a budget alarm.** In *Billing → Budgets → Create budget*, choose a monthly cost budget of e.g. $20 with email alerts at 80% and 100%. It's free and catches surprises.
2. **Right-size the instance.** Run `free -m` and `uptime` on the server.
   - If memory use stays under 50% and load stays under 0.5, move down a size.
   - **Graviton (`t4g`)** instances cost about 20% less than `t3` for the same size, and everything here runs on ARM (Python, Postgres, Docker images).
   - `t4g.small` (2 GB) is plenty for hundreds of trackers.
   - Changing type: stop the instance, use *Instance settings → Change instance type*, then start it. Check first that it has an Elastic IP, or the public IP changes.
3. **Commit for a year.** A Compute Savings Plan (1 year, no upfront) takes about 30–40% off the instance once the size is settled.
4. **Disk:** use **gp3**, not gp2. It's 20% cheaper and can be changed live under *EC2 → Volumes → Modify*. 20–30 GB is enough with retention on.
5. **Delete leftovers.** Look for:
   - unattached EBS volumes;
   - old snapshots and AMIs;
   - Elastic IPs not attached to a running instance (charged hourly);
   - stopped test instances;
   - any NAT gateway, load balancer or RDS instance. None of these is needed here: a NAT gateway alone is about $35 a month.
6. **Public IPv4.** Every public IPv4 address costs about $3.60 a month. Keep exactly one, the Elastic IP the trackers are configured with.
7. **CloudWatch.** If the CloudWatch agent was ever installed, set log retention to 7–14 days. Logs default to "never expire".

## Free-tier limits to watch

- **Cloudflare Workers: 100,000 requests a day free.** Both the dashboard and the native app call the API through the worker. A live-map socket counts once per connection, not per message. The app's background alert service checks ignition once a minute, about 1,440 requests a day per phone. Past roughly 50 phones with background alerts on, either move to Workers Paid ($5/month) or point the app straight at a TLS endpoint on the server (Cloudflare Tunnel, also free).
- **OpenFreeMap:** free with no limits. It's a volunteer service, so keep a self-hosted PMTiles file as plan B if it ever matters.

## Rough monthly cost (Mumbai, on-demand, Oct 2026)

| Item | Cost |
|---|---|
| t4g.small | ~$12 (≈ $8 with a Savings Plan) |
| 30 GB gp3 | ~$2.70 |
| Public IPv4 | ~$3.60 |
| Data out, gzipped | usually within the free 100 GB |
| S3 backups (optional) | < $0.50 |
| **Total** | **≈ $15–19/month** |

Check the current numbers on the AWS pricing page; they change.
