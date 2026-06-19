# SN101 Miner — Setup Runbook

> **Purpose**: deploy a Tag101 miner on a fresh Ubuntu 22.04 VPS, point it at your
> already-running solver backend, and have it serving validators within 25 minutes.

This is the **miner-side** runbook. For solver setup see [RUNBOOK.md](./RUNBOOK.md).

> [!NOTE]
> **Multi-miner-per-VPS model.** Each miner you install gets its own dedicated
> config files: one env file (`~/.sn101-miner-<HOTKEY>.env`) and one pm2 ecosystem
> file (`~/sn101-fleet-pm2/sn101-miner-<HOTKEY>.config.cjs`). Different miners
> never share config. You can run as many miners per VPS as your CPU/RAM allow —
> each runs on its own port (8091, 8092, 8093…), with its own coldkey/hotkey, on
> its own pm2 process. See the [multi-miner example](#example-running-3-miners-on-one-vps).

---

## At a glance

```
   Bittensor validators
            │  (query miners every ~15 min with a tweet)
            ▼
   ┌────────────────────────┐
   │ Your miner VPS         │   pm2 process: sn101-miner-<HOTKEY>
   │ - tag101 (upstream)    │   listens on port 8091 (or next free)
   │ - thin_miner.py (ours) │
   └────────┬───────────────┘
            │ HTTPS-ish, X-Solver-Key auth
            ▼
   ┌────────────────────────┐
   │ Solver VPS             │   pm2 process: sn101-solver
   │ - returns canonical    │   port 7311
   │   tags for the tweet   │
   └────────────────────────┘
```

Each miner is a **thin client** of the central solver. It receives a tweet from a
validator, forwards it to the solver, gets back canonical tags, returns them to
the validator. No LLM calls happen on the miner side — the solver does that
work once and caches it for every miner in the fleet.

---

## What you need before starting

| Item | Where you get it |
|------|------------------|
| A fresh **Ubuntu 22.04 LTS** VPS (any provider — we used OVH) | Provider dashboard |
| **sudo or root** access on that VPS | Provider dashboard |
| The **solver URL** (e.g. `http://<SOLVER_VPS_IP>:7311`) | From your solver install output |
| The **solver API key** (32-byte hex) | From your solver install output, stored in your password manager |
| A **registered hotkey** on netuid 101 | `btcli subnet register --netuid 101 ...` (done in advance) |
| Your **wallet folder** with the hotkey file | `~/.bittensor/wallets/<COLDKEY>/hotkeys/<HOTKEY>` on whatever machine generated it |

**Don't have a registered hotkey yet?** Run on your laptop / wherever you keep
the coldkey:

```bash
btcli subnet register --netuid 101 \
    --wallet.name <COLDKEY> --wallet.hotkey <HOTKEY> \
    --subtensor.network finney
```

This costs TAO (current burn cost: `btcli subnet hyperparameters --netuid 101 | grep burn`).

---

## The 7-step install per VPS

### Step 1 — SSH into the new VPS

```bash
ssh ubuntu@<NEW_VPS_IP>
```

If your provider only gave you root, ssh as root and create an ubuntu user:
```bash
adduser ubuntu
usermod -aG sudo ubuntu
su - ubuntu
```

Then re-ssh as `ubuntu@<NEW_VPS_IP>`.

### Step 2 — Install git

```bash
sudo apt-get update -qq
sudo apt-get install -y git
```

### Step 3 — Clone the fleet repo

```bash
cd ~
git clone https://github.com/Vector99dev/sn101-fleet.git
cd sn101-fleet
```

### Step 4 — Run the installer (first pass — will fail at wallet check)

This is intentional. The installer runs all the bootstrap (Node, pm2, Python
venv, bittensor, the tag101 repo), then checks for the hotkey file and stops
with a clear error message telling you where to put the wallet.

```bash
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 \
SN101_SOLVER_API_KEY=<YOUR_SOLVER_API_KEY> \
./miner-install.sh <PM2_NAME> <COLDKEY_NAME> <HOTKEY_NAME> <AXON_PORT>
```

Replace:
- `<SOLVER_IP>` → your solver VPS IP (e.g. `<SOLVER_VPS_IP>`)
- `<YOUR_SOLVER_API_KEY>` → the 32-byte hex key from your password manager
- `<PM2_NAME>` → the pm2 process name you want (e.g. `sn101-miner-jinsai25` or any unique label). This drives the env file (`~/.sn101-miner-<PM2_NAME>.env`), the ecosystem file, and the pm2 log file names.
- `<COLDKEY_NAME>` → your wallet/coldkey directory name (e.g. `jinsai25`)
- `<HOTKEY_NAME>` → the registered hotkey filename (e.g. `jinsai25`)
- `<AXON_PORT>` → port this miner listens on (e.g. `8091`). **Required** — declare it explicitly so it's obvious which miner is on which port.

**Example for the first miner**:
```bash
./miner-install.sh sn101-miner-jinsai25 jinsai25 jinsai25 8091
```

**Expected output**: green "ok" lines through steps 1–4, then:

```
[FAIL] Hotkey file not found:
       /home/ubuntu/.bittensor/wallets/<COLDKEY>/hotkeys/<HOTKEY>
```

That's the signal to upload the wallet next.

### Step 5 — Upload the wallet (from a different machine)

Open a new terminal on the machine where the wallet lives (laptop or offline
machine — wherever you ran `btcli`). Then:

```bash
# Option A: upload the whole wallet directory (simplest, includes coldkey)
scp -r ~/.bittensor/wallets/<COLDKEY_NAME> \
    ubuntu@<NEW_VPS_IP>:~/.bittensor/wallets/
```

Or if you want to be more careful and **only** upload the hotkey + public coldkey
marker (recommended — keeps your private coldkey off the VPS):

```bash
# Option B: hotkey + coldkeypub only
ssh ubuntu@<NEW_VPS_IP> 'mkdir -p ~/.bittensor/wallets/<COLDKEY_NAME>/hotkeys'

scp ~/.bittensor/wallets/<COLDKEY_NAME>/coldkeypub.txt \
    ubuntu@<NEW_VPS_IP>:~/.bittensor/wallets/<COLDKEY_NAME>/

scp ~/.bittensor/wallets/<COLDKEY_NAME>/hotkeys/<HOTKEY_NAME> \
    ubuntu@<NEW_VPS_IP>:~/.bittensor/wallets/<COLDKEY_NAME>/hotkeys/
```

**Why Option B matters**: if the VPS gets compromised, an attacker with the
coldkey can transfer your TAO out. With only the hotkey, the worst they can do
is hijack your miner — they can't steal your TAO. See [the security cleanup
section](#security-cleanup-recommended) below.

### Step 6 — Re-run the installer (will now finish successfully)

Back on the VPS:

```bash
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 \
SN101_SOLVER_API_KEY=<YOUR_SOLVER_API_KEY> \
./miner-install.sh <PM2_NAME> <COLDKEY_NAME> <HOTKEY_NAME> <AXON_PORT>
```

Same command as Step 4 (e.g. `./miner-install.sh sn101-miner-jinsai25 jinsai25 jinsai25 8091`).
The installer is idempotent — it skips finished steps, verifies the wallet,
writes the env file, generates the pm2 ecosystem, opens the chosen port in
ufw, starts the miner under pm2, saves pm2 state, and installs the boot-time
systemd hook.

**Expected output** ends with the green box:

```
========================================================================
  MINER INSTALLED: sn101-miner-<HOTKEY>
========================================================================
  Miner name:     sn101-miner-<HOTKEY>
  Wallet:         <COLDKEY> / <HOTKEY>
  Axon port:      8091  (public: <NEW_VPS_IP>:8091)
  ...
```

### Step 7 — Verify the miner is actually mining

Four checks. All four should pass.

```bash
# 1. pm2 status — should show "online"
pm2 list
```
Expected: `sn101-miner-<HOTKEY>  ...  online`

```bash
# 2. axon TCP port is listening
ss -tlnp | grep :8091
```
Expected: `LISTEN  0  2048  0.0.0.0:8091  ...  users:(("python",pid=...))`

```bash
# 3. miner registered on the chain
pm2 logs sn101-miner-<HOTKEY> --lines 30 --nostream | grep -E "miner ready|serving"
```
Expected:
```
INFO miner ready: netuid=101 uid=<YOUR_UID> endpoint=wss://entrypoint-finney.opentensor.ai:443
INFO miner serving at block <BLOCK_NUMBER>
```

```bash
# 4. axon reachable from outside (run from a DIFFERENT machine, e.g. your laptop)
nc -zv <NEW_VPS_IP> 8091
```
Expected: `Connection to <NEW_VPS_IP> 8091 port [tcp/*] succeeded!`

If all four pass, **the miner is mining**. Validators will start querying it
within 1–2 forward intervals (~15 min each per validator).

To watch for incoming validator queries:

```bash
pm2 logs sn101-miner-<HOTKEY> --lines 100 --nostream | grep MINER_SOLVED_TASK
```

You'll see lines like:
```
MINER_SOLVED_TASK task=<uuid> kind=sn101.tags.v1 elapsed=0.087s answer_keys=['tags']
```

That's a validator query → solver call → tags returned. The "elapsed=0.087s"
confirms the solver is doing the work (no per-query LLM latency on the miner).

---

## Adding a second/third miner on the same VPS

Just re-run `miner-install.sh` with a **different pm2 name + different hotkey
+ different port**. The installer creates fresh per-miner env and ecosystem
files keyed by the pm2 name:

```bash
# Upload the second hotkey first:
scp ~/.bittensor/wallets/<COLDKEY>/hotkeys/<SECOND_HOTKEY> \
    ubuntu@<VPS_IP>:~/.bittensor/wallets/<COLDKEY>/hotkeys/

# Then on the VPS:
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 \
SN101_SOLVER_API_KEY=<YOUR_SOLVER_API_KEY> \
./miner-install.sh <PM2_NAME> <COLDKEY> <SECOND_HOTKEY> <PORT>
```

The installer:
- Skips system deps (already installed)
- Skips repo cloning (already there)
- Validates the port is free
- Adds a new pm2 entry named `<PM2_NAME>`
- Writes a NEW per-miner env file at `~/.sn101-miner-<PM2_NAME>.env`
- Writes a NEW ecosystem at `~/sn101-fleet-pm2/<PM2_NAME>.config.cjs`
- Opens that port in ufw
- `pm2 save` updates persistence

You can run as many miners per VPS as your CPU/RAM allow. Each miner adds
~130 MB RAM and a tiny bit of CPU (most work is on the solver).

### Example: running 3 miners on one VPS

After running `miner-install.sh` three times:

```bash
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 SN101_SOLVER_API_KEY=<KEY> \
    ./miner-install.sh sn101-miner-jinsai25  jinsai25  jinsai25  8091
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 SN101_SOLVER_API_KEY=<KEY> \
    ./miner-install.sh sn101-miner-minerA    jinsai25  minerA    8092
SN101_SOLVER_URL=http://<SOLVER_IP>:7311 SN101_SOLVER_API_KEY=<KEY> \
    ./miner-install.sh sn101-miner-minerB    jinsai25  minerB    8093
```

Result — each miner is fully independent:

```
/home/ubuntu/
├── .sn101-miner-sn101-miner-jinsai25.env  # SN101_COLDKEY=jinsai25, SN101_HOTKEY=jinsai25, SN101_AXON_PORT=8091
├── .sn101-miner-sn101-miner-minerA.env    # SN101_COLDKEY=jinsai25, SN101_HOTKEY=minerA,   SN101_AXON_PORT=8092
├── .sn101-miner-sn101-miner-minerB.env    # SN101_COLDKEY=jinsai25, SN101_HOTKEY=minerB,   SN101_AXON_PORT=8093
└── sn101-fleet-pm2/
    ├── sn101-miner-jinsai25.config.cjs    # → reads matching env file
    ├── sn101-miner-minerA.config.cjs      # → reads matching env file
    └── sn101-miner-minerB.config.cjs      # → reads matching env file
```

(Tip: pick shorter pm2 names to keep the env-file paths readable. E.g. use
`miner-A` instead of `sn101-miner-minerA` — the installer doesn't care, and
the file becomes `~/.sn101-miner-miner-A.env`.)

And `pm2 list` shows:

```
│ id │ name                    │ status │ port  │
├────┼─────────────────────────┼────────┼───────┤
│  0 │ sn101-miner-jinsai25    │ online │ 8091  │
│  1 │ sn101-miner-minerA      │ online │ 8092  │
│  2 │ sn101-miner-minerB      │ online │ 8093  │
```

Each miner can be operated independently:

```bash
# Restart just one miner
pm2 restart sn101-miner-minerA

# Change one miner's port without affecting others — edit its env file then:
nano ~/.sn101-miner-sn101-miner-minerA.env       # change SN101_AXON_PORT=8094
sudo ufw allow 8094/tcp
pm2 restart sn101-miner-minerA --update-env

# Point one miner at a different solver (e.g. for testing) without affecting others
nano ~/.sn101-miner-minerB.env       # change SN101_SOLVER_URL=http://test-solver:7311
pm2 restart sn101-miner-minerB --update-env
```

### Inside a per-miner env file

Each `~/.sn101-miner-<HOTKEY>.env` is self-contained:

```bash
# Common settings (typically identical across miners on the same VPS)
TASK_MINER_MODULE=thin_miner
SN101_SOLVER_URL=http://<SOLVER_IP>:7311
SN101_SOLVER_API_KEY=<32-byte hex>
SN101_SOLVER_TIMEOUT_S=5.0
SN101_VENV_BIN=/home/ubuntu/sn101-venv/bin
SN101_TAG101_DIR=/home/ubuntu/tag101
SN101_FLEET_DIR=/home/ubuntu/sn101-fleet
SN101_SUBTENSOR_NETWORK=finney
SN101_LOG_LEVEL=--logging.info

# Per-miner identity (these MUST differ between miners on the same VPS)
SN101_COLDKEY=<this miner's coldkey>
SN101_HOTKEY=<this miner's hotkey>
SN101_AXON_PORT=<this miner's unique port>
```

The pm2 ecosystem just points at this file — no args, no duplication:

```javascript
module.exports = {
  apps: [{
    name: "sn101-miner-<HOTKEY>",
    script: "/home/ubuntu/sn101-fleet/deploy/start-miner.sh",
    interpreter: "bash",
    env: {
      SN101_MINER_ENV: "/home/ubuntu/.sn101-miner-<HOTKEY>.env"
    },
    // ... reliability knobs ...
  }]
};
```

---

## Troubleshooting

### `Hotkey file not found` (after wallet upload)

The filename or coldkey directory name doesn't match what you passed to
the installer. Check:

```bash
ls ~/.bittensor/wallets/<COLDKEY>/hotkeys/
# What you see here must match the <HOTKEY> arg to miner-install.sh
```

If the file is there with the right name, check permissions:
```bash
chmod 600 ~/.bittensor/wallets/<COLDKEY>/hotkeys/<HOTKEY>
```

### `ModuleNotFoundError: No module named 'munch'` (or `tenacity` or `aiohttp`)

You ran an old version of the installer that didn't install these. Fix:
```bash
~/sn101-venv/bin/pip install munch tenacity aiohttp
pm2 restart all
```

### pm2 shows `errored` or repeatedly `online → waiting`

Look at the actual error:
```bash
pm2 logs sn101-miner-<HOTKEY> --lines 80 --nostream
```

Most common causes:
- **Wallet not loadable** → typo in coldkey/hotkey name; permissions wrong
- **Subtensor unreachable** → outbound HTTPS blocked (rare on OVH)
- **Port 8091 already in use** → another process bound it; check `ss -tlnp | grep :8091`

### `nc -zv <VPS_IP> 8091` from outside fails

UFW rule wasn't added. Add it manually:
```bash
sudo ufw allow 8091/tcp
sudo ufw status numbered
```

If still failing, check whether the VPS provider has a separate inbound
firewall (some clouds layer their own block on top of `ufw`).

### Miner shows `online` but no `MINER_SOLVED_TASK` after 30+ minutes

This is normal for the first few intervals — validators don't all immediately
discover newly-registered miners. Things to check:

1. **Is the axon endpoint registered on chain?**
   ```bash
   ~/sn101-venv/bin/btcli subnet metagraph --netuid 101 --subtensor.network finney \
       | grep <HOTKEY_SS58>
   ```
   The output should show your axon IP:port. If it shows `0.0.0.0:0`, the
   axon serve announcement failed.

2. **Is your VPS IP reachable?** Run `nc -zv <VPS_IP> 8091` from a third machine
   that isn't your laptop or solver VPS. If it fails, the validators won't
   reach you either.

3. **Patience.** Subnet 101 has a forward interval of 15 minutes per validator.
   With ~10 active validators, expect first queries within ~5 minutes typically,
   but it can be 30+ minutes if validators aren't all running their forward at
   the same cadence.

### Wrong solver API key — miner returns safe defaults silently

Symptom: `pm2 logs sn101-miner-<HOTKEY>` shows `solver call failed: 401 Unauthorized`,
and `MINER_SOLVED_TASK` lines show `answer_keys=['tags']` but the tags are always
`['ai', 'tech', 'release']` (the safe defaults).

Fix: update the per-miner env file with the correct key:
```bash
nano /home/ubuntu/.sn101-miner-<HOTKEY>.env
# Update SN101_SOLVER_API_KEY=...
pm2 restart sn101-miner-<HOTKEY> --update-env
```

If you have multiple miners and want to update them all at once:
```bash
sed -i "s|^SN101_SOLVER_API_KEY=.*|SN101_SOLVER_API_KEY=<NEW_KEY>|" \
    /home/ubuntu/.sn101-miner-*.env
pm2 restart all --update-env
```

---

## Security cleanup (recommended)

After the miner is verified working, remove the **private coldkey** from the
VPS. The miner doesn't need it day-to-day — only registration, unstaking, and
transfers use it.

```bash
rm /home/ubuntu/.bittensor/wallets/<COLDKEY>/coldkey
```

What this protects against:
- If the VPS is ever compromised, an attacker with **only** the hotkey can:
  - Run your miner (but rewards still go to YOUR coldkey)
  - Change the axon endpoint (annoying but recoverable)
- An attacker who **also** has the coldkey can:
  - **Transfer your accumulated TAO out** (catastrophic)

Keep this on the VPS:
- `coldkeypub.txt` (public — safe)
- `hotkeys/<HOTKEY>` (signs forward responses)

Keep ONLY on your laptop/offline machine:
- `coldkey` (private — never put on internet-connected machines longer than necessary)

If you ever need to do an operation that requires the coldkey (unstake,
transfer, register more hotkeys), temporarily upload it, do the operation,
then delete it again:
```bash
scp ~/.bittensor/wallets/<COLDKEY>/coldkey ubuntu@<VPS_IP>:~/.bittensor/wallets/<COLDKEY>/
ssh ubuntu@<VPS_IP> 'btcli ... && rm ~/.bittensor/wallets/<COLDKEY>/coldkey'
```

---

## Daily operations

### Status of all miners on this VPS

```bash
pm2 list
```

### Tail logs

```bash
pm2 logs sn101-miner-<HOTKEY> --lines 50 --nostream
pm2 logs sn101-miner-<HOTKEY>                   # live tail
```

### Restart a miner

```bash
pm2 restart sn101-miner-<HOTKEY>
pm2 restart sn101-miner-<HOTKEY> --update-env   # also re-read env file
```

### Stop / remove

```bash
pm2 stop sn101-miner-<HOTKEY>
pm2 delete sn101-miner-<HOTKEY>
pm2 save                                         # persist removal
```

### See a miner's env file

```bash
cat /home/ubuntu/.sn101-miner-<HOTKEY>.env
```

Edit it directly (`nano /home/ubuntu/.sn101-miner-<HOTKEY>.env`) to change that
miner's port, coldkey, solver URL, or solver key — then `pm2 restart
sn101-miner-<HOTKEY> --update-env`. Only that miner is affected.

### List all per-miner env files on this VPS

```bash
ls -la /home/ubuntu/.sn101-miner-*.env
```

### Inspect the pm2 ecosystem for this miner

```bash
cat /home/ubuntu/sn101-fleet-pm2/sn101-miner-<HOTKEY>.config.cjs
```

### Watch for incoming validator forwards

```bash
pm2 logs sn101-miner-<HOTKEY> --lines 100 --nostream | grep MINER_SOLVED_TASK
```

### Check your hotkey's metagraph state

```bash
~/sn101-venv/bin/btcli wallet overview \
    --wallet.name <COLDKEY> --wallet.hotkey <HOTKEY> \
    --subtensor.network finney
```

---

## Rotating the solver API key (do this together with the solver side)

When you rotate `SOLVER_API_KEY` on the solver VPS, every miner needs to be
updated **at the same time** or they'll all start returning safe defaults.

On the solver VPS:
```bash
sudo sed -i "s/^SOLVER_API_KEY=.*/SOLVER_API_KEY=<NEW_KEY>/" /home/ubuntu/.sn101.env
pm2 restart sn101-solver --update-env
```

Then **on every miner VPS** (within a minute) — updates ALL per-miner env
files in one command:
```bash
sed -i "s|^SN101_SOLVER_API_KEY=.*|SN101_SOLVER_API_KEY=<NEW_KEY>|" /home/ubuntu/.sn101-miner-*.env
pm2 restart all --update-env
```

If you script this with parallel ssh (e.g. `pdsh`) or a loop, the gap can be
under 10 seconds — barely noticeable.

---

## Repo layout (what's where on the miner VPS)

After `miner-install.sh` finishes (with two miners as the example), the VPS
looks like this:

```
/home/ubuntu/
├── sn101-fleet/                                # cloned from your GitHub
│   ├── thin_miner.py                           # imported by tag101.miner at startup
│   ├── deploy/start-miner.sh                   # pm2 wrapper (reads env, no args)
│   └── miner-install.sh                        # the installer you ran
│
├── tag101/                                     # cloned from tag101-ai/tag101
│   ├── miner.py                                # entry point
│   ├── tasks/sn101.py                          # default handler (we override this)
│   └── chain/                                  # bittensor wallet/axon plumbing
│
├── sn101-venv/                                 # Python venv (bittensor + httpx)
│
├── sn101-fleet-pm2/                            # one ecosystem PER miner
│   ├── sn101-miner-<HOTKEY_1>.config.cjs       # → .sn101-miner-<HOTKEY_1>.env
│   └── sn101-miner-<HOTKEY_2>.config.cjs       # → .sn101-miner-<HOTKEY_2>.env
│
├── .sn101-miner-<HOTKEY_1>.env                 # SELF-CONTAINED per-miner config
├── .sn101-miner-<HOTKEY_2>.env                 # SELF-CONTAINED per-miner config
│   # Each contains: SN101_COLDKEY, SN101_HOTKEY, SN101_AXON_PORT, solver URL+key, paths
│
├── .bittensor/                                 # bittensor's data directory
│   ├── wallets/<COLDKEY>/
│   │   ├── coldkeypub.txt
│   │   └── hotkeys/<HOTKEY_1>, <HOTKEY_2>
│   └── sn101/<HOTKEY_1>/, sn101/<HOTKEY_2>/    # per-miner state directories
│
└── .pm2/                                       # pm2 daemon state
    └── logs/sn101-miner-<HOTKEY_1>-{out,err}.log
    └── logs/sn101-miner-<HOTKEY_2>-{out,err}.log
```

Three properties this layout guarantees:

1. **Per-miner isolation.** Editing `~/.sn101-miner-<HOTKEY_1>.env` and running
   `pm2 restart sn101-miner-<HOTKEY_1> --update-env` affects ONLY that one
   miner. The others keep running with their own config.
2. **No port conflicts.** Each `.env` declares its own `SN101_AXON_PORT`. The
   installer refuses to start if the port you specified is already in use, so
   you can't accidentally double-bind.
3. **Self-contained config.** Looking at one `.env` file tells you everything
   that miner does: which wallet, which port, which solver, which key.

---

## Why the architecture works the way it does

- **One solver, many miners**: the embedding model (`all-MiniLM-L6-v2`),
  vocabulary cache, and OpenRouter wiring all live on the solver VPS. Miners
  just forward tweets and return tags. Memory / CPU on miner VPSes is tiny
  (~130 MB per miner).
- **Per-tweet dedup lock on the solver**: when multiple miners on different
  VPSes receive the same tweet from the same validator at the same instant,
  the solver fires **one** LLM call and the other miners share the cached
  result. This is what saves LLM cost — see [README.md](./README.md) for the
  architecture diagram.
- **`thin_miner.py` plugs into Tag101's task registry** via the
  `TASK_MINER_MODULE=thin_miner` env var. The upstream `tag101.miner` reads
  this env var and loads your handler instead of the default OpenAI reference.
  No upstream modification needed.

For solver-side details see [RUNBOOK.md](./RUNBOOK.md).
