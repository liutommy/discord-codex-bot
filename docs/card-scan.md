# Card recognition service API

The bot reaches a Yu-Gi-Oh card recognition service on the host through its own relay
(`CARDSCAN_URL`, for example `http://172.31.254.1:18084`; `deploy/orbstack/README.md`). The
service owns the card index and the matching; the bot owns when to scan (the model's
`<cardscan>` tag), the Gemini fallback for photos, and the text the model reads. Plain HTTP +
JSON, no authentication: the service binds `127.0.0.1` on the host and only the relay reaches it.

## How a model asks

HELP carries a `[遊戲王認卡]` section while `CARDSCAN_URL` is set. A model replies with only
`<cardscan/>` (every image of the question) or `<cardscan image="N"/>` (the Nth image, in the
order the model received them: attachments, then link screenshots, then images read with
`<earlier>`). The bot scans each image once per answer, at most 8 per answer, and returns one
`<RESULT kind="cardscan" image="N">` block per image: the cards grouped by main, extra and side
deck with copy counts, Japanese and English names, passcode and Konami cid, then the candidates
of every card marked 不確定.

## GET /health

```json
{"ok": true, "indexes": {"full": 14662, "faces": 14825, "ocg-arts": 14942, "ocg-faces": 14942},
 "names": 14380}
```

Images per index and cards with names. `full` and `faces` are YGOPRODeck's (TCG) artwork crops
and card faces; `ocg-arts` and `ocg-faces` are Konami Neuron's Japanese (OCG) card faces and
their art regions. A Neuron screenshot is matched against the OCG indexes only; other images
against all four.

## POST /scan

Request: `{"image": "<base64 PNG, JPEG or WebP>", "boxes": [[x, y, w, h], ...]}`. `boxes` is
optional: pixel boxes around cards the outline search may have missed. The service refuses a body
over 20 MB, an image over 40 million pixels and more than 100 boxes (413 or 400 with
`{"error": "..."}`); it scans one image at a time and answers 503 `busy` after a 60 s wait.

Response:

```json
{
  "kind": "neuron",
  "image": [1079, 1697],
  "sections_from": "image",
  "groups": {
    "main": [{"id": 89631139, "name": "Blue-Eyes White Dragon", "jp_name": "青眼の白龍",
              "cn_name": "青眼白龙", "cid": 4007, "count": 3, "unsure": 0}],
    "extra": [],
    "side": []
  },
  "cards": [{"pos": 1, "section": "main", "box": [0, 119, 108, 154], "sure": true,
             "top": [{"id": 89631139, "name": "Blue-Eyes White Dragon", "score": 0.81,
                      "frame": "normal", "jp_name": "青眼の白龍", "cid": 4007}]}]
}
```

- `kind`: `neuron` (a Neuron deck list screenshot, split by its section headers), `photo` (card
  outlines found), `photo+boxes` / `boxes` (with the request's boxes added), `whole` (no outline
  found: the whole image matched as one card).
- `groups`: copies of one card merged, in reading order of first appearance. A screenshot's
  sections come from its headers; in a photo fusion, synchro, xyz and link cards go to `extra` and
  everything else to `main` (a photo shows no side deck).
- `cards[].sure`: the best card leads the second by at least 0.03. On three Neuron screenshots
  (216 cards) 214 were recognised and 205 were sure, none of them wrong; the threshold was
  picked on those samples.
- `id` is the passcode; `cid` is Konami's card database id.

## Gemini fallback

When a scan returns `photo` or `whole` and `GEMINI_API_KEY` is set, the bot asks Gemini for the
cards' boxes (normalised `box_2d`, converted to pixels with the scan's `image` size). When Gemini
marks more cards than the outline search found, the bot scans again with those boxes; the
service keeps the cards it found and adds each box whose centre lies outside them.

## Limits

- Recognition compares artwork, not printed names: cards with near-identical artwork (one
  series, alternate artworks) can be matched to the wrong card. The model is told to check
  cards marked 不確定 against the image.
- The bot escapes angle brackets in every string the service returns before it goes into the
  RESULT block, and a reply not in the documented shape becomes an error line.
- The service shares the host's GPU with the local LLM and the image service outside their
  queue; a scan holds it for about a second.
