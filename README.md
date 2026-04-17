# SysOp — Virtual DevOps Engineer

SysOp is a Slack bot that acts as a conversational DevOps engineer. It runs on your local machine, takes requests over Slack (Socket Mode — no public URL needed), and drives the Claude Code CLI (Max subscription) to inspect Kubernetes clusters, read and modify a GitOps platform repo, create PRs, and run mutating `kubectl` commands. Every potentially destructive action is gated behind an interactive Slack approval button. Real-time progress is streamed back as an editable status message in the thread so you can see exactly what Claude is doing as it works.

This is a POC — one bot, one cluster, runs on a dev machine. The full SysOp system will enforce PR-only mutations and support multiple personas; this POC intentionally allows direct `kubectl` writes with human gates.

> **⚠️ Security posture.** This bot runs Claude Code with `--permission-mode bypassPermissions`. The only thing between a prompt-injected Claude and arbitrary command execution is (a) the PreToolUse hook in `hooks/pre_tool_gate.py` and (b) the Kubernetes RBAC bound to the bot's kubeconfig. Read [SECURITY](#security) below before pointing this at anything you cannot afford to lose. In particular: the bot inherits the host user's environment (`~/.ssh`, `~/.kube`, shell env), so run it under a dedicated unix user in a sandbox if you intend to leave it running unattended.

---

## Architecture

```
User (Slack)
    │  DM or @mention
    ▼
┌─────────────────────────────────────────────────┐
│  SysOp Bot  (Python, asyncio, Slack Bolt)          │
│                                                  │
│  • Socket Mode handler (no public URL)           │
│  • Thread → Session mapping                      │
│  • GateManager (Unix socket server)              │
│  • StatusMessage (editable Slack progress msg)   │
│  • AuditDB (SQLite)                              │
└──────────────┬──────────────────────────────────┘
               │  subprocess per conversation thread
               ▼
┌─────────────────────────────────────────────────┐
│  Claude Code CLI  (--output-format stream-json)  │
│                                                  │
│  • persona/CLAUDE.md sets identity and rules     │
│  • cwd = persona/  (picks up settings.json)      │
│  • --resume <id> for follow-up messages          │
│  • PreToolUse hook fires before every Bash call  │
└──────────────┬──────────────────────────────────┘
               │  stdin: hook_input JSON
               ▼
┌─────────────────────────────────────────────────┐
│  hooks/pre_tool_gate.py  (hook bridge)           │
│                                                  │
│  • Classifies command: allow / gate / deny       │
│  • For "gate": connects to bot via Unix socket   │
│  • Blocks until bot relays Slack decision        │
│  • Returns exit code: 0=allow  1=error  2=deny   │
└──────────────┬──────────────────────────────────┘
               │  Unix socket ($SYSOP_SOCKET_PATH)
               ▼
┌─────────────────────────────────────────────────┐
│  GateManager (inside SysOp Bot)                    │
│                                                  │
│  • Posts "Action requires approval" in Slack     │
│  • [Approve] [Deny] Block Kit buttons            │
│  • 5-minute timeout → auto-deny                  │
│  • Returns "approved" or "denied" to hook        │
└─────────────────────────────────────────────────┘
```

### Stream-JSON Live Updates

Claude runs with `--output-format stream-json --verbose`, emitting a line-delimited JSON event for every tool call, hook invocation, and text chunk. The bot reads these in real time and edits a single Slack status message in place:

```
Thread layout while Claude is working:

  ⏳ Working...
  • Session started...
  • Running: `kubectl get pods -n production`
  • Checking permissions...
  • Running: `kubectl describe deployment api-server -n production`

  ⚠️ Action requires approval:        ← gate message (separate)
  kubectl scale deployment/api-server --replicas=3 -n production
  [Approve]  [Deny]

  Here is what I found: ...            ← final response (separate)
```

