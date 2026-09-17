from datetime import date as date_cls
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError(f"Invalid ObjectId: {v}")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, field_schema):
        field_schema.update(type="string")


class ORMModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")


# ---------------- Customers ----------------
class CustomerCreate(BaseModel):
    name: str
    customer_type: str = "HOTEL"
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    billing_method: str = "PER_ITEM"
    payment_terms: str = "NET_30"
    is_active: bool = True
    notes: Optional[str] = None


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    customer_type: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    billing_method: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


# ---------------- Categories & Items ----------------
class CategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_active: bool = True


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ItemCreate(BaseModel):
    name: str
    category_id: Optional[str] = None
    standard_cost: float = 0.0
    default_rate: float = 0.0
    unit: str = "PIECE"
    is_active: bool = True


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    category_id: Optional[str] = None
    standard_cost: Optional[float] = None
    default_rate: Optional[float] = None
    unit: Optional[str] = None
    is_active: Optional[bool] = None


class CustomerRateCreate(BaseModel):
    customer_id: Optional[str] = None
    item_id: str
    rate: float = 0.0
    cost: float = 0.0
    is_active: bool = True


# ---------------- Transactions ----------------
class TransactionItemCreate(BaseModel):
    item_id: Optional[str] = None
    item_name: Optional[str] = None
    category_name: Optional[str] = None
    quantity_received: float = 0.0
    quantity_washed: float = 0.0
    quantity_delivered: float = 0.0
    quantity_rejected: float = 0.0
    quantity_damaged: float = 0.0
    quantity_missing: float = 0.0
    quantity_stored: float = 0.0
    rate: float = 0.0
    cost: float = 0.0
    line_total: float = 0.0
    line_cost: float = 0.0
    line_profit: float = 0.0
    status: str = "RECEIVED"
    notes: Optional[str] = None


class TransactionCreate(BaseModel):
    transaction_date: date_cls
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    invoice_number: Optional[str] = None
    status: str = "COMPLETED"
    source: str = "MANUAL"
    import_batch_id: Optional[str] = None
    items: List[TransactionItemCreate]
    total_quantity: float = 0.0
    total_amount: float = 0.0
    total_cost: float = 0.0
    total_profit: float = 0.0
    notes: Optional[str] = None


class TransactionUpdate(BaseModel):
    transaction_date: Optional[date_cls] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    invoice_number: Optional[str] = None
    status: Optional[str] = None
    items: Optional[List[TransactionItemCreate]] = None
    total_quantity: Optional[float] = None
    total_amount: Optional[float] = None
    total_cost: Optional[float] = None
    total_profit: Optional[float] = None
    notes: Optional[str] = None


# ---------------- Payments ----------------
class PaymentCreate(BaseModel):
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    amount: float
    payment_date: date_cls
    payment_method: str = "CASH"
    reference: Optional[str] = None
    notes: Optional[str] = None


class PaymentUpdate(BaseModel):
    amount: Optional[float] = None
    payment_date: Optional[date_cls] = None
    payment_method: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    customer_name: Optional[str] = None


# ---------------- Employees & Salaries ----------------
class EmployeeCreate(BaseModel):
    name: str
    position: Optional[str] = None
    department: str = "GENERAL"
    phone: Optional[str] = None
    nic: Optional[str] = None
    salary_type: str = "MONTHLY"
    basic_salary: float = 0.0
    daily_rate: float = 0.0
    weekly_rate: float = 0.0
    contract_amount: float = 0.0
    overtime_rate: float = 0.0
    allowance: float = 0.0
    allowance_type: str = "FIXED"
    epf_rate: float = 0.0
    etf_rate: float = 0.0
    epf_base: str = "ADJUSTED"
    attendance_required: bool = True
    salary_components: List[Dict[str, Any]] = Field(default_factory=list)
    joined_date: Optional[date_cls] = None
    leaving_date: Optional[date_cls] = None
    status: str = "ACTIVE"
    notes: Optional[str] = None


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    position: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    nic: Optional[str] = None
    salary_type: Optional[str] = None
    basic_salary: Optional[float] = None
    daily_rate: Optional[float] = None
    weekly_rate: Optional[float] = None
    contract_amount: Optional[float] = None
    overtime_rate: Optional[float] = None
    allowance: Optional[float] = None
    allowance_type: Optional[str] = None
    epf_rate: Optional[float] = None
    etf_rate: Optional[float] = None
    epf_base: Optional[str] = None
    attendance_required: Optional[bool] = None
    salary_components: Optional[List[Dict[str, Any]]] = None
    joined_date: Optional[date_cls] = None
    leaving_date: Optional[date_cls] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class SalaryPackageComponent(BaseModel):
    type: str = "OTHER_PAYMENT"
    name: str = ""
    amount: float = 0.0


