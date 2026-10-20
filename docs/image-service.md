# Image service API

The bot reaches an image-generation service on the host through the same relay path as the
local LLM (`IMAGE_GEN_URL`, for example `http://172.31.254.1:18082`). The service owns the model
catalog, the prompt pipeline and the GPU queue; the bot owns Discord: who may generate, how many,
and delivery. Every call is plain HTTP + JSON except the image download. Jobs are asynchronous:
the bot submits, polls, downloads, and may cancel. A request never holds a connection open for
the whole generation (the relay closes idle connections and serves one connection per worker).

When `IMAGE_GEN_API_KEY` is set, every request carries `Authorization: Bearer <key>`; the service
answers 401 without it.

## GET /v1/image/models

```json
{"models": [
  {"id": "chroma1-flash", "name": "Chroma1 Flash", "prompt_style": "natural",
   "options": ["seed", "steps", "cfg", "negative", "aspect", "count", "hires", "upscale", "detailer", "cloud_enhance", "split"],
   "defaults": {"steps": 8, "cfg": 1.0, "aspect": "1:1", "count": 1, "hires": false, "upscale": "off", "detailer": false, "cloud_enhance": true, "split": true}}
],
 "default_model": "chroma1-flash",
 "aspects": ["1:1", "3:4", "4:3", "9:16", "16:9"],
 "upscale": ["off", "2x", "4x"],
 "max_count": 4}
```

`options` lists what the model supports. A request that sets an option the model does not list
is refused with 400 and `{"error": "unsupported_option", "option": "detailer"}`, never ignored.

## POST /v1/image/jobs

```json
{"model": "chroma1-flash", "prompt": "...", "negative": "", "aspect": "3:4", "count": 1,
 "seed": null, "steps": null, "cfg": null,
 "hires": null, "upscale": null, "detailer": null, "cloud_enhance": null, "split": null,
 "max_bytes": 8000000, "requester": {"guild": "123", "user": "456"}}
```

`null` or a missing field means the model's default. `max_bytes` is the largest file the caller
can deliver; the service encodes each image (PNG, then WEBP or JPEG at falling quality) to fit.
`requester` is for the service log only.

- 202 `{"job_id": "j-...", "state": "queued", "position": 2}`
- 400 `{"error": "unknown_model" | "unsupported_option" | "bad_value", ...}`
- 429 `{"error": "queue_full", "queue": 5}`
- 503 `{"error": "unavailable", "detail": "..."}`

## GET /v1/image/jobs/{job_id}

```json
{"job_id": "j-...", "state": "queued|preparing|generating|postprocessing|done|failed|cancelled",
 "position": 0, "step": 3, "steps": 8, "model": "chroma1-flash",
 "images": [{"index": 0, "bytes": 812345, "mime": "image/webp", "seed": 1234}],
 "error": null, "elapsed_s": 41.2}
```

`position` counts jobs ahead in the queue. `images` is filled when `state` is `done`. A finished
job and its images are kept for at least 30 minutes after it ends, then deleted.

## GET /v1/image/jobs/{job_id}/images/{index}

The image bytes with the `mime` above as `Content-Type`. 404 once deleted.

## DELETE /v1/image/jobs/{job_id}

Cancels a queued or running job: 200 `{"state": "cancelled"}`; a job already finished answers
200 with its final state.

## GET /v1/health

`{"ok": true, "queue": 1, "busy": true}`.
