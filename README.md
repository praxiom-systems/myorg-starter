# myorg-starter

A bounded, Sovereign-grade starter kit for [Claude Code](https://docs.claude.com/en/docs/claude-code).
Generated from canonical [`praxiom-systems/getmyorg`](https://github.com/praxiom-systems/getmyorg).

## What it is

Ten agents, four standards, two skills, and a hook stack — enough for a solo
developer to plan, build, debug, review, test, document, and reason about
architecture. **Generated, never authored.** Issues and PRs against this repo
are redirected upstream.

## Use it

```sh
git clone https://github.com/praxiom-systems/myorg-starter.git
cp -r myorg-starter/blueprint/agents/* ~/.claude/agents/
cp -r myorg-starter/blueprint/hooks/    ~/.claude/hooks/
```

See `blueprint/agents/` for the included agent definitions and
`blueprint/hooks/` for hook scripts.

## Verify

```sh
bash bin/doctor.sh
```

`doctor` reports on the installed hook stack: which guards are active, how many patterns each one carries, recent denies / advisories, and how to override defaults. Exits 0 when the safety layer is healthy.

## Outgrow it

`myorg-starter` is the public starter SKU. The full product —
[`getmyorg`](https://github.com/praxiom-systems/getmyorg) — adds the mapping
plane (project map, area-of-responsibility inference), org presets, delivery
bundles, and the binary that drives `/myorg:*` slash commands. Reach out when
you've outgrown the starter.

## License

MIT. See `LICENSE` and `NOTICE`.
