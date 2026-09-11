# Persona (operator-managed)

Every `*.md` in this directory except this README is ignored by Git and appended, at build time,
to `/workspace/AGENTS.md` — the file Codex loads as project instructions for every request. That
makes it the persona layer: who the assistant is, how it judges, how it talks.

`/workspace-plain/AGENTS.md` holds the same runtime rules without the persona; the Bot uses it
for members who set a personal output style (`/<prefix>-style`), so a personal style replaces
the persona rather than fighting it. A thread that was started under one workspace keeps it
until the member starts a new one.

Suggested structure for `persona/AGENTS.md` (what actually moves the model, strongest first):

1. 5–8 example exchanges written in character (question → how *they* would answer), including a
   greeting, a taunt, a serious technical question, and "are you an AI?".
2. Concrete speech habits: self-reference, how they address others, catchphrases **with when and
   how often to use them**, sentence length, punctuation, emoji or not.
3. Values and judgment: what they care about first, what they look down on, what excites them.
4. Hard no's, and the fixed in-character answer to meta questions ("who are you", "are you Codex").
5. Pointers to knowledge: e.g. "you know the lore indexed under 永久記憶 and bring it up naturally".

After editing: `docker compose up -d --build`.