The status message accumulates up to 20 lines (older lines are collapsed into a "… (N earlier steps)" header). `tool_use` events force-flush immediately; all others debounce at 1 second to stay inside Slack rate limits. After the session finishes the header changes to `Done` or `Failed`.

---

## Quick Start

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) — `pip install uv` or `curl -Ls https://astral.sh/uv/install.sh | sh`
- Claude Code CLI with an active Max subscription — `npm install -g @anthropic-ai/claude-code`
- `kubectl` on `$PATH`, configured to reach your cluster
- A Slack workspace where you can create apps

### 1. Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
2. **Socket Mode**: Settings → Socket Mode → Enable. Copy the generated `xapp-…` app-level token.
3. **Bot scopes**: OAuth & Permissions → Bot Token Scopes → add:
   `chat:write`, `app_mentions:read`, `im:history`, `im:read`, `im:write`,
   `reactions:write`, `reactions:read`, `channels:history`
4. **Events**: Event Subscriptions → Enable → Subscribe to bot events:
   `app_mention`, `message.im`
5. **Interactivity**: Interactivity & Shortcuts → Enable (URL can be anything — Socket Mode overrides it).
6. **Install**: Install to Workspace → copy the `xoxb-…` bot token.

### 2. Kubernetes RBAC

SysOp's RBAC is deliberately split in two:

1. A **cluster-wide read** ClusterRole for observability (pods, deployments, configmaps — but *not* Secrets cluster-wide).
2. A **namespace-scoped write** Role that must be bound, per namespace, to each namespace you want the bot to mutate.

Apply the base RBAC (service account + read role) and then enroll each write namespace:

```bash
# Apply base RBAC (requires cluster-admin)
kubectl apply -f k8s/sysop-rbac.yaml

# Enroll a namespace for mutations: copy the write Role + RoleBinding from
# k8s/sysop-rbac.yaml, replace `REPLACE_ME` with your namespace, and apply.
# Repeat for every namespace the bot should be allowed to write in.
#
# The default layout intentionally does NOT grant pods/exec, pods/portforward,
# cluster-wide Secrets write, or deployments/rollback. Those are the fastest
# cluster-compromise primitives and should be added back only deliberately,
# in a separate Role, bound to a specific namespace.

# Generate a scoped kubeconfig for the bot
KUBECONFIG=/path/to/admin.yaml ./k8s/generate-kubeconfig.sh sysop-bot.kubeconfig
```

Move `sysop-bot.kubeconfig` somewhere permanent and use that path in `config.yaml`.

### 3. Configuration

```bash
cp config.yaml.example config.yaml
```

Create a `.env` file (gitignored) with your secrets:

```bash
SLACK_APP_TOKEN=xapp-1-...
SLACK_BOT_TOKEN=xoxb-...
```

Edit `config.yaml` — at minimum set `kubeconfig` and `git_repo_path`:

```yaml
slack:
  app_token: ${SLACK_APP_TOKEN}
  bot_token: ${SLACK_BOT_TOKEN}

kubeconfig: /home/you/.kube/sysop-bot.kubeconfig
git_repo_path: /path/to/your/platform-repo
git_branch: main
github_bot_user: sysop-bot
```

### 4. Install and Run

```bash
uv sync
uv run sysop
```

To have the bot announce itself on startup and shutdown, set:

```bash
export SYSOP_NOTIFY_CHANNEL="#ops-bots"
uv run sysop
```

---

## Configuration Reference

All values support `${ENV_VAR}` substitution. The config file path is read from `$SYSOP_CONFIG` (default: `./config.yaml`).

