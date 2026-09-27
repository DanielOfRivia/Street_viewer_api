import os
import sqlite3
import threading
from collections import Counter
from math import asin, cos, radians, sin, sqrt
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="GPS Tracking API")

API_KEY = os.environ["API_KEY"]
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

# Stay-point detection thresholds (Li et al.). LocationPoint.timestamp is
# epoch milliseconds (matches the Android client's System.currentTimeMillis()).
STAY_DIST_THRESHOLD_M = 200
STAY_TIME_THRESHOLD_MS = 20 * 60 * 1000
# A gap this long between consecutive samples is treated as tracking having
# paused/stopped (app closed, phone off), i.e. an implicit departure.
STAY_MAX_GAP_MS = 60 * 60 * 1000
# The client only reports a fix after moving MIN_UPDATE_DISTANCE_METERS, so
# sitting still indoors can produce multi-hour silences. A gap longer than
# STAY_MAX_GAP_MS is still bridged if the points on either side of it are
# this close together (we never saw you leave), up to STAY_MAX_BRIDGED_GAP_MS
# so that e.g. a day with tracking off doesn't merge two home stays.
STAY_GAP_BRIDGE_DIST_M = 50
STAY_MAX_BRIDGED_GAP_MS = 12 * 60 * 60 * 1000
# A new stay within this distance of an already-known place reuses that
# place's address/coordinates/businesses instead of a fresh Places lookup,
# which would otherwise pick whichever business is closest to this stay's
# slightly different centroid and label the same building differently.
PLACE_REUSE_RADIUS_M = 30
# Google Places has no "exact address" filter, only radius search. Kept tight
# so it captures businesses co-located at the same building/address rather
# than others nearby on the same block.
NEARBY_BUSINESS_RADIUS_M = 15

stay_detection_lock = threading.Lock()

def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# Pydantic Schemas for incoming payload validation
class LocationPoint(BaseModel):
    latitude: float
    longitude: float
    timestamp: int
    accuracy: float

class LocationBatch(BaseModel):
    locations: List[LocationPoint]

class LocationRecord(LocationPoint):
    id: int

class Business(BaseModel):
    name: str
    types: List[str]
    place_id: str

class VisitedPlace(BaseModel):
    id: int
    latitude: float
    longitude: float
    arrival_time: int
    departure_time: int
    point_count: int
    address: Optional[str]
    businesses: List[Business] = []

# Create SQLite tables if they don't exist
def init_db():
    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timestamp INTEGER NOT NULL,
            accuracy REAL NOT NULL
        )
    """)
    # Guards against the client resending an already-uploaded batch (e.g. a
    # retry that fires even though the server already processed it): without
    # this, executemany() blindly inserts fresh duplicate rows, which then
    # get reclustered into duplicate visited_places entries.
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_locations_dedup
        ON locations (latitude, longitude, timestamp, accuracy)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visited_places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            arrival_time INTEGER NOT NULL,
            departure_time INTEGER NOT NULL,
            point_count INTEGER NOT NULL,
            address TEXT
        )
    """)
    # Keyed on arrival/departure rather than lat/lon because the stored
    # coordinates are replaced by Google's resolved location.
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_visited_places_stay
        ON visited_places (arrival_time, departure_time)
    """)
    # Tracks the first location row of the stay-point candidate that is
    # still "open" (hasn't been broken by a point outside STAY_DIST_THRESHOLD_M
    # yet), so detection can resume from there instead of rescanning history.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stay_detection_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            candidate_start_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visited_place_businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visited_place_id INTEGER NOT NULL REFERENCES visited_places(id),
            name TEXT NOT NULL,
            types TEXT,
            place_id TEXT
        )
    """)
    conn.commit()
    conn.close()

# Initialize DB at startup
init_db()

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * r * asin(sqrt(a))

