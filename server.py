from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import re
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional
import uuid
from datetime import datetime, timezone, date

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---- Models ----

class EmployeeCreate(BaseModel):
    employee_id: str
    full_name: str
    email: str
    department: str

    @field_validator('employee_id')
    @classmethod
    def validate_employee_id(cls, v):
        if not v or not v.strip():
            raise ValueError('Employee ID is required')
        return v.strip()

    @field_validator('full_name')
    @classmethod
    def validate_full_name(cls, v):
        if not v or not v.strip():
            raise ValueError('Full Name is required')
        return v.strip()

    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if not v or not v.strip():
            raise ValueError('Email is required')
        v = v.strip()
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, v):
            raise ValueError('Invalid email format')
        return v

    @field_validator('department')
    @classmethod
    def validate_department(cls, v):
        if not v or not v.strip():
            raise ValueError('Department is required')
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

    @field_validator('employee_id')
    @classmethod
    def validate_employee_id(cls, v):
        if not v or not v.strip():
            raise ValueError('Employee ID is required')
        return v.strip()

    @field_validator('date')
    @classmethod
    def validate_date(cls, v):
        if not v or not v.strip():
            raise ValueError('Date is required')
        try:
            datetime.strptime(v.strip(), '%Y-%m-%d')
        except ValueError:
            raise ValueError('Invalid date format. Use YYYY-MM-DD')
        return v.strip()

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if not v or not v.strip():
            raise ValueError('Status is required')
        v = v.strip()
        if v not in ['Present', 'Absent']:
            raise ValueError('Status must be Present or Absent')
        return v

class AttendanceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    employee_id: str
    date: str
    status: str
    marked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ---- Employee APIs ----

@api_router.post("/employees")
async def create_employee(emp: EmployeeCreate):
    existing = await db.employees.find_one({"employee_id": emp.employee_id}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail=f"Employee ID '{emp.employee_id}' already exists")
    
    email_exists = await db.employees.find_one({"email": emp.email}, {"_id": 0})
    if email_exists:
        raise HTTPException(status_code=409, detail=f"Email '{emp.email}' already exists")

    employee = Employee(**emp.model_dump())
    doc = employee.model_dump()
    await db.employees.insert_one(doc)
    return {k: v for k, v in doc.items() if k != '_id'}

@api_router.get("/employees")
async def get_employees(department: Optional[str] = None, search: Optional[str] = None):
    query = {}
    if department:
        query["department"] = department
    if search:
        query["$or"] = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"employee_id": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
        ]
    employees = await db.employees.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return employees

@api_router.get("/employees/{employee_id}")
async def get_employee(employee_id: str):
    emp = await db.employees.find_one({"employee_id": employee_id}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp

@api_router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: str):
    result = await db.employees.delete_one({"employee_id": employee_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    await db.attendance.delete_many({"employee_id": employee_id})
    return {"message": f"Employee '{employee_id}' deleted successfully"}

@api_router.get("/departments")
async def get_departments():
    departments = await db.employees.distinct("department")
    return departments

# ---- Attendance APIs ----

@api_router.post("/attendance")
async def mark_attendance(att: AttendanceCreate):
    emp = await db.employees.find_one({"employee_id": att.employee_id}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    existing = await db.attendance.find_one(
        {"employee_id": att.employee_id, "date": att.date}, {"_id": 0}
    )
    if existing:
        await db.attendance.update_one(
            {"employee_id": att.employee_id, "date": att.date},
            {"$set": {"status": att.status, "marked_at": datetime.now(timezone.utc).isoformat()}}
        )
        updated = await db.attendance.find_one(
            {"employee_id": att.employee_id, "date": att.date}, {"_id": 0}
        )
        return updated
    
    record = AttendanceRecord(**att.model_dump())
    doc = record.model_dump()
    await db.attendance.insert_one(doc)
    return {k: v for k, v in doc.items() if k != '_id'}

@api_router.get("/attendance")
async def get_attendance(employee_id: Optional[str] = None, date_filter: Optional[str] = None):
    query = {}
    if employee_id:
        query["employee_id"] = employee_id
    if date_filter:
        query["date"] = date_filter
    records = await db.attendance.find(query, {"_id": 0}).sort("date", -1).to_list(5000)
    return records

@api_router.get("/attendance/summary/{employee_id}")
async def get_attendance_summary(employee_id: str):
    emp = await db.employees.find_one({"employee_id": employee_id}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    records = await db.attendance.find({"employee_id": employee_id}, {"_id": 0}).to_list(5000)
    total_present = sum(1 for r in records if r.get("status") == "Present")
    total_absent = sum(1 for r in records if r.get("status") == "Absent")
    total_days = len(records)
    
    return {
        "employee_id": employee_id,
        "full_name": emp.get("full_name", ""),
        "total_days": total_days,
        "total_present": total_present,
        "total_absent": total_absent,
        "attendance_rate": round((total_present / total_days * 100), 1) if total_days > 0 else 0
    }

# ---- Dashboard API ----

@api_router.get("/dashboard")
async def get_dashboard():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    total_employees = await db.employees.count_documents({})
    
    today_attendance = await db.attendance.find({"date": today}, {"_id": 0}).to_list(5000)
    present_today = sum(1 for r in today_attendance if r.get("status") == "Present")
    absent_today = sum(1 for r in today_attendance if r.get("status") == "Absent")
    
    # Department breakdown
    pipeline = [
        {"$group": {"_id": "$department", "count": {"$sum": 1}}}
    ]
    dept_breakdown = await db.employees.aggregate(pipeline).to_list(100)
    departments = [{"department": d["_id"], "count": d["count"]} for d in dept_breakdown]
    
    # Recent activity (last 10 attendance records)
    recent = await db.attendance.find({}, {"_id": 0}).sort("marked_at", -1).to_list(10)
    # Enrich with employee names
    for record in recent:
        emp = await db.employees.find_one({"employee_id": record["employee_id"]}, {"_id": 0})
        record["full_name"] = emp.get("full_name", "Unknown") if emp else "Unknown"
    
    return {
        "total_employees": total_employees,
        "present_today": present_today,
        "absent_today": absent_today,
        "unmarked_today": total_employees - present_today - absent_today,
        "departments": departments,
        "recent_activity": recent
    }

@api_router.get("/")
async def root():
    return {"message": "Ethara.AI HRMS API"}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
