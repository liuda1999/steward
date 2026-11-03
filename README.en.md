# steward

[简体中文](README.md) | **English**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-413%20passing-brightgreen.svg)](tests)
[![Version](https://img.shields.io/badge/version-1.3.0-informational.svg)](https://github.com/liuda1999/steward/releases)

> Your local model is the workhorse; the cloud is the specialist you call in. steward owns everything in between: when spending money is worth it, whether the context still fits, and how to keep the archive from filling your disk.
> **Open `http://127.0.0.1:27418/` for the console** — fill both slots, tune the quota, issue project keys, never touch the command line.

```
agent ──OpenAI-compatible──▶ 127.0.0.1:27418/v1 ──┬── local model (default)
                                                  └── cloud model (only on request)
```

| | |
|---|---|
| Version | **v1.3.0** ([release notes](docs/releases/v1.3.0.md)) |
| License | **Apache-2.0** ([LICENSE](LICENSE) · [NOTICE](NOTICE)) — commercial use, modification, closed-source distribution, with a patent grant |
| Shape | single-process FastAPI gateway, stateless; web console on the same port |
| Default port | 27418 |
| Wire protocol | `/v1/chat/completions` + `/v1/responses` (converted both ways) |
| Dependencies | fastapi / uvicorn / httpx / PyYAML — no database |
| Size | 5,824 lines of Python (comments included), 14 modules |
| Tests | 413: reliability 20 · differential audit 23 · port/slots/version 31 · docs consistency 9 · console 38 · net policy 7 · the rest unit and end-to-end |
| Per-turn injection | 531 tokens (system + quota + tool definition) |
| Upstream | any OpenAI-compatible endpoint: llama.cpp / vLLM / LM Studio / Ollama / a cloud API |

**The name means "steward".** It does not decide what you do; it keeps three things: the books (can you afford this call), the window (does it still fit), the archive (does it survive on disk). Those three are exactly what breaks when a local model runs a long task — and exactly what the old name `cloudbridge` could not express: a bridge moves traffic, it does not exercise restraint.

---

## 1. The problem

Using a local model as the daily driver and the cloud as backup is not a hard idea. Three things are hard.

**1. Who decides "should we spend this money".**

Gateway-side routing (scoring the request, classifying it, redirecting it) is cheap and controllable, but it judges the **request text**, not the **model's actual confidence**. What it calls hard, the model may finish in one second; what it calls easy, the model may not know how to start.

So this takes the other path: put a `consult_cloud` tool in the tool list and let the model ask. But the guardrails live **in the gateway, not in the prompt**. The model may judge "I am not sure about this"; whether it may spend this call is counted by the gateway.

**2. The local window is far smaller than the cloud's, and its size is not fixed.**

Cloud models are commonly 1M+; local ones may be 64K / 128K / 256K, and `--parallel` divides that (`-c 131072 --parallel 4` gives 32,768 per slot).

Worse: llama.cpp with `--context-shift` **drops from position 0**, so the system prompt and tool definitions die first; Ollama truncates from the head and only writes a server-side log. Both return **HTTP 200**. Exceeding the window is not an error — it just makes the model quietly dumber.

**3. Cloud output is long; one round trip back into context eats 12% of the local window.**

A 16K-character deliverable is roughly 11K tokens, resident forever as a `role=tool` message. Ask twice in one turn and the window is gone.

Those three things, plus "will three months of running fill the disk", are the whole project.

---

## 2. Three diagrams

### 2.1 Deployment topology

![Deployment topology](docs/topology.svg)

How to read it:

- The agent sees one endpoint and one `st-` key. It does not know how many models sit behind it.
- Routing is decided inside the gateway: **local by default**; traffic leaves the machine only when the model calls `consult_cloud`.
- The cloud receives the brief plus files expanded from `refs` — **never the full conversation history**.
- Three kinds of local state: `artifacts/` (full deliverables), `state/` (quota and estimator coefficients), and the files inside the refs allowlist.

### 2.2 Module structure

![Module structure](docs/structure.svg)

Three layers, all in one process:

| Layer | Modules | Responsibility |
|---|---|---|
| Decision | `guard.py` / `prompts.py` / `keys.py` / `refs.py` | May this call proceed, is the brief good enough, whose key is this, may this file leave |
| Context | `context.py` / `spine.py` / `artifacts.py` | Measure the window, trim, keep a spine, archive and reclaim |
| Transport | `upstream.py` / `cloud.py` / `responses.py` | Two upstream clients plus protocol conversion |

Why not split this into an MCP server — see `ARCHITECTURE.md`: the tool loop has to live in the gateway, because only the gateway knows both **how much the client sent** and **how much the local model can actually hold**. That information gap is the premise of the whole design, and an MCP server does not have it.

### 2.3 Request flow

![Request flow](docs/flow.svg)

How to read it:

- **Solid** lines are the normal path; **dashed** lines are paths that spend nothing (trim, reject, blocked by a gate).
- Trimming happens **inside the loop**, before every request — deliverables and tool results grow mid-loop. Checking only at the entrance means round four blows up.
- The cloud call is an optional step inside the loop. If the model does not call it, the turn ends there.

> All three diagrams are generated by `scripts/gen_diagrams.py`. Change a module or a flow and re-run it; the pictures cannot drift behind the code. The generator self-checks three things: text overflowing its box, elements outside the canvas, arrows crossing boxes.

---

## 3. Three key designs

### 3.1 When to delegate: judge facts, not "complexity"

The first criterion was "use it only when the task is genuinely complex and local attempts still leave you unsure". The problem: **"complex" is subjective**, so the model cannot decide with it and ends up calling on vibes. The prohibition list also contained "you already know the answer and just don't feel like typing" — a judgement no model can honestly make about itself.

Replaced with four questions it can actually answer:

| Criterion | Meaning |
|---|---|
| Do I have enough premises to decide? | If not, go look it up; don't ask with half the information |
| Can I write the steps with failure criteria right now? | If yes, write them yourself |
| Is the cost of being wrong irreversible? | Data migration / bulk delete / production change — **must ask**, even when confident |
| Have I actually tried and failed? | Try first; ask with the real error |

Plus two mechanisms:

- **Quality gate**: empty `ask` or over 400 characters, five-line format without `goal`, `tried` set to `none` for `diagnosis`/`review`, or plain prose under 40 characters — sent back for a rewrite, **not counted, not charged**. At most two rejections per turn, to stop it spinning in place.
- **Visible quota**: "how many calls are left today" is written into the system message every turn. Saying it only after a rejection means that turn was already wasted.

The bar is deliberately loose: badly formatted but sufficient content **passes**. The cloud can work from prose; an extra round trip just burns local time.

**Injection cost is capped by tokens.** This gateway wraps the agent's own prompt, so every injected token is pure addition. Per-turn fixed injection is held at **531 tokens**:

| Component | Cost |
|---|---|
| system positioning (one line of attitude) | 21 tok |
| quota note (changes every turn) | 12 tok |
| tool definition (description + parameter schema) | 498 tok |

`tests/test_prompts.py` asserts hard upper bounds. Before trimming it was 804 — what got cut was duplication (positioning stated in both system and tool description, the five-line brief format written twice) and meta-commentary explaining its own structure. Not one criterion was removed.

### 3.2 Context governance: the local window is the real bottleneck

Four steps, in the order they happen.

**Probe the real window.** `/props` → `/v1/models` → `/api/v0/models` → config → conservative default, tried in that order, and `total_slots` is read because `--parallel` divides `n_ctx`.

**Count exactly.** Character heuristics underestimate code and JSON by 25–40% (`"1234567890"` is 10 tokens). So the upstream `/tokenize` is preferred for exact values; estimation is only a fallback, self-calibrated by EWMA against every returned `usage.prompt_tokens`.

**Trim in levels.** Cheapest first; the first four never call a model (level 3 writes to disk but runs no inference):

| Level | What is touched | Recoverable |
|---|---|---|
| 1 | Reasoning content from earlier turns | Yes (already spent) |
| 2 | Images replaced with placeholders | No |
| 3 | Old tool output → head + tail + error lines, full text archived | Yes (read the file) |
| 4 | Whole `tool_call` + result pairs dropped, oldest first | Yes (archived) |
| 5 | Hard truncation | No |
| 6 | Structural repair (drop orphan tool messages, strip dangling `tool_calls`) | — |

Level 6 was added later: hard truncation deletes by position and ignores pairing, so it can delete an assistant message carrying `tool_calls` while keeping its tool result. llama.cpp tolerates that; vLLM and strict Qwen templates return **400**.

Also added: **the first user message must survive** — that is the task itself. Only the last user message used to be protected, so hard truncation would delete the goal outright and leave the model reconstructing its purpose from a chunk of test logs. That defect was reproduced on real data; there is now a regression test watching it.

**Task spine.** Trimmed to 10%, all the model holds is one line saying "226 messages were compressed". It knows there is a gap but not what is in it, so it either retries a rejected approach or invents one with "as we discussed earlier". So when a large region is dropped, the gateway asks the local model to distill it into six lines:

```
goal: change this service's quota from per-call to per-token, and add tests.
constraints: key format must not change
facts: prefill measured 1305 tok/s; single slot n_ctx=131072
rejected: Redis for counting (ops will not approve)
current: guard.py done, tests not run
next: add metering cases to test_guard.py
```

`rejected` and `facts` are the valuable ones: lose the first and the model retries a known failure; lose the second and it re-measures. Neither can come from a mechanical summary — that requires judging which sentence is a conclusion and which is process. So a model really is called here: measured 7–13 seconds, at most once per request.

### 3.3 Deliverable archiving and reclamation

A deliverable over 1,500 characters is **written to disk first**, and only a ~500-token digest goes back into context (id + absolute path + heading outline + first 800 characters). About 500 tokens replacing 30,000.

The point is not the digest, it is that **the path lives in the agent's own filesystem** — it has file-reading tools and can go look when it needs detail.

Three constraints learned from long use:

- **Uniform header**: every file starts with `kind / turn / created / chars`. Readable by humans, parseable by scripts.
- **Path contains only date + kind + content hash**: the turn goes in the header only. Otherwise the same content is stored once per turn.
- **Batching and reclamation**: one trim can compress hundreds of tool outputs; writing each separately produced 113 files. Merged into one file with line anchors it is **2**, same total size. Reclamation decides by the **date in the directory name**, not mtime — a directory that is appended to daily would otherwise always look new and never be deleted.

---

## 4. Quick start

```bash
git clone https://github.com/liuda1999/steward.git
cd steward

# 1. dependencies (Python 3.11+)
pip install -r requirements.txt

# 2. start with the shipped example config
python -m steward serve

# 3. open http://127.0.0.1:27418/, fill both slots, save, restart
```

**Start with the console** — no YAML editing: type the URL, model name and API key, hit "test connection" to prove it answers, then save. Keys go into the adjacent `.env` (`config.yaml` keeps the `${BASE_API_KEY}` placeholder) and every comment in the config file survives.

The CLI is still there, and equivalent:

```bash
python -m steward init          # interactive wizard, good for one-shot setup
python -m steward serve --check # validate the config without starting
```

No local model yet? Point both `base_url`s at any OpenAI-compatible endpoint; `python -m steward test` tells you whether they answer.

Once running:

```bash
python -m steward test          # connectivity of both slots
python -m steward ctx           # real window and input budget
```

The client side needs two lines:

```python
base_url = "http://127.0.0.1:27418/v1"
api_key  = "st-..."                 # printed once by init, only the hash is stored
```

**The key is shown exactly once.** Lose it and issue a new one, revoke the old — the gateway stores only a hash and cannot recover it. The console's *project key* panel issues and revokes them, with the same one-time display.

On first connection, run `python -m steward ctx`: it prints the real window, the input budget and the counting method. Most "the model suddenly got dumb" problems are visible in those three numbers.

### 4.1 Port

Default **27418**. Three rules, look at them before changing it:

| Rule | Why |
|---|---|
| Not 80 / 443 / 8080 / 8000 / 3000 | Something usually owns those already; a collision reports only `address in use` |
| Not a sequential number like 12345 / 56789 | When two services collide you cannot tell whose port it is |
| Between 1024 and 49151 | Below 1024 needs root; above 49151 is the Windows ephemeral range, where random outbound connections grab it |

A bad port does not block startup, but it is flagged:

```
[config] note  server.port=8080 is a common port and is easily taken by another service; consider 27418
```

A real collision is louder than `shutdown complete`:

```
[port] 127.0.0.1:27418 is already in use (PID 13028, tasklist /FI "PID eq 13028" shows the name).
[port] Stop it, or pass --port to use another (default 27418, or edit server.port in config.yaml).
```

Temporary override without editing config: `python -m steward serve --port 27xxx`.

### 4.2 Two slots — you may fill just one

`base` and `advanced` are equal slots, not "local" and "cloud".

| Configuration | Result |
|---|---|
| Both filled | Full capability. Daily work on base, hard problems on advanced after the model asks |
| Only base | **Legal.** Pure-local mode: `consult_cloud` is not injected, saving ~500 tokens per turn |
| Only advanced | Will not start. Without a base model there is no model at all |
| Half filled (URL but no model name) | Treated as an empty slot, with a warning at startup |

An empty slot is **deliberately legal**, because "I only have one local model" is the most common situation. The gateway will not hand the model a tool that is guaranteed to refuse it — those 500 tokens are paid again every turn.

```bash
python -m steward serve --check -c config.yaml
# [config] note  advanced slot is empty (missing advanced.base_url): consult_cloud not injected… saves ~500 tokens per turn
# [config] OK  base=http://127.0.0.1:11434/v1 model=qwen3 protocol=chat
# [config]     advanced=empty slot (consult_cloud not injected)
```

`GET /health` reports the same state as `advanced.configured: false`.
### 4.4 Two numbers that look cryptic

**What "trim to 0.65 of the budget" means.**

Trimming does not mean "fit exactly in the window". It means **cut down to 65% of the window**, leaving 35% headroom. The chain is:

```
usable window × usable_ratio(0.85) − output reserve − safety margin × learned shrink factor  =  budget
budget × target_ratio(0.65)                                                              =  trim target
```

Why not trim to exactly fitting? Because the next turn grows — the model's answer and the tool results go back into the window. Trim to 100% and the next turn overflows immediately, so you trim again; and every trim invalidates llama.cpp's **prefix cache**, forcing the whole history to be pre-filled again (measured at ~1300 tok/s). Leaving 35% headroom means several turns fit before the next trim.

Tuning it:

| Value | Effect |
|---|---|
| Higher (0.75–0.85) | Shallower, more frequent trims; less recompute each time, but the cache is invalidated more often |
| Lower (around 0.5) | One deep cut; the gap between trims stretches, but each recompute is bigger |

It pairs with `min_clear_tokens` (8000 by default): a trim must free at least that many tokens to be worth doing, otherwise the history stays — **no trim means the cache survives**. Hence the policy is "trim rarely, but trim properly".

**How long do we wait by default.**

| Where | Default | Why |
|---|---|---|
| Base model `base.timeout_s` | **600 s** | A local 30B is slow to first token on long context |
| Advanced model `advanced.timeout_s` | **120 s** | The usual ceiling for a cloud API |
| Connect timeout | **15 s** | Same for both; the handshake should fail fast rather than hang |
| Spine generation | 30 s | Internal call, does not affect the main request |

What a timeout means depends on how far the request got:

- **Never connected** (service not running, wrong address) → fails immediately and **the quota is refunded**, because nothing reached the upstream;
- **Connected but no result** (the model was too slow) → waits out the timeout and **the quota is not refunded**, because the upstream may already be generating and billing.

So an accidental timeout never burns quota for free — at worst you wait after having already paid. To calibrate, watch the `X-Steward-Cloud-Ms` response header: it is how long the advanced model actually took that turn. Both timeouts are editable in the console.

> Fixed a misleading problem along the way: with `HTTP_PROXY` set, even loopback addresses were sent to the proxy, so "the model is not running" showed up as "timeout". Local and private addresses now connect directly; public ones still use the proxy.


### 4.3 The console

The same port also serves a UI: open `http://127.0.0.1:27418/`. The API lives under `/v1/*`, the console at `/`; they do not interfere.

What it edits:

| Panel | What you can change |
|---|---|
| Base / advanced slot | URL, model name, upstream key, protocol, thinking — each slot has a **test connection** button, so you confirm it answers *before* saving |
| Quota | Per-request cap, daily total, minimum interval |
| Context | Window cap, trim target, spine toggle |
| Archive / refs | Retention days, size cap, outbound toggle and allowlist |
| Listener | Host and port |
| Project keys | Issue, list, revoke (plaintext shown once) |

Getting in needs a passphrase, printed in the startup banner and stored in `state/console.token`:

```
  console http://127.0.0.1:27418/  (slots, quota, keys)
  pass    CP5jJsFQ45yUZRBKuGW_tOPj
```

It stays the same across restarts, so a browser session survives one. Delete the file if you want a new one.

**Why not "open and edit".** This page rewrites configuration and reveals whether a key is set. Sitting on loopback does not protect it from two things: another web page POSTing cross-site (any tab can reach `127.0.0.1:27418`), and other scripts on the same machine. Hence:

- **The passphrase travels in a custom `X-Console-Token` header.** A custom header triggers a CORS preflight cross-origin, and we send no CORS headers, so the browser blocks it outright — a harder guarantee than cookies. The cookie is only a convenience in ordinary browsers (no retyping after a reload); environments without cookie storage are unaffected;
- Write endpoints check `Origin`: same-origin, a loopback origin, and a sandboxed iframe's `null` are allowed, everything else is a 403. The case that matters, `https://evil.example`, can never look like any of those;
- Eight wrong passphrases lock it for a minute;
- The API only ever reports *whether* a slot has a key, never the key.

**What happens on save** (all three on the server; the page is just a form):

1. **Values are replaced line by line, comments preserved.** Rewriting the whole file would turn `config.yaml` from documentation into machine output.
2. **Keys go to `.env`, not YAML.** The config keeps `${BASE_API_KEY}` — copying a config around should not carry two upstream keys with it. An empty field means "leave it alone"; otherwise every save would wipe the existing key.
3. **Validate before writing.** The candidate goes through the same `validate()` the startup path uses; if it fails, the old file stays untouched and the reason is shown. A config that saves but cannot start is worse than one that refuses to save.

**A save needs a restart** to take effect:

```bash
# Ctrl+C, then
python -m steward serve
```

There is no supervisor, and the gateway cannot restart itself in-process — it holds the port, so the replacement would fight it for the same socket. The console says so, and shows the config path.


---

## 5. Layout

```
.
├── steward/                   the gateway (14 modules)
│   ├── config.py              config parsing + validate
│   ├── keys.py                project keys (hash only)
│   ├── prompts.py             tool description · quality gate · spine/digest templates
│   ├── guard.py               four gates + dedupe + quota persistence
│   ├── upstream.py            base-model client (404 self-heal, window probe)
│   ├── cloud.py               advanced-model client (deliverable externalization)
│   ├── responses.py           Responses ↔ Chat conversion
│   ├── context.py             window probe / exact counting / tiered trimming
│   ├── spine.py               task spine
│   ├── artifacts.py           archive · batching · reclamation
│   ├── refs.py                local file allowlist and audit trail
│   ├── webconsole.py          web console: routes, config writing, session
│   ├── web/console.html       console page (single file, no external deps)
│   ├── server.py              FastAPI routes, auth, tool loop
│   ├── __main__.py            serve / init / test / key / ctx / refs / artifacts
│   ├── envfile.py             .env reading and writing (init + console)
│   ├── netpolicy.py           net policy: direct for local, proxy for public
│   └── README.md              implementation notes and troubleshooting (Chinese)
├── docs/                      three diagrams, generated by scripts/gen_diagrams.py
├── scripts/                   integration tests, live checks, diagram generation, publishing
│   └── publish.py             push to GitHub: git first, Git Data API as fallback
├── tests/                     413 cases
│   └── fixtures/              363-message history sample used by the audit
├── legacy/                    archives of two earlier generations, not recommended
├── ARCHITECTURE.md            decisions and the alternatives that lost (Chinese)
├── CONTEXT-DESIGN.md          the context-governance argument (Chinese)
├── HANDOFF-PROTOCOL.md        original definition of the brief and the four-part reply (Chinese)
│
├── state/                     runtime state (not in the repo)
└── .steward/artifacts/        deliverables and tool output (not in the repo, reclaimable via CLI)
```

---

## 6. Commands

| Command | Purpose |
|---|---|
| `python -m steward serve` | start the gateway |
| `python -m steward init` | configure both slots and issue the first key |
| `python -m steward test` | connectivity of both endpoints |
| `python -m steward ctx` | real window / budget / counting method |
| `python -m steward key new --label <name>` | issue a key |
| `python -m steward key list` / `revoke` | list / revoke |
| `python -m steward refs <path…>` | would this path pass the refs policy? |
| `python -m steward artifacts` | archive usage; `--clean` reclaims |
| `python -m steward --version` | version |
| `python scripts/publish.py --doctor` | diagnose publishing (certificates / connectivity / token / dry run) |
| `python scripts/publish.py -m "msg"` | commit and push, environment-agnostic |
| `python scripts/publish.py --release vX.Y.Z --notes-file …` | tag and publish a release |

### 6.1 Why publishing needs a script

`git push` often fails behind a proxy, and the error points you the wrong way:

| Error | Real cause |
|---|---|
| `CRYPT_E_NO_REVOCATION_CHECK` | the proxy is doing TLS interception and git cannot check revocation |
| `unable to get local issuer certificate` | same thing, as reported by the openssl backend |
| `CONNECT tunnel failed 502` | the proxy allows api.github.com but not github.com:443 |
| `Authentication failed` | `Authorization: bearer` is API-only; git needs `x-access-token:` in the URL |

`scripts/publish.py` wraps all of it: it finds a CA bundle that **actually connects** at runtime (no hardcoded path, no edits to `.git/config`), and falls back to the Git Data API when git cannot get through — same result, and the tree hash lets you verify it.

```bash
STEWARD_GH_TOKEN=xxx python scripts/publish.py --doctor   # inspect the environment
STEWARD_GH_TOKEN=xxx python scripts/publish.py -m "msg"   # commit and push
```

The token is read from an environment variable or `--token`, never written to disk.

---

## 7. Configuration cheat sheet

Fully commented version in `steward/config.yaml`.

```yaml
base:          # local, the daily driver. Any OpenAI-compatible endpoint
  base_url: http://127.0.0.1:8080/v1
  model: /models/your-model.gguf
  thinking: "off"           # turn off for reasoning models, or tool calls stall

advanced:      # cloud, used only for consult_cloud
  base_url: https://your-provider.example.com/v1
  model: your-advanced-model
  max_tokens: 4096

guard:
  max_calls_per_turn: 2     # per request
  daily_limit: 5            # per day (the last line of defence)
  min_interval_s: 30
  dedupe: true

context:
  ctx_limit: 0              # 0 = probe automatically
  target_ratio: 0.65        # how far below the budget one trim goes
  min_clear_tokens: 8000    # clear at least this much, or the KV-cache hit is not worth breaking
  spine: true               # generate the six-line spine after trimming

artifacts:
  dir: .steward/artifacts
  max_age_days: 30
  max_total_mb: 512

refs:          # let the cloud read local files. Off by default — the only outbound channel
  enabled: false
  roots: []                # enabled with no allowlist refuses to start
```

**refs is the only channel that sends local content out.** Off by default, root allowlist, 25 deny patterns, 64 KB per file / 120K characters per call, one audit line per expansion. It is off by default on purpose: using it correctly requires that you have checked `roots`.

---

## 8. Tests

```bash
python -m pytest tests -q                    # 413
python -m pytest tests/test_audit.py -s -q   # just the comparison tables
python scripts/ctx_live.py --fit 130000 --spine   # one real trim + spine against a live model
```

Three groups worth explaining:

**`test_audit.py` (23) is not "run the unit tests again".** It carries the pre-fix implementation into the test suite as a control group and runs the same data through both. Three comparisons plus three meta-checks (feed the old implementation to the judge; it must be graded as failing) ensure the audit is not "written but unable to detect".

**`test_reliability.py` (20)** targets failure paths: ten concurrent requests racing for three slots and exactly three succeeding, malformed tokens, upstream exceptions, a `usage` field that is a list, a client that already has a same-named tool. All three fixes were verified by regression injection — revert the fix and the test must go red.

**`test_prompts.py`** turns injection cost into hard assertions: fixed injection ≤560 tokens per turn, tool description ≤240, plus no-duplication rules such as "the five-line format is stated once" and "system does not restate the tool".

Measured on real hardware (27B / `n_ctx=131072` / single slot / prefill 1305 tok/s):

```
363 messages / 159,239 tokens
  → trimmed to 68,926, one prefill, HTTP 200, 62.1s
  → upstream counted 61,126 (budget 106,803)
```

---

## 9. Documentation index

**Current**

| Document | Contents |
|---|---|
| `steward/README.md` | implementation detail: how each module works, what each parameter means, troubleshooting (Chinese) |
| `ARCHITECTURE.md` | why it is built this way: decisions and rejected alternatives (Chinese) |
| `CONTEXT-DESIGN.md` | full analysis of the window problem and the L0–L5 design, with P0–P3 delivery notes (Chinese) |
| `HANDOFF-PROTOCOL.md` | original definition of the BRIEF and the four-part reply; `prompts.py` is its simplified form (Chinese) |
| `README.md` | this document, in Chinese |

**`legacy/`: archives of two earlier generations**

The project had two predecessors, neither of which is this thing:

1. **Generation one wrapped a third-party MCP server** (`cli-bridge-mcp`) exposing `ask_mycloud` / `ask_cascade`, with quota controlled by a `DAILY_LIMIT` environment variable. The problem: the guardrail could only count calls, not judge whether the work deserved the cloud, and the tool definition occupied context permanently. See `legacy/MCP-SETUP.md`, `legacy/EXTERNAL-AI-RULES.md`.
2. **Generation two was a self-built gateway** called `cloudbridge`. The name only said "connected to the cloud", while the work was budgeting, windowing and archiving — three steward duties. It outgrew "bridge", so it was renamed `steward`.

The L0/L1/L2 escalation criteria in `legacy/ESCALATION-RULES.md` still have reference value (absorbed into the trigger conditions in `prompts.py`); `legacy/VENDOR-SURVEY.md` is a vendor survey whose conclusions are stale, though its analysis of "gateway decides vs model decides" still holds.

Those files are kept to explain **why things are the way they are**, not as recommended usage.

---

## 10. Known trade-offs

**Internal non-streaming.** Time to first token equals total turn time. In exchange you never get "half an answer, a tool call appears, start over".

**The loop is single-request only.** The gateway is stateless and keeps no cross-request context; the client owns the history. So window governance is computed per request — if the client blows up the history itself, the gateway can only trim what it receives.

**The spine is a summary, not a backup.** Dropped error text and code fragments do not come back, though they are mostly on disk. The real risk is the model **inventing** the missing part, so the spine always ends with "re-confirm details with tools instead of guessing".

**Trimming costs a prefix recompute.** Rewriting the middle of the history invalidates the llama.cpp prefix cache. Hence "trim rarely but deeply", with `min_clear_tokens` making each trim worth its cost. If your prefill is far faster than 1305 tok/s, raise `target_ratio` toward 0.8 and trim more finely and more often.

**Console changes need a restart.** The gateway does not supervise itself, and cannot restart in-process — the new process would fight the old one for the same port. The console writes the config correctly and safely; applying it is the command after `Ctrl+C`.

**Silent truncation is only detectable after the fact.** Upstream returns 200 and the content looks fine; only a `prompt_tokens` well below the estimate gives it away. Once detected the gateway permanently shrinks the budget, but that turn already lost information.

**The archive is not encrypted.** It holds full deliverables and raw tool output, possibly including source fragments. Directory permissions follow the gateway process umask.

**On Windows `chmod 0600` is a no-op** (`keys.json` uses the default ACL), but the file holds only hashes, so a copy of it yields no key. **`state/usage.json` assumes a single process**; two instances pointing at the same file will overwrite each other's counts.

---

## 11. License

**Apache License 2.0.** Full text in [LICENSE](LICENSE); copyright and third-party notes in [NOTICE](NOTICE).

It was chosen over MIT for one practical difference: **the patent grant**. This project talks to model services and inference frameworks, and its users may sit inside companies with patent portfolios. MIT is silent on patents — silence is not safety, it is asking every user's legal team to guess. Apache-2.0 says it plainly: contributors grant you a license to their necessary patents, and if you sue the project over patents, that license terminates.

What you may do:

| | |
|---|---|
| Commercial use | Yes — no fee, no notification required |
| Modify | Yes — you do not have to open-source your changes |
| Closed-source distribution | Yes — embed it in internal systems or commercial products |
| Private deployment | Yes — unlimited machines |

Three obligations: keep [LICENSE](LICENSE) and [NOTICE](NOTICE), state that modified files were changed, and do not use the project name to endorse your derivative (trademark is not covered by the grant).

Every source file begins with `SPDX-License-Identifier: Apache-2.0` so tooling can detect it automatically.
