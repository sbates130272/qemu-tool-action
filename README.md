# qemu-tool-action

Boot a fleet of QEMU guests inside a GitHub Actions job, each with emulated
ROCm GPUs, RDMA NICs and NVMe, run your tests in them, and have them torn
down when the job ends. The fleet is described by fleet XML: one that
[`qemu-tool`](https://github.com/sbates130272/qemu-minimal) ships, or your
own.

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v7

      - uses: sbates130272/qemu-tool-action@v2
        with:
          vm-count: 1
          copy: .
          run: |
            make
            make test
```

That one step:

1. installs `qemu-tool`, makes `/dev/kvm` usable and installs `oras` if the
   runner lacks it;
2. checks the fleet XML, pulls the guest image it names and boots the fleet;
3. waits until every guest answers SSH, then loads the GPU driver in each;
4. copies the checkout to `~/workspace` in each guest and runs the script
   there.

A post step collects the logs and removes the stack, whether the job passed
or not. With no fleet named, as above, it is qemu-tool's default one.

## Requirements

- A Linux x86_64 runner with KVM, and Docker with the compose plugin.
  GitHub's `ubuntu-latest` has both.
- `python3` 3.10 or newer on PATH, with the `venv` module. Hosted runners
  have it; a bare `container:` image such as `ubuntu:24.04` does not.
- Passwordless `sudo`, or root, to open `/dev/kvm`, install missing
  packages and create `/var/lib/qemu-tool`. Set `setup-host: false` on a
  runner you have prepared yourself.
- A runner that can run `node24` actions. Hosted runners can.
- Disk for the guest image; about 30 GB free for the default fleet
  (`images-dir`).
- Enough cores. The emulated RDMA NIC's driver does not probe in a guest
  with fewer than four vCPUs, and a standard hosted runner has four cores:
  one guest at par, two guests oversubscribed two to one. Use `vm-count: 1`
  on a standard runner and a larger runner for two or more.

## Which fleet

| You give | The fleet is |
| --- | --- |
| nothing | qemu-tool's default fleet |
| `flavor: <name>` | another fleet qemu-tool ships |
| `xml: <files>` | your own |

On top of whichever it is, in this order: the files of `xml-overlay`, then
`vm-count`, `vcpus`, `vmem` and the `fleet` lines. A later layer overrides
the elements it sets and leaves the rest.

```yaml
# The default fleet, with two NVMe namespaces and two GPUs per guest.
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 1
    fleet: |
      vm-nvme: 2
      rocjitsu-per-vm: 2
```

```yaml
# A shipped flavor, changed by an overlay file kept in your repository.
- uses: sbates130272/qemu-tool-action@v2
  with:
    flavor: helios
    xml-overlay: ci/helios-small.xml
```

```yaml
# A fleet of your own.
- uses: sbates130272/qemu-tool-action@v2
  with:
    xml: ci/fleet.xml
```

### The shipped fleets

`flavor` takes the name of any fleet the installed qemu-tool ships;
`qemu-tool quick-start --help` lists them. qemu-tool 3.0.0 has:

| `flavor` | Guests | Each guest has |
| --- | --- | --- |
| `default` | 2 | one gfx1250 GPU, one rocm-ernic RDMA NIC, one NVMe namespace, one SDXI device |
| `mi355x` | 4 | 8 GPUs, 8 NICs and 8 PCIe switches |
| `helios` | 1 | 4 GPUs, 8 NICs, one SDXI device |
| `xio` | 1 | 2 GPUs, 2 NICs, an NVMe namespace and an SPDK NVMe controller; not in 2.1.0 |

A flavor added after the qemu-tool you install is refused, with the names
that version does have. This action boots a fleet and prepares its GPUs; it
does not run the further guest setup some flavors have a playbook for, as
`xio` has for rocm-xio. Its CI boots the `default` flavor and a fleet of
its own; the other three are rendered there, not booted.

### A fleet of your own

Start from
[`examples/fleet-minimal.xml`](https://github.com/sbates130272/qemu-tool-action/blob/main/examples/fleet-minimal.xml),
which is as small as a fleet gets, or from the fleet qemu-tool ships,
[`qemu/stacks/rocm-golden.xml`](https://github.com/sbates130272/qemu-minimal/blob/main/qemu/stacks/rocm-golden.xml),
which documents every element it uses. What qemu-tool requires of one:

- **The container images:** `<qemu-ref>`, `<ernic-ref>` and
  `<rocjitsu-ref>`. They are one build and have to stay one.
- **A guest image:** `<backing-ref>`, pulled with `oras`, on a tag that does
  not move. `latest` is refused.
- **At least one NIC and one GPU per guest.** SDXI, NVMe, more NICs and
  GPUs, and PCIe switches are optional; a guest with no NIC, or no GPU, is
  not something qemu-tool renders.

Before anything is pulled the action has qemu-tool read and render the
fleet, so its refusals arrive in the first minute, in its own words:

```text
Error: fleet.xml: unknown element <vm-cpus>
Error: <backing-ref> names a tag that moves
Error: <ernic-per-vm> must be at least 1, got 0
```

**A fleet XML is tied to the qemu-tool that reads it.** Elements are added
in new releases, and a version that does not know one refuses the file
(`unknown element`) instead of ignoring it. An older file still reads on a
newer qemu-tool of the same major version; qemu-tool 3 removed ten
elements, listed under
[From qemu-tool 2.x to 3.x](#from-qemu-tool-2x-to-3x). So when your XML
uses something new, or something 3.x no longer has, say which qemu-tool
you need; see the next section.

The `fleet` input is flat `element: value` lines. Anything with structure,
such as the `<devices>` block that gives each NIC or GPU of a guest its own
settings (newer than qemu-tool 2.1.0), goes in a file.

What the action does for the guests depends on what the fleet and its guest
image provide:

| Feature | Needs |
| --- | --- |
| The GPU step (`gpu`) | `amdgpu-probe` in the guest image, as images baked by qemu-minimal have. A guest without it is left as booted, and the log says so; with `gpu: required` it fails the step. |
| `wait-for: rdma`, `QT_RDMA_DEVICE` | rdma-core and the NIC's driver in the guest image, and four vCPUs. |
| `QT_RDMA_IP` and the peers' | A guest image that addresses its NIC at boot, as qemu-minimal's do. The address is the one qemu-tool assigns the guest's NIC on the mesh. |
| Logging in | A guest image published with `qemu-tool publish-qcow`, which carries its own key. For any other, `ssh-key-file` and `ssh-user`. |

A guest image of your own that was not published that way: give
`ssh-key-file` (a private key the image accepts) and `ssh-user`. To boot
images already in `images-dir`, with no `<backing-ref>` at all, also set
`clean-overlays: false`. Neither path is exercised by this action's own CI.

A fleet of your own gets the default fleet's Grafana dashboards and log
collection when `metrics: true`.

## Which qemu-tool

One of three inputs says. `qemu-tool-ref` cannot be combined with the
other two; `qemu-tool-source` overrides `qemu-tool-version`.

| Input | Installs |
| --- | --- |
| `qemu-tool-version` | A release from PyPI: `2.1.0`, a pip specifier such as `~=2.1`, or `latest`. |
| `qemu-tool-ref` | qemu-minimal at a tag, a branch or a full commit SHA: `v2.1.0`, `main`, `fddac6f2c69e...`. |
| `qemu-tool-source` | The `qemu/` directory of a checkout already on the runner. |

With none, it is the newest 2.x or 3.x release (`>=2.1,<4`).

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    qemu-tool-version: 2.1.0      # a release, exactly
```

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    qemu-tool-ref: v2.1.0         # the same release, from its tag
```

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    qemu-tool-ref: fddac6f2c69e0782228ead604f03d244ec065443   # a commit
    xml: ci/fleet.xml              # pinned to exactly that commit
```

Which to use:

- **A release, pinned** (`qemu-tool-version: 2.1.0`), for a job that should
  not change until you change it. The fleet a release ships pins its
  container images and guest image too, so this pins the whole stack.
- **The default range**, to follow releases without touching your
  workflow. A new release can change the default fleet's images, and the
  range crosses a major version: a job on the default now gets qemu-tool
  3.x, where it got 2.1.0 before this action's 2.2.0, and a fleet XML of
  yours that sets an element 3.x removed stops being read. See
  [From qemu-tool 2.x to 3.x](#from-qemu-tool-2x-to-3x); `~=2.1` stays
  on 2.x.
- **A ref**, for a fleet XML element or a flavor that is not in a release
  yet, or to pin to a commit. A branch name moves; a tag or a SHA does not.
  `qemu-tool-repository` names a fork. qemu-tool reports the version of the
  last release it was cut from, so the `qemu-tool-sha` output is what
  identifies a ref install.

**The stack's containers run the same qemu-tool as the runner.** Each QEMU
container runs `qemu-tool run-vm`, and left alone it installs the latest
release from PyPI when it starts, whatever the runner has: a compose file
rendered by one version and run by another. The action places the runner's
own installed qemu-tool where the containers look for a source tree, so
they build that. `container-qemu-tool: pypi` restores qemu-tool's own
behaviour. The containers still need network access to PyPI at start, for
`setuptools` and qemu-tool's one dependency.

## The default fleet's guests

This section describes the guest image the shipped fleets name. A fleet of
your own has whatever its `<backing-ref>` has.

Ubuntu with a mainline 7.2 kernel on a 64 GiB virtual disk, logged in as
`ubuntu` with passwordless `sudo` and outbound network access. Installed:
build-essential, cmake, ninja-build, pkg-config, the autotools, git,
python3-venv, pipx; rdma-core with its headers and libraries under
`/usr/local`; perftest (`ib_send_bw` and friends); nvme-cli, fio, pciutils;
and the ROCm 10.2 runtime, hipFile and BLAS development packages.
`rocminfo` is not installed. The guests share an L2 network, so RDMA works
between them.

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
from the stack's own rocjitsu image, for the generation the fleet's
`<rocjitsu-config>` names, installs it in each guest and runs the image's
`amdgpu-probe`, then waits for a GPU node in the KFD topology.
`amdgpu-probe --status` in a guest prints which state it is in, and the
`gpu` field of `vm-manifest` and the step summary record it for every
guest (`loaded` when usable), also when the step fails.

What the step does with a guest whose GPU is not usable is the `gpu` input:

| `gpu` | A guest with no GPU (`absent`), or no `amdgpu-probe` (`unknown`) | A GPU that cannot be bound (`unbound`, `stranded`) |
| --- | --- | --- |
| `true` (default) | left alone, not a failure | fails the step |
| `required` | fails the step | fails the step |
| `optional` | left alone | a warning; the step goes on |
| `false` | not looked at | not looked at |

Use `required` when the job is about the GPU: the step then fails unless
every guest ends as `loaded`, and the error names each guest that did not
and the status it was left in. Use `optional` when a job should run with
or without one and reads `vm-manifest` to find out which.

## Operations

| `operation` | What it does |
| --- | --- |
| `up` (default) | Install, check and boot a fleet, wait, bring up the GPU, then `copy`, `run` and `fetch` if given. Tears down at job end. |
| `exec` | `copy`, `run` and `fetch` against the fleet an earlier `up` step booted. |
| `down` | Collect logs and tear the fleet down now, e.g. before uploading the logs. |
| `install` | Install `qemu-tool` and prepare the host, nothing else. Later steps call `qemu-tool` themselves. |
| `command` | Run one `qemu-tool` command line, given as `args`. |

## Examples

### Two guests and an RDMA test between them

The script runs on every guest at once. Each copy knows which guest it is
and where its peers are, so one script can play both roles. Two guests want
a runner with at least eight cores.

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 2
    wait-for: rdma
    run: |
      if [ "$QT_VM_INDEX" = 1 ]; then
        ib_send_bw -F -d "$QT_RDMA_DEVICE" --iters 1000
      else
        sleep 10
        ib_send_bw -F -d "$QT_RDMA_DEVICE" --iters 1000 "$QT_VM_1_RDMA_IP"
      fi
```

`QT_RDMA_DEVICE` is set only with `wait-for: rdma`. Let an RDMA test run to
completion, or bound it with `--iters`: killing one part way can leave the
guest's RDMA device unusable until the fleet is restarted. `-F` keeps
perftest from failing a run that moved every byte, on a CPU-frequency
reading that is noisy on a busy runner.

### Your own names for the guests

The `ssh-config` output names each guest by its own name and by `vm1`,
`vm2`, and so on. `ssh-aliases` adds names of yours. A word containing
`{index}` names every guest; any other word names one guest, by the line
it is on. Only a line with such a word counts: blank lines, comments and
lines of `{index}` words alone are no guest's line.

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 2
    ssh-aliases: guest guest{index}

- run: |
    ssh -F "$QEMU_TOOL_SSH_CONFIG" guest uname -r    # the first guest
    ssh -F "$QEMU_TOOL_SSH_CONFIG" guest2 uname -r   # the second
```

That gives the first guest `guest` and `guest1` and the second `guest2`.
A list works too, one line per guest:

```yaml
    ssh-aliases: |
      server
      client
```

An alias is letters, digits, `.`, `_` and `-`, and names one guest. A name
for a guest the fleet does not have, one two guests would share, or one
that is another guest's own name is refused before anything is booted.
`vm<n>` stays beside your names, unless you give that very word to another
guest. Each guest's aliases are in its `vm-manifest` entry as
`ssh_aliases`.

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

- uses: actions/upload-artifact@v7
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

### Keep the logs as an artifact

The automatic teardown runs after every step, too late for an upload step.
Tear down explicitly first; the automatic one then finds nothing to do:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: down
  if: always() && steps.fleet.outputs.stack-dir != ''
  with:
    operation: down

- uses: actions/upload-artifact@v7
  if: always() && steps.down.outputs.logs-dir != ''
  with:
    name: fleet-logs
    path: ${{ steps.down.outputs.logs-dir }}
    if-no-files-found: ignore
```

`fleet` is the `id` of the `up` step. The directory holds each container's
log and the end of each guest's `dmesg`.

### Full diagnostics at teardown

`diagnostics: true` on the `up` step adds a `diagnostics` directory to
`logs-dir`, written at teardown before the fleet is removed, whether the
job passed or not, by the explicit `down` step or by the automatic one:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  id: fleet
  with:
    diagnostics: true
```

| File | |
| --- | --- |
| `<guest>/dmesg.txt` | The guest's whole kernel log. |
| `<guest>/lspci.txt` | `lspci -nnk`: every PCI device and the driver bound to it. |
| `<guest>/lsblk.txt` | Block devices, with transport, model, serial and sector sizes. |
| `<guest>/lsmod.txt` | Loaded modules. |
| `<guest>/module-parameters.txt` | `/sys/module/<m>/parameters` of `amdgpu`, `ionic`, `ionic_rdma`, `ernic_p4`, `ib_core`, `ib_uverbs`, `rdma_cm`, `nvme`, `nvme_core`, `nvme_fabrics`, `nvme_rdma`, `sdxi_char` and `rocm_xio`, as `module/parameter=value`; a module that is not loaded is listed as such. |
| `compose-config.yml` | The stack as `docker compose config` resolves it, any `docker-compose.override.yml` in the stack directory included. |
| `NOTES.txt` | What this teardown collected and what it could not. |

It is best effort. Each command has 45 seconds and is stopped in the
guest when they are up, keeping what it had printed; a guest that has
used two minutes has the rest of its commands skipped, and one that does
not answer SSH is noted and skipped. None of that can fail the job or
keep the fleet from being torn down; what was missed is in `NOTES.txt`,
in the step summary and in warnings. Each guest file is at most its last
4 MiB, and `compose-config.yml` its first, counted in bytes as written.

Each guest's directory is emptied before it is collected into, and
`NOTES.txt` and the count in the step summary are of this teardown alone.
So a second `up` in one job, or a `work-dir` that outlives the job, does
not show a guest that no longer answers with an earlier fleet's files.
Directories of guests this fleet does not have are left where they are
and listed in `NOTES.txt` as an earlier collection's.

`compose-config.yml` is redacted by name, not by value. The value of a
key, an environment variable or a command-line flag named like a
credential is written as `[redacted]`: a name containing `password`,
`passwd`, `passphrase`, `secret`, `token`, `credential`, `api-key` or
`private-key` in any spelling, one that is or ends in `_KEY`, `_KEY_ID`,
`_PASS` or `_AUTH` (`SMTP_PASS`, not `PGPASS`), `AUTHORIZATION` and
`COOKIE`, a `Bearer` or `Basic` value, the password of a
`user:password@` or `:password@` URL, and the whole user part of a URL
that has no password (`https://TOKEN@host/`; a plain user name there
goes too, since nothing tells the two apart). When the value is a
mapping or a list under such a key, all of it is replaced: the
top-level `secrets:` section and a service's own go that way. The one
place a name is not read is the names of services, networks and
volumes themselves, where they are defined and where a service's
`depends_on:` or `networks:` refers to them, so a service called `auth`
keeps its definition and the services that wait for it still say so.
Grafana's admin password is one such value; the guest's password is
not in that file. That covers the stacks qemu-tool ships.

It does not cover a secret under any other name, or with no name:

- a short flag with its value attached or after it, `-pSECRET` or
  `-u user:SECRET`. No rule is safe there: `-p2222`, `-p8080:80`,
  `-pthread` and `-u 1000:1000` read the same and are not secrets;
- a variable such as `DSN` or `DATABASE_URL` whose value is the secret
  itself, outside the URL and `name=value` forms above;
- the inline `content:` of a top-level `configs:` entry, which is under
  no such name;
- a header or a JSON member inside a longer value, `-H "X-Api-Key: ..."`
  or `{"password": ...}`, other than an Authorization value with a
  `Bearer`, `Basic` or `Token` scheme.

A name that starts `max`, `min` or `num`, or holds `tokenizer`, is taken
for a setting and kept: `MAX_TOKENS`, `--num-tokens`,
`TOKENIZERS_PARALLELISM`.

`docker compose config` also renders
your `docker-compose.override.yml` and resolves variables passed through
from the runner, so if either holds a secret, read the file before you
upload it. The guest files are as the guest printed them; look before
publishing them from a guest that holds something private.
A fleet whose guests never answered still gets `compose-config.yml`.

### Just the tool

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    operation: install
    qemu-tool-version: 2.1.0

- run: |
    qemu-tool check
    qemu-tool gen-compose --xml ci/fleet.xml --output-dir "$RUNNER_TEMP/stack"
```

`--output-dir` matters for a fleet of your own: without it `gen-compose`
targets the directory of a shipped stack.

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    operation: command
    args: gen-compose --xml ci/fleet.xml --dry-run
```

## Inputs

All are optional.

| Input | Default | |
| --- | --- | --- |
| `operation` | `up` | See the table above. |
| **Which qemu-tool** | | One of the first three. |
| `qemu-tool-version` | `>=2.1,<4` | A release from PyPI: a version, a pip specifier, or `latest`. |
| `qemu-tool-ref` | | A tag, a branch or a full commit SHA of qemu-minimal. |
| `qemu-tool-source` | | Path to the `qemu/` directory of a checkout on the runner. Installed from a copy; the checkout is not written to. |
| `qemu-tool-repository` | `sbates130272/qemu-minimal` | Where `qemu-tool-ref` is fetched from: `owner/name` or a URL. |
| `container-qemu-tool` | `runner` | `runner`: the stack's containers run the runner's qemu-tool. `pypi`: they install the latest release. |
| `install` | `true` | `false` uses the `qemu-tool` already on PATH. |
| `setup-host` | `true` | Open `/dev/kvm` to this user; install missing `oras`, `qemu-utils`, `zstd`, `ansible-core`, `git`, `openssh-client`. |
| `oras-version` | `1.3.4` | Installed when the runner's is absent or older than 1.3.4. |
| **Fleet** (`up`) | | |
| `flavor` | `default` | A fleet qemu-tool ships. Not used when `xml` is given. |
| `xml` | | A fleet of your own: fleet XML files, one per line; later ones win. |
| `xml-overlay` | | Fleet XML files layered on the flavor or on `xml`; later ones win. |
| `vm-count` | fleet's own | Number of guests; 2 for the default flavor. |
| `vcpus`, `vmem` | see `size-from-host` | Per guest; `vmem` in MiB. |
| `fleet` | | Other fleet elements, `element: value` per line. Applied last. |
| `size-from-host` | `auto` | `auto`: size guests from the runner (4 to 8 vCPUs, 4 to 8 GiB) unless `vcpus`, `vmem`, `fleet` or a file of yours sets a size. `true`: also override sizes in your files. `false`: never. |
| `metrics` | `false` | Also start Prometheus, Grafana and log collection. |
| `clean-overlays` | `true` | Fresh guest disks rather than ones an earlier job left. |
| `wait-for` | `ssh` | `ssh`, `rdma` (also wait for `PORT_ACTIVE`), or `none`. |
| `wait-timeout` | `900` | Seconds the guests have, once the stack is up, to answer SSH and, with `wait-for: rdma`, to bring up their RDMA port. The limit on a guest's boot. |
| `stack-timeout` | `0` | Seconds `qemu-tool quick-start up` has to pull the guest image and the container images and start the stack's containers, which comes before `wait-timeout` and is not part of it. `0`: no limit but the job's. |
| `gpu` | `true` | Stage the GPU firmware and load `amdgpu` in each guest. `true`, `required`, `optional` or `false`; see The GPU. Skipped with `wait-for: none`, which `required` cannot be combined with. |
| `gpu-generation` | from the fleet | GPU generation the firmware is generated for; read from `<rocjitsu-config>`, else `gfx1250`. |
| `ssh-key-file`, `ssh-user` | the image's own | Key and account for a guest image that carries none to fetch. |
| `ssh-aliases` | `vm{index}` | Extra names for the guests in `ssh-config`. Words with `{index}` name every guest; other words name one guest per line. `vm<n>` is kept as well, unless you give that word to another guest. |
| `in-network` | `auto` | Reach guests on the stack's Docker network; `auto` does so in a `container:` job. |
| `teardown` | `always` | `never` leaves the fleet running at job end. |
| `log-lines` | `40` | Lines of each container log printed at teardown. |
| `diagnostics` | `false` | `true` also writes full guest diagnostics and the resolved compose configuration to `logs-dir` at teardown. On `down`, the default is what `up` was given. |
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

## Outputs

| Output | |
| --- | --- |
| `vm-manifest` | JSON array, one object per guest: `name`, `index`, `ssh_host`, `ssh_port`, `username`, `ssh_key_file`, `ssh_cmd`, `ssh_aliases`, `rdma_ip`, `rdma_ips`, `nics`, `rdma_device`, `gpu`. |
| `vm-names`, `vm-count` | Guest names, space separated; and how many. |
| `ssh-config` | An ssh config naming every guest, by its name, by `vm1`, `vm2`, ... and by your `ssh-aliases`. |
| `inventory` | An Ansible inventory of the guests. |
| `key-file`, `key-dir` | The private key the guests accept, and its directory. |
| `stack-dir`, `stack-name` | The generated compose stack. |
| `fleet-xml` | The fleet XML layers the fleet was booted from, lowest first, one path per line. |
| `images-dir`, `logs-dir`, `fetch-dir` | |
| `qemu-tool-version`, `qemu-tool-path` | The version as qemu-tool reports it, and where it is. |
| `qemu-tool-sha`, `qemu-tool-checkout` | With `qemu-tool-ref`: the commit it resolved to, and the qemu-minimal checkout. Empty otherwise. |

`up` sets all of them. `exec` sets `fetch-dir`, `down` sets `logs-dir`, and
`install` and `command` set the four `qemu-tool-*` outputs.

In a manifest entry, `rdma_ip` is the address of the NIC qemu-tool treats
as the guest's own, the one on the mesh that reaches other guests;
`rdma_ips` and `nics` (`index`, `mac`, `ip`) list every NIC. `rdma_device`
is filled in only with `wait-for: rdma`. `gpu` is the guest's
`amdgpu-probe --status` word after the GPU step, `null` with `gpu: false`
or `wait-for: none`.

Later steps of the job also get `qemu-tool` and `oras` on PATH (nothing
else is added to it) and two variables: `QEMU_TOOL_SSH_CONFIG`, and
`QEMU_TOOL_STACK`, which lets `qemu-tool compose ps` (or `logs`) find the
stack from any directory.

## What the script sees

| Variable | |
| --- | --- |
| `QT_VM_NAME`, `QT_VM_INDEX` | This guest; indexes start at 1. |
| `QT_VM_COUNT`, `QT_VM_NAMES` | The fleet. |
| `QT_RDMA_IP` | This guest's RDMA address on the mesh. |
| `QT_NIC_IPS` | The address of every NIC this guest has. |
| `QT_RDMA_DEVICE` | Its verbs device; empty unless the fleet was booted with `wait-for: rdma`. |
| `QT_RDMA_IPS`, `QT_PEER_RDMA_IPS` | Every guest's address, and every other guest's. |
| `QT_VM_<n>_NAME`, `QT_VM_<n>_RDMA_IP` | Guest *n*. |

With one guest the output streams live. With more, the script runs on all
of them in parallel and each guest's output is printed as its own group
once every guest has finished, so a script that hangs on one guest shows
nothing until the job times out: wrap long commands in `timeout`. The step
fails if the script fails on any guest.

## Time and disk

This action's own CI boots one guest of the default fleet, runs a few
checks in it and tears it down in three to four minutes on a hosted
runner, the image pull included. Set `timeout-minutes` on the job all the
same.

These waits make up a boot, and each has its own limit:

| Wait | Limit |
| --- | --- |
| `qemu-tool quick-start up`: the pulls, then the stack's containers | `stack-timeout`; none by default |
| Every guest answering SSH, then its RDMA port with `wait-for: rdma` | `wait-timeout`, 900 s |
| The GPU: generating the firmware on the runner, once per firmware set | 600 s, fixed |
| The GPU, per guest: copying the firmware in | 300 s, fixed |
| The GPU, per guest: `amdgpu` binding it after the firmware is staged | 120 s, fixed |
| The GPU, per guest: installing the firmware and that wait together | 600 s, fixed |

The default fleet's guest image is a few gigabytes to pull and several more
unpacked, once per job however many guests there are, plus the container
images of QEMU and the device servers. Each guest then adds an overlay that
grows as it is written and one file per NVMe namespace; qemu-tool asks for
about 30 GB. On a hosted runner the image is pulled on every job. On a
self-hosted runner `images-dir` persists and the backing image is reused.

## Compatibility

- **This action and qemu-tool.** `@v2` drives qemu-tool 2.x and 3.x. Its
  CI boots a guest on 2.1.0 and on 3.0.0 on every pull request, and two
  guests with RDMA between them on every push to `main` and twice a week;
  on a schedule it also renders every fleet of qemu-minimal's `main`. A
  new 3.x release needs nothing from you.
- **Your fleet XML and qemu-tool.** Forward only within a major version:
  a file written for one release reads on later ones, and a file using a
  newer element is refused by an older release. qemu-tool 3 removed
  elements; see below. If your XML needs another qemu-tool than the
  default gives, say so with `qemu-tool-version` or `qemu-tool-ref`.
- **Reproducibility.** Pin `qemu-tool-version` or give `qemu-tool-ref` a
  tag or SHA. The shipped fleets pin their images, so that fixes the stack;
  a fleet of your own pins what its own XML pins.
- **`@v1`** wrapped qemu-tool 1.x and is not maintained. It installs the
  latest qemu-tool unless told otherwise, which is no longer a 1.x
  release, so a workflow still on `@v1` has to set `qemu-tool-version` to a 1.x release; see below.

### From qemu-tool 2.x to 3.x

The default `qemu-tool-version` accepts 3.x, so a job that does not pin
gets the newest 3.x release. The inputs and outputs of this action
are the same on both, and every flavor 2.1 ships is in 3.x. What changes
is your own fleet XML: qemu-tool 3 removed ten elements, and a file that
sets one, an `xml-overlay` or a `fleet` line included, is refused by
name before anything is pulled (`<prom-port> was removed from the fleet
format`). Delete the element; none has a replacement to write in its
place.

| Element | What 3.x does |
| --- | --- |
| `<qemu-digest>`, `<ernic-digest>`, `<rocjitsu-digest>`, `<backing-digest>` | Nothing read them, so they never pinned an image. To pin the guest image by digest, write `name@sha256:...` in `<backing-ref>`. |
| `<domain-pattern>` | Nothing read it. |
| `<prom-port>`, `<loki-port>` | Nothing read them: Prometheus and Loki have no host port. |
| `<vm-backing-shared>` | A stack always starts its guests with image locking off, so that they share one guest image, whatever this said. |
| `<ernic-subnet>` | The guests' RDMA NICs are always on `192.168.100.0/24`, the network the guest image addresses them on. |
| `<ernic-mac-prefix>` | Every emulated NIC's MAC address starts `72:6f:63:6d`. |

A fleet XML started from this action's `examples/fleet-minimal.xml` as
it was in 2.1.0, or from one of the fleets qemu-tool 2.x shipped
(`rocm-golden.xml`, `helios-tray.xml`, `mi355x-server.xml`), sets
`<vm-backing-shared>` and is refused until that line is deleted.

A file without them reads on 2.1 and on 3.x alike, as
[`examples/fleet-minimal.xml`](https://github.com/sbates130272/qemu-tool-action/blob/main/examples/fleet-minimal.xml)
does. To stay on 2.x until your files are changed:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    qemu-tool-version: '~=2.1'    # the newest 2.x, never 3.x
```

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
`log-lines` lines of each container log are printed. `diagnostics: true`
adds each guest's whole `dmesg`, `lspci -nnk`, `lsblk`, module parameters
and the resolved compose configuration. Upload the directory (see above)
before asking why a boot failed.

| Symptom | Cause and fix |
| --- | --- |
| `qemu-tool X cannot read this fleet: ... unknown element <...>` | Your fleet XML uses an element this qemu-tool does not have. Install a newer one with `qemu-tool-version` or `qemu-tool-ref`, or drop the element. |
| `qemu-tool X cannot render this fleet` | qemu-tool's own reason follows: a guest image on a moving tag, a guest with no NIC or GPU, container images from different builds. |
| `this qemu-tool ships no flavor ...` | The flavor is newer than the qemu-tool installed; the message lists the ones it has. |
| `the fleet names no <backing-ref>` | A fleet of your own has to name its guest image; see A fleet of your own. |
| `/dev/kvm does not exist on this runner` | The runner has no nested virtualisation. Use a GitHub-hosted Linux x64 runner or a self-hosted one with KVM; in a container job pass `--device /dev/kvm`. |
| `<vm> never answered SSH` | The guest did not boot inside `wait-timeout`, or the key is not one its image accepts. A verbose SSH attempt and the end of the `qemu-<n>` container log are printed above the error. On an oversubscribed runner lower `vm-count`. |
| `<vm> has no RDMA device with an active port` | The guest has fewer than 4 vCPUs (`vcpus`), or the NIC server died; see the log of its `ernic-<n>` container, `ernic-<guest>-<nic>` in a fleet with several NICs per guest. |
| `the guest image has no ibv_devinfo` | `wait-for: rdma` on a guest image without rdma-core. Use `wait-for: ssh`. |
| A guest dies mid-job with `vfio-user: timed out waiting for reply` | More vCPUs were allocated than the runner has cores, and the NIC mesh evicted a live node. Use `vm-count: 1` or a larger runner. |
| `gpu: required, and <vm> is absent (...)` | `gpu: required` and a guest did not end with its GPU bound. The status after the guest's name says how far it got: `absent` is a fleet that gives that guest no GPU, `unknown` a guest image without `amdgpu-probe`. `gpu: true` does not fail on those two. |
| `qemu-tool quick-start up ... did not finish within N s` | `stack-timeout` ran out during the image pulls or the start of the containers. Raise it, or leave it at `0`. |
| `ssh-aliases: ... would name both ...` | Two guests would get one alias: a `{index}` word that expands to a name you also gave, or another guest's own name. |
| `<vm>: amdgpu did not bind the emulated GPU` | The lines above it carry `amdgpu-probe --status` and the guest's `amdgpu` kernel messages. `gpu: false` boots without the GPU step. |
| A warning that the images directory has less than about 30 GB free | Point `images-dir` at a larger disk. |
| `no fleet from an earlier operation: up step was found` | `exec` or `down` ran in a different job from `up`, or `up` failed before it recorded anything. Fleets do not cross jobs. |
| `/var/lib/qemu-tool is not bind-mounted into this job container` | Add the mount shown under Container jobs. |
| `could not join the stack network` | The job container cannot reach the Docker daemon, or its hostname is not its container id. Mount `/var/run/docker.sock`. |
| `qemu-tool X is a 1.x release` | This major version drives `qemu-tool` 2.x and 3.x. Leave `qemu-tool-version` at its default, or use `@v1` for 1.x. |
| `<...> was removed from the fleet format` | qemu-tool 3 no longer has that element. Delete it from your fleet XML or `fleet` lines, or stay on 2.x with `qemu-tool-version: '~=2.1'`; see From qemu-tool 2.x to 3.x. |

## Security

The guest image the shipped fleets use is public, and so are the key and
password that log in to it. A fleet publishes its ports on `127.0.0.1`
only; do not change `publish-bind-addr` on a runner other machines can
reach.

`qemu-tool-ref` runs code from the repository and ref you name. Give it a
full commit SHA, or a tag you trust not to move, rather than a branch.

## Migrating from v1

v1 wrapped individual `qemu-tool` 1.x subcommands and loaded env-files.
`qemu-tool` 2.0 replaced env-files with fleet XML and renamed the stacks, so
v2 is a different action rather than an update:

| v1 | v2 |
| --- | --- |
| `operation: gen-vm` / `run-vm` / `gen-compose` / `compose` with per-flag inputs | `operation: command` with `args`, or `operation: install` and a `run:` step |
| `env-file` | `xml` (fleet XML), `xml-overlay` and `fleet` |
| `stack`, `compose-profiles`, `compose-args` | `flavor` / `xml`; `metrics: true` for the metrics profile |
| `wait-for-healthy` | `wait-for: ssh` (default) or `rdma` |
| `vm-manifest` output | Same name; entries gain host, user, key, RDMA and GPU fields, and `image_path` is gone |
| `vcpus`, `vmem`, `extra-args`, `wait-timeout` | Same names, new meaning: the size of every guest, flags for `quick-start up`, and the wait for the guests (default 900 s, was 120) |

A workflow that stays on `@v1` must set `qemu-tool-version` to a 1.x
release: v1 installs the latest `qemu-tool` by default, which is no longer
1.x, and v1's command lines no longer match it. That pins the runner only. The
containers of a 1.x stack install the latest release themselves, so a v1
fleet that starts containers is not kept working by the pin; move to `@v2`.

## Versioning

Tags are `vX.Y.Z` (exact), `vX.Y` (floating minor) and `vX` (floating
major); `@v2` follows the newest 2.x release. Changes are listed in
[CHANGELOG.md](https://github.com/sbates130272/qemu-tool-action/blob/main/CHANGELOG.md);
how a release is cut is in
[RELEASING.md](https://github.com/sbates130272/qemu-tool-action/blob/main/RELEASING.md).

## Development

```bash
python3 -m unittest discover -s tests   # no runner, no guest
tests/smoke.sh                          # installs qemu-tool into a scratch dir,
                                        # renders every fleet it ships
tests/smoke.sh ../qemu-minimal/qemu     # the same, against a checkout
tests/test_release.sh                   # the release workflow's tag logic
```

The action is `action.yml`, three dependency-free JavaScript shims, and
`qemu_tool_action.py`, which is standard-library Python.

## License

[MIT](https://github.com/sbates130272/qemu-tool-action/blob/main/LICENSE)
