# Street Viewer API

The FastAPI backend for the [Street Viewer Android app](https://github.com/DanielOfRivia/street_viewer_v2).
It stores uploaded GPS points in SQLite, detects the places you stayed at, and labels them with
an address and the businesses at that spot using Google's Geocoding and Places APIs.

## How it works

- **Storage**: points go into a local SQLite file, `locations.db`, created on first start.
  Re-uploaded points are ignored, so retrying an upload is safe.
- **Stay detection**: after each upload, a background task clusters new points into stays
  (Li et al. stay-point detection): at least 20 minutes within 200 m. Gaps of up to 12 hours are
  bridged when you resume at the same spot, because the app sends nothing while you're still.
- **Place labels**: each new stay is reverse-geocoded and matched to businesses within 15 m.
  A stay within 30 m of a known place reuses that place's label instead of another lookup.
  Without a Google key, stays are still detected but have no address.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- Optional: a Google Cloud API key with **Geocoding API** and **Places API (New)** enabled

## Setup

1. Clone the repo and create a `.env` file in its root (it's gitignored):

   ```dotenv
   # Required: shared secret the app sends in the X-API-Key header
   API_KEY=a-long-random-string

   # Optional: enables addresses and business names for visited places
   GOOGLE_MAPS_API_KEY=your-server-side-key
   ```

   Generate a key with, for example, `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Put the same value in the app's `local.properties` as `NGROK_SECURE_API_KEY`.

2. Install dependencies and start the server:

   ```bash
   uv sync
   uv run uvicorn main:app --host 0.0.0.0 --port 8080 --reload
   ```

   Port 8080 matches the app's default debug URL (`http://10.0.2.2:8080/` from the emulator).
   For a physical phone, expose the server with a tunnel, for example `ngrok http 8080`, and
   set the app's `BASE_URL_DEBUG` to the tunnel URL.

Interactive API docs are served at `/docs` while the server runs.

## API

Every endpoint requires the header `X-API-Key: <API_KEY>`; otherwise it returns `401`.
Timestamps are Unix epoch **milliseconds**.

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/locations` | Upload a batch of points. Returns `201` with `{"status": "success", "received": n}` |
| `GET` | `/api/v1/locations?start=&end=` | Points with a timestamp in `[start, end]`, oldest first |
| `GET` | `/api/v1/visited-places?start=&end=` | Stays overlapping `[start, end]`, clipped to that range |

Upload body:

```json
{
  "locations": [
    { "latitude": 43.6532, "longitude": -79.3832, "timestamp": 1790540434803, "accuracy": 8.5 }
  ]
}
```

Visited place:

```json
{
  "id": 1,
  "latitude": 43.6532,
  "longitude": -79.3832,
  "arrival_time": 1790540434803,
  "departure_time": 1790544034803,
  "point_count": 42,
  "address": "100 Queen St W, Toronto, ON M5H 2N2, Canada",
  "businesses": [{ "name": "Toronto City Hall", "types": ["city_hall"], "place_id": "ChIJ..." }]
}
```

A place you're still at appears only once a later point shows you've left.

## Privacy

`locations.db` holds your full location history. It's gitignored; keep it that way and back it
up separately. Anyone with `API_KEY` can read the whole history, so use a long random value and
serve the API only over HTTPS (ngrok does this for you).

## License

[MIT](LICENSE)
