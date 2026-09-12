# Release announcements (operator-managed)

`announce/latest.md` (gitignored) is baked into the image. On every start the Bot compares its
content hash with `CODEX_HOME/announced.json` and posts it once to each channel listed in
`ANNOUNCE_CHANNEL_IDS` that has not seen this version. **Off by default**: with no channel
configured nothing is posted anywhere — there is no guild-wide fallback. Keep it under 2000
characters (one Discord message). Edit, rebuild, and the Bot announces on startup.