| Key | Description | Default |
|-----|-------------|---------|
| `slack.app_token` | Socket Mode app-level token (`xapp-…`) | — required |
| `slack.bot_token` | Bot OAuth token (`xoxb-…`) | — required |
| `kubeconfig` | Path to kubeconfig file passed to every `kubectl` call | — required |
| `git_repo_path` | Absolute path to the GitOps platform repo on disk | — required |
| `git_branch` | Branch Claude works on for commits and PRs | `main` |
| `github_bot_user` | GitHub username for commits | `sysop-bot` |
| `openbrain.mcp_config` | Path to MCP config JSON for openbrain. Leave empty to inherit from `~/.claude/settings.json` | `""` |
| `audit.db_path` | SQLite database file for the audit log | `./sysop_audit.db` |
| `gates.timeout_seconds` | Seconds to wait for a Slack approval before auto-denying | `300` |
| `gates.require_initiator_approval` | Only the user who sent the message can click Approve | `true` |
| `gates.kubectl_read_commands` | kubectl subcommands always allowed without a gate | `[get, describe, logs, top, explain, api-resources]` |
| `gates.kubectl_deny_commands` | kubectl subcommands unconditionally blocked | `[delete namespace, delete clusterrole, delete clusterrolebinding, delete pv, delete node]` |
| `gates.bash_read_allowlist` | Non-k8s shell commands always allowed (`ls`, `cat`, `grep`, …). Everything not on this list is gated. | see `sysop/config.py` |
| `gates.bash_deny_commands` | Shell commands unconditionally blocked (`shutdown`, `mkfs`, …) | `[shutdown, reboot, halt, poweroff, mkfs, …]` |
| `claude.max_turns` | `--max-turns` passed to Claude Code CLI | `50` |
| `claude.persona_claude_md` | Path to the persona CLAUDE.md file | `./persona/CLAUDE.md` |
| `claude.persona_dir` | Override for persona directory (use when running from an installed wheel) | `""` (use in-tree layout) |
| `claude.hooks_dir` | Override for hooks directory | `""` (use in-tree layout) |
| `session.idle_timeout_seconds` | Time before an idle session's subprocess is cleaned up | `1800` |
| `session.socket_dir` | Directory for per-session Unix sockets | `/tmp/sysop` |
| `session.max_queue_per_thread` | Max pending requests per Slack thread before rejecting | `3` |

---

## Command Classification

Every Bash tool call Claude makes passes through `hooks/pre_tool_gate.py` before it executes. Commands fall into one of three tiers:

The classifier tokenises the command (shlex + pipeline-aware splitter), walks each segment, and picks the decision from the strictest segment (deny > gate > allow). It is resistant to the common bypass shapes: whitespace padding, interleaved flags (`kubectl -n foo delete namespace bar`), command substitution (`echo $(kubectl apply ...)`), and variable-expansion exec (`K=kubectl; $K delete namespace prod`).

### Always Allowed (exit 0, no prompt)

- `kubectl get`, `kubectl describe`, `kubectl logs`, `kubectl top`, `kubectl explain`, `kubectl api-resources`
- `git status`, `git log`, `git diff`, `git show`, `git branch`, `git remote`, `git tag`, `git reflog`, …
- Read-only shell commands on the `gates.bash_read_allowlist` (defaults: `ls`, `cat`, `echo`, `grep`, `head`, `tail`, `jq`, `awk`, `sed`, …) on non-sensitive paths

```bash
kubectl get pods -n production          # allowed
kubectl describe deployment api-server  # allowed
git log --oneline -10                   # allowed
cat /tmp/output.txt                     # allowed
```

### Gated (requires Slack approval)

