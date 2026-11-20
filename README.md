# qemu-tool-action

Boot QEMU guests with an emulated ROCm GPU, an RDMA NIC and NVMe inside a
GitHub Actions job, run your tests in them, and have them torn down when the
job ends. It drives [`qemu-tool`](https://github.com/sbates130272/qemu-minimal)
2.x; no checkout of that repository is needed.

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v6

      - uses: sbates130272/qemu-tool-action@v2
        with:
          vm-count: 1
          copy: .
          run: |
            make
            make test
```

That one step:

1. installs `qemu-tool` from PyPI, makes `/dev/kvm` usable and installs
   `oras` if the runner lacks it;
2. pulls the published `rocm-golden` guest image and boots the fleet;
3. waits until every guest answers SSH, then loads the GPU driver in each;
4. copies the checkout to `~/workspace` in each guest and runs the script
   there.

A post step collects the logs and removes the stack, whether the job passed
or not.

## Requirements

- A Linux x86_64 runner with KVM, and Docker with the compose plugin.
  GitHub's `ubuntu-latest` has both.
- `python3` 3.10 or newer on PATH, with the `venv` module. Hosted runners
  have it; a bare `container:` image such as `ubuntu:24.04` does not.
- Passwordless `sudo`, or root, to open `/dev/kvm`, install missing
  packages and create `/var/lib/qemu-tool`. Set `setup-host: false` on a
  runner you have prepared yourself.
- A runner that can run `node24` actions. Hosted runners can.
- About 30 GB free where the images go (`images-dir`).
- Four vCPUs per guest: below that the guest's RDMA driver does not probe.
  A standard hosted runner has four cores, which is one guest at par and two
  guests oversubscribed two to one. Use `vm-count: 1` on a standard runner
  and a larger runner for two or more.

## What is in the guest

Each guest of the default fleet has one emulated gfx1250 GPU (served by
rocjitsu), one rocm-ernic RDMA NIC, one NVMe namespace and one SDXI device.
The guests share an L2 network, so RDMA works between them.

The guest is Ubuntu with a mainline 7.2 kernel on a 64 GiB virtual disk,
logged in as `ubuntu` with passwordless `sudo` and outbound network access.
Installed: build-essential, cmake, ninja-build, pkg-config, the autotools,
git, python3-venv, pipx; rdma-core with its headers and libraries under
`/usr/local`; perftest (`ib_send_bw` and friends); nvme-cli, fio, pciutils;
and the ROCm 10.2 runtime, hipFile and BLAS development packages.
`rocminfo` is not installed.

Install anything else in `run`. Guests start from a fresh disk on every job,
so nothing persists:

```yaml
run: |
  sudo apt-get update && sudo apt-get install -y meson libgtest-dev
```

Do not `apt install libibverbs-dev`: the archive's headers would shadow the
rdma-core the image was built with.

**The GPU.** A freshly booted guest has the GPU on its PCI bus and nothing
more: `amdgpu` is blacklisted, and the one firmware file it needs is not in
the image because it belongs to the rocjitsu build serving the device. The
action supplies both (`gpu: true`, the default): it generates the firmware
from the stack's own rocjitsu image, installs it in each guest and runs the
image's `amdgpu-probe`, then waits for a GPU node in the KFD topology.
`amdgpu-probe --status` in a guest prints which state it is in, and the
`gpu` field of `vm-manifest` records it (`loaded` when usable).

## Operations

| `operation` | What it does |
| --- | --- |
| `up` (default) | Install, boot a fleet, wait, bring up the GPU, then `copy`, `run` and `fetch` if given. Tears down at job end. |
| `exec` | `copy`, `run` and `fetch` against the fleet an earlier `up` step booted. |
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
        sleep 10
        ib_send_bw -d "$QT_RDMA_DEVICE" --iters 1000 "$QT_VM_1_RDMA_IP"
      fi
```

`QT_RDMA_DEVICE` is set only with `wait-for: rdma`. Let an RDMA test run to
completion, or bound it with `--iters`: killing one part way can leave the
guest's RDMA device unusable until the fleet is restarted.

### Build once, test in separate steps, keep the results

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: fleet
  with:
    vm-count: 2
    wait-for: rdma
    copy: |
      .:src
    run: |
      cmake -S . -B build -G Ninja && ninja -C build

- name: Unit tests, on the first guest
  uses: sbates130272/qemu-tool-action@v2
  id: tests
  with:
    operation: exec
    run-on: first
    run: ctest --test-dir build --output-junit results/junit.xml
    fetch: |
      src/build/results

- uses: actions/upload-artifact@v6
  if: always() && steps.tests.outputs.fetch-dir != ''
  with:
    name: test-results
    path: ${{ steps.tests.outputs.fetch-dir }}

- name: Anything else, with plain tools
  run: |
    ssh -F "$QEMU_TOOL_SSH_CONFIG" vm2 sudo nvme list
    qemu-tool ssh ${{ fromJSON(steps.fleet.outputs.vm-manifest)[0].name }} -- ibv_devinfo -l
    qemu-tool inspect
    ansible -i "${{ steps.fleet.outputs.inventory }}" all -m ping
```

`exec` starts in the directory the first `copy` went to (`src` here).
`fetch` paths are relative to the guest's home, not to that directory.
`fetch` runs whether the script passed or not, and puts each guest's copy
in `<fetch-dir>/<guest-name>/`.

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

Guests are sized from the runner unless you size them: `vcpus` or `vmem`,
or `vm-vcpus` or `vm-vmem` in `fleet`, turn that off. Sizes written inside
an `xml` file need `size-from-host: false` to take effect.

### Keep the logs as an artifact

The automatic teardown runs after every step, too late for an upload step.
Tear down explicitly first; the automatic one then finds nothing to do:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: down
  if: always() && steps.fleet.outputs.stack-dir != ''
  with:
    operation: down

- uses: actions/upload-artifact@v6
  if: always() && steps.down.outputs.logs-dir != ''
  with:
    name: fleet-logs
    path: ${{ steps.down.outputs.logs-dir }}
    if-no-files-found: ignore
```

`fleet` is the `id` of the `up` step. The directory holds each container's
log and the end of each guest's `dmesg`.

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
| --- | --- | --- |
| `operation` | `up` | See the table above. |
| **Install** | | |
| `qemu-tool-version` | `>=2.1,<3` | A version, a pip specifier, or `latest`. See the note below. |
| `qemu-tool-source` | | Path to the `qemu/` directory of a qemu-minimal checkout to install instead. |
| `install` | `true` | `false` uses the `qemu-tool` already on PATH. |
| `setup-host` | `true` | Open `/dev/kvm` to this user; install missing `oras`, `qemu-utils`, `zstd`, `ansible-core`, `git`, `openssh-client`. |
| `oras-version` | `1.3.4` | Installed when the runner's is absent or older than 1.3.4. |
| **Fleet** (`up`) | | |
| `flavor` | `default` | `default`, `mi355x` or `helios`. |
| `xml` | | Fleet XML files, one per line; later ones win. Replaces the flavor's file. |
| `vm-count` | fleet's own | Number of guests; 2 for the default flavor. |
| `vcpus`, `vmem` | sized from runner | Per guest; `vmem` in MiB. Either one turns `size-from-host` off. |
| `fleet` | | Other fleet elements, `element: value` per line. |
| `size-from-host` | `true` | Derive guest size from the runner (4 to 8 vCPUs, 4 to 8 GiB). |
| `metrics` | `false` | Also start Prometheus, Grafana and log collection. |
| `clean-overlays` | `true` | Fresh guest disks rather than ones an earlier job left. |
| `wait-for` | `ssh` | `ssh`, `rdma` (also wait for `PORT_ACTIVE`), or `none`. |
| `wait-timeout` | `900` | Seconds. |
| `gpu` | `true` | Stage the GPU firmware and load `amdgpu` in each guest. Skipped with `wait-for: none`. |
| `gpu-generation` | `gfx1250` | GPU generation the firmware is generated for. |
| `in-network` | `auto` | Reach guests on the stack's Docker network; `auto` does so in a `container:` job. |
| `teardown` | `always` | `never` leaves the fleet running at job end. |
| `log-lines` | `40` | Lines of each container log printed at teardown. |
| `extra-args` | | Further flags for `qemu-tool quick-start up`. |
| **Paths** | | |
| `work-dir` | `/var/lib/qemu-tool` | Root of everything written; falls back to a directory under `RUNNER_TEMP`. |
| `stack-name` | `ci` | The compose project name. Ignored when `stack-dir` is given, whose last component is used instead. For `exec` and `down`: which fleet, when a job boots more than one. |
| `stack-dir` | `<work-dir>/stacks/<stack-name>` | Where the stack is generated. Also names the fleet for `exec` and `down`. |
| `key-dir`, `images-dir` | under `work-dir` | Point `images-dir` at a larger disk if `/` is short. |
| **In the guests** (`up`, `exec`) | | |
| `copy` | | `source[:destination]` per line. The destination is relative to the guest home; the default is `workspace` for a directory and the home itself for a file. The whole tree is sent, `.git` included. |
| `run` | | Bash script, run in a login shell with `set -eo pipefail`. |
| `fetch` | | `guest-path[:local-directory]` per line, copied out after the script whether it passed or not. Relative guest paths are relative to the guest home. |
| `run-on` | `all` | Which guests `copy`, `run` and `fetch` apply to: `all`, `first`, `last`, or guest names or indexes. |
| `working-directory` | first `copy` destination | Remembered for later `exec` steps; else the guest home. |
| `env` | | `KEY=VALUE` per line, for the script. |
| **`command`** | | |
| `args` | | The command line, without `qemu-tool`. |

`qemu-tool-version` pins the `qemu-tool` on the runner, which generates and
drives the stack. Each guest's container installs the latest `qemu-tool`
from PyPI when it starts, and needs network access to do so, unless
`qemu-tool-source` names the `qemu/` directory of a full qemu-minimal
checkout, in which case that checkout is used there too.

## Outputs

| Output | |
| --- | --- |
| `vm-manifest` | JSON array, one object per guest: `name`, `index`, `ssh_host`, `ssh_port`, `username`, `ssh_key_file`, `ssh_cmd`, `rdma_ip`, `rdma_ips`, `rdma_device`, `gpu`. |
| `vm-names`, `vm-count` | Guest names, space separated; and how many. |
| `ssh-config` | An ssh config naming every guest (and `vm1`, `vm2`, ...). |
| `inventory` | An Ansible inventory of the guests. |
| `key-file`, `key-dir` | The private key the guests accept, and its directory. |
| `stack-dir`, `stack-name` | The generated compose stack. |
| `fleet-xml` | The fleet XML layers the action passed to `quick-start`, one path per line. Empty when the flavor's shipped file is used unchanged. The overlays `quick-start` adds itself (host sizing, the images directory) are in `stack-dir` and not listed. |
| `images-dir`, `logs-dir`, `fetch-dir` | |
| `qemu-tool-version`, `qemu-tool-path` | |

`up` sets all of them. `exec` sets `fetch-dir`, `down` sets `logs-dir`, and
`install` and `command` set the two `qemu-tool-*` outputs. `rdma_device` is
filled in only with `wait-for: rdma`.

Later steps of the job also get `qemu-tool` and `oras` on PATH (nothing
else is added to it) and two variables: `QEMU_TOOL_SSH_CONFIG`, and
`QEMU_TOOL_STACK`, which lets `qemu-tool compose ps` (or `logs`) find the
stack from any directory.

## What the script sees

| Variable | |
| --- | --- |
| `QT_VM_NAME`, `QT_VM_INDEX` | This guest; indexes start at 1. |
| `QT_VM_COUNT`, `QT_VM_NAMES` | The fleet. |
| `QT_RDMA_IP` | This guest's RDMA address. |
| `QT_RDMA_DEVICE` | Its verbs device; empty unless the fleet was booted with `wait-for: rdma`. |
| `QT_RDMA_IPS`, `QT_PEER_RDMA_IPS` | Every guest's address, and every other guest's. |
| `QT_VM_<n>_NAME`, `QT_VM_<n>_RDMA_IP` | Guest *n*. |

With one guest the output streams live. With more, the script runs on all
of them in parallel and each guest's output is printed as its own group
once every guest has finished, so a script that hangs on one guest shows
nothing until the job times out: wrap long commands in `timeout`. The step
fails if the script fails on any guest.

## Time and disk

The long step is the pull: the guest image is about 2 GB to download and
unpacks to about 8.4 GB, once per job however many guests there are, plus
the container images of QEMU and the device servers. Each guest then adds
an overlay that grows as it is written and one file per NVMe namespace. Set
`timeout-minutes` on the job; qemu-tool's own one-guest boot check allows
45. On a hosted runner the image is pulled on every job. On a self-hosted
runner `images-dir` persists and the backing image is reused.

## Limits

- One fleet per runner at a time. Concurrent fleets on one self-hosted
  runner need at least distinct `stack-name` and `images-dir`, and that is
  untested.
- Fleets do not survive the job: `exec` and `down` only see a fleet booted
  earlier in the same job.
- A single RDMA READ of 16 MiB or more drops the emulated NIC's mesh
  connection; keep transfers below that.
- The emulation is functional, not performance-representative.

## Container jobs

In a `container:` job the stack's containers are siblings of the job
container, not children. The image needs `python3` 3.10 or newer, `tar`,
and the `docker` CLI with the compose plugin. The action installs `oras`
itself, and on a Debian or Ubuntu image running as root also `qemu-utils`,
`zstd`, `ansible-core`, `git` and `openssh-client`. Four options are needed:

```yaml
container:
  image: your/image
  options: >-
    --device /dev/kvm
    --pid=host
    -v /var/run/docker.sock:/var/run/docker.sock
    -v /var/lib/qemu-tool:/var/lib/qemu-tool
```

`--device /dev/kvm` gives the guests KVM; the Docker socket lets the action
start the sibling containers; `--pid=host` lets `qemu-tool` (`inspect`,
`list`) see the QEMU processes running in them; and `/var/lib/qemu-tool`
must be mounted at that same path, because the Docker daemon resolves the
stack's bind mounts on the host. `up` refuses to start without that mount.

The action detects the container, joins it to the stack's network and
reaches the guests there. Use `ssh -F "$QEMU_TOOL_SSH_CONFIG" vm1 ...` or
`operation: exec` in later steps: `qemu-tool ssh` targets the host's
published port, which the job container cannot reach.

## Troubleshooting

Every container's full log and the last 500 lines of each reachable
guest's `dmesg` are written to `logs-dir` at teardown, and the last
`log-lines` lines of each container log are printed. Upload the directory
(see above) before asking why a boot failed.

| Symptom | Cause and fix |
| --- | --- |
| `/dev/kvm does not exist on this runner` | The runner has no nested virtualisation. Use a GitHub-hosted Linux x64 runner or a self-hosted one with KVM; in a container job pass `--device /dev/kvm`. |
| `<vm> never answered SSH` | The guest did not boot inside `wait-timeout`. A verbose SSH attempt and the end of the `qemu-<n>` container log are printed above the error. On an oversubscribed runner lower `vm-count`. |
| `<vm> has no RDMA device with an active port` | The guest has fewer than 4 vCPUs (`vcpus`), or the NIC server died; see the `ernic-<n>` log. |
| A guest dies mid-job with `vfio-user: timed out waiting for reply` | More vCPUs were allocated than the runner has cores, and the NIC mesh evicted a live node. Use `vm-count: 1` or a larger runner. |
| `<vm>: amdgpu did not bind the emulated GPU` | The lines above it carry `amdgpu-probe --status` and the guest's `amdgpu` kernel messages. `gpu: false` boots without the GPU step. |
| A warning that the images directory has less than about 30 GB free | Point `images-dir` at a larger disk. |
| `no fleet from an earlier operation: up step was found` | `exec` or `down` ran in a different job from `up`, or `up` failed before it recorded anything. Fleets do not cross jobs. |
| `/var/lib/qemu-tool is not bind-mounted into this job container` | Add the mount shown under Container jobs. |
| `could not join the stack network` | The job container cannot reach the Docker daemon, or its hostname is not its container id. Mount `/var/run/docker.sock`. |
| `qemu-tool X is a 1.x release` | This major version drives `qemu-tool` 2.x. Leave `qemu-tool-version` at its default, or use `@v1` for 1.x. |

## Security

The guest image is public, and so are the key and password that log in to
it. The fleet publishes its ports on `127.0.0.1` only; do not change
`publish-bind-addr` on a runner other machines can reach.

## Migrating from v1

v1 wrapped individual `qemu-tool` 1.x subcommands and loaded env-files.
`qemu-tool` 2.0 replaced env-files with fleet XML and renamed the stacks, so
v2 is a different action rather than an update:

| v1 | v2 |
| --- | --- |
| `operation: gen-vm` / `run-vm` / `gen-compose` / `compose` with per-flag inputs | `operation: command` with `args`, or `operation: install` and a `run:` step |
| `env-file` | `xml` (fleet XML) and `fleet` |
| `stack`, `compose-profiles`, `compose-args` | `flavor` / `xml`; `metrics: true` for the metrics profile |
| `wait-for-healthy` | `wait-for: ssh` (default) or `rdma` |
| `vm-manifest` output | Same name; entries gain host, user, key, RDMA and GPU fields, and `image_path` is gone |
| `vcpus`, `vmem`, `extra-args`, `wait-timeout` | Same names, new meaning: the size of every guest, flags for `quick-start up`, and the wait for the guests (default 900 s, was 120) |

A workflow that stays on `@v1` must now set `qemu-tool-version` to a 1.x
release: v1 installs the latest `qemu-tool` by default, which is 2.x, and
v1's command lines no longer match it.

## Versioning

Tags are `vX.Y.Z` (exact), `vX.Y` (floating minor) and `vX` (floating
major); `@v2` follows the newest 2.x release. Changes are listed in
[CHANGELOG.md](https://github.com/sbates130272/qemu-tool-action/blob/main/CHANGELOG.md);
how a release is cut is in
[RELEASING.md](https://github.com/sbates130272/qemu-tool-action/blob/main/RELEASING.md).

## Development

```bash
python3 -m unittest discover -s tests   # no runner, no guest
tests/smoke.sh                          # installs qemu-tool into a scratch dir
tests/test_release.sh                   # the release workflow's tag logic
```

The action is `action.yml`, three dependency-free JavaScript shims, and
`qemu_tool_action.py`, which is standard-library Python.

## License

[MIT](https://github.com/sbates130272/qemu-tool-action/blob/main/LICENSE)
