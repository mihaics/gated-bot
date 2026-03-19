# SysOp — Virtual DevOps Engineer

SysOp is a Slack bot that acts as a conversational DevOps engineer. It runs on your local machine, takes requests over Slack (Socket Mode — no public URL needed), and drives the Claude Code CLI (Max subscription) to inspect Kubernetes clusters, read and modify a GitOps platform repo, create PRs, and run mutating `kubectl` commands. Every potentially destructive action is gated behind an interactive Slack approval button. Real-time progress is streamed back as an editable status message in the thread so you can see exactly what Claude is doing as it works.

This is a POC — one bot, one cluster, runs on a dev machine. The full SysOp system will enforce PR-only mutations and support multiple personas; this POC intentionally allows direct `kubectl` writes with human gates.

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

Apply the service account and ClusterRole, then generate a scoped kubeconfig:

```bash
# Apply RBAC (requires cluster-admin)
kubectl apply -f k8s/sysop-rbac.yaml

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
| `gates.bash_gate_patterns` | Substrings that trigger gating on bash commands | `[kubectl, git, helm]` |
| `claude.max_turns` | `--max-turns` passed to Claude Code CLI | `50` |
| `claude.persona_claude_md` | Path to the persona CLAUDE.md file | `./persona/CLAUDE.md` |
| `session.idle_timeout_seconds` | Time before an idle session's subprocess is cleaned up | `1800` |
| `session.socket_dir` | Directory for per-session Unix sockets | `/tmp/sysop` |
| `session.max_queue_per_thread` | Max pending requests per Slack thread before rejecting | `3` |

---

## Command Classification

Every Bash tool call Claude makes passes through `hooks/pre_tool_gate.py` before it executes. Commands fall into one of three tiers:

### Always Allowed (exit 0, no prompt)

- `kubectl get`, `kubectl describe`, `kubectl logs`, `kubectl top`, `kubectl explain`, `kubectl api-resources`
- `git status`, `git log`, `git diff`, `git show`, `git branch`, `git remote`, `git stash list`
- Pure bash: `cat`, `ls`, `echo`, `jq`, `curl`, anything without `kubectl`/`git`/`helm` in it

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

```bash
kubectl apply -f manifest.yaml             # gate → Slack prompt
kubectl scale deployment/api --replicas=3  # gate
kubectl delete pod api-7f8b9-x2k4n         # gate
git push origin feature-branch             # gate
helm upgrade my-chart ./chart              # gate
```

### Always Denied (exit 2, no prompt)

Commands that can cause unrecoverable cluster damage are blocked unconditionally — no Slack button is shown:

```bash
kubectl delete namespace production      # denied
kubectl delete clusterrole admin         # denied
kubectl delete clusterrolebinding admin  # denied
kubectl delete pv my-volume              # denied
kubectl delete node worker-1             # denied
```

---

## RBAC Permissions

The `k8s/sysop-rbac.yaml` ClusterRole follows a "read everything, write selectively" philosophy. StatefulSets, PVCs, and cluster-level resources are intentionally omitted from write access — RBAC denial is a hard backstop on top of the hook-level deny list.

### Can Do

| Resource | Verbs |
|----------|-------|
| All core/apps/batch/networking resources | `get`, `list`, `watch` |
| CRDs, events, metrics | `get`, `list` |
| Pods | `delete` (triggers restart) |
| Pods/log, pods/exec, pods/portforward | `get` / `create` |
| Deployments | `patch`, `update` |
| Deployments/scale, deployments/rollback | `patch`, `update` / `create` |
| ConfigMaps, Secrets | `create`, `patch`, `update`, `delete` |
| Services, Ingresses | `create`, `patch`, `update`, `delete` |
| HorizontalPodAutoscalers | `create`, `patch`, `update`, `delete` |
| Jobs, CronJobs | `create`, `patch`, `update`, `delete` |

### Cannot Do (no verbs granted)

| Resource | Why |
|----------|-----|
| StatefulSets | Database workloads — too dangerous to mutate directly |
| PersistentVolumeClaims | Data loss risk |
| PersistentVolumes | Cluster-level storage |
| Namespaces | Can't create or delete namespaces |
| Nodes | No cordon, drain, or delete |
| ClusterRoles / ClusterRoleBindings | No privilege escalation |
| DaemonSets | No mutation |
| ServiceAccounts | No mutation |
| CRDs | No mutation |

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
├── requirements.txt
│
├── sysop/                       # Main package
│   ├── main.py                # Entry point: health checks, startup/shutdown, signal handling
│   ├── config.py              # YAML loading with ${ENV_VAR} substitution
│   ├── bot.py                 # Slack Bolt AsyncApp, event handlers, StatusMessage
│   ├── session.py             # Claude Code subprocess management, stream-json parsing
│   ├── gates.py               # Unix socket server, approval flow, per-gate asyncio.Event
│   ├── audit.py               # SQLite audit log (sessions + gate decisions)
│   └── memory.py              # Openbrain MCP integration
│
├── hooks/
│   └── pre_tool_gate.py       # PreToolUse hook: classify → allow/gate/deny
│
├── persona/
│   ├── CLAUDE.md              # Bot identity, rules, memory instructions
│   └── .claude/
│       └── settings.json      # Hook registration (Claude Code discovers this as project settings)
│
├── k8s/
│   ├── sysop-rbac.yaml          # ServiceAccount, ClusterRole, ClusterRoleBinding
│   └── generate-kubeconfig.sh # Generates scoped kubeconfig from the ServiceAccount token
│
└── tests/
    ├── test_audit.py          # AuditDB: schema, log_action, sessions
    ├── test_config.py         # Config loading, env var substitution, defaults
    ├── test_gates.py          # GateManager: socket lifecycle, approve/deny flow
    ├── test_hook_bridge.py    # classify_command: all three tiers, edge cases
    ├── test_session.py        # SessionManager: command builder, stream-json parser, run()
    └── test_status_message.py # StatusMessage: debounce, line cap, rate-limit retry
```

---

## Testing

```bash
uv run pytest tests/ -v
```

59 tests across 6 files. No live Slack connection or Kubernetes cluster needed — everything is mocked.

| File | Tests | What's covered |
|------|-------|----------------|
| `test_hook_bridge.py` | 16 | Command classification: read/gate/deny decisions, piped commands, substring false positives |
| `test_session.py` | 22 | Command builder, stream-json line parser, `run()` with callbacks, timeout, stderr drain, no-result fallback |
| `test_status_message.py` | 9 | Debounce, force-flush, 20-line cap, rate-limit retry, finalize |
| `test_gates.py` | 3 | Socket creation/cleanup, approve flow, deny flow |
| `test_config.py` | 4 | YAML load, `${VAR}` substitution, defaults, missing required fields |
| `test_audit.py` | 6 | Table creation, log_action, gate results, session upsert |

---

## Development

### Adding Gate Patterns

The three command lists are runtime-configurable in `config.yaml` — no code changes needed for common adjustments:

```yaml
gates:
  kubectl_read_commands: [get, describe, logs, top, explain, api-resources]
  kubectl_deny_commands: [delete namespace, delete clusterrole, delete pv]
  bash_gate_patterns: [kubectl, git, helm]
```

To gate a new tool (e.g. `terraform`), add it to `bash_gate_patterns`. All `terraform` commands will then require approval. To allow specific read subcommands through without a gate, extend the classification logic in `hooks/pre_tool_gate.py`.

### Modifying the Persona

Edit `persona/CLAUDE.md`. This file is the working-directory CLAUDE.md for every Claude Code session. Changes take effect on the next conversation. The `## Rules` section is the most impactful part — particularly the instruction to prefer read-only investigation before suggesting mutations.

### Hook Configuration

`persona/.claude/settings.json` registers the PreToolUse hook. `$SYSOP_HOOKS_DIR` is injected by the session manager as the absolute path to `hooks/`, so scripts are found regardless of the working directory.

### Audit Log

The SQLite database (`sysop_audit.db`, gitignored) has two tables: `audit_log` (every action Claude takes, with gate results and the full Claude response) and `sessions` (thread-to-conversation-ID mapping for `--resume`). Query directly with `sqlite3 sysop_audit.db`.

