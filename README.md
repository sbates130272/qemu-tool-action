# qemu-tool-action

Boot QEMU guests with emulated ROCm GPUs, RDMA NICs and NVMe inside a GitHub
Actions job, run your tests in them, and have them torn down when the job
ends. It drives [`qemu-tool`](https://github.com/sbates130272/qemu-minimal)
2.x; no checkout of that repository is needed.

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: sbates130272/qemu-tool-action@v2
        with:
          copy: .
          run: |
            make
            make test
```

That one step installs `qemu-tool` from PyPI, makes `/dev/kvm` usable,
installs `oras` if the runner lacks it, pulls the published `rocm-golden`
guest image, boots the default fleet, waits until every guest answers SSH,
copies the checkout to `~/workspace` in each guest and runs the script there.
A post step collects the logs and removes the stack, whether the job passed
or not.

Each guest of the default fleet has one emulated gfx1250 GPU (rocjitsu), one
rocm-ernic RDMA NIC, one NVMe namespace and one SDXI device, with ROCm and
rdma-core installed. The guests share an L2 network, so RDMA works between
them.

## Requirements

- A Linux x86_64 runner with KVM and Docker with the compose plugin.
  GitHub's `ubuntu-latest` has both.
- About 30 GB free where the images go (`images-dir`).
- Four vCPUs per guest: below that the guest's RDMA driver does not probe.
  A standard hosted runner has four cores, which is one guest at par and two
  guests oversubscribed two to one. For two or more guests prefer a larger
  runner; `vm-count: 1` is the safe choice on a standard one.

## Operations

| `operation` | What it does |
|---|---|
| `up` (default) | Install, boot a fleet, wait, then `copy` and `run` if given. Tears down at job end. |
| `exec` | `copy` and/or `run` against the fleet an earlier `up` step booted. |
| `down` | Collect logs and tear the fleet down now, e.g. before uploading the logs. |
| `install` | Install `qemu-tool` and prepare the host, nothing else. Later steps call `qemu-tool` themselves. |
| `command` | Run one `qemu-tool` command line, given as `args`. |

## Examples

### Two guests and an RDMA test between them

The script runs on every guest at once. Each copy knows which guest it is
and where its peers are, so one script can play both roles:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 2
    wait-for: rdma
    run: |
      if [ "$QT_VM_INDEX" = 1 ]; then
        ib_send_bw -d "$QT_RDMA_DEVICE" --iters 1000
      else
        sleep 5
        ib_send_bw -d "$QT_RDMA_DEVICE" --iters 1000 "$QT_VM_1_RDMA_IP"
      fi
```

### Build once, then test in separate steps

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: fleet
  with:
    vm-count: 2
    wait-for: rdma
    copy: |
      .:src
    run: |
      meson setup build && ninja -C build

- name: Unit tests, on the first guest
  uses: sbates130272/qemu-tool-action@v2
  with:
    operation: exec
    run-on: first
    run: ninja -C build test

- name: Anything else, with plain tools
  run: |
    ssh -F "$QEMU_TOOL_SSH_CONFIG" vm2 nvme list
    qemu-tool ssh ${{ fromJSON(steps.fleet.outputs.vm-manifest)[0].name }} -- rocminfo
    qemu-tool inspect
    ansible -i "${{ steps.fleet.outputs.inventory }}" all -m ping
```

`exec` starts in the directory the first `copy` went to (`src` here).

### A different fleet

`flavor` picks a shipped fleet, `xml` layers fleet XML files of your own,
and `fleet` sets individual elements on top of either:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 1
    vcpus: 4
    vmem: 8192
    fleet: |
      vm-nvme: 2
      rocjitsu-per-vm: 2
```

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    xml: |
      ci/fleet.xml
      ci/fleet-small.xml
```

The elements are those of
[`qemu/stacks/rocm-golden.xml`](https://github.com/sbates130272/qemu-minimal/blob/main/qemu/stacks/rocm-golden.xml),
which is also the file to copy for a fleet of your own.

### Keep the logs as an artifact

The automatic teardown runs after every step, too late for an upload step.
Tear down explicitly first; the automatic one then finds nothing to do:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: down
  if: always()
  with:
    operation: down

- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: fleet-logs
    path: ${{ steps.down.outputs.logs-dir }}
```

The directory holds each container's log and each guest's `dmesg`.

### Just the tool

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    operation: install

- run: |
    qemu-tool check
    qemu-tool gen-compose --xml ci/fleet.xml --output-dir "$RUNNER_TEMP/stack"
```

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    operation: command
    args: gen-compose --xml qemu/stacks/rocm-golden.xml --check
    qemu-tool-source: ./qemu
```

## Inputs

All are optional.

| Input | Default | |
|---|---|---|
| `operation` | `up` | See the table above. |
| **Install** | | |
| `qemu-tool-version` | `>=2.1,<3` | A version, a pip specifier, or `latest`. |
| `qemu-tool-source` | | Path to the `qemu/` directory of a qemu-minimal checkout to install instead. |
| `install` | `true` | `false` uses the `qemu-tool` already on PATH. |
| `setup-host` | `true` | Open `/dev/kvm` to this user; install missing `oras`, `qemu-utils`, `ansible-core`, `git`, `openssh-client`. |
| `oras-version` | `1.3.4` | Installed when the runner's is absent or older than 1.3.4. |
| **Fleet** (`up`) | | |
| `flavor` | `default` | `default`, `mi355x` or `helios`. |
| `xml` | | Fleet XML files, one per line; later ones win. Replaces the flavor's file. |
| `vm-count` | fleet's own | Number of guests. |
| `vcpus`, `vmem` | sized from runner | Per guest; `vmem` in MiB. Either one turns `size-from-host` off. |
| `fleet` | | Other fleet elements, `element: value` per line. |
| `size-from-host` | `true` | Derive guest size from the runner (4 to 8 vCPUs, 4 to 8 GiB). |
| `metrics` | `false` | Also start Prometheus, Grafana and log collection. |
| `clean-overlays` | `true` | Fresh guest disks rather than ones an earlier job left. |
| `wait-for` | `ssh` | `ssh`, `rdma` (also wait for `PORT_ACTIVE`), or `none`. |
| `wait-timeout` | `900` | Seconds. |
| `in-network` | `auto` | Reach guests on the stack's Docker network; `auto` does so in a `container:` job. |
| `teardown` | `always` | `never` leaves the fleet running at job end. |
| `log-lines` | `40` | Lines of each container log printed at teardown. |
| `extra-args` | | Further flags for `qemu-tool quick-start up`. |
| **Paths** | | |
| `work-dir` | `/var/lib/qemu-tool` | Root of everything written; falls back to a directory under `RUNNER_TEMP`. |
| `stack-name` | `ci` | The compose project name. |
| `stack-dir`, `key-dir`, `images-dir` | under `work-dir` | Point `images-dir` at a larger disk if `/` is short. |
| **In the guests** (`up`, `exec`) | | |
| `copy` | | `source[:destination]` per line; destination relative to the guest home, default `workspace`. |
| `run` | | Bash script, run in a login shell with `set -eo pipefail`. |
| `run-on` | `all` | `all`, `first`, `last`, or guest names or indexes. |
| `working-directory` | first `copy` destination | |
| `env` | | `KEY=VALUE` per line, for the script. |
| **`command`** | | |
| `args` | | The command line, without `qemu-tool`. |

## Outputs

| Output | |
|---|---|
| `vm-manifest` | JSON array, one object per guest: `name`, `index`, `ssh_host`, `ssh_port`, `username`, `ssh_key_file`, `ssh_cmd`, `rdma_ip`, `rdma_ips`, `rdma_device`. |
| `vm-names`, `vm-count` | Guest names, space separated; and how many. |
| `ssh-config` | An ssh config naming every guest (and `vm1`, `vm2`, ...). |
| `inventory` | An Ansible inventory of the guests. |
| `key-file`, `key-dir` | The private key the guests accept, and its directory. |
| `stack-dir`, `stack-name` | The generated compose stack. |
| `images-dir`, `logs-dir` | |
| `qemu-tool-version`, `qemu-tool-path` | |

`rdma_device` is filled in only with `wait-for: rdma`.

Later steps of the job also get `qemu-tool` on PATH and these variables:
`QEMU_TOOL_SSH_CONFIG`, and `QEMU_TOOL_STACK`, which lets
`qemu-tool compose ps` (or `logs`, `down`) find the stack from any directory.

## What the script sees

| Variable | |
|---|---|
| `QT_VM_NAME`, `QT_VM_INDEX` | This guest; indexes start at 1. |
| `QT_VM_COUNT`, `QT_VM_NAMES` | The fleet. |
| `QT_RDMA_IP`, `QT_RDMA_DEVICE` | This guest's RDMA address and verbs device. |
| `QT_RDMA_IPS`, `QT_PEER_RDMA_IPS` | Every guest's address, and every other guest's. |
| `QT_VM_<n>_NAME`, `QT_VM_<n>_RDMA_IP` | Guest *n*. |

With more than one guest the script runs on all of them in parallel and each
guest's output is printed as its own group afterwards; with one, output
streams live. The step fails if the script fails on any guest.

## Container jobs

In a `container:` job the stack's containers are siblings of the job
container, not children, so three things have to be shared with the host:

```yaml
container:
  image: your/image
  options: >-
    --device /dev/kvm
    --pid=host
    -v /var/run/docker.sock:/var/run/docker.sock
    -v /var/lib/qemu-tool:/var/lib/qemu-tool
```

`/var/lib/qemu-tool` must be mounted at that same path, because the Docker
daemon resolves the stack's bind mounts on the host. The action detects the
container, joins it to the stack's network and reaches the guests there.

## Security

The guest image is public, and so are the key and password that log in to
it. The fleet publishes its ports on `127.0.0.1` only; do not change
`publish-bind-addr` on a runner other machines can reach.

## Migrating from v1

v1 wrapped individual `qemu-tool` 1.x subcommands and loaded env-files.
`qemu-tool` 2.0 replaced env-files with fleet XML and renamed the stacks, so
v2 is a different action rather than an update:

| v1 | v2 |
|---|---|
| `operation: gen-vm` / `run-vm` / `gen-compose` / `compose` with per-flag inputs | `operation: command` with `args`, or `operation: install` and a `run:` step |
| `env-file` | `xml` (fleet XML) and `fleet` |
| `stack`, `compose-profiles`, `compose-args` | `flavor` / `xml`; `metrics: true` for the metrics profile |
| `wait-for-healthy` | `wait-for: ssh` (default) or `rdma` |
| `vm-manifest` output | Same name; entries gain host, user, key and RDMA fields, and `image_path` is gone |

`@v1` still works with `qemu-tool` 1.x; pin `qemu-tool-version` there.

## Versioning

`v2.0.0` (exact), `v2.0` (floating minor), `v2` (floating major). The
release workflow moves the floating tags on every release.

## Development

```bash
python3 -m unittest discover -s tests   # no runner, no guest
tests/smoke.sh                          # installs qemu-tool into a scratch dir
```

The action is `action.yml`, three dependency-free JavaScript shims, and
`qemu_tool_action.py`, which is standard-library Python.

## License

MIT
