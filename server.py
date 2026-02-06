from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import re
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict

# ------------------ APP INIT ------------------

app = FastAPI(title="Ethara.AI HRMS API")

# ------------------ CORS (ROBUST SETUP) ------------------

# Get origins from Env, defaulting to your Vercel app
env_origins = os.environ.get("CORS_ORIGINS", "https://ethara-frontend.vercel.app")

# Split by comma and clean up whitespace
origins_list = [origin.strip() for origin in env_origins.split(",") if origin.strip()]

# Add Localhost for local testing
default_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://ethara-frontend.vercel.app" 
]

# Combine and deduplicate
ALLOWED_ORIGINS = list(set(origins_list + default_origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ DATABASE ------------------

MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "ethara_db")

if not MONGO_URL:
    raise RuntimeError("MONGO_URL environment variable is not set!")

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

# ------------------ EMPLOYEE APIs ------------------

@api_router.post("/employees")
async def create_employee(emp: EmployeeCreate):
    # Check for duplicates
    if await db.employees.find_one({"employee_id": emp.employee_id}):
        raise HTTPException(status_code=409, detail="Employee ID already exists")

    if await db.employees.find_one({"email": emp.email}):
        raise HTTPException(status_code=409, detail="Email already exists")

    employee = Employee(**emp.model_dump())
    await db.employees.insert_one(employee.model_dump())
    return employee


@api_router.get("/employees")
async def get_employees(
    search: Optional[str] = None, 
    department: Optional[str] = None
):
    # ✅ FIX: Dynamic Query Construction
    query = {}

    # Filter by Department
    if department and department.lower() != "all":
        query["department"] = department

    # Filter by Search (Case Insensitive Regex)
    if search:
        search_regex = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"full_name": search_regex},
            {"email": search_regex},
            {"employee_id": search_regex}
        ]

    return await db.employees.find(query, {"_id": 0}).to_list(1000)


@api_router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: str):
    result = await db.employees.delete_one({"employee_id": employee_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Cascade delete attendance records
    await db.attendance.delete_many({"employee_id": employee_id})
    return {"message": "Employee deleted"}

# ------------------ ATTENDANCE APIs ------------------

@api_router.post("/attendance")
async def mark_attendance(att: AttendanceCreate):
    # 1. Verify employee exists
    if not await db.employees.find_one({"employee_id": att.employee_id}):
        raise HTTPException(status_code=404, detail="Employee not found")

    # ✅ FIX: Check for existing record (Upsert Logic)
    existing_record = await db.attendance.find_one({
        "employee_id": att.employee_id,
        "date": att.date
    })

    if existing_record:
        # Update existing
        await db.attendance.update_one(
            {"_id": existing_record["_id"]},
            {
                "$set": {
                    "status": att.status,
                    "marked_at": datetime.now(timezone.utc).isoformat()
                }
            }
        )
        # Return updated structure
        return {**existing_record, "status": att.status, "message": "Attendance updated"}
    
    else:
        # Create new
        record = AttendanceRecord(**att.model_dump())
        await db.attendance.insert_one(record.model_dump())
        return record


@api_router.get("/attendance")
async def get_attendance(employee_id: Optional[str] = None):
    query = {"employee_id": employee_id} if employee_id else {}
    return await db.attendance.find(query, {"_id": 0}).to_list(5000)

# ------------------ DASHBOARD API ------------------

@api_router.get("/dashboard")
async def dashboard():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Basic Counts
    total_employees = await db.employees.count_documents({})
    today_records = await db.attendance.find({"date": today}).to_list(5000)

    present = sum(1 for r in today_records if r.get("status") == "Present")
    absent = sum(1 for r in today_records if r.get("status") == "Absent")

    # 2. ✅ FIX: Department Breakdown
    pipeline = [
        {"$group": {"_id": "$department", "count": {"$sum": 1}}}
    ]
    dept_counts = await db.employees.aggregate(pipeline).to_list(None)
    
    # Convert to frontend-friendly dictionary
    department_breakdown = {item["_id"]: item["count"] for item in dept_counts}

    # 3. ✅ FIX: Recent Activity
    # Fetch last 5 attendance records, sorted by time descending
    recent_activity_cursor = db.attendance.find().sort("marked_at", -1).limit(5)
    recent_logs = []
    
    async for record in recent_activity_cursor:
        # Get employee name
        emp = await db.employees.find_one({"employee_id": record["employee_id"]})
        name = emp["full_name"] if emp else "Unknown"
        
        recent_logs.append({
            "id": str(record["_id"]),
            "action": f"Marked {record['status']}",
            "employee": name,
            "time": record["marked_at"]
        })

    return {
        "total_employees": total_employees,
        "present_today": present,
        "absent_today": absent,
        "unmarked_today": max(total_employees - present - absent, 0),
        "department_breakdown": department_breakdown,
        "recent_activity": recent_logs
    }

# ------------------ HEALTH & ROOT ------------------

@api_router.get("/health")
async def health():
    return {"status": "ok"}

@api_router.get("/")
async def root():
    return {"message": "Ethara.AI HRMS API is Running"}

# ------------------ APP CONFIG ------------------

app.include_router(api_router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ethara")

@app.on_event("shutdown")
async def shutdown():
    client.close()
