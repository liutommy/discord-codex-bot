# Image service API

The bot reaches an image-generation service on the host through the same relay path as the
local LLM (`IMAGE_GEN_URL`, for example `http://172.31.254.1:18082`). The service owns the model
catalog, the prompt pipeline and the GPU; the bot owns Discord: who may generate, how many, and
delivery. Every call is plain HTTP + JSON except the image download. Jobs are asynchronous: the
bot submits, polls, downloads, and may cancel. A request never holds a connection open for the
whole generation (the relay closes idle connections and serves one connection per worker).

When `IMAGE_GEN_API_KEY` is set, every request carries `Authorization: Bearer <key>`; the service
answers 401 without it.

## One GPU queue

The local LLM and image generation share one GPU, so the same service also fronts the local LLM
(`LOCAL_LLM_URL` reaches it through the LLM relay) and runs every image job and every chat
completion through one queue: one item runs, at most 3 wait. A fourth waiting item is refused at
once with 429 `queue_full`; nothing is queued beyond the limit.

Time spent waiting is not run time. A caller starts its own timeout when its item leaves the
queue: for an image job, the first poll whose `state` is not `queued`; for a streamed chat
completion, the first event that is not an SSE comment.

## GET /v1/queue

```json
{"running": {"kind": "image", "since_s": 42.0}, "waiting": [{"kind": "chat"}, {"kind": "image"}],
 "limit": 3}
```

`running` is `null` when the GPU is idle.

## POST /v1/chat/completions, GET /v1/models

OpenAI-compatible, passed through to the local LLM. A chat completion is one queue item; the
model list is not. While a streamed request (`"stream": true`) waits, the service sends an SSE
comment every 10 s: `: queued position=2` (2 items ahead). If the LLM was stopped for image work,
the service starts it again after the request leaves the queue. A full queue answers 429 with
`{"error": {"type": "queue_full", "code": "queue_full", "message": "local queue is full (3 waiting)"}}`.
A request that is not streamed waits without any bytes on the wire.

## GET /v1/image/models

```json
{"models": [
  {"id": "chroma1-flash", "name": "Chroma1 Flash", "prompt_style": "natural",
   "options": ["seed", "steps", "cfg", "negative", "aspect", "count", "enhance", "init_image", "strength", "mask", "upscale", "detailer"],
   "enhancers": ["local", "off"],
   "defaults": {"steps": 8, "cfg": 1.0, "aspect": "1:1", "count": 1, "enhance": "local", "strength": 0.6, "upscale": "off", "detailer": false}}
],
 "default_model": "chroma1-flash",
 "aspects": ["1:1", "3:4", "4:3", "9:16", "16:9"],
 "upscale": ["off", "2x", "4x"],
 "max_count": 4,
 "max_input_bytes": 12000000}
```

`options` lists what the model supports. A request that sets an option the model does not list
is refused with 400 and `{"error": "unsupported_option", "option": "detailer"}`, never ignored.

`enhance` chooses how the prompt is rewritten before generation:

- `local`: rewritten on the host by a local model for the target model's prompt style.
- `cloud`: rewritten by a hosted model. The service uses it only for a request it judges to be
  general-audience; otherwise it falls back to `local`. The job reports what it used.
- `off`: the prompt is used as written.

## POST /v1/image/jobs

```json
{"model": "chroma1-flash", "prompt": "...", "negative": null, "aspect": "3:4", "count": 1,
 "seed": null, "steps": null, "cfg": null, "enhance": null,
 "init_image": null, "strength": null, "mask": null, "ref_image": null, "ref_strength": null,
 "upscale": null, "detailer": null,
 "max_bytes": 8000000, "requester": {"guild": "123", "user": "456"}}
```

`null` or a missing field means the model's default. `init_image`, `mask` and `ref_image` are
`{"mime": "image/png", "data": "<base64>"}` (png, jpeg or webp, at most `max_input_bytes` decoded
in total). With `init_image` the job is image-to-image and `strength` (0 to 1) is how much it may
change; `mask` (white = repaint) needs `init_image`. `ref_image` is a reference image; what it
does depends on the kind of model, and only a model whose `options` list them takes `ref_image`
and `ref_strength`:

- An IP-Adapter model (for example `wai-illustrious`) lists both. The output carries over the
  reference's subject and appearance (mainly hair colour and general look), not its layout, while
  `init_image` carries over layout and pose. `ref_strength` (0 to 1, model default when `null`)
  is how strongly the reference applies, and needs `ref_image`. `ref_image` works alone, with a
  text prompt, or together with `init_image`.
- An edit model (for example `qwen-image-2.1-turbo` or `anima`) lists `ref_image` but not
  `ref_strength`. The model takes `ref_image` as the image to edit or the character to keep, and
  the prompt is an instruction or a description of the result ("replace the background with a
  snowy forest at night; keep the character, pose and clothing"). The service turns prompt
  enhancement off by default for such a job.

`max_bytes` is the largest file the caller can deliver; the service encodes each image (PNG, then
WEBP or JPEG at falling quality) to fit. `requester` is for the service log only.

- 202 `{"job_id": "j-...", "state": "queued", "position": 2}`
- 400 `{"error": "unknown_model" | "unsupported_option" | "bad_value", ...}`
- 413 `{"error": "input_too_large", "max_input_bytes": 12000000}`
- 429 `{"error": "queue_full", "queue": 3}`
- 503 `{"error": "unavailable", "detail": "..."}`

## GET /v1/image/jobs/{job_id}

```json
{"job_id": "j-...", "state": "queued|preparing|generating|postprocessing|done|failed|cancelled",
 "position": 0, "step": 3, "steps": 8, "model": "chroma1-flash", "enhance_used": "local",
 "images": [{"index": 0, "bytes": 812345, "mime": "image/webp", "seed": 1234}],
 "error": null, "sensitive": false, "queued_s": 95.0, "elapsed_s": 41.2}
```

`position` counts items ahead in the shared queue. `queued_s` is the time spent waiting;
`elapsed_s` counts from when the job left the queue. `images` is filled when `state` is `done`. A
finished job and its images are kept for at least 30 minutes after it ends, then deleted.

`sensitive` is `true` when the service judges the request sexual from the prompt as asked and as
rewritten; it is never judged from the image. A missing `sensitive` reads as `false`. The bot
uses it to decide which images to post behind a spoiler.

## GET /v1/image/jobs/{job_id}/images/{index}

The image bytes with the `mime` above as `Content-Type`. 404 once deleted.

## DELETE /v1/image/jobs/{job_id}

Cancels a queued or running job: 200 `{"state": "cancelled"}`; a job already finished answers
200 with its final state.

## GET /v1/health

`{"ok": true, "queue": 1, "busy": true}`.
