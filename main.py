import os
import sqlite3
from typing import List
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="GPS Tracking API")

API_KEY = os.environ["API_KEY"]

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

# Create SQLite table if it doesn't exist
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
    conn.commit()
    conn.close()

# Initialize DB at startup
init_db()

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
#uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload