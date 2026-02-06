from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import re
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional

# ------------------ APP INIT ------------------

app = FastAPI(title="Ethara.AI HRMS API")

# ------------------ CORS (FINAL FIX) ------------------

ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "https://ethara-frontend.vercel.app"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ DATABASE ------------------

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

# ------------------ ROUTER ------------------

api_router = APIRouter(prefix="/api")

# ------------------ MODELS ------------------

class EmployeeCreate(BaseModel):
    employee_id: str
    full_name: str
    email: str
    department: str

    @field_validator("employee_id", "full_name", "department")
    @classmethod
    def not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Field is required")
        return v.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(pattern, v):
            raise ValueError("Invalid email format")
        return v.strip()


class Employee(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    employee_id: str
    full_name: str
    email: str
    department: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AttendanceCreate(BaseModel):
    employee_id: str
    date: str
    status: str

    @field_validator("employee_id", "date", "status")
    @classmethod
    def not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Field is required")
        return v.strip()

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ["Present", "Absent"]:
            raise ValueError("Status must be Present or Absent")
        return v


class AttendanceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    employee_id: str
    date: str
    status: str
    marked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ------------------ EMPLOYEE APIs (FIXED) ------------------

@api_router.post("/employees")
async def create_employee(emp: EmployeeCreate):
    if await db.employees.find_one({"employee_id": emp.employee_id}):
        raise HTTPException(status_code=409, detail="Employee ID already exists")

    if await db.employees.find_one({"email": emp.email}):
        raise HTTPException(status_code=409, detail="Email already exists")

    employee = Employee(**emp.model_dump())
    await db.employees.insert_one(employee.model_dump())
    return employee


# ✅ FIXED: Now accepts search and department parameters
@api_router.get("/employees")
async def get_employees(
    search: Optional[str] = None, 
    department: Optional[str] = None
):
    # 1. Start with an empty query
    query = {}

    # 2. Add Department Filter
    if department and department.lower() != "all":
        query["department"] = department

    # 3. Add Search Filter (Regex for Name, Email, or ID)
    if search:
        # "i" option makes it case-insensitive
        search_regex = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"full_name": search_regex},
            {"email": search_regex},
            {"employee_id": search_regex}
        ]

    # 4. Run the query
    return await db.employees.find(query, {"_id": 0}).to_list(1000)


@api_router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: str):
    result = await db.employees.delete_one({"employee_id": employee_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")

    await db.attendance.delete_many({"employee_id": employee_id})
    return {"message": "Employee deleted"}

# ------------------ ATTENDANCE APIs ------------------

@api_router.post("/attendance")
async def mark_attendance(att: AttendanceCreate):
    if not await db.employees.find_one({"employee_id": att.employee_id}):
        raise HTTPException(status_code=404, detail="Employee not found")

    record = AttendanceRecord(**att.model_dump())
    await db.attendance.insert_one(record.model_dump())
    return record


@api_router.get("/attendance")
async def get_attendance(employee_id: Optional[str] = None):
    query = {"employee_id": employee_id} if employee_id else {}
    return await db.attendance.find(query, {"_id": 0}).to_list(5000)

# ------------------ DASHBOARD (SAFE) ------------------

@api_router.get("/dashboard")
async def dashboard():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total_employees = await db.employees.count_documents({})
    today_records = await db.attendance.find({"date": today}).to_list(5000)

    present = sum(1 for r in today_records if r.get("status") == "Present")
    absent = sum(1 for r in today_records if r.get("status") == "Absent")

    return {
        "total_employees": total_employees,
        "present_today": present,
        "absent_today": absent,
        "unmarked_today": max(total_employees - present - absent, 0),
    }

# ------------------ HEALTH ------------------

@api_router.get("/health")
async def health():
    return {"status": "ok"}

# ------------------ ROOT ------------------

@api_router.get("/")
async def root():
    return {"message": "Ethara.AI HRMS API"}

# ------------------ REGISTER ROUTES ------------------

app.include_router(api_router)

# ------------------ LOGGING ------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ethara")

# ------------------ SHUTDOWN ------------------

@app.on_event("shutdown")
async def shutdown():
    client.close()
