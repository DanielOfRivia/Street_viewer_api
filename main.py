import os
import sqlite3
from math import asin, cos, radians, sin, sqrt
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
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

class VisitedPlace(BaseModel):
    id: int
    latitude: float
    longitude: float
    arrival_time: int
    departure_time: int
    point_count: int
    address: Optional[str]

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
    # Tracks the first location row of the stay-point candidate that is
    # still "open" (hasn't been broken by a point outside STAY_DIST_THRESHOLD_M
    # yet), so detection can resume from there instead of rescanning history.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stay_detection_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            candidate_start_id INTEGER
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

def reverse_geocode(lat: float, lon: float) -> Optional[str]:
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
        return results[0]["formatted_address"] if results else None
    except Exception:
        return None

def detect_and_store_stay_points(conn: sqlite3.Connection):
    """
    Incremental stay-point detection (Li et al.): walks points in
    chronological order, growing a candidate cluster while each new point
    stays within STAY_DIST_THRESHOLD_M of the cluster's first point and
    within STAY_MAX_GAP_MS of the previous point. When a point breaks either
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
            if gap > STAY_MAX_GAP_MS:
                # Tracking was paused/stopped for a long stretch: treat this
                # as an implicit departure rather than bridging the cluster
                # across it (otherwise repeat visits to the same spot, e.g.
                # home, would all merge into one multi-day "stay").
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
            address = reverse_geocode(lat, lon)
            cursor.execute(
                """
                INSERT INTO visited_places
                    (latitude, longitude, arrival_time, departure_time, point_count, address)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (lat, lon, arrival, departure, len(cluster), address),
            )
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

@app.post("/api/v1/locations", status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
def receive_locations(batch: LocationBatch):
    """
    Synchronous 'def' handler: FastAPI runs this inside an external thread pool
    so the blocking SQLite disk operations don't freeze the main event loop.
    """
    if not batch.locations:
        return {"status": "success", "received": 0}

    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()

    # Efficient bulk insert using executemany
    records = [
        (loc.latitude, loc.longitude, loc.timestamp, loc.accuracy)
        for loc in batch.locations
    ]
    cursor.executemany(
        "INSERT INTO locations (latitude, longitude, timestamp, accuracy) VALUES (?, ?, ?, ?)",
        records
    )
    conn.commit()

    detect_and_store_stay_points(conn)
    conn.close()

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
    Returns derived stay points (clustered visited places) with arrival_time
    in [start, end] (unix epoch, same units as LocationPoint.timestamp),
    ordered oldest to newest. A place you're currently still at won't appear
    until a later point outside STAY_DIST_THRESHOLD_M confirms departure.
    """
    conn = sqlite3.connect("locations.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, latitude, longitude, arrival_time, departure_time, point_count, address
        FROM visited_places
        WHERE arrival_time BETWEEN ? AND ?
        ORDER BY arrival_time ASC
        """,
        (start, end),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "latitude": r[1],
            "longitude": r[2],
            "arrival_time": r[3],
            "departure_time": r[4],
            "point_count": r[5],
            "address": r[6],
        }
        for r in rows
    ]
#uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