def reverse_geocode(lat: float, lon: float) -> Optional[dict]:
    """
    Returns {"address", "latitude", "longitude"} using Google's resolved
    location for the matched address (usually rooftop-accurate), which is
    more representative of the actual building than a raw noisy GPS fix.
    Returns None if no key is set or the lookup fails/finds nothing.
    """
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        top = results[0]
        location = top["geometry"]["location"]
        return {
            "address": top["formatted_address"],
            "latitude": location["lat"],
            "longitude": location["lng"],
        }
    except Exception:
        return None

def fetch_nearby_businesses(lat: float, lon: float) -> list:
    """
    Returns businesses at (lat, lon) via Places API (New) Nearby Search
    within NEARBY_BUSINESS_RADIUS_M, as
    [{"name", "types", "place_id", "address", "latitude", "longitude"}, ...].
    Each place's own location/address is included so callers can pick the
    closest one instead of making a separate Geocoding call. Empty list if
    no key is set or the lookup fails.
    """
    if not GOOGLE_MAPS_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.types,places.id,places.formattedAddress,places.location",
            },
            json={
                "maxResultCount": 20,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lon},
                        "radius": NEARBY_BUSINESS_RADIUS_M,
                    }
                },
            },
            timeout=5,
        )
        resp.raise_for_status()
        places = resp.json().get("places") or []
        return [
            {
                "name": p["displayName"]["text"],
                "types": p.get("types", []),
                "place_id": p["id"],
                "address": p.get("formattedAddress"),
                "latitude": p["location"]["latitude"],
                "longitude": p["location"]["longitude"],
            }
            for p in places
        ]
    except Exception:
        return []