- Any mutating `kubectl`: `apply`, `delete pod`, `scale`, `rollout restart`, `patch`, `exec`, `port-forward`
- `git commit`, `git push`, `git checkout` (write ops)
- Any `helm` command (install, upgrade, uninstall, rollback)
- PR creation via `gh` CLI
- **Anything not on the read allowlist**: `curl`, `wget`, `docker`, `terraform`, `python3 -c`, `npm`, `chmod`, `rm <single-file>`, `cp`, `mv` — all gate by default. Explicit user approval is the only way to let Claude invoke an unknown tool.
- Commands containing `$(...)` or backticks (dynamic execution that the classifier can't safely introspect)

```bash
kubectl apply -f manifest.yaml             # gate → Slack prompt
kubectl scale deployment/api --replicas=3  # gate
kubectl delete pod api-7f8b9-x2k4n         # gate
git push origin feature-branch             # gate
helm upgrade my-chart ./chart              # gate
curl https://example.com                   # gate (unknown tool)
rm /tmp/foo.txt                            # gate (rm single-file)
```

### Always Denied (exit 2, no prompt)

Commands that can cause unrecoverable damage or exfiltrate credentials are blocked unconditionally — no Slack button is shown:

```bash
kubectl delete namespace production      # denied
kubectl delete clusterrole admin         # denied
kubectl delete clusterrolebinding admin  # denied
kubectl delete pv my-volume              # denied
kubectl delete node worker-1             # denied
rm -rf $HOME                             # denied
rm -rf /                                 # denied
cat ~/.ssh/id_rsa                        # denied (sensitive path)
cat /etc/shadow                          # denied
cat ~/.aws/credentials                   # denied
shutdown -h now                          # denied
mkfs.ext4 /dev/sda1                      # denied
```

---

## RBAC Permissions

`k8s/sysop-rbac.yaml` splits permissions into two layers:

- **`sysop-bot-read`** — a ClusterRole bound cluster-wide. Pods, deployments, configmaps, services, ingresses, jobs, pod logs, metrics, CRDs. **Secrets are *not* included** so a compromised bot cannot exfiltrate them cluster-wide.
- **`sysop-bot-write`** — a namespace-scoped Role bound per namespace via RoleBinding. Apply this Role + RoleBinding only to the namespaces the bot should mutate in. Secret *read* is granted here (not cluster-wide).

### Can Do (cluster-wide)

| Resource | Verbs |
|----------|-------|
| Pods, ConfigMaps, Services, ServiceAccounts, Endpoints, Events, Namespaces, Nodes, PVs, PVCs, ResourceQuotas, LimitRanges | `get`, `list`, `watch` |
| Pods/log | `get` |
| All `apps`, `batch`, `networking.k8s.io`, `autoscaling`, `policy` resources | `get`, `list`, `watch` |
| CRDs, metrics | `get`, `list` |

### Can Do (only in enrolled namespaces — via the `sysop-bot-write` RoleBinding)

| Resource | Verbs |
|----------|-------|
| Secrets | `get`, `list` |
| Pods | `delete` (restart-via-delete) |
| Deployments, Deployments/scale | `patch`, `update` |
| ConfigMaps | `create`, `patch`, `update`, `delete` |
| Services, Ingresses | `create`, `patch`, `update`, `delete` |
| HorizontalPodAutoscalers | `create`, `patch`, `update`, `delete` |
| Jobs, CronJobs | `create`, `patch`, `update`, `delete` |

### Intentionally *not* granted

| Resource | Why |
|----------|-----|
| `pods/exec`, `pods/portforward` | Arbitrary shell / network access in any pod is a cluster-compromise primitive — grant explicitly in a separate Role if you need it. |
| `secrets` write (create/update/patch/delete) | Secret *write* is outsized blast radius for a chatbot. Push secret changes through Sealed Secrets or External Secrets Operator. |
| `deployments/rollback` | Rolling back to an arbitrary old ReplicaSet is a stealth image-swap primitive. |
| StatefulSets, PVCs, PVs, DaemonSets, ClusterRoles, ClusterRoleBindings, Namespaces, Nodes, CRDs | No mutation — data-loss, privilege-escalation, or cluster-level impact. |

---

## Live Status Updates

Claude runs in streaming mode (`--output-format stream-json --verbose`). The bot parses each event line and maps it to a human-readable summary:

| CLI Event | What appears in Slack status |
|-----------|------------------------------|
| Session initialized | `Session started...` |
| Bash tool call | `Running: kubectl get pods -n prod` |
| Other tool call | `Using <ToolName>...` |
| PreToolUse hook fired | `Checking permissions...` |
| Text chunk | First 80 chars of the text |

The status message is a single Slack message that gets edited in place:

- `tool_use` events force an immediate edit (tool calls appear the moment they start)
- All other events debounce at 1 second to stay inside Slack API limits
- After 20 lines the oldest are collapsed: `_... (N earlier steps)_`
- Slack 429 rate-limit errors: wait `Retry-After`, retry once, then skip the update
- On completion the header changes to `Done` or `Failed`
- Final response is always posted as a separate message so it stays readable

---

## Project Structure

```
sysop/
├── config.yaml.example        # Copy this to config.yaml
├── pyproject.toml             # Package metadata, entry point: sysop
│
├── sysop/                       # Main package
│   ├── main.py                # Entry point: health checks, startup/shutdown, signal handling
│   ├── config.py              # YAML loading with ${ENV_VAR} substitution
│   ├── bot.py                 # Slack Bolt AsyncApp, event handlers, StatusMessage
│   ├── session.py             # Claude Code subprocess management, stream-json parsing
│   ├── gates.py               # Unix socket server, UUID-routed approvals, per-thread lifecycle
│   ├── audit.py               # SQLite audit log (sessions + gate decisions)
│   └── redact.py              # Secret redaction for Slack echoes
│
├── hooks/
│   └── pre_tool_gate.py       # PreToolUse hook: tokenising classifier → allow/gate/deny
│
├── persona/
│   ├── CLAUDE.md              # Bot identity, rules, memory instructions
│   └── .claude/
│       └── settings.json      # Hook registration (committed — required for gating to work)
│
├── k8s/
│   ├── sysop-rbac.yaml          # SA + cluster-wide read + namespace-scoped write template
│   └── generate-kubeconfig.sh # Generates scoped kubeconfig from the ServiceAccount token
│
└── tests/
    ├── test_audit.py          # AuditDB: schema, log_action, sessions
    ├── test_config.py         # Config loading, env var substitution, defaults
    ├── test_gates.py          # GateManager: socket lifecycle, approve/deny, concurrent gates
    ├── test_hook_bridge.py    # classify_command: allow/gate/deny + bypass-resistance fuzz
    ├── test_redact.py         # Secret redaction
    ├── test_session.py        # SessionManager: command builder, stream-json parser, run()
    └── test_status_message.py # StatusMessage: debounce, line cap, rate-limit retry
```

---

## Testing

```bash
uv run pytest tests/ -v
```

No live Slack connection or Kubernetes cluster needed — everything is mocked.

| File | What's covered |
|------|----------------|
| `test_hook_bridge.py` | Command classification: read/gate/deny decisions, bypass-resistance fuzz (whitespace, interleaved flags, variable expansion, command substitution, heredoc), sensitive-path denial, destructive `rm -rf` denial |
| `test_session.py` | Command builder (incl. `--max-turns`), stream-json line parser, `run()` with callbacks, timeout, stderr drain, no-result fallback |
| `test_status_message.py` | Debounce, force-flush, 20-line cap, rate-limit retry, finalize |
| `test_gates.py` | Socket lifecycle, approve/deny flow, concurrent independent gates, unsafe thread_ts rejection, post-timeout click safety |
| `test_redact.py` | Bearer tokens, `--from-literal`, key=value, Slack tokens, JWTs, truncation |
| `test_config.py` | YAML load, `${VAR}` substitution, defaults, missing required fields |
| `test_audit.py` | Table creation, log_action, gate results, session upsert |

---

## Development

### Adding Gate Patterns

The command lists are runtime-configurable in `config.yaml` — no code changes needed for common adjustments:

```yaml
gates:
  kubectl_read_commands: [get, describe, logs, top, explain, api-resources]
  kubectl_deny_commands: [delete namespace, delete clusterrole, delete pv]
  # bash_read_allowlist / bash_deny_commands use the built-in defaults (sysop/config.py).
  # bash_read_allowlist: [ls, cat, echo, grep, ...]
  # bash_deny_commands:  [shutdown, reboot, mkfs, ...]
```

To let a new tool (e.g. `terraform`) run without approval, add its binary name to `gates.bash_read_allowlist`. Without that, every `terraform …` invocation will be gated (which is almost always the right default for a DevOps tool). For more intricate classification — e.g. allowing only `terraform plan` but not `apply` — edit `hooks/pre_tool_gate.py`.

### Modifying the Persona

Edit `persona/CLAUDE.md`. This file is the working-directory CLAUDE.md for every Claude Code session. Changes take effect on the next conversation. The `## Rules` section is the most impactful part — particularly the instruction to prefer read-only investigation before suggesting mutations.

### Hook Configuration

`persona/.claude/settings.json` registers the PreToolUse hook. `$SYSOP_HOOKS_DIR` is injected by the session manager as the absolute path to `hooks/`, so scripts are found regardless of the working directory.

### Audit Log

The SQLite database (`sysop_audit.db`, gitignored) has two tables: `audit_log` (every action Claude takes, with gate results and the full Claude response) and `sessions` (thread-to-conversation-ID mapping for `--resume`). Query directly with `sqlite3 sysop_audit.db`.

**Treat the audit DB as a secrets store.** It records every bash command the bot executed (which may contain tokens embedded in `--from-literal=` flags, `Authorization:` headers, etc.) and the raw JSON of Claude's reply (which can summarise secrets). The DB file is chmod'd 0600 at init; keep it that way. Gate prompts echoed into Slack *are* redacted (see `sysop/redact.py`), but the audit DB stores the pre-redaction text for forensics.

---

## Security

This is an honest POC, not a hardened production bot. The threat model below is what you are signing up for if you run it.

### Trust boundary

The bot spawns Claude Code with `--permission-mode bypassPermissions`. That means the *only* things between a prompt-injected Claude (e.g. a malicious kubectl output, a crafted GitHub issue fed into context, a dodgy kubeconfig file) and arbitrary command execution are:

1. **The PreToolUse hook** — `hooks/pre_tool_gate.py`. Classifies every `Bash` tool call. Unknown commands default to **gate** (Slack button) rather than allow; destructive patterns (`rm -rf /`, `cat ~/.ssh/id_rsa`, `kubectl delete namespace`) **deny** outright. This hook is registered via `persona/.claude/settings.json`; the bot refuses to start if that file is missing, to avoid the trap of running wide-open.
2. **The kubeconfig's RBAC** — see the RBAC section. Reads are cluster-wide (minus Secrets); writes are namespace-scoped, and `pods/exec`/`pods/portforward` are not granted.
3. **The human clicking Approve** — rate-limited by how fast they read the prompt. If you Approve commands without reading them, none of the above matters.

### Known limitations

- **Host-level sandboxing is your problem.** The Claude subprocess inherits the host user's env. That means `~/.ssh`, `~/.aws`, shell env vars (including your own Slack tokens if they're exported) are reachable to the subprocess. If prompt injection escalates past the hook (e.g. via a zero-day in the classifier), there is no OS-level backstop. Run the bot under a dedicated unix user with `systemd-run --user --scope -p ProtectHome=tmpfs …`, a rootless container, or a VM if you care.
- **The audit DB has secrets in it.** See above.
- **The classifier is best-effort.** It tokenises, understands pipelines and command substitution, and default-gates unknown commands, but a sufficiently creative shell construction may still slip through into the "gate" tier (not allow) — which is why the human-in-the-loop is the real safety valve.
- **Slack approval is not MFA.** If an attacker controls the initiator's Slack account (or if you've set `gates.require_initiator_approval: false`), they can approve anything the bot asks.

### Before running

- Don't point this at production on your first try.
- Put your Slack tokens in `.env` — never in `config.yaml` committed anywhere.
- Rotate any token that you suspect has ever been in a checked-out worktree.
- Enroll namespaces for writes one at a time; do not bind `sysop-bot-write` to every namespace "just in case".

