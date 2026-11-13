# AV Derivative Provenance

AV Derivative Provenance is a Python backend for deterministic audio and video
derivation. It accepts PCM WAV and planar Y4M media, applies bounded
operations, records content digests and lineage, and reproduces a completed
result on demand to verify that the stored bytes still match the plan.

The application uses only the Python standard runtime. It does not need
FFmpeg, network access, credentials, or an online database.

## What it does

- Ingests one- or two-channel signed 16-bit PCM WAV.
- Ingests planar 4:2:0 YUV4MPEG2 in the `C420` family.
- Applies audio trim, gain, normalization, and two-source mixing.
- Applies video trim, even-aligned crop, luma offset, and horizontal mirror.
- Persists immutable assets, rendition state, provenance edges, and integrity
  checks.
- Replays the stored operation plan and compares both bytes and media
  descriptors.

## Ranged local reads for short selections

When a plan begins with `media_trim` and every later operation is pointwise or
window-local (audio gain/normalization, video crop/luma/mirror), the executor
does not load the whole source blob. It opens a byte-counted ranged source,
probes only the container header, and decodes just the frames or sample frames
inside the trim window. The source bytes read and the processing work therefore
shrink in proportion to the delivered selection rather than the full asset.

Plans that genuinely need whole-clip state are never misclassified:

- `audio_normalize` (or any other operation) appearing *before* the trim stays
  on the full path, because its peak is computed over the entire source.
- `audio_mix` always stays on the full path because it reads the full-length
  secondary asset.
- A leading trim is required; plans without one use the unchanged full path.

If a ranged parser cannot prove its layout is equivalent to the full parser
(truncated payload, variable frame headers, unexpected RIFF layout), it raises
an internal `LocalReadUnavailable` signal and the request transparently falls
back to the full read-and-decode path. No plan becomes newly rejectable, and
the local path shares the exact same per-operation functions, so output bytes
are identical to full execution. No decoded source content is cached.

The integrity check remains independent of this optimization: it re-reads every
parent asset in full, verifies each whole-source SHA-256 against the immutable
asset record (`source_digest_drift` on failure), and only then performs a real
full replay of the stored plan. A short clip rendering successfully never
endorses bytes outside its window.

Two scripts demonstrate and verify the behavior:

```bash
# Byte-identity, locality/fallback rules, and independent audit behavior
python3 -I scripts/local_read_checks.py
# Near-24 MiB WAV/Y4M fixtures with byte and wall-time savings for short clips
python3 -I scripts/local_read_benefit_demo.py
```

## Quick start

```bash
python3 -m compileall -q avprovenance scripts
python3 -m avprovenance --host 127.0.0.1 --port 8765 --data-dir .avprovenance-data
```

The startup line includes the active host, port, and data directory. Stop the
process with `Ctrl+C`.

## HTTP endpoints

### Health

```http
GET /health
```

### Upload an asset

```http
POST /v1/assets
Content-Type: application/json

{
  "filename": "source.wav",
  "data_base64": "<strict base64>"
}
```

### Read an asset

```http
GET /v1/assets/{asset_id}
```

### Create a rendition

```http
POST /v1/renditions
Content-Type: application/json

{
  "primary_asset_id": "asset_...",
  "secondary_asset_id": null,
  "output_name": "derived.wav",
  "operations": [
    {
      "type": "media_trim",
      "start_seconds": 0.1,
      "duration_seconds": 0.3
    },
    {"type": "audio_gain", "db": -3},
    {"type": "audio_normalize", "target_peak": 0.8}
  ]
}
```

Rendition creation is synchronous at the HTTP boundary. The durable transition
history still records `queued`, `running`, and the terminal state.

Audio-only operations:

- `media_trim` with `start_seconds` and `duration_seconds`
- `audio_gain` with `db`
- `audio_normalize` with `target_peak`
- `audio_mix` with `secondary_gain`, `primary_weight`, and `secondary_weight`

Video-only operations:

- `media_trim` with `start_frame` and `frame_count`
- `video_crop` with even `x`, `y`, `width`, and `height`
- `video_luma_offset` with `amount`
- `video_mirror`

### Read rendition state

```http
GET /v1/renditions/{rendition_id}
```

### Read lineage

```http
GET /v1/renditions/{rendition_id}/lineage
```

### Verify reproduction

```http
POST /v1/renditions/{rendition_id}/integrity-checks
Content-Type: application/json

{}
```

The successful response has `status: "verified"`. A byte or descriptor
difference returns HTTP `409` with code `INTEGRITY_MISMATCH`; the mismatch
record remains durable.

## Media operation examples

Audio mixing plan:

```json
[
  {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.3},
  {"type": "audio_gain", "db": -3},
  {
    "type": "audio_mix",
    "secondary_gain": -6,
    "primary_weight": 0.75,
    "secondary_weight": 0.4
  },
  {"type": "audio_normalize", "target_peak": 0.8}
]
```

Video plan:

```json
[
  {"type": "media_trim", "start_frame": 1, "frame_count": 4},
  {
    "type": "video_crop",
    "x": 8,
    "y": 6,
    "width": 32,
    "height": 18
  },
  {"type": "video_luma_offset", "amount": 12},
  {"type": "video_mirror"}
]
```

## Data layout

```text
.avprovenance-data/
├── state.json
└── content/
    └── <digest-prefix>/
        └── <digest-prefix>/
            └── <sha256>
```

`state.json` is replaced atomically. Binary objects are content-addressed by
SHA-256 and written through temporary siblings before replacement.

## Source layout

```text
avprovenance/
├── application.py
├── http_api.py
├── models.py
├── validation.py
├── media/
├── renditions/
├── storage/
└── transport/
```

The HTTP boundary calls the application layer. The application coordinates
plan handling, media operations, and persistence. The media layer does not
depend on HTTP or the repository.

## Workflow checks

The baseline intentionally has no automated test suite. Tests are deferred to
the later task stage, which will add red/green verification coverage. The
current production workflow checks are:

```bash
python3 scripts/workflow_checks.py ingest-media-asset
python3 scripts/workflow_checks.py execute-rendition
python3 scripts/workflow_checks.py verify-reproduction
```

Each check starts a temporary HTTP server, generates media bytes in memory,
exercises the public endpoints, and shuts the server down. The
`local_read_checks.py` and `local_read_benefit_demo.py` scripts provide
additional differential and measurement coverage for the ranged read path but
are not part of the deferred baseline suite.

## Environment

No environment variables are required. Runtime choices use `--host`, `--port`,
and `--data-dir`. Incoming request bodies are limited to 36 MiB, decoded media
payloads to 24 MiB, WAV geometry to one or two channels, and Y4M geometry to
1920x1080 with at most 240 frames.
