---
name: config
description: >-
  Show or change the myorg-starter session guards: the guard level and gate
  mode. Invoke as /myorg-starter:config in Claude Code or $myorg-starter:config
  in Codex, optionally with guards <level> or gate on|off, and --repo for this
  repo only.
argument-hint: '[guards <strict|guarded|relaxed|off> | gate on|off] [--repo]'
---

# Config: show or change the guard settings

| Setting | Values | Where it's kept |
|---|---|---|
| `guards` | `strict`, `guarded`, `relaxed` (the default), `off` | the user's `~/.config/getmyorg/config.yaml`, or this repo's gitignored `.myorg/config.local.yaml` with `--repo` |
| `gate` | `on`, `off` (the default) | the same |

- **Guards** decide what the session guards stop when they check the agent's commands, file reads and edits.
- **Gate mode** makes the Bash guard ask the user, in Claude Code's permission prompt, about every command that isn't routine: commands no rule knows, and risky ones that would otherwise bounce back to the agent. It's for people who turn Claude Code's own prompts off (`Bash(*)` or `--dangerously-skip-permissions`). Codex's hooks can't ask, so it doesn't apply there.

Only the user changes these. A myorg-starter hook reads the user's prompt before you do: it has already shown or applied the setting, and its result is in your context.

- **No arguments:** your context has the settings. Show them verbatim.
- **`guards <level>` or `gate on|off`:** your context says what changed. Say it in one line, and that it takes effect on the next command.
- **No hook result in your context:** the hook didn't run. In Codex, plugin hooks run only after the user trusts them in `/hooks`. Otherwise tell the user to run `bash bin/doctor.sh` from their myorg-starter clone, which checks the install.

Never edit the settings files yourself, and never run the hook's scripts: the guards stop both. Don't argue for a different value. If the user picked guards `off`, mention once that `/myorg-starter:config guards relaxed` turns the guards back on.
