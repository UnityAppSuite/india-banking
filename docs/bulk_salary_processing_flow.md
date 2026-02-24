# Bulk Salary Processing Flow: Payroll Entry → Payment Order

## Overview

This document describes the complete salary payment flow from Payroll Entry to Payment Order,
covering the **current manual flow** and the **new automated bulk salary processing flow**.

---

## Current Flow (Manual — 6 Steps)

### Step 1: Create & Submit Payroll Entry → Salary Slips

**Trigger**: User fills employee details and submits Payroll Entry.

| Action | File | Function | Line |
|--------|------|----------|------|
| Fill employees | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `fill_employee_details()` | 219 |
| Submit → create salary slips | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `on_submit()` → `create_salary_slips()` | 74, 258 |
| Background job (>30 employees) | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `create_salary_slips_for_employees()` | 1544 |

**Override (cn_indian_payroll)**: `cn_indian_payroll/overrides/payroll_entry.py` — `PayrollEntryOverride.fill_employee_details()` (line 60) adds employment type filtering and new joinee arrear handling.

**Override (walnut_hrms)**: `walnut_hrms/public/js/payroll.js` — Replaces "Get Employees" button with school-filtered employee selection.

### Step 2: Submit Salary Slips → Accrual Journal Entry

**Trigger**: User clicks "Submit Salary Slip" button.

| Action | File | Function | Line |
|--------|------|----------|------|
| JS button | `hrms/payroll/doctype/payroll_entry/payroll_entry.js` | `submit_salary_slip()` | 419 |
| Server method | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `submit_salary_slips()` | 321 |
| Background job (>30 slips) | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `submit_salary_slips_for_employees()` | 1626 |
| Create accrual JE | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `make_accrual_jv_entry()` | 556 |
| Build JE accounts | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `get_accounting_entries_and_payable_amount()` | 782 |
| Save & submit JE | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `make_journal_entry()` | 635 |
| Link JE to salary slips | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `set_journal_entry_in_salary_slips()` | 1103 |

**Accrual JE structure**:
- DEBIT: Earning component accounts (Basic Pay, HRA, etc.)
- CREDIT: Deduction component accounts (PF, TDS, etc.)
- CREDIT: Payroll Payable account (net salary)
  - When employee-wise: `party_type=Employee`, `party=employee_id`, `reference_type=Payroll Entry`

**Result**: Accrual JE is **auto-submitted**. `Salary Slip.journal_entry` is set to accrual JE name.

### Step 3: Create Bank Entry JE (MANUAL)

**Trigger**: User clicks "Make Bank Entry" button on Payroll Entry.