class SalaryPackageBase(BaseModel):
    salary_type: str = "MONTHLY"
    attendance_required: bool = True
    basic_salary: float = 0.0
    daily_rate: float = 0.0
    weekly_rate: float = 0.0
    contract_amount: float = 0.0
    overtime_rate: float = 0.0
    allowance: float = 0.0
    allowance_type: str = "FIXED"
    epf_rate: float = 0.0
    etf_rate: float = 0.0
    epf_base: str = "ADJUSTED"
    salary_components: List[SalaryPackageComponent] = Field(default_factory=list)
    notes: Optional[str] = None


class SalaryPackageUpsert(SalaryPackageBase):
    employee_id: str
    month: str


class SalaryPackageUpdate(BaseModel):
    salary_type: Optional[str] = None
    attendance_required: Optional[bool] = None
    basic_salary: Optional[float] = None
    daily_rate: Optional[float] = None
    weekly_rate: Optional[float] = None
    contract_amount: Optional[float] = None
    overtime_rate: Optional[float] = None
    allowance: Optional[float] = None
    allowance_type: Optional[str] = None
    epf_rate: Optional[float] = None
    etf_rate: Optional[float] = None
    epf_base: Optional[str] = None
    salary_components: Optional[List[SalaryPackageComponent]] = None
    notes: Optional[str] = None


class SalaryCreate(BaseModel):
    month: str
    year: Optional[int] = None
    basic_salary: float = 0.0
    overtime_hours: float = 0.0
    overtime_rate: float = 0.0
    allowances: float = 0.0
    deductions: float = 0.0
    epf_deduction: float = 0.0
    etf_deduction: float = 0.0
    loan_deduction: float = 0.0
    advance_deduction: float = 0.0
    epf_employee: float = 0.0
    epf_employer: float = 0.0
    etf_employer: float = 0.0
    amount_paid: float = 0.0
    net_salary: float = 0.0
    paid: bool = False
    paid_date: Optional[date_cls] = None
    notes: Optional[str] = None


class SalaryUpdate(BaseModel):
    basic_salary: Optional[float] = None
    overtime_hours: Optional[float] = None
    overtime_rate: Optional[float] = None
    allowances: Optional[float] = None
    epf_deduction: Optional[float] = None
    etf_deduction: Optional[float] = None
    loan_deduction: Optional[float] = None
    advance_deduction: Optional[float] = None
    amount_paid: Optional[float] = None
    net_salary: Optional[float] = None
    paid: Optional[bool] = None
    paid_date: Optional[date_cls] = None
    notes: Optional[str] = None


class AttendanceCreate(BaseModel):
    employee_id: str
    date: date_cls
    status: str = "PRESENT"
    overtime_hours: float = 0.0
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    notes: Optional[str] = None


class AttendanceUpdate(BaseModel):
    status: Optional[str] = None
    overtime_hours: Optional[float] = None
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    notes: Optional[str] = None


class AttendanceBulkRecord(BaseModel):
    employee_id: str
    status: str = "PRESENT"
    overtime_hours: float = 0.0


class AttendanceBulkDay(BaseModel):
    date: date_cls
    records: List[AttendanceBulkRecord]


# ---------------- Salary Advances ----------------
class AdvanceCreate(BaseModel):
    employee_id: str
    amount: float
    date: date_cls
    reason: Optional[str] = None
    reference: Optional[str] = None


class AdvanceUpdate(BaseModel):
    amount: Optional[float] = None
    date: Optional[date_cls] = None
    reason: Optional[str] = None
    reference: Optional[str] = None
    status: Optional[str] = None


class AdvanceDeduct(BaseModel):
    advance_id: str
    salary_slip_id: Optional[str] = None
    amount_deducted: float
    reason: Optional[str] = None


# ---------------- Holidays ----------------
class HolidayCreate(BaseModel):
    name: str
    date: date_cls
    description: Optional[str] = None
    is_recurring: bool = False


class HolidayUpdate(BaseModel):
    name: Optional[str] = None
    date: Optional[date_cls] = None
    description: Optional[str] = None
    is_recurring: Optional[bool] = None


# ---------------- Extra Work ----------------
class ExtraWorkCategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None
    rate: float = 0.0
    calculation_method: str = "FIXED"
    unit: str = "DAY"
    is_active: bool = True


class ExtraWorkCategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    rate: Optional[float] = None
    calculation_method: Optional[str] = None
    unit: Optional[str] = None
    is_active: Optional[bool] = None


class ExtraWorkRecordCreate(BaseModel):
    employee_id: str
    category_id: str
    date: date_cls
    units: float = 1.0
    amount: float = 0.0
    notes: Optional[str] = None


