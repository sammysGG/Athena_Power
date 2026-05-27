# Deploy the SCADA range

`ansible/main.yml` is an **Ansible role tasks file** (a flat task list, no play
wrapper) — the same style as the other Athena range roles. The range harness
consumes it as `roles/power/tasks/main.yml`; it is *not* run standalone with
`ansible-playbook`.

It brings a fresh Debian/Ubuntu host up as a fully working range target — no
Docker. Everything is in the one `main.yml`: range vars (`set_fact`, overridable),
the rendered `.env`, four **systemd** services, nginx TLS, and a boot-time update
unit.

The four units (run from a shared Python venv as the unprivileged `scada` user):

| Unit | Role | Bind |
|---|---|---|
| `scada-simulator.service` | plant simulator (FastAPI) | `127.0.0.1:9101` |
| `scada-modbus.service` | Modbus TCP slave (OT attack surface) | `0.0.0.0:15020` |
| `scada-bridge.service` | protocol bridge / historian | `127.0.0.1:9102` |
| `scada-hmi.service` | operator HMI (FastAPI) | `{{ hmi_bind }}:{{ hmi_port }}`, fronted by nginx TLS on 443 |

## How it deploys

The role:

1. installs python/nginx/openssl/git,
2. `git clone`s `https://github.com/sammysGG/Athena_Power.git` to `/opt/scada-range`,
3. builds the venv and installs each service's `requirements.txt`,
4. renders `/opt/scada-range/.env` (first deploy only — preserves blue-team changes),
5. installs + starts the four `scada-*` units, fronts the HMI with a self-signed cert,
6. installs `scada-update.service`, which `git reset --hard origin/main` + restarts on boot.

It expects **`hostname`** and **`domain`** from the surrounding playbook (used for
the TLS cert CN and the nginx `server_name`), exactly like the other range roles.
Every `scada_*` / `hmi_*` / `modbus_*` var has a default in the `set_fact` block at
the top of `main.yml` and can be overridden via `group_vars` or `-e`.

## What the target needs

| Requirement | Why |
|---|---|
| Debian 11/12 or Ubuntu 22.04/24.04 | Recent systemd (`Type=exec`) and `python3-venv`. |
| Outbound internet during deploy (or a local PyPI mirror) | First run `git clone`s the repo and `pip install`s each service's requirements. |
| Host firewall ports | `443/tcp` (HMI via nginx) and `15020/tcp` (Modbus). Set `range_open_firewall: true` to have the role open them via `ufw`. |

You do **not** need a domain on public DNS, a public CA, or a Cloudflare tunnel —
the range is reached by IP on its isolated network and the TLS cert is self-signed.

## Managing the services on the target

```bash
systemctl status scada-hmi.service          # any of the four scada-* units
systemctl restart scada-bridge.service
journalctl -u scada-simulator.service -f    # live logs
```

## Secrets handling

The two inter-service keys (`bridge_api_key`, `simulator_api_key`) protect the
internal channels — keep them out of git and pass them at runtime (e.g. via the
harness's `group_vars`/vault), overriding the `CHANGE_ME_*` defaults.

The HMI credentials (`admin` / `Cool2Pass`) are intentionally weak — they're
documented in `/manual` as part of the scenario. Don't rotate them on the range.

## Network gotchas

* The Modbus port (`15020`) is what makes the OT-pivot scenario work; make sure no
  upstream firewall blocks it between the attacker VLAN and the host.
  `scada-modbus.service` binds `0.0.0.0` **by design** — that's the OT attack
  surface. The blue-team fix is to bind it to `127.0.0.1` (or firewall it off the
  attacker VLAN) once the pivot has been demonstrated.
* The simulator and bridge bind `127.0.0.1` only — nothing on the control plane is
  reachable off-host.

## Verifying the deploy

From your kit box on the attacker VLAN:

```bash
curl http://<target>:18080/health                   # → {"status":"ok"}
curl http://<target>:18080/api/diag | head          # → live plant snapshot (unauth)
python3 ../../red-team/exploit.py --target <target> --mb <target> --only recon
```

A clean recon phase confirms all the intended attack surfaces are reachable.