| Action | File | Function | Line |
|--------|------|----------|------|
| JS button | `hrms/payroll/doctype/payroll_entry/payroll_entry.js` | `add_bank_entry_button()` → `make_bank_entry()` | 181, 441 |
| Check existing entries | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `has_bank_entries()` | 886 |
| Get salary details | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `get_salary_slip_details()` | 954 |
| Build bank entry accounts | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `set_accounting_entries_for_bank_entry()` | 1012 |
| Create JE | `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `make_journal_entry(voucher_type="Bank Entry")` | 635 |

**Bank Entry JE structure**:
- CREDIT: Company bank/payment account
- DEBIT: Payroll Payable account
  - When employee-wise: `party_type=Employee`, `party=employee_id`, `reference_type=Payroll Entry`, `reference_name=payroll_entry.name`

**Result**: Bank Entry JE is saved in **DRAFT** state. User is routed to Journal Entry list.

### Step 4: Submit Bank Entry JE (MANUAL)

User navigates to the Bank Entry JE and submits it manually.

### Step 5: Create Payment Order from Bank Entry JE (MANUAL)

**Trigger**: User creates a new Payment Order, clicks "Get Payments from > Bank Entry(JV)".

| Action | File | Function | Line |
|--------|------|----------|------|
| JS button (india_banking) | `india_banking/public/js/payment_order.js` | `get_payments_from_journal_entry()` | 189 |
| Search query for eligible JEs | `india_banking/overrides/journal_entry.py` | `get_bank_entry()` | 204 |
| Map JE → Payment Order | `india_banking/overrides/journal_entry.py` | `make_payment_order()` | 12 |
| Validate party bank accounts | `india_banking/overrides/journal_entry.py` | `validate_party_bank_account()` | 15 |
| Build PO references from JEA rows | `india_banking/overrides/journal_entry.py` | `update_bank_entry()` | 74 |

**Payment Order Reference structure** (per employee):
```
reference_doctype: "Journal Entry"
reference_name: bank_entry_je.name
journal_entry_account: JEA_row.name  (stores the child row name)
amount: JEA.debit (net salary for employee)
party_type: "Employee"
party: employee_id
bank_account: employee's default Bank Account
```

**Validations**:
- Employee must have a default, enabled Bank Account (`is_default=1`, `disabled=0`)
- If bank account workflow is active: `workflow_state` must be "Approved"
- Bank Account currency must be INR

### Step 6: Get Summary, Submit PO, Initiate Payment (MANUAL)

| Action | File | Function | Line |
|--------|------|----------|------|
| Get Summary button | `india_banking/public/js/payment_order.js` | `get_summary()` | 368 |
| Generate summary | `india_banking/overrides/payment_order.py` | `get_party_summary()` | 244 |
| Determine transfer mode | `india_banking/overrides/payment_order.py` | `get_mode_of_transfer()` | 308 |
| Validate summary on save | `india_banking/overrides/payment_order.py` | `validate_summary()` | 47 |
| Submit PO | `india_banking/overrides/payment_order.py` | `on_submit()` | 127 |
| Update JEA payment status | `india_banking/overrides/payment_order.py` | `update_payment_status()` | 208 |
| Initiate Payment button | `india_banking/public/js/payment_order.js` | `make_payment()` | 288 |
| Bank API call | `india_banking/.../bank_connector.py` | `make_payment()` | 706 |

**Summary grouping fields** (from `india_banking/default.py` line 1):
```python
PAYMENT_SUMMARY_FIELDS = [
    "party_type", "party", "bank_account", "account",
    "cost_center", "project", "tax_withholding_category",
    "reference_doctype", "reference_name",
    "payment_entry", "journal_entry_account",
]
```

**Mode of Transfer selection** (`get_mode_of_transfer()` line 308):
- Same bank (party_bank == company_bank) → A2A/FT/Internal
- Different bank → IMPS (≤2L) / RTGS (>2L) / NEFT (fallback)

---

## New Flow: Bulk Salary Processing (Automated)

### Single Button: "Process Salary Payment"

**Location**: Payroll Entry form (submitted state, salary slips submitted)
**Implementation**: `india_banking/public/js/payroll_entry.js` + `india_banking/overrides/payroll_entry.py`

### User Choice Dialog

On click, user is presented with two options:

#### Option A: "Review Bank Entry" (mode="review")

Creates Bank Entry JE in **draft** state. User is routed to JE form for review.
From there, user follows the existing manual flow (submit JE → create PO → etc.)

**Flow**: `process_salary_payment(mode="review")`
1. Validate prerequisites
2. Call `payroll_entry.make_bank_entry()` (existing HRMS function)
3. Return `{journal_entry: je_name}`
4. JS routes to `Form/Journal Entry/{je_name}`

#### Option B: "Create Payment Order" (mode="direct")

Creates Bank Entry JE (submitted) + Payment Order with summary, all in one shot.
User is routed to Payment Order form (in draft, ready for review/submit/initiate).

**Flow**: `process_salary_payment(mode="direct")`
1. Validate prerequisites
2. Call `payroll_entry.make_bank_entry()` → Bank Entry JE (draft)
3. Submit the Bank Entry JE
4. Call `make_payment_order(bank_entry.name)` → Payment Order doc with references
5. Set `company_bank_account` from Payroll Entry's `bank_account`
6. Call `get_party_summary()` → auto-generate summary with transfer modes
7. Populate summary table on Payment Order, save
8. Return `{payment_order: po_name, journal_entry: je_name}`
9. JS routes to `Form/Payment Order/{po_name}`

### Prerequisites (Enforced)

| Check | Why |
|-------|-----|
| `docstatus == 1` | Payroll Entry must be submitted |
| `salary_slips_submitted == 1` | Salary slips must be submitted (accrual JE created) |
| `payment_account` is set | Required for Bank Entry JE credit side |
| `process_payroll_accounting_entry_based_on_employee == 1` | Each employee needs a separate JEA row with `party_type=Employee` for the Payment Order to map bank accounts |
| No existing Bank Entry for this Payroll Entry | Prevents duplicate bank entries |

### What Stays Unchanged

- All existing HRMS buttons ("Get Employees", "Submit Salary Slip", "Make Bank Entry", "Release Withheld Salaries")
- All existing Payment Order flows (from Payment Request, Payment Entry, Bank Entry JV)
- cn_indian_payroll override (employment type filter, new joinee arrear)
- walnut_hrms override (school-based employee filter)
- Payment initiation flow (Single/Bulk via bank connector)
- Bank status polling and UTR updates

---

## Key Files Reference

### HRMS (Source — not modified)

| File | Key Functions |
|------|---------------|
| `hrms/payroll/doctype/payroll_entry/payroll_entry.py` | `make_bank_entry()` (910), `has_bank_entries()` (886), `make_journal_entry()` (635), `set_accounting_entries_for_bank_entry()` (1012), `get_salary_slip_details()` (954) |
| `hrms/payroll/doctype/payroll_entry/payroll_entry.js` | `add_bank_entry_button()` (181), `make_bank_entry()` JS (441) |
| `hrms/payroll/doctype/payroll_entry/payroll_entry_dashboard.py` | Dashboard links: Salary Slip, Journal Entry |

### India Banking (Modified/Created)

| File | Key Functions |
|------|---------------|
| `india_banking/overrides/payroll_entry.py` | **NEW**: `process_salary_payment()` |
| `india_banking/public/js/payroll_entry.js` | **NEW**: "Process Salary Payment" button |
| `india_banking/overrides/journal_entry.py` | `make_payment_order()` (12), `get_bank_entry()` (204) — called from new flow |
| `india_banking/overrides/payment_order.py` | `get_party_summary()` (244), `get_mode_of_transfer()` (308) — called from new flow |
| `india_banking/hooks.py` | `doctype_js` registration for Payroll Entry |
| `india_banking/default.py` | `PAYMENT_SUMMARY_FIELDS` (1), `ALLOWED_PAYMENT_DOCTYPE` (123) |

### Other Apps (Not modified — context only)

| File | Relevance |
|------|-----------|
| `cn_indian_payroll/overrides/payroll_entry.py` | Extends `PayrollEntry` with employment type filter, new joinee arrear — unaffected |
| `walnut_hrms/public/js/payroll.js` | School-based employee selection — unaffected |
| `india_banking/.../bank_connector.py` | Payment initiation and status — unaffected |

---

## Accounting Flow Diagram

```
Payroll Entry (submitted)
    │
    ├── Salary Slips (submitted) ──── linked via: salary_slip.payroll_entry
    │
    ├── Accrual JE (submitted) ────── linked via: salary_slip.journal_entry
    │   ├── DEBIT:  Earning accounts
    │   ├── CREDIT: Deduction accounts
    │   └── CREDIT: Payroll Payable (party=Employee)
    │
    ├── Bank Entry JE (submitted) ── linked via: JEA.reference_type="Payroll Entry"
    │   ├── CREDIT: Company Bank Account
    │   └── DEBIT:  Payroll Payable (party=Employee, per employee)
    │
    └── Payment Order (draft) ────── linked via: POR.reference_name=JE, POR.journal_entry_account=JEA
        ├── References: one row per employee JEA
        ├── Summary: grouped by party/bank/dimensions
        └── Status: Pending → Submit → Initiate Payment → Processed