def find_known_place(cursor: sqlite3.Cursor, lat: float, lon: float) -> Optional[tuple]:
    """
    Returns (id, latitude, longitude, address) of a visited place with an
    address within PLACE_REUSE_RADIUS_M of (lat, lon), or None. Prefers the
    address most places in range share (then the closest), since one building
    may already carry several labels from before and "closest" alone would
    keep flipping between them.
    """
    dlat = PLACE_REUSE_RADIUS_M / 111_320
    dlon = PLACE_REUSE_RADIUS_M / (111_320 * cos(radians(lat)))
    candidates = cursor.execute(
        """
        SELECT id, latitude, longitude, address FROM visited_places
        WHERE address IS NOT NULL
          AND latitude BETWEEN ? AND ?
          AND longitude BETWEEN ? AND ?
        """,
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall()
    in_range = [
        c for c in candidates
        if haversine_m(lat, lon, c[1], c[2]) <= PLACE_REUSE_RADIUS_M
    ]
    if not in_range:
        return None
    address_counts = Counter(c[3] for c in in_range)
    return min(
        in_range,
        key=lambda c: (-address_counts[c[3]], haversine_m(lat, lon, c[1], c[2])),
    )

def resolve_place(cursor: sqlite3.Cursor, lat: float, lon: float) -> tuple:
    """
    Returns (latitude, longitude, address, businesses) for a stay centred on
    (lat, lon). Reuses an already-known place within PLACE_REUSE_RADIUS_M so
    repeat visits get a consistent label; otherwise looks it up via Google.
    """
    known = find_known_place(cursor, lat, lon)
    if known:
        place_id, lat, lon, address = known
        biz_rows = cursor.execute(
            "SELECT name, types, place_id FROM visited_place_businesses WHERE visited_place_id = ?",
            (place_id,),
        ).fetchall()
        businesses = [
            {"name": b[0], "types": b[1].split(",") if b[1] else [], "place_id": b[2]}
            for b in biz_rows
        ]
        return lat, lon, address, businesses

    # Try Places first: it's one call that gives us both the businesses
    # list and (via each result's own location) an address, so a stay with
    # anything nearby needs no separate Geocoding call. Only fall back to
    # Geocoding when Places finds nothing within NEARBY_BUSINESS_RADIUS_M.
    businesses = fetch_nearby_businesses(lat, lon)
    if businesses:
        closest = min(
            businesses,
            key=lambda b: haversine_m(lat, lon, b["latitude"], b["longitude"]),
        )
        return closest["latitude"], closest["longitude"], closest["address"], businesses

    geocoded = reverse_geocode(lat, lon)
    if geocoded:
        return geocoded["latitude"], geocoded["longitude"], geocoded["address"], []
    return lat, lon, None, []

def detect_and_store_stay_points(conn: sqlite3.Connection):
    """
    Incremental stay-point detection (Li et al.): walks points in
    chronological order, growing a candidate cluster while each new point
    stays within STAY_DIST_THRESHOLD_M of the cluster's first point and
    within STAY_MAX_GAP_MS of the previous point (or a longer gap that
    resumes at the same spot, see STAY_GAP_BRIDGE_DIST_M). When a point breaks either
    condition, the cluster is finalized (and reverse-geocoded) if it spans at
    least STAY_TIME_THRESHOLD_MS, and a new candidate starts at the breaking
    point. Only reprocesses points since the last still-open candidate,
    tracked in stay_detection_state.
    """
    cursor = conn.cursor()
    state_row = cursor.execute(
        "SELECT candidate_start_id FROM stay_detection_state WHERE id = 1"
    ).fetchone()
    candidate_start_id = state_row[0] if state_row else None

    if candidate_start_id is None:
        min_id_row = cursor.execute("SELECT MIN(id) FROM locations").fetchone()
        candidate_start_id = min_id_row[0]
        if candidate_start_id is None:
            return

    points = cursor.execute(
        """
        SELECT id, latitude, longitude, timestamp FROM locations
        WHERE id >= ?
        ORDER BY timestamp ASC, id ASC
        """,
        (candidate_start_id,),
    ).fetchall()

    n = len(points)
    i = 0
    while i < n:
        j = i + 1
        while j < n:
            gap = points[j][3] - points[j - 1][3]
            if gap > STAY_MAX_GAP_MS and not (
                gap <= STAY_MAX_BRIDGED_GAP_MS
                and haversine_m(points[j - 1][1], points[j - 1][2], points[j][1], points[j][2])
                <= STAY_GAP_BRIDGE_DIST_M
            ):
                # Tracking was paused/stopped for a long stretch, or we
                # resurfaced somewhere else: treat this as an implicit
                # departure rather than bridging the cluster across it
                # (otherwise repeat visits to the same spot, e.g. home,
                # would all merge into one multi-day "stay").
                break
            dist = haversine_m(points[i][1], points[i][2], points[j][1], points[j][2])
            if dist > STAY_DIST_THRESHOLD_M:
                break
            j += 1

        if j == n:
            # Ran out of points without a break: this candidate might still
            # be growing, so leave it open rather than finalizing it.
            break

        duration = points[j - 1][3] - points[i][3]
        if duration >= STAY_TIME_THRESHOLD_MS:
            cluster = points[i:j]
            lat = sum(p[1] for p in cluster) / len(cluster)
            lon = sum(p[2] for p in cluster) / len(cluster)
            arrival = cluster[0][3]
            departure = cluster[-1][3]

            lat, lon, address, businesses = resolve_place(cursor, lat, lon)

            cursor.execute(
                """
                INSERT OR IGNORE INTO visited_places
                    (latitude, longitude, arrival_time, departure_time, point_count, address)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (lat, lon, arrival, departure, len(cluster), address),
            )
            # lastrowid isn't reset by an ignored insert, so check rowcount
            # instead or the businesses would attach to the previous place.
            inserted = cursor.rowcount == 1
            visited_place_id = cursor.lastrowid
            for business in businesses if inserted else []:
                cursor.execute(
                    """
                    INSERT INTO visited_place_businesses (visited_place_id, name, types, place_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        visited_place_id,
                        business["name"],
                        ",".join(business["types"]),
                        business["place_id"],
                    ),
                )
            # Commit per place so the write lock isn't held across the next
            # cluster's Google calls, which would block concurrent uploads.
            conn.commit()
            i = j
        else:
            # points[i] can never join a cluster with points[j] or beyond
            # (we already know it's out of range), so move past it.
            i += 1

    new_candidate_start_id = points[i][0] if i < n else points[-1][0] + 1
    cursor.execute(
        """
        INSERT INTO stay_detection_state (id, candidate_start_id) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET candidate_start_id = excluded.candidate_start_id
        """,
        (new_candidate_start_id,),
    )
    conn.commit()

def run_stay_detection():
    #  Fix for race condition where multiple threads could run this concurrently and step on each other.
    #  Assumes a single uvicorn process.
    with stay_detection_lock:
        conn = sqlite3.connect("locations.db")
        try:
            detect_and_store_stay_points(conn)
        finally:
            conn.close()

@app.post("/api/v1/locations", status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
def receive_locations(batch: LocationBatch, background_tasks: BackgroundTasks):
    """
    Synchronous 'def' handler: FastAPI runs this inside an external thread pool
    so the blocking SQLite disk operations don't freeze the main event loop.
    Clustering (which makes slow Google API calls) runs after the response is
    sent, so the client isn't left waiting long enough to time out and retry.
    """
    if not batch.locations:
        return {"status": "success", "received": 0}

    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()

    # Efficient bulk insert using executemany. OR IGNORE silently drops any
    # point that matches an already-stored (lat, lon, timestamp, accuracy),
    # so a resent batch is a no-op instead of creating duplicate rows.
    records = [
        (loc.latitude, loc.longitude, loc.timestamp, loc.accuracy)
        for loc in batch.locations
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO locations (latitude, longitude, timestamp, accuracy) VALUES (?, ?, ?, ?)",
        records
    )
    conn.commit()
    conn.close()

    background_tasks.add_task(run_stay_detection)

    return {"status": "success", "received": len(batch.locations)}

@app.get("/api/v1/locations", response_model=List[LocationRecord], dependencies=[Depends(verify_api_key)])
def get_locations(start: int, end: int):
    """
    Returns locations with timestamp in [start, end] (unix epoch, same
    units as LocationPoint.timestamp), ordered oldest to newest.
    """
    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, latitude, longitude, timestamp, accuracy
        FROM locations
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
        """,
        (start, end),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {"id": r[0], "latitude": r[1], "longitude": r[2], "timestamp": r[3], "accuracy": r[4]}
        for r in rows
    ]

@app.get("/api/v1/visited-places", response_model=List[VisitedPlace], dependencies=[Depends(verify_api_key)])
def get_visited_places(start: int, end: int):
    """
    Returns derived stay points (clustered visited places) that overlap
    [start, end] (unix epoch, same units as LocationPoint.timestamp), ordered
    oldest to newest. A stay extending past either edge is clipped to it, so
    a stay across midnight appears on both days with each day's portion. A
    place you're currently still at won't appear until a later point outside
    STAY_DIST_THRESHOLD_M confirms departure.
    """
    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, latitude, longitude, arrival_time, departure_time, point_count, address
        FROM visited_places
        WHERE arrival_time <= ? AND departure_time >= ?
        ORDER BY arrival_time ASC
        """,
        (end, start),
    )
    rows = cursor.fetchall()

    places = []
    for r in rows:
        arrival = max(r[3], start)
        departure = min(r[4], end)
        point_count = r[5]
        if (arrival, departure) != (r[3], r[4]):
            # A stay's points are a contiguous run in time, so the points in
            # the clipped window are exactly this day's share of the stay.
            point_count = cursor.execute(
                "SELECT COUNT(*) FROM locations WHERE timestamp BETWEEN ? AND ?",
                (arrival, departure),
            ).fetchone()[0]
        biz_rows = cursor.execute(
            "SELECT name, types, place_id FROM visited_place_businesses WHERE visited_place_id = ?",
            (r[0],),
        ).fetchall()
        places.append({
            "id": r[0],
            "latitude": r[1],
            "longitude": r[2],
            "arrival_time": arrival,
            "departure_time": departure,
            "point_count": point_count,
            "address": r[6],
            "businesses": [
                {"name": b[0], "types": b[1].split(",") if b[1] else [], "place_id": b[2]}
                for b in biz_rows
            ],
        })

    conn.close()
    return places
#uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
