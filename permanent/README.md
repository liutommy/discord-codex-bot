# Permanent memory (operator-managed)

Everything in this directory except this README is ignored by Git and baked into the image at
`/opt/discord-codex/permanent`. The Bot never writes here and exposes no command for it.

- `MEMORY.md` — injected verbatim into every prompt as `[永久記憶索引]`. No line or byte window,
  never evicted, never consolidated. Keep it an index: one line per topic, e.g.
  `- [house-rules](house-rules.md) — 伺服器規則全文`.
- `topics/*.md` — read on demand: the model can `<search scope="permanent" query="…"/>` across
  every file or `<recall scope="permanent" name="<file stem>"/>` one page at a time.

After editing: `docker compose up -d --build`.
