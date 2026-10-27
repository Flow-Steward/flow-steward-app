# Local extension runtime guarantees

This is the current runtime trust boundary, not a network-isolation proposal.

## What an installed extension is

An installed extension is executable code that the owner of the installation
chose to trust. It runs with the rights of the process that runs it: what that
process can read and reach, the extension can too.

- Layers 1 and 2 reduce the damage of mistakes and runaway code, and they make
  what an extension does in plain sight visible at install.
- They are not a sandbox against a deliberately malicious extension.
- The extension author is responsible for what the extension does. Core is
  responsible for its own checks being correct and for saying precisely what
  they guarantee, which is this document.
- OAuth tokens and grants an extension receives for its own connections are
  intended: the shared OAuth/SDK model stays.

## Execution boundary

A local extension runs as `subprocess.Popen` under the same OS identity as the
app or worker that invokes it. These things are already in place:

- an environment allowlist, so no DSNs and no signing key reach the child;
- a timeout, output caps and concurrency limits;
- grants pinned to the extension's own id.

These application-level controls are complemented by the process confinement
described below. They do not isolate a trusted extension from the service user's
files or network. The child can still:

- read any file the service user can read;
- open any socket;
- start descendants that escape cleanup by leaving the process group and
  clearing the run marker;
- consume aggregate CPU and memory across descendants beyond the per-process
  limits; there is no shared resource budget;
- send an OAuth bearer it was given anywhere.

## Code review at install

`extension_code_scan.py` parses the extension's **own** Python code with `ast`:

- its files, skipping tests, caches and virtual environments;
- the inline `python -c` sources of the manifest's entrypoint and health
  commands;
- every `.py` file those commands run, even under `tests/`.

The review stops with `extension_code_too_large` past 2,000 files or 5 MiB.
Each file is read only up to what is left of the budget, before it is parsed.

What the review finds is listed on the extension's page, and in the result of
an install or update, as `file:line: what why`. No extra confirmation is
required.

- **Refused:**
  - `exec`, `eval`, `compile`;
  - `__import__`, and `importlib.import_module` with a computed name;
  - `ctypes`, `cffi`;
  - `os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `os.fork`, `pty.spawn`;
  - `subprocess` with a shell, `asyncio.create_subprocess_shell`;
  - loading pickle or marshal data.
- **Listed for review:**
  - raw `socket`;
  - processes without a shell;
  - `open()` on an absolute path;
  - `chown`, `chmod`, `setuid`.

This is a review aid, **not the boundary**, for two reasons:

- Python reaches anything indirectly (`getattr` on a computed name, code
  shipped as data).
- An extension's dependencies (`requests`, `pillow`, the SDK) legitimately use
  sockets, files and processes, and they cannot be scanned without refusing
  every real extension.

## Process confinement

`process_confinement.py`, used for every local invocation:

- **Its processes end with it.** At timeout, on an output overflow and after
  the run, these are killed:
  - the extension's process group;
  - every process still carrying the run's environment marker
    (`FS_EXTENSION_RUN`). This finds descendants that left the group
    (`start_new_session`, `setsid`).

  The guarantee is exactly this. A descendant that clears its own environment
  is not found. Without `/proc` (not Linux), only the group is killed.
- **Per-process limits:**
  - CPU time: twice the timeout, at least 30 s;
  - 4 GiB of address space;
  - 1 GiB per written file;
  - no core dumps.

  These are limits of each process, not a budget shared by all of an
  extension's processes, and not a disk quota.

  The values are fixed in code on purpose. They are a safety ceiling for
  runaway code, generous enough for the current extensions: none loads models
  in-process, and DocLing calls a service. They are not a tuning knob.
  Per-extension values would be a manifest declaration, as for egress.
- **The launcher fails closed.** A short launcher applies the limits and
  `no_new_privs`, then `exec`s the extension. `preexec_fn` is unsafe in a
  threaded service, so it is not used.
  - A limit is never set above the hard limit the service already runs under.
  - On Linux, a limit or `no_new_privs` that cannot be applied stops the run
    with exit code 70 and a message, instead of running unconfined.
- **`no_new_privs`** (Linux): a setuid program gains nothing.
- **A private `TMPDIR`** per invocation, removed afterwards.

It needs no privileges. It does not limit file or network access, because the
extension still shares the service's user.

## Egress declaration, not isolation

An extension declares where it connects in its manifest (optional):

```yaml
egress:
  hosts: [api.telegram.org]
  connection_fields: [custom_smtp.host]
```

- `hosts` are fixed host names.
- `connection_fields` are the host fields a project fills in on its
  connection.

The compiler checks the format, and checks that each connection field exists
in the extension's connection types.

- A missing declaration installs with the `egress_undeclared` warning, shown on
  the extension page and in the install result.
- `egress: {}` declares that the extension connects nowhere.

**Nothing enforces it.** This release claims **no network isolation**: an
extension can still reach any address the service can.