```

---

## Prerequisites for Accounts Team

Before using the "Process Salary Payment" button, the following must be configured:

### 1. Payroll Settings

| Setting | Where | Required Value |
|---------|-------|---------------|
| Process Payroll Accounting Entry Based on Employee | Payroll Settings | **Enabled** (checked) |

**Why**: Without this, the Bank Entry JE creates a single lump-sum row for Payroll Payable instead of employee-wise rows. The Payment Order flow requires individual rows per employee to map each employee's bank account.

**Where to find**: Setup > HR > Payroll Settings > Accounts section

### 2. Payroll Entry Fields

| Field | Where | Required |
|-------|-------|----------|
| Payment Account | Payroll Entry form > Salary Slip section | Must be set to the company's bank GL account (e.g., "Bank Account - UESF") |
| Bank Account | Payroll Entry form | Must be set to the company's Bank Account doc (e.g., "UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank"). This becomes the `company_bank_account` on the Payment Order. |

**Note**: `Payment Account` is the GL Account (Chart of Accounts) used for the credit side of the Bank Entry JE. `Bank Account` is the Bank Account DocType record linked to the company.

### 3. Employee Bank Accounts

Each employee in the Payroll Entry must have a **Bank Account** DocType record (not just the flat `bank_ac_no` field on Employee):

| Field | Required Value |
|-------|---------------|
| Party Type | Employee |
| Party | Employee ID (e.g., HR-EMP-00076) |
| Bank | Must match an existing Bank DocType (e.g., "ICICI Bank", "HDFC Bank") |
| Bank Account No | Employee's bank account number |
| Branch Code (IFSC) | Valid IFSC code (format: `^[A-Z]{4}0[A-Z0-9]{6}$`, e.g., "ICIC0001234") |
| Is Default | **Checked** |
| Disabled | **Unchecked** |
| Currency | INR |
| Email | Employee's email address (mandatory custom field) |

**If Bank Account Approval Workflow is active** (check India Banking Settings > `activate_workflow_on_bank_account`):
- Each Bank Account must have `workflow_state = "Approved"`
- Bank accounts in "Pending" state will be rejected during Payment Order creation

**Where to check**: HR > Employee > Bank Account linked section, or Bank Account list filtered by Party Type = Employee

### 4. Mode of Transfer

At least one Mode of Transfer must be configured for each payment scenario:

| Mode | Is Bank Specific | Bank | Min Limit | Max Limit | When Used |
|------|-----------------|------|-----------|-----------|-----------|
| A2A/FT/Internal | Yes | Company's bank (e.g., ICICI Bank) | 1 | 50 Cr | Same bank transfers |
| IMPS | No | - | 1 | 2,00,000 | Different bank, small amounts |
| RTGS | No | - | 2,00,000 | 50 Cr | Different bank, large amounts |
| NEFT | No | - | 1 | 50 Cr | Different bank, fallback |

**Important**: The A2A mode must have the **Bank** field set to your company's bank for same-bank matching to work.

**Where to find**: India Banking > Mode of Transfer

### 5. India Banking Settings

| Setting | Where | Value |
|---------|-------|-------|
| Summarise Payment Based On | India Banking Settings | "Party" (recommended for salary) — groups by employee instead of individual JE references |
| Activate Workflow on Bank Account | India Banking Settings | If enabled, all employee Bank Accounts must be in "Approved" workflow state |

### 6. Payroll Payable Account

| Setting | Where | Required |
|---------|-------|----------|
| Account Type | Chart of Accounts > Payroll Payable account | Must be **"Payable"** |

**Why**: The Bank Entry JE uses this account with `party_type=Employee` for each employee row. If account_type is not "Payable", the JE may fail validation.

---

## Testing Flow

### Test Environment

- **Site**: test.localhost
- **Company**: Unique Educational and Sports Foundation (UESF)
- **Company Bank Account**: UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank
- **Company GL Account**: Bank Account - UESF
- **Payroll Payable Account**: Payroll Payable - UESF

### Test Reference Documents (Persisted in DB)

#### Direct Mode Test

| Document | Name | Status |
|----------|------|--------|
| Payroll Entry | `HR-PRUN-2026-00007` | Submitted |
| Salary Slips | 86 slips (Dec 2025) | Submitted |
| Bank Entry JE | `ACC-JV-2026-00053` | Submitted |
| Payment Order | `PMO-00069` | Draft |

- PO has 86 references (one per employee) + 86 summary rows
- Total: Rs 18,97,323.24
- Transfer modes auto-assigned: A2A/FT/Internal (same bank) + IMPS (different bank, ≤2L)

#### Review Mode Test

| Document | Name | Status |
|----------|------|--------|
| Payroll Entry | `HR-PRUN-2026-00008` | Submitted |
| Salary Slips | 66 slips (Dec 2025) | Submitted |
| Bank Entry JE | `ACC-JV-2026-00054` | Draft |
| Payment Order | — | Not created (user reviews JE first) |

- JE has 66 employee debit rows + 1 company bank credit row
- Total: Rs 19,73,043.59
- User is expected to review JE, submit, then create PO manually

---

### Manual End-to-End Testing Flow

#### Before You Start — Verify Prerequisites

1. **Payroll Settings** (`/app/payroll-settings`)
   - "Process Payroll Accounting Entry Based on Employee" must be **enabled**

2. **India Banking Settings** (`/app/india-banking-settings`)
   - Note the value of "Summarise Payment Based On" (recommended: "Party" for salary)
   - If "Activate Workflow on Bank Account" is enabled, ensure all employee Bank Accounts are in "Approved" state

3. **Mode of Transfer** (`/app/mode-of-transfer`)
   - A2A/FT/Internal: `is_bank_specific=Yes`, **Bank must be set** to your company bank (e.g., ICICI Bank)
   - IMPS: `is_bank_specific=No`, min=1, max=2,00,000
   - RTGS: `is_bank_specific=No`, min=2,00,000, max=50,00,00,000
   - NEFT: `is_bank_specific=No`, min=1, max=50,00,00,000

4. **Employee Bank Accounts** (`/app/bank-account?party_type=Employee`)
   - Each employee must have a Bank Account doc with: `is_default=1`, `disabled=0`, `currency=INR`
   - Bank must match an existing Bank DocType record
   - Branch Code (IFSC) must be valid format: `^[A-Z]{4}0[A-Z0-9]{6}$`
   - Email field must be filled (mandatory custom field)
   - If workflow is active: `workflow_state` must be "Approved"

5. **Payroll Payable Account** (`/app/account/Payroll Payable - UESF`)
   - Account Type must be **empty** (not set) — HRMS v15 throws error if account_type is set on payroll payable account

#### Test A: Direct Mode (Create Payment Order)

1. Open a **submitted Payroll Entry** where salary slips are submitted
2. Set **Payment Account** (GL account, e.g., "Bank Account - UESF")
3. Set **Bank Account** (Bank Account doc, e.g., "UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank")
4. Click **India Banking > Process Salary Payment**
5. Select **"Create Payment Order"** from the dialog
6. Click **Proceed**
7. Wait for processing (freeze message: "Creating Bank Entry and Payment Order...")
8. You will be routed to the **Payment Order** form

**Verify on Payment Order:**
- [ ] PO is in **Draft** status
- [ ] `company_bank_account` is set to your company bank account
- [ ] **References** table has one row per employee with:
  - `reference_doctype` = Journal Entry
  - `party_type` = Employee
  - `party` = Employee ID
  - `bank_account` = Employee's bank account
  - `amount` = Employee's net salary
- [ ] **Summary** table is auto-populated with:
  - Correct amounts per employee
  - Mode of Transfer auto-assigned (A2A for same bank, IMPS/RTGS for different bank)
- [ ] `total` matches sum of all reference amounts

**Verify on Journal Entry** (linked from PO references):
- [ ] JE is **Submitted** (docstatus=1)
- [ ] Voucher Type = Bank Entry
- [ ] Reference No = Payroll Entry name
- [ ] CREDIT row: Company bank account (full salary amount)
- [ ] DEBIT rows: Payroll Payable account, one per employee with `party_type=Employee`
- [ ] Total Debit = Total Credit

**Continue to Payment:**
- [ ] Review the PO, submit it
- [ ] Click "Initiate Payment" to process via bank API

#### Test B: Review Mode (Review Bank Entry)

1. Open a **different submitted Payroll Entry** where salary slips are submitted (no existing bank entry)
2. Set **Payment Account** and **Bank Account** (same as above)
3. Click **India Banking > Process Salary Payment**
4. Select **"Review Bank Entry"** from the dialog
5. Click **Proceed**
6. You will be routed to the **Journal Entry** form

**Verify on Journal Entry:**
- [ ] JE is in **Draft** status (docstatus=0)
- [ ] Voucher Type = Bank Entry
- [ ] CREDIT row: Company bank account (full salary amount)
- [ ] DEBIT rows: Payroll Payable account, one per employee with `party_type=Employee`
- [ ] Total Debit = Total Credit

**Continue manually:**
- [ ] Review the JE, add Reference No/Date if needed, then **Submit**
- [ ] Create Payment Order manually: New Payment Order > Get Payments from > Bank Entry(JV) > Select the JE
- [ ] Click "Get Summary" on PO, review, submit, initiate payment

#### Test C: Prerequisite Validation

Test that the button correctly blocks when prerequisites are missing:

1. **Missing Payment Account**: Remove `payment_account` from PE, click button → should show "Payment Account is mandatory"
2. **Employee-wise OFF**: Disable "Process Payroll Accounting Entry Based on Employee" in Payroll Settings, click button → should show error linking to Payroll Settings
3. **Existing Bank Entry**: Try clicking button on a PE that already has a bank entry → should show "Bank Entry already exists"
4. **Unsubmitted salary slips**: Try on a PE where slips are not submitted → should show "Salary Slips must be submitted"

### Known Limitations

| Limitation | Detail |
|-----------|--------|
| Employees without Bank Account docs are silently skipped | The PO will only have references for employees who have valid Bank Account records. Check the PO reference count against salary slip count. |
| Bank Account workflow blocking | If `activate_workflow_on_bank_account` is ON, any employee with a "Pending" bank account will cause Payment Order creation to fail with "Cannot proceed with un-approved bank account" |
| A2A mode requires bank field | The A2A/FT/Internal Mode of Transfer must have its `bank` field set to your company's bank name, otherwise same-bank employees get no transfer mode assigned and PO validation fails |
| Synchronous execution | The entire flow runs synchronously. For very large payrolls (500+ employees), this may be slow. |
| Payment Order is Draft | The PO is created in draft — user must still review, submit, and initiate payment manually |
| Bank Entry Reference No | In direct mode, `cheque_no` is auto-set to the Payroll Entry name (e.g., "HR-PRUN-2026-00007"). User can edit this on the JE if needed before the PO is submitted. |
| One PE = One Bank Entry | Cannot create multiple bank entries for the same Payroll Entry. If a bank entry already exists (draft or submitted), the button will not appear. |

### Issues Encountered During Development

| Issue | Root Cause | Fix Applied |
|-------|-----------|-------------|
| Bank Entry JE submission fails: "Reference No & Reference Date is required" | ERPNext validates `cheque_no`/`cheque_date` for Bank Entry voucher type | Auto-set `cheque_no` to Payroll Entry name and `cheque_date` to posting date before submit (payroll_entry.py line 42-45) |
| Payment Order save fails: "Please validate the summary" | `validate_summary()` throws if summary is empty; PO was being saved before summary generation | Restructured to generate summary BEFORE first save (payroll_entry.py line 128-142) |
| "Cannot proceed with un-approved bank account" | Bank Account Approval Workflow is active; programmatically created Bank Accounts default to "Pending" state | Documented as prerequisite — accounts team must ensure Bank Accounts are approved |
| `get_mode_of_transfer` returns None for same-bank transfers | A2A Mode of Transfer had `bank=None` instead of company bank name | Documented as prerequisite — A2A mode must have Bank field set |
