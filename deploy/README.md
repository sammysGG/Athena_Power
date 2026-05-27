# Deploy the SCADA range

Ansible playbook that brings a fresh Debian/Ubuntu host up as a fully working
range target: source, a Python venv, four **systemd** services, a rendered
`.env`, and the HMI + Modbus ports exposed on the range VLAN. No Docker.

The four units are:

| Unit | Role | Bind |
|---|---|---|
| `scada-simulator.service` | plant simulator (FastAPI) | `127.0.0.1:9101` |
| `scada-modbus.service` | Modbus TCP slave (OT attack surface) | `0.0.0.0:15020` |
| `scada-bridge.service` | protocol bridge / historian | `127.0.0.1:9102` |
| `scada-hmi.service` | operator HMI (FastAPI) | `{{ hmi_bind }}:{{ hmi_port }}`, fronted by nginx TLS on 443 |

## What the target needs

| Requirement | Why |
|---|---|
| Debian 11/12 or Ubuntu 22.04/24.04 | Recent enough systemd (`Type=exec`) and `python3-venv`. |
| Python 3 (`python3`) | Ansible needs it. One-shot bootstrap below if it's missing. |
| SSH key access for an account with sudo | The playbook uses `become: true`. |
| Outbound internet during deploy (or a local PyPI mirror) | First run `pip install`s each service's requirements into the venv. See [Air-gap](#air-gap-deploy). |
| Host firewall ports | `443/tcp` (HMI via nginx), `18080/tcp` (HMI direct, optional), `15020/tcp` (Modbus). The playbook can open them via `ufw` — see `range_open_firewall` in `group_vars/all.yml`. |

You do **not** need: a domain, a public CA, a Cloudflare tunnel, or any external
reverse proxy. The range is meant to be reached directly by IP from the attacker
subnet — the nginx TLS cert is self-signed.

## What you need on your control box

```
pip install ansible
ansible-galaxy collection install ansible.posix community.general
```

## First deploy

```bash
cd deploy/ansible
cp inventory.ini.example inventory.ini
$EDITOR inventory.ini          # set the target IP and SSH user
$EDITOR group_vars/all.yml     # set bridge_api_key, simulator_api_key (or use --extra-vars)

ansible-playbook -i inventory.ini playbook.yml
```

The playbook prints the access details at the end:

```
HMI:    https://<target>           (nginx TLS -> 127.0.0.1:18080)
Manual: https://<target>/manual
Modbus: <target>:15020             (HR20 writable)
Login:  admin / Cool2Pass
```

## Bootstrap a target that has no python yet

```bash
ansible -i inventory.ini all -m raw -a 'apt-get update && apt-get install -y python3' -b
```

Then run the playbook normally.

## Re-deploy after editing source

`synchronize` ships changes incrementally; the playbook reinstalls any changed
dependencies and restarts the four units:

```bash
ansible-playbook -i inventory.ini playbook.yml
```

## Managing the services on the target

```bash
systemctl status scada-hmi.service          # any of the four scada-* units
systemctl restart scada-bridge.service
journalctl -u scada-simulator.service -f    # live logs
```

## Secrets handling

The two inter-service keys (`bridge_api_key`, `simulator_api_key`) protect the
internal channels. Bake them into a vault file rather than checking the cleartext
into git:

```bash
ansible-vault create group_vars/all.vault.yml
# put bridge_api_key / simulator_api_key inside
ansible-playbook -i inventory.ini playbook.yml --ask-vault-pass
```

The HMI credentials (`admin` / `Cool2Pass`) are intentionally weak — they're
documented in `/manual` as part of the scenario. Don't vault them.

## Air-gap deploy

If the range network has no internet at all, pre-stage the Python wheels on a
connected box and copy them over:

```bash
# on a connected box (same OS/arch as the target)
pip download -d wheels \
  -r ../../services/simulator/requirements.txt \
  -r ../../services/modbus-server/requirements.txt \
  -r ../../services/bridge/requirements.txt \
  -r ../../services/hmi/requirements.txt
```

Copy `wheels/` to the target and point pip at it (e.g. set
`PIP_FIND_LINKS=/path/to/wheels` and `PIP_NO_INDEX=1` in the environment, or
pre-create `{{ scada_dir }}/.venv` from the offline wheels). A local PyPI mirror
works the same way.

## Network gotchas

* The Modbus port (`15020`) is what makes the OT-pivot scenario work. Make
  sure no upstream firewall blocks it between the attacker VLAN and the host.
  `scada-modbus.service` binds `0.0.0.0` **by design** — that's the OT attack
  surface. The blue-team fix is to bind it to `127.0.0.1` (or firewall it off
  the attacker VLAN) once the pivot has been demonstrated.
* The simulator and bridge bind `127.0.0.1` only — nothing on the control plane
  is reachable off-host.

## Verifying the deploy

From your kit box on the attacker VLAN:

```bash
curl http://<target>:18080/health                   # → {"status":"ok"}
curl http://<target>:18080/api/diag | head          # → live plant snapshot (unauth)
python3 ../../red-team/exploit.py --target <target> --mb <target> --only recon
```

A clean recon phase confirms all the intended attack surfaces are reachable.
