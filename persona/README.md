# Persona (operator-managed)

Every `*.md` in this directory except this README and `*.example.md` is ignored by Git, copied into
the image, and appended to `AGENTS.md` in the Codex workspace (`CODEX_WORKSPACE`, by default
`/var/lib/codex/workspace`) — the file Codex loads as project instructions for every request. That
makes it the persona layer: who the assistant is, how it judges, how it talks. The composition
happens when the Bot starts and again after every change, not when the image is built: an
operator can upload a replacement with `/<prefix>-persona`, which wins until it is reset.

`AGENTS.md` in the plain workspace (`CODEX_WORKSPACE_PLAIN`, by default
`/var/lib/codex/workspace-plain`) holds the same runtime rules without the persona. The Bot uses it
for members who turn the persona off (`/<prefix>-style persona:關閉人設`). A personal output style
is separate: it is added to every request on top of whichever workspace applies, with or without
the persona. A thread that was started under one workspace keeps it until the member starts a new
one.

Suggested structure for `persona/AGENTS.md` (what actually moves the model, strongest first):

1. 5–8 example exchanges written in character (question → how *they* would answer), including a
   greeting, a taunt, a serious technical question, and "are you an AI?".
2. Concrete speech habits: self-reference, how they address others, catchphrases **with when and
   how often to use them**, sentence length, punctuation, emoji or not.
3. Values and judgment: what they care about first, what they look down on, what excites them.
4. Firm refusals, and the fixed in-character answer to meta questions ("who are you", "are you Codex").
5. Pointers to knowledge: e.g. "you know the lore indexed under 永久記憶 and bring it up naturally".

After editing: `docker compose up -d --build`.
