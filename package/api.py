"""따릉길 web + Bicycle ETA API. Run: uvicorn api:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations
import os, sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from eta_service import BicycleETAService

ROOT = Path(__file__).resolve().parent.parent
INVENTORY_CODE = ROOT / "inventory_model" / "02_코드"
if str(INVENTORY_CODE) not in sys.path:
    sys.path.insert(0, str(INVENTORY_CODE))

class ETARequest(BaseModel):
    start_station_id: str
    end_station_id: str
    departure: str = Field(description="Asia/Seoul local time, e.g. 2026-10-15T08:30:00")
    bike_type: str = "regular"
    user_type: str = "domestic"
    use_kma: bool = False
    allow_weather_fallback: bool = True
    weather: dict[str, Any] | None = None

class BatchETARequest(BaseModel):
    requests: list[ETARequest]

class InventoryRiskRequest(BaseModel):
    station_id: str = Field(description="Inventory model ST-ID; separate ID system from ETA station IDs")
    current_bikes: int = Field(ge=0, le=500)
    eta_minutes: float = Field(ge=0, le=60)
    as_of: str
    snapshot_at: str | None = None
    epsilon: float = Field(default=0.10, gt=0, lt=1)
    rental_multiplier: float = Field(default=1.0, ge=0, le=5)
    return_multiplier: float = Field(default=1.0, ge=0, le=5)

service: BicycleETAService | None = None
inventory_predictor = None

@asynccontextmanager
async def lifespan(_: FastAPI):
    global service
    service = BicycleETAService()
    try:
        yield
    finally:
        if service is not None:
            service.close()

app = FastAPI(title="따릉길 경로 추천 API", version="1.0.0", lifespan=lifespan)

def current_service() -> BicycleETAService:
    if service is None:
        raise HTTPException(status_code=503, detail="모델을 불러오는 중입니다.")
    return service

def run_prediction(request: ETARequest) -> dict:
    model = current_service()
    common = {"start_station_id": request.start_station_id, "end_station_id": request.end_station_id,
              "departure": request.departure, "bike_type": request.bike_type, "user_type": request.user_type}
    if request.weather is not None:
        return model.predict(**common, weather=request.weather)
    if not request.use_kma:
        return model.predict(**common)
    try:
        return model.predict_with_kma(**common)
    except Exception as error:
        if not request.allow_weather_fallback:
            raise HTTPException(status_code=502, detail=f"기상청 예보 조회 실패: {error}") from error
        result = model.predict(**common)
        result.setdefault("model", {})["weather_source"] = "missing_model_fallback"
        result["model"]["weather_error"] = str(error)
        return result

@app.get("/")
async def home():
    return FileResponse(ROOT / "index.html")

@app.get("/health")
async def health():
    model = current_service()
    counts = model.stations["model_station_status"].value_counts().to_dict()
    return {"status":"ok", "model_version":model.model_version, "active_station_count":len(model.stations),
            "station_status_counts":counts, "route_source":"OSM/SRTM offline graph",
            "kma_key_configured":bool(os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY"))}

@app.get("/v1/stations")
async def stations(district: str | None = None, search: str | None = None,
                   limit: int = Query(default=3000, ge=1, le=5000)):
    frame = current_service().station_table(district=district)
    if search:
        matched = (frame["station_id"].astype(str).str.contains(search, case=False, na=False) |
                   frame["station_name"].astype(str).str.contains(search, case=False, na=False))
        frame = frame.loc[matched]
    columns = ["station_id","station_name","district","latitude","longitude","model_station_status"]
    selected = frame[columns].head(limit)
    return {"count":len(selected), "stations":selected.to_dict("records")}

@app.post("/v1/eta")
async def eta(request: ETARequest):
    try:
        return run_prediction(request)
    except HTTPException:
        raise
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

@app.post("/v1/eta/batch")
async def eta_batch(request: BatchETARequest):
    if len(request.requests) > 100:
        raise HTTPException(status_code=422, detail="한 번에 최대 100개 경로까지 요청할 수 있습니다.")
    try:
        return {"count":len(request.requests), "results":[run_prediction(item) for item in request.requests]}
    except HTTPException:
        raise
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

@app.post("/v1/inventory-risk")
async def inventory_risk(request: InventoryRiskRequest):
    """Inventory risk model endpoint; requires its own ST-ID and current inventory input."""
    global inventory_predictor
    try:
        if inventory_predictor is None:
            from predictor import InventoryPredictor
            inventory_predictor = InventoryPredictor(rates_path=ROOT / "inventory_model" / "03_모델" / "weekly_rates.csv")
        return inventory_predictor.predict(**request.model_dump())
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
