# myorg-starter

Agents, skills and session guards for [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://developers.openai.com/codex), installed as one plugin.
Generated from [`praxiom-systems/getmyorg`](https://github.com/praxiom-systems/getmyorg).

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/praxiom-systems/myorg-starter/main/bin/install.sh | bash
```

This registers the `myorg-starter` plugin in Claude Code and Codex, whichever you have. It installs once for you and works in every project, and each host keeps it updated. Restart the host afterwards.

**In Codex,** open `/hooks` and trust myorg-starter's hooks. Codex runs a plugin's hooks only after you trust them.

You can also install by hand:

| Host | Commands |
|---|---|
| Claude Code | `claude plugin marketplace add praxiom-systems/myorg-starter`, then `claude plugin install myorg-starter@myorg-starter` |
| Codex | `codex plugin marketplace add praxiom-systems/myorg-starter`, then `codex plugin add myorg-starter@myorg-starter` |

Then check that it works:

```sh
curl -fsSL https://raw.githubusercontent.com/praxiom-systems/myorg-starter/main/bin/doctor.sh | bash
```

The doctor checks that the plugin is installed in each host, sends a never-OK command through the guard to confirm it's stopped, and shows your settings.

## What you get

### Agents (Claude Code)

| Agent | Use it to |
|---|---|
| `code-reviewer` | perform thorough code reviews focusing on correctness, security, performance, and maintainability. |
| `debugger` | investigate and fix bugs systematically using root cause analysis. |
| `fullstack-engineer` | implement features, fix bugs, and refactor code with production-quality standards. |
| `junior-engineer` | implement straightforward features, fix simple bugs, and write code under guidance. |
| `product-owner` | define product direction, prioritize for value, write specs, and make clear decisions. |
| `security-auditor` | perform security assessments identifying vulnerabilities and recommending mitigations. |
| `systems-architect` | provide high-level architectural guidance on system design, component interactions, data flows, and change impact analysis. |
| `tech-lead` | plan implementation approaches and break down complex tasks. |
| `technical-writer` | create clear, minimal documentation that follows DRY principles. |
| `test-engineer` | design and implement comprehensive test suites including unit, integration, and e2e tests. |

### Skills

- **systematic-debugging:** find the root cause of a bug or failing test before changing code.
- **decompose-changeset:** split a large change into reviewable pieces.

`standards/` holds the engineering standards the agents follow.

### Session guards

The guards check every shell command, file read and edit the agent makes, in both hosts.

- **Bash:** never-OK commands, such as `rm -rf /` or force-pushing `main`, are always stopped. Risky ones (`sudo`, writing your shell profile, `terraform apply`, `kubectl delete`, `curl … | sh`) get a note at the default level, and are stopped or bounced back to the agent at stricter levels. Routine commands run.
- **Read:** a file that tries to slip instructions to the agent gets flagged.
- **Edit:** an edit that hides a problem instead of fixing it, such as a swallowed exception, a naked `@ts-ignore` or a skipped test, gets a note. At `strict` the worst of these are stopped.

Commands the guards don't recognise are left to your host's own permission prompt. The guards step in only for commands they know are risky.

## Settings

| Command | What it does |
|---|---|
| `/myorg-starter:config` | Shows your settings and where each comes from. |
| `/myorg-starter:config guards <level>` | Sets how strict the guards are. |
| `/myorg-starter:config gate on\|off` | Turns gate mode on or off. |

In Codex, type `$myorg-starter:config` instead. Add `--repo` to set a value for the current repo only.

| Level | What it means |
|---|---|
| `strict` | Only known-safe commands run without a second look; risky ones are stopped. |
| `guarded` | Everyday commands run; risky ones are stopped or bounced back to the agent. |
| `relaxed` (the default) | Everything runs except never-OK commands and force-pushes to protected branches. |
| `off` | The guards do nothing. |

Only you change these. The guards stop the agent from changing them itself.

### Gate mode: when your host's prompts are off

Some people turn Claude Code's own prompts off, by allowing `Bash(*)` or starting with `--dangerously-skip-permissions`. Then nothing asks about a command the guards don't recognise, and it simply runs.

Turn on gate mode for that setup. It makes the guard ask you, in Claude Code's permission prompt, about every command that isn't routine:

- commands no rule recognises;
- risky commands that would otherwise only get a note or bounce back to the agent.

The agent can't clear these prompts itself, and never-OK commands are still stopped. Gate mode is Claude Code only: Codex's hooks can stop a command but can't ask you.

A plugin can't set permissions. To skip the prompts for other routine tools as well, add an allow-list like this to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(*)", "Read(/tmp/*)", "Write(/tmp/*)", "Edit(/tmp/*)", "WebSearch", "WebFetch"]
  }
}
```

With `Bash(*)` allowed, turn on gate mode too, so unrecognised commands still reach you.

## GitHub workflows

```sh
curl -fsSL https://raw.githubusercontent.com/praxiom-systems/myorg-starter/main/bin/install.sh | bash -s -- --workflows
```

Run this inside a repo. It adds two workflows to `.github/workflows/`, both checked by the same guards:

- `claude.yml` answers `@claude` in issues and PRs;
- `claude-code-review.yml` reviews pull requests.

They need a `CLAUDE_CODE_OAUTH_TOKEN` repo secret; `claude setup-token` makes one.

## Moving to getmyorg

[getmyorg](https://getmyorg.com) adds a project map of your code, org presets that route work to the right agents, and every agent in its plans.

Your guard settings carry over: both read the same `~/.config/getmyorg/config.yaml`.

- **Claude Code:** the starter's guards step aside in any project where getmyorg's plugin runs.
- **Codex:** both plugins guard until you remove the starter, since Codex runs a plugin's hooks only once you trust them. Trust getmyorg's in `/hooks` first.

Remove the starter when you're ready (see Uninstall below). getmyorg's installer tells you when you still have it.

A `.myorg/config.local.yaml` that's committed to a repo is ignored, so a repo can't change your settings.

## Uninstall

```sh
claude plugin uninstall myorg-starter@myorg-starter
codex plugin remove myorg-starter@myorg-starter
```

## License

MIT. See `LICENSE` and `NOTICE`. Issues and PRs go [upstream](https://github.com/praxiom-systems/getmyorg); this repo is generated.