class ExtraWorkRecordUpdate(BaseModel):
    category_id: Optional[str] = None
    date: Optional[date_cls] = None
    units: Optional[float] = None
    amount: Optional[float] = None
    notes: Optional[str] = None


# ---------------- Salary Slip (Enhanced) ----------------
class SalarySlipCreate(BaseModel):
    employee_id: str
    period_type: str = "MONTHLY"
    period_start: date_cls
    period_end: date_cls
    basic_salary: float = 0.0
    adjusted_base_salary: float = 0.0
    base_salary_for_period: float = 0.0
    calendar_days: int = 30
    working_days: int = 0
    worked_days: float = 0.0
    absent_days: float = 0.0
    leave_days: float = 0.0
    holiday_count: int = 0
    weekend_count: int = 0
    overtime_hours: float = 0.0
    overtime_rate: float = 0.0
    overtime_pay: float = 0.0
    allowances: float = 0.0
    allowance_details: List[Dict[str, Any]] = Field(default_factory=list)
    extra_work_total: float = 0.0
    extra_work_details: List[Dict[str, Any]] = Field(default_factory=list)
    bonus: float = 0.0
    other_payments: float = 0.0
    epf_employee: float = 0.0
    epf_employer: float = 0.0
    etf_employer: float = 0.0
    epf_base: str = "ADJUSTED"
    advance_deductions: float = 0.0
    advance_details: List[Dict[str, Any]] = Field(default_factory=list)
    loan_deduction: float = 0.0
    other_deductions: float = 0.0
    components: List[Dict[str, Any]] = Field(default_factory=list)
    attendance_required: bool = True
    calculation_method: str = "MONTHLY_ATTENDANCE"
    total_deductions: float = 0.0
    gross_salary: float = 0.0
    net_salary: float = 0.0
    amount_paid: float = 0.0
    paid: bool = False
    paid_date: Optional[date_cls] = None
    status: str = "DRAFT"
    notes: Optional[str] = None
    slip_number: Optional[str] = None


class SalarySlipUpdate(BaseModel):
    basic_salary: Optional[float] = None
    adjusted_base_salary: Optional[float] = None
    base_salary_for_period: Optional[float] = None
    worked_days: Optional[float] = None
    absent_days: Optional[float] = None
    leave_days: Optional[float] = None
    holiday_count: Optional[int] = None
    weekend_count: Optional[int] = None
    overtime_hours: Optional[float] = None
    overtime_rate: Optional[float] = None
    overtime_pay: Optional[float] = None
    allowances: Optional[float] = None
    allowance_details: Optional[List[Dict[str, Any]]] = None
    extra_work_total: Optional[float] = None
    extra_work_details: Optional[List[Dict[str, Any]]] = None
    bonus: Optional[float] = None
    other_payments: Optional[float] = None
    epf_employee: Optional[float] = None
    epf_employer: Optional[float] = None
    etf_employer: Optional[float] = None
    epf_base: Optional[str] = None
    advance_deductions: Optional[float] = None
    advance_details: Optional[List[Dict[str, Any]]] = None
    loan_deduction: Optional[float] = None
    other_deductions: Optional[float] = None
    components: Optional[List[Dict[str, Any]]] = None
    attendance_required: Optional[bool] = None
    calculation_method: Optional[str] = None
    total_deductions: Optional[float] = None
    gross_salary: Optional[float] = None
    net_salary: Optional[float] = None
    amount_paid: Optional[float] = None
    paid: Optional[bool] = None
    paid_date: Optional[date_cls] = None
    status: Optional[str] = None
    notes: Optional[str] = None


# ---------------- Company Settings ----------------
class CompanySettingsUpdate(BaseModel):
    company_name: Optional[str] = None
    working_days_per_week: Optional[int] = None
    working_days_pattern: Optional[List[int]] = None
    default_overtime_rate: Optional[float] = None
    salary_basis_days: Optional[int] = None


# ---------------- Expenses ----------------
class ExpenseCategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_active: bool = True


class ExpenseCategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ExpenseCreate(BaseModel):
    date: date_cls = Field(validation_alias=AliasChoices("date", "expense_date"))
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    amount: float
    description: Optional[str] = None
    reference: Optional[str] = None
    payment_method: str = "CASH"
    is_recurring: bool = False
    notes: Optional[str] = None


class ExpenseUpdate(BaseModel):
    date: Optional[date_cls] = Field(default=None, validation_alias=AliasChoices("date", "expense_date"))
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    reference: Optional[str] = None
    payment_method: Optional[str] = None
    is_recurring: Optional[bool] = None
    notes: Optional[str] = None


# ---------------- Bulk Import ----------------
class BulkImportCreate(BaseModel):
    file_name: str
    status: str = "IMPORTED"
    total_rows: int = 0
    success_rows: int = 0
    error_rows: int = 0
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    notes: Optional[str] = None

