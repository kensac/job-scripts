# CLAUDE.md

**The instructions for this repository live in [AGENTS.md](AGENTS.md) and the
topic library under [docs/agents/](docs/agents/).** Read those. This file
exists so that Claude Code finds them.

Everything durable is there deliberately, in one copy, so it stays true for any
agent rather than only this one. Do not restate a rule here — a second copy is
a copy that drifts, and the standards in this repository say to delete
duplication rather than maintain it.

## Working notes

Scratch space for facts that are true right now and expected to change. Nothing
here is a rule; rules go in `docs/agents/`.

- Scale: roughly 67k mail messages, 49k jobs, 74k AI query rows, ~476MB.
- The frontend deploys via Vercel on push to main, outside the fleet that runs
  this application.
