# ICICI Payment Flow Documentation

## Table of Contents

- [1. Architecture Overview with Request Flow](#1-architecture-overview-with-request-flow)
- [2. DocType Relationships - All Key DocTypes and How They Connect](#2-doctype-relationships---all-key-doctypes-and-how-they-connect)
- [3. Connector Record Resolution - How the Right Connector is Found](#3-connector-record-resolution---how-the-right-connector-is-found)
- [4. Single Payment Flow - Complete Step-by-Step with Code References](#4-single-payment-flow---complete-step-by-step-with-code-references)
- [5. Bulk Payment Flow - Complete Step-by-Step including OTP, File Construction, Encryption](#5-bulk-payment-flow---complete-step-by-step-including-otp-file-construction-encryption)
- [6. Naming Conventions - How unique_id Differs Between Single and Bulk](#6-naming-conventions---how-unique_id-differs-between-single-and-bulk)
- [7. API Endpoints Table - Actual ICICI Production URLs for Single vs Bulk](#7-api-endpoints-table---actual-icici-production-urls-for-single-vs-bulk)
- [8. Encryption Differences - RSA-only vs RSA+AES](#8-encryption-differences---rsa-only-vs-rsaaes)
- [9. Real Working Examples](#9-real-working-examples)

---

## 1. Architecture Overview with Request Flow

### Two-App Architecture

| App | Server Location | Role |
|-----|----------------|------|
| **india_banking** | ERP Server (e.g., main Frappe site) | Core app - Payment Order UI, Bank Connector config, payment orchestration, status tracking |
| **india_banking_connector** | Connector Server (e.g., `https://erp.walnutedu.in`) | Bank-specific implementations - encryption, API payloads, bank communication |

**Simple English:** The ERP server handles all the business logic (Payment Orders, Payment Entries, user interface). When it needs to talk to a bank, it sends a POST request to a separate connector server, which knows how to format data, encrypt it, and communicate with ICICI's specific API.

### Complete Request Flow Diagram (Method-by-Method)

```
USER BROWSER
│
│ (1) User clicks "Initiate Payment" button
│     payment_order.js:249-251 → frm.trigger("make_payment")
│
│ (2) JS calls frappe.call
│     payment_order.js:289-306 → frappe.call({
│       method: "india_banking...bank_connector.make_payment",
│       args: { payment_order: frm.doc.name }
│     })
│
▼ ─── HTTP POST to ERP Server ───
│
ERP SERVER (india_banking)
│
│ (3) Whitelisted entry point
│     bank_connector.py:705-713 → make_payment(payment_order, otp=None)
│     │
│     ├── (3a) frappe.get_doc("Payment Order", payment_order)
│     │         → loads the full Payment Order doc with all children
│     │
│     ├── (3b) get_bank_connector(company_bank_account, company)
│     │         bank_connector.py:690-702
│     │         → frappe.db.exists("Bank Connector", {company, bank_account})
│     │         → frappe.get_doc("Bank Connector", name)
│     │         → Returns the Bank Connector doc (has bulk_transaction flag)
│     │
│     └── (3c) bank_connector.make_post_request(payment_order, action="initiate_payment")
│               bank_connector.py:85-139
│
│ (4) Inside make_post_request():
│     ├── (4a) check_otp_enabled(otp) — bank_connector.py:50-54
│     │         → checks if (bank, bulk_transaction) is in OTP_ENABLED_BANK
│     │         → OTP_ENABLED_BANK = [("ICICI Bank", 1)] — line 24-26
│     │
│     ├── (4b) Pre-initiation status check — bank_connector.py:92-96
│     │         → recursive call: make_post_request(action="get_payment_status")
│     │
│     ├── (4c) BRANCH on self.bulk_transaction — bank_connector.py:103
│     │
│     │   IF bulk_transaction=1 (line 103-116):
│     │     → get_payload() — bank_connector.py:59-77
│     │     → ONE POST to connector_url with full Payment Order
│     │
│     │   ELSE bulk_transaction=0 (line 117-133):
│     │     → For EACH summary row:
│     │       → make_single_request() — bank_connector.py:357-373
│     │       → POST per summary with individual party details
│     │
│     └── (4d) POST to self.connector_url
│               = f"{self.url}/api/method/india_banking_connector.api.connect"
│               With headers: Authorization token + Content-Type: application/json
│
▼ ─── HTTP POST to Connector Server ───
│
CONNECTOR SERVER (india_banking_connector)
│
│ (5) API entry point
│     api.py:7-28 → connect(**payload)
│     │
│     ├── (5a) PayloadValidator(payload) — api.py:31-62
│     │         → validates the payload is valid JSON/dict
│     │
│     ├── (5b) settings = frappe.get_single("Connector Settings")
│     │         → loads the singleton config document
│     │
│     ├── (5c) connector = settings.get_connector(payload)
│     │         connector_settings.py:14-49
│     │         │
│     │         ├── get_bank_connector(company_bank) — line 57-65
│     │         │   └── check_connector(bank) — line 67-97
│     │         │       → queries Connector Map child table by bank name
│     │         │       → returns "ICICI Connector" (the DocType name)
│     │         │
│     │         ├── frappe.get_doc("ICICI Connector", {account_number: ...})
│     │         │   → loads the specific ICICI Connector record
│     │         │
│     │         └── Sets: connector_doc.bulk_transaction = payload.bulk_transaction
│     │                   connector_doc.doc = payload.doc
│     │                   connector_doc.payment_doc = payload
│     │
│     └── (5d) connector.get_response(method)
│               base bank_connector.py:53-58 (connector app)
│               → calls getattr(self, method)()
│               → dispatches to: initiate_payment() / get_payment_status() / generate_otp()
│
│ (6) ICICI Connector executes bank-specific logic
│     icici_connector.py:110-140 (initiate_payment)
│     │
│     ├── (6a) update_client_details("make_payment") — line 685-694
│     │         → sets self.client_key from password field
│     │
│     ├── (6b) validate_duplicate_payments(unique_id) — base bank_connector.py:60-90
│     │         → checks Bank Request Log for existing payment with same unique_id
│     │
│     ├── (6c) get_encrypted_payload(method="make_payment") — icici_connector.py:197-223
│     │         → calls get_account_config() → set_payment_data() (line 339-401)
│     │         → encrypts based on bulk_transaction flag
│     │
│     ├── (6d) Resolves URL: self.urls.make_payment
│     │         base bank_connector.py:28-47 (connector app)
│     │         → queries Bank API Endpoint filtered by bank + environment + bulk_transaction
│     │
│     └── (6e) requests.post(url, headers, data=payload)
│               → ACTUAL HTTP call to ICICI Bank API
│
▼ ─── HTTP POST to ICICI Bank API ───
│
ICICI BANK API SERVER
│  Processes payment and returns encrypted response
│
▼ ─── HTTP Response ───
│
CONNECTOR SERVER
│
│ (7) Decrypt & format response
│     icici_connector.py:433-473 → get_decrypted_response()
│     icici_connector.py:475-568 → get_formated_response()
│     │
│     └── Returns standardized dict:
│         {
│           payment_status: "ACCEPTED" / "FAILED",
│           file_sequence_number: "..." (bulk only),
│           summary_details: { summary_name: { payment_status: "Accepted" } }
│         }
│
▼ ─── HTTP Response back to ERP Server ───
│
ERP SERVER
│
│ (8) Process response
│     bank_connector.py:141-147 → verify_response()
│     │
│     ├── (8a) verify_payment_response() — bank_connector.py:149-231
│     │         → updates Payment Order Summary rows based on summary_details
│     │         → stores file_sequence_number on Payment Order (bulk)
│     │
│     └── (8b) update_payment_status() — bank_connector.py:459-513
│               → sets Payment Order status: Pending/Initiated/Approved/Failed
│
│ (9) Returns to JS
│     → payment_order.js:303 → frm.reload_doc()
│
▼
USER BROWSER — sees updated Payment Order with new status
```

---

## 2. DocType Relationships - All Key DocTypes and How They Connect

### Visual Relationship Map

```
                        ┌─────────────────────────┐
                        │    Payment Order (PO)    │
                        │  (india_banking override) │
                        │  CustomPaymentOrder class │
                        │                           │
                        │  Fields:                  │
                        │  ├─ company               │
                        │  ├─ company_bank_account ─┼──────────────┐
                        │  ├─ company_bank          │              │
                        │  ├─ payment_order_type    │              │
                        │  ├─ posting_date          │              │
                        │  ├─ status                │              │
                        │  ├─ total                 │              │
                        │  ├─ file_sequence_number  │              │
                        │  │  (bulk only, from bank)│              │
                        │  ├─ summarise_payment_    │              │
                        │  │  based_on (Party/Voucher)             │
                        │  └─ default_mode_of_      │              │
                        │     transfer              │              │
                        └──┬────────────┬───────────┘              │
                           │            │                          │
              ┌────────────┘            └────────────┐             │
              ▼                                      ▼             ▼
┌─────────────────────────┐          ┌──────────────────────────────────────┐
│ Payment Order Reference │          │         Bank Account (ERPNext)       │
│   (child table of PO)   │          │                                      │
│                         │          │  (company_bank_account links here)    │
│  Fields:                │          │  Fields:                              │
│  ├─ party_type          │          │  ├─ bank (e.g., "ICICI Bank")        │
│  ├─ party               │          │  ├─ bank_account_no                  │
│  ├─ amount              │          │  ├─ account_name                     │
│  ├─ bank_account ───────┼──┐       │  ├─ branch_code (IFSC)              │
│  ├─ payment_request ────┼──┼──┐    │  ├─ is_company_account              │
│  ├─ payment_entry       │  │  │    │  ├─ is_default                      │
│  ├─ reference_doctype   │  │  │    │  └─ party / party_type              │
│  ├─ reference_name      │  │  │    └──────────────────────────────────────┘
│  └─ is_adhoc            │  │  │               │
└─────────────────────────┘  │  │               │ (same bank_account field)
                             │  │               │
              ┌──────────────┘  │               │
              ▼                 │               │
┌─────────────────────────┐    │    ┌───────────────────────────────┐
│ Payment Order Summary   │    │    │    Bank Connector              │
│   (child table of PO)   │    │    │  (india_banking DocType)       │
│                         │    │    │  autoname: field:bank_account  │
│ One per party (grouped) │    │    │  ONE record per bank account   │
│                         │    │    │                                │
│  Fields:                │    │    │  Fields:                       │
│  ├─ party_type          │    │    │  ├─ company                    │
│  ├─ party               │    │    │  ├─ bank_account ──────────────┤
│  ├─ amount (sum)        │    │    │  ├─ bank (fetched)             │
│  ├─ bank_account ───────┼────┼──▶ │  ├─ url (connector server URL)│
│  ├─ bank                │    │    │  ├─ api_key                    │
│  ├─ bank_account_no     │    │    │  ├─ api_secret                 │
│  ├─ account_name        │    │    │  └─ bulk_transaction ◀─────────┤
│  ├─ branch_code (IFSC)  │    │    │     (Check, only for ICICI)    │
│  ├─ mode_of_transfer    │    │    └────────────────────────────────┘
│  ├─ payment_status      │    │
│  ├─ payment_initiated   │    │
│  ├─ payment_entry ──────┼────┼────▶ Payment Entry (created on submit)
│  ├─ payment_date        │    │
│  ├─ reference_number    │    │      (UTR from bank, set after Processed)
│  ├─ summary_references  │    │      (JSON list of PO Reference names)
│  ├─ message             │    │
│  └─ email               │    │
└─────────────────────────┘    │
                               │
              ┌────────────────┘
              ▼
┌─────────────────────────┐
│   Payment Request       │
│ (standard ERPNext doc)  │
│                         │
│  Fields:                │
│  ├─ party_type, party   │
│  ├─ grand_total         │
│  ├─ net_total           │
│  ├─ bank                │
│  ├─ bank_account        │
│  ├─ payment_term        │
│  ├─ status              │
│  └─ reference_doctype/  │
│     reference_name      │
│     (Purchase Invoice,  │
│      Purchase Order)    │
└─────────────────────────┘
```

### Connector Server DocTypes

```
┌──────────────────────────────────────┐
│      Connector Settings              │
│  (SingleDocType - one per site)      │
│                                      │
│  Child Tables:                       │
│  ├─ connectors[] (Connector Map)     │
│  └─ h2h_connectors[] (H2H Map)      │
└──────────────┬───────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│      Connector Map (child table)     │
│                                      │
│  Fields:                             │
│  ├─ bank: "ICICI Bank"              │
│  ├─ connector: "ICICI Connector"    │◀── DocType name (not record name)
│  ├─ bulk_transaction: 0 or 1        │
│  └─ encrypt_log: 0 or 1             │
│                                      │
│  YOUR DB HAS (for ICICI):            │
│  Row 8cs0fguqs9: ICICI, bulk=0      │
│  Row 8cs5igjtp0: ICICI, bulk=1      │
│  (+ more rows from other sites)      │
└──────────────┬───────────────────────┘
               │ check_connector() returns
               │ "ICICI Connector" (DocType name)
               ▼
┌──────────────────────────────────────┐
│      ICICI Connector                 │
│  (DocType on connector server)       │
│  autoname: field:account_number      │
│                                      │
│  YOUR DB HAS ONLY 1 RECORD:         │
│  name = "756501000565"               │
│                                      │
│  Fields:                             │
│  ├─ account_number: 756501000565     │
│  ├─ corp_id: 600336118              │
│  ├─ aggr_id: BULK0092              │
│  ├─ aggr_name: UNIQUE              │
│  ├─ urn: SR266115962               │
│  ├─ corp_usr: NEELAVIN             │
│  ├─ status_corp_usr: 600336118...   │
│  ├─ client_key: (Password)          │
│  ├─ public_key: (Attach - .pem)     │
│  ├─ private_key: (Attach - .pem)    │
│  ├─ ifsc_code                       │
│  ├─ active: 1                       │
│  └─ testing: 0                      │
│                                      │
│  NOTE: NO bulk_payment field!        │
│  bulk_transaction is set dynamically │
│  on the instance at runtime          │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│      Bank API Endpoint               │
│  (DocType on connector server)       │
│                                      │
│  Fields:                             │
│  ├─ bank: "ICICI Bank"              │
│  ├─ environment: Testing/Production  │
│  ├─ bulk_transaction: 0 or 1        │◀── Separate records for single vs bulk
│  └─ end_points[] (Endpoint URLs)     │
│       ├─ action: "make_payment"      │
│       │  url: ".../Transaction" or   │
│       │       ".../bulkPayment"      │
│       ├─ action: "payment_status"    │
│       │  url: ".../TransactionInquiry│
│       │       " or ".../ReverseMis"  │
│       └─ action: "generate_otp"      │
│          url: ".../Create"           │
└──────────────────────────────────────┘
```

### How Payment Entry Gets Created

**Simple English:** When a Payment Order (type = "Payment Request") is submitted, it automatically creates Payment Entry documents BEFORE any bank API call happens. The bank payment flow is separate from the accounting entries.

```
Payment Order submitted
  └── on_submit() — overrides/payment_order.py:127-136
      └── make_payment_entries(docname) — doc_events/payment_order.py:56-236
          │
          │  For EACH summary row:
          │  ├── Creates a new Payment Entry doc
          │  ├── Sets: payment_type = "Pay"
          │  ├── Sets: paid_from = PO.account (company bank GL account)
          │  ├── Sets: paid_to = summary.account (party GL account)
          │  ├── Sets: paid_amount = summary.amount
          │  ├── Adds references[] linking to Purchase Invoice/Order
          │  ├── pe.insert() → pe.submit()
          │  └── Links PE back to summary row:
          │      frappe.db.set_value("Payment Order Summary", row.name, "payment_entry", pe.name)
          │
          └── update_payment_status() — overrides/payment_order.py:208-241
              → sets PO status to "Pending"
              → updates Payment Request status to "Payment Ordered"
```

---

## 3. Connector Record Resolution - How the Right Connector is Found

**Simple English:** There are TWO levels of lookup happening. First, the ERP server finds its own "Bank Connector" record (which stores the connector server URL and credentials). Then, on the connector server, "Connector Settings" maps the bank name to the right bank-specific connector.

### Level 1: ERP Server - Finding the Bank Connector

```python
# bank_connector.py:690-702 (india_banking)
def get_bank_connector(bank_account, company):
    bank_connector = frappe.db.exists(
        "Bank Connector",
        {
            "company": company,                    # e.g., "Unique Educational..."
            "bank_account": bank_account,          # e.g., "UNIQUE EDUCATION...ICICI Bank"
        },
    )
    return frappe.get_doc("Bank Connector", bank_connector)
```

**How it works:**
- Bank Connector is named by `bank_account` field (autoname: `field:bank_account`)
- So `bank_account` is unique — only ONE Bank Connector per bank account
- Your database has exactly ONE record: `"UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank"`
- This record has `bulk_transaction = 1` (currently set to bulk mode)

**What the Bank Connector provides:**
| Field | Value | Purpose |
|-------|-------|---------|
| `url` | `https://erp.walnutedu.in` | Connector server URL |
| `api_key` | (encrypted) | Auth header for connector server |
| `api_secret` | (encrypted) | Auth header for connector server |
| `bank` | `ICICI Bank` | Fetched from Bank Account link |
| `bulk_transaction` | `1` | Controls single vs bulk mode (ICICI only) |

### Level 2: Connector Server - Finding the ICICI Connector

When the ERP server POSTs to the connector server, the payload contains `bulk_transaction` and `company_account_number`.

**Step-by-step resolution in `connector_settings.py`:**

```
api.py:19 → settings = frappe.get_single("Connector Settings")
api.py:20 → connector = settings.get_connector(payload)
               │
               ▼
connector_settings.py:14 → get_connector(payload)
  │
  ├── (1) Extract bank name from payload
  │       doc.company_bank = "ICICI Bank"           # from payload
  │       doc.company_account_number = "756501000565" # from payload
  │       doc.bulk_transaction = 1                   # from Bank Connector checkbox
  │
  ├── (2) get_bank_connector("ICICI Bank") — line 57-65
  │       └── check_connector("ICICI Bank") — line 67-97
  │           │
  │           │  filters = { "parent": "Connector Settings", "bank": "ICICI Bank" }
  │           │  frappe.get_value("Connector Map", filters, "connector")
  │           │
  │           │  ⚠️ IMPORTANT: This query does NOT filter by bulk_transaction!
  │           │     It just finds the FIRST Connector Map row for "ICICI Bank"
  │           │     and returns the "connector" field value.
  │           │
  │           └── Returns: "ICICI Connector" (this is a DocType NAME, not a record)
  │
  ├── (3) Load the actual ICICI Connector record — line 29-36
  │       │
  │       │  connector_filter = { "account_number": "756501000565" }
  │       │
  │       │  # Check: does ICICI Connector have a "bulk_payment" field?
  │       │  frappe.get_meta("ICICI Connector").has_field("bulk_payment")
  │       │  → FALSE ❌ (ICICI Connector JSON has NO bulk_payment field!)
  │       │
  │       │  # So only filter by account_number:
  │       │  connector_doc = frappe.get_doc("ICICI Connector", {"account_number": "756501000565"})
  │       │
  │       └── Returns the ONE record: name = "756501000565"
  │
  ├── (4) Set runtime properties — line 37-39
  │       connector_doc.bulk_transaction = 1      # ← SET DYNAMICALLY on instance
  │       connector_doc.doc = payload.doc          # full Payment Order dict
  │       connector_doc.payment_doc = payload      # full payload
  │
  └── (5) Return the connector doc instance
```

### Answer: Why There Are Multiple Connector Map Entries But Only ONE ICICI Connector Record

**Your database reality:**

| DocType | Records | Reason |
|---------|---------|--------|
| **Bank Connector** (ERP) | 1 record | `"UNIQUE EDUCATION...ICICI Bank"`, `bulk_transaction=1` |
| **Connector Map** (Connector) | Multiple ICICI rows | `8cs0fguqs9` (bulk=0), `8cs5igjtp0` (bulk=1), and more |
| **ICICI Connector** (Connector) | **1 record only** | `"756501000565"`, NO `bulk_payment` field |
| **Bank API Endpoint** (Connector) | 2 ICICI records | 1 for `bulk_transaction=0`, 1 for `bulk_transaction=1` |

**The Connector Map's `bulk_transaction` field is NOT used in the connector lookup query.** The `check_connector()` method (line 67-97) only filters by `bank` name, not by `bulk_transaction`. Having two Connector Map rows for ICICI (one with `bulk_transaction=0` and one with `bulk_transaction=1`) doesn't affect which connector is chosen — both resolve to "ICICI Connector".

**Where `bulk_transaction` actually matters:**

1. **Bank Connector** (ERP) — the checkbox value is sent in the payload
2. **ICICIConnector instance** — set dynamically: `connector_doc.bulk_transaction = payload.bulk_transaction`
3. **Bank API Endpoint** — the `urls` property filters by `bulk_transaction` to get different URLs
4. **Throughout ICICIConnector methods** — branches on `self.bulk_transaction` for:
   - Different payload formats (`set_payment_data`)
   - Different encryption methods (`get_encrypted_payload`)
   - Different response parsing (`get_formated_response`)
   - Different unique_id generation
   - Different HTTP headers (`headers()`)

---

## 4. Single Payment Flow - Complete Step-by-Step with Code References

**Simple English:** In single mode, each payment in the Payment Order is sent to the bank individually. No OTP is needed. Each payment gets its own API call, its own encryption, and its own response.

### Step 1: User Clicks "Initiate Payment"

**File:** `india_banking/public/js/payment_order.js`

```
Line 236: set_payment_and_status_buttons(frm)
  │
  ├── Line 238-241: Checks docstatus === 1 AND frm.has_perm("write")
  │
  ├── Line 243-245: Checks if ANY summary row has payment_status === "Pending"
  │     const has_pending_payments = frm.doc.summary.some(
  │         (item) => item.payment_status === "Pending"
  │     );
  │
  ├── Line 247: Checks user has "Payment Manager" role
  │     if (frappe.user_roles.includes("Payment Manager") && has_pending_payments)
  │
  └── Line 249-251: Adds the button
        frm.add_custom_button(__("Initiate Payment"), () => {
            frm.trigger("make_payment");
        });
```

**Simple English:** The "Initiate Payment" button only appears if: (1) the Payment Order is submitted, (2) the user has write permission, (3) the user has "Payment Manager" role, and (4) there are pending payments.

### Step 2: JS `make_payment` Trigger

**File:** `india_banking/public/js/payment_order.js`

```
Line 288-307: make_payment: function (frm)
  │
  └── frappe.call({
        method: "india_banking.india_banking.doctype.bank_connector.bank_connector.make_payment",
        freeze: true,
        freeze_message: __("Initiating Payment..."),
        args: {
            payment_order: frm.doc.name,   // e.g., "PMO-00012"
        },
        callback: (res) => {
            if (res.message && res.message.otp_required) {
                frm.trigger("verify_otp");     // Only for bulk+ICICI
            }
            frm.reload_doc();
        },
      });
```

**Simple English:** The JS sends the Payment Order name to the server. The screen freezes with "Initiating Payment..." message. When done, it checks if OTP is needed (won't be for single mode) and reloads the form.

### Step 3: Server-side `make_payment()` Entry Point

**File:** `india_banking/india_banking/doctype/bank_connector/bank_connector.py`

```
Line 705-713:
@frappe.whitelist()
def make_payment(payment_order, otp=None):
    payment_order = frappe.get_doc("Payment Order", payment_order)
    │
    │  → Loads full Payment Order doc including:
    │    - .summary[] (Payment Order Summary child rows)
    │    - .references[] (Payment Order Reference child rows)
    │    - .company_bank_account, .company, .company_bank
    │
    bank_connector = get_bank_connector(
        payment_order.company_bank_account,   # "UNIQUE EDUCATION...ICICI Bank"
        payment_order.company                  # "Unique Educational..."
    )
    │
    │  Line 690-702: get_bank_connector()
    │  → frappe.db.exists("Bank Connector", {company: ..., bank_account: ...})
    │  → frappe.get_doc("Bank Connector", name)
    │  → Returns doc with:
    │      .url = "https://erp.walnutedu.in"
    │      .bank = "ICICI Bank"
    │      .bulk_transaction = 0  (for single mode)
    │      .api_key, .api_secret = (encrypted credentials)
    │
    return bank_connector.make_post_request(
        payment_order, otp=None, action="initiate_payment"
    )
```

### Step 4: `make_post_request()` — The Core Orchestrator

**File:** `india_banking/india_banking/doctype/bank_connector/bank_connector.py`

```
Line 85-139: make_post_request(self, payment_order, otp=None, action=None)
  │
  ├── Line 86: self.check_user_permission()
  │     → frappe.has_permission("Payment Order", "write")
  │
  ├── Line 88-96: OTP CHECK (action == "initiate_payment")
  │     │
  │     ├── Line 89: self.check_otp_enabled(otp=None)
  │     │     Line 50-54: check_otp_enabled()
  │     │     → OTP_ENABLED_BANK = [("ICICI Bank", 1)]  — line 24-26
  │     │     → (self.bank="ICICI Bank", self.bulk_transaction=0)
  │     │     → ("ICICI Bank", 0) NOT IN [("ICICI Bank", 1)]
  │     │     → Returns None (falsy) → OTP SKIPPED ✅
  │     │
  │     ├── Line 92-96: PRE-INITIATION STATUS CHECK
  │     │     action = "get_payment_status"
  │     │     self.make_post_request(payment_order, action="get_payment_status")
  │     │     │
  │     │     │  → Recursively calls itself with get_payment_status
  │     │     │  → This checks if any payments were already initiated
  │     │     │  → Updates status of previously initiated payments
  │     │     │  → Then returns, and action is reset to "initiate_payment"
  │     │     │
  │     │     action = "initiate_payment"  # reset
  │     │
  │     └── Line 98: self.action = "initiate_payment"
  │
  ├── Line 103: if self.bulk_transaction:  → FALSE (0)
  │
  └── Line 117-133: ELSE BRANCH (single mode)
        │
        ├── Line 119-124: Background check
        │     if len(payment_order.summary) > 10 or cint(
        │         frappe.get_single("India Banking Settings").enable_payment_in_the_background
        │     ):
        │         return self.add_payment_in_the_background(payment_order)
        │
        │     → For single mode: if more than 10 summaries OR background setting enabled,
        │       payments are enqueued as background jobs (one per summary row)
        │     → Otherwise: inline loop
        │
        └── Line 126-133: INLINE LOOP
              for summary in payment_order.summary:
                  if not summary.payment_initiated and summary.payment_status != "Pending":
                      continue   # skip already-initiated or non-pending rows

                  self.make_single_request(payment_order, summary)
                  │
                  └── Calls make_single_request for EACH summary row individually
```

### Step 5: `make_single_request()` — Building Per-Summary Payload

**File:** `india_banking/india_banking/doctype/bank_connector/bank_connector.py`

```
Line 357-373: make_single_request(self, payment_order, summary)
  │
  ├── Line 358-359: URL and headers
  │     url = self.connector_url
  │       = f"{self.url}/api/method/india_banking_connector.api.connect"
  │       = "https://erp.walnutedu.in/api/method/india_banking_connector.api.connect"
  │     headers = self.headers
  │       = { "Authorization": "token <api_key>:<api_secret>",
  │           "Content-Type": "application/json" }
  │
  ├── Line 361-362: Build payload base
  │     payload = self.get_payload(payment_order)
  │     │
  │     │  Line 59-77: get_payload()
  │     │  bank_account = frappe.get_doc("Bank Account", payment_order.company_bank_account)
  │     │  → Loads the company Bank Account doc to get bank details
  │     │
  │     │  payment_payload.doc = payment_order.as_dict() + {
  │     │      "company_account_number": bank_account.bank_account_no,  # "756501000565"
  │     │      "company_bank_account_name": bank_account.account_name,  # "UNIQUE EDUCATION..."
  │     │      "company_ifsc": bank_account.branch_code,                # "ICIC0007565"
  │     │      "mobile_number": bank_account.mobile_number,
  │     │  }
  │     │  payment_payload.method = "initiate_payment"
  │     │  payment_payload.bulk_transaction = 0
  │     │  payment_payload.doc.otp = None
  │     │
  │     payload.update(summary.as_dict())
  │       → Merges summary row fields into payload root level:
  │         name, party_type, party, amount, bank_account,
  │         bank_account_no, account_name, branch_code, bank,
  │         mode_of_transfer, etc.
  │
  ├── Line 363-364: Party name lookup
  │     payload.party_name = frappe.db.get_value(
  │         summary.party_type,            # e.g., "Supplier"
  │         summary.party,                 # e.g., "Naresh"
  │         get_party_field_name(summary.party_type)  # "supplier_name"
  │     )
  │     │
  │     │  india_banking/utils.py:58-63: get_party_field_name()
  │     │  → "Supplier" returns "supplier_name"
  │     │  → "Customer" returns "customer_name"
  │     │  → "Employee" returns "employee_name"
  │
  ├── Line 366: Bank address lookup
  │     payload.address = json.dumps(get_bank_address_details(summary.bank_account))
  │     │
  │     │  india_banking/utils.py:15-55: get_bank_address_details()
  │     │  → Finds Address linked to the party's Bank Account via Dynamic Link
  │     │  → Returns: { AddressLine, StreetName, BuildingNumber, PostCode, TownName, Country }
  │
  ├── Line 368: POST to connector server
  │     response = request.post(url, headers=headers, data=json.dumps(payload))
  │
  ├── Line 370-371: Log the request
  │     create_api_log(response, self.action, payment_order.doctype, payment_order.name)
  │     → Creates "India Banking Request Log" record on ERP server
  │
  └── Line 373: Process the response
        self.verify_response(response, payment_order)
```

### Step 6: Connector Server Processing

**File:** `india_banking_connector/api.py`

```
Line 7-28: connect(**payload)
  │
  ├── PayloadValidator(payload) — validates payload format
  │
  ├── settings = frappe.get_single("Connector Settings")
  │
  ├── connector = settings.get_connector(payload)
  │     │
  │     │  connector_settings.py:14-49: get_connector()
  │     │  (1) check_connector("ICICI Bank") → returns "ICICI Connector"
  │     │  (2) frappe.get_doc("ICICI Connector", {account_number: "756501000565"})
  │     │  (3) connector_doc.bulk_transaction = 0  (from payload)
  │     │  (4) connector_doc.doc = payload.doc
  │     │  (5) connector_doc.payment_doc = payload
  │     │
  │     └── Returns ICICIConnector instance with runtime properties set
  │
  └── connector.get_response("initiate_payment")
        │
        │  base bank_connector.py:53-58 (connector app):
        │  def get_response(self, method):
        │      self.is_active()       # checks self.active == True
        │      return getattr(self, method)()
        │      → calls self.initiate_payment()
        │
        ▼
```

**File:** `india_banking_connector/connectors/doctype/icici_connector/icici_connector.py`

```
Line 110-140: initiate_payment(self)
  │
  ├── Line 111: self.update_client_details("make_payment")
  │     │  Line 685-694: Sets self.client_key from password field
  │     │  → self.client_key = self.get_password("client_key")
  │     │  (Used as "apikey" HTTP header for ICICI API)
  │
  ├── Line 112-115: Determine unique_id
  │     payment_details = self.payment_doc  # (for single, uses payment_doc not doc)
  │     unique_id = "".join(re.findall(r"[0-9a-zA-Z]", payment_details.name))[-10:]
  │     if not self.bulk_transaction:   # TRUE — this is single mode
  │         unique_id = payment_details.name   # e.g., "9jp794m7ar" (full summary name)
  │
  ├── Line 117-120: Duplicate payment check
  │     existing_payment_response = self.validate_duplicate_payments(unique_id=unique_id)
  │     │
  │     │  base bank_connector.py:60-90 (connector app):
  │     │  → frappe.db.exists("Bank Request Log", {
  │     │        unique_id: "9jp794m7ar",
  │     │        action: "Initiate Payment",
  │     │        status_code: "200"
  │     │    })
  │     │  → If found: returns the existing response (prevents double-payment)
  │     │  → If not found: returns None → continues
  │
  ├── Line 122: Resolve URL
  │     url = self.urls.make_payment
  │     │
  │     │  base bank_connector.py:28-47 (connector app):
  │     │  → Queries Bank API Endpoint:
  │     │    WHERE bank = "ICICI Bank"
  │     │    AND environment = "Production"   (testing=0)
  │     │    AND bulk_transaction = 0         (single mode)
  │     │  → Returns dict of {action: url} pairs
  │     │  → .make_payment = ".../api/Corporate/CIB/v1/Transaction"
  │
  ├── Line 123: Build headers
  │     headers = self.headers(payment_details.mode_of_transfer)
  │     │
  │     │  icici_connector.py:39-57: headers()
  │     │  → For single (bulk_transaction=False):
  │     │    { "accept": "*/*",
  │     │      "content-type": "text/plain",        ← RSA encrypted blob
  │     │      "apikey": self.client_key,
  │     │      "host": self.urls.host }
  │
  ├── Line 124: Encrypt payload
  │     payload = self.get_encrypted_payload(method="make_payment")
  │     │
  │     │  icici_connector.py:197-223: get_encrypted_payload()
  │     │  (1) data = self.get_account_config("make_payment")
  │     │      → calls set_payment_data(data) — line 339-401
  │     │      → SINGLE branch (line 366-401):
  │     │        data = {
  │     │            "AGGRID":    "BULK0092",
  │     │            "AGGRNAME":  "UNIQUE",
  │     │            "CORPID":    "600336118",
  │     │            "USERID":    "NEELAVIN",
  │     │            "URN":       "SR266115962",
  │     │            "UNIQUEID":  "9jp794m7ar",         ← full summary name
  │     │            "DEBITACC":  "756501000565",        ← company account
  │     │            "CREDITACC": "157588285774",        ← payee account
  │     │            "IFSC":      "INDB0000746",         ← payee IFSC
  │     │            "AMOUNT":    "10.0",
  │     │            "CURRENCY":  "INR",
  │     │            "TXNTYPE":   "IFS",                 ← IMPS → IFS
  │     │            "PAYEENAME": "Naresh Tak",
  │     │            "REMARKS":   "Supplier Naresh",
  │     │            "WORKFLOW_REQD": "Y",
  │     │            "BENLEI":    ""
  │     │        }
  │     │
  │     │  (2) Encryption — SINGLE path (line 221-223):
  │     │      public_key_path = self.get_file_relative_path(connector_doc.public_key)
  │     │      return self.rsa_encrypt_data(data, public_key_path)
  │     │      │
  │     │      │  base bank_connector.py:318-328 (connector app):
  │     │      │  → JSON serializes data dict
  │     │      │  → RSA encrypts with ICICI's public key
  │     │      │  → Returns base64-encoded encrypted string
  │     │
  │     └── payload = "<base64 RSA encrypted blob>"
  │
  ├── Line 126: POST to ICICI Bank API
  │     response = requests.post(url, headers=headers, data=payload)
  │     → URL: https://apibankingonesandbox.icicibank.com/api/Corporate/CIB/v1/Transaction
  │     → Body: RSA encrypted blob (text/plain)
  │
  ├── Line 128-136: Log the request/response
  │     create_api_log(response, action="Initiate Payment", ...)
  │     → Creates "Bank Request Log" record on connector server
  │     → Stores: request payload, response, status_code, unique_id
  │
  └── Line 138-140: Decrypt and format response
        return self.get_decrypted_response(response, method="make_payment", log_id=log_id)
        │
        │  icici_connector.py:433-473: get_decrypted_response()
        │  → SINGLE path (line 461-464):
        │    decrypted_data = self.rsa_decrypt_data(response.text, private_key_path)
        │    │
        │    │  base bank_connector.py:343-351: rsa_decrypt_data()
        │    │  → base64 decode → RSA decrypt with private key → JSON parse
        │    │  → Returns dict: {STATUS: "SUCCESS", UTRNUMBER: "...", MESSAGE: "..."}
        │
        │  icici_connector.py:475-568: get_formated_response()
        │  → SINGLE path — method == "make_payment" (line 503-532):
        │    if data.STATUS in ["SUCCESS", "PENDING", "PENDING FOR PROCESSING", "PENDING FOR APPROVAL"]:
        │        res_dict.payment_status = "ACCEPTED"
        │        res_dict.summary_details = {
        │            "9jp794m7ar": { "payment_status": "Accepted" }
        │        }
        │
        └── Returns: {
                payment_status: "ACCEPTED",
                message: "Payment Success",
                summary_details: { "9jp794m7ar": { payment_status: "Accepted" } }
            }
```

### Step 7: Back on ERP Server — Processing Response

**File:** `india_banking/india_banking/doctype/bank_connector/bank_connector.py`

```
Line 141-147: verify_response(self, response, payment_order)
  │
  ├── self.action == "initiate_payment"
  │   → self.verify_payment_response(response, payment_order)
  │
  │   Line 149-231: verify_payment_response()
  │     │
  │     ├── Line 150: payment_response = self.get_response_details(response)
  │     │     → response.json().get("message")
  │     │     → { payment_status: "ACCEPTED", summary_details: {...} }
  │     │
  │     ├── Line 152: if response.ok:  → True (200)
  │     │
  │     ├── Line 160: if payment_status == "ACCEPTED":
  │     │
  │     ├── Line 161: if self.bulk_transaction: → False (single mode)
  │     │     (Bulk would store file_sequence_number here — SKIPPED)
  │     │
  │     └── Line 171-218: Process summary_details
  │           for _name, details in summary_details.items():
  │             │  _name = "9jp794m7ar"
  │             │  details = { payment_status: "Accepted" }
  │             │
  │             ├── details.payment_status == "Accepted" (line 172):
  │             │   frappe.db.set_value("Payment Order Summary", "9jp794m7ar", {
  │             │       "payment_status": "Initiated",
  │             │       "payment_date": today,
  │             │       "payment_initiated": 1,
  │             │       "message": ""
  │             │   })
  │             │   self.success_count += 1  → 1
  │             │
  │             ├── "Failed" → sets payment_status = "Failed"
  │             ├── "Request Failure" → sets payment_status = "Pending" (retry)
  │             └── else → sets payment_status = "Pending"
  │
  └── Line 147: self.update_payment_status(payment_order)
        │
        │  Line 459-513: update_payment_status()
        │  → Reloads payment_order
        │  → Counts: success_count, failed_count, rejected_count, initiated_count
        │  → Sets Payment Order status:
        │    - All Initiated → "Initiated"
        │    - All Processed → "Approved"
        │    - All Failed → "Failed"
        │    - Mix of processed + failed → "Partially Approved"
        │    - Some initiated → "Partially Initiated"
```

### Step 8: Success Message and Reload

```
Line 135-139: (back in make_post_request)
  if self.action == "initiate_payment":
      msg = _(f"{self.success_count} Payment(s) Initiated")
      frappe.msgprint(msg)
      → Shows: "1 Payment(s) Initiated"

→ Returns to JS callback
→ payment_order.js:303: frm.reload_doc()
→ User sees: Payment Order with status "Initiated", summary row showing "Initiated"
```

---

## 5. Bulk Payment Flow - Complete Step-by-Step including OTP, File Construction, Encryption

**Simple English:** In bulk mode, ALL payments in the Payment Order are sent to the bank in a single API call. The payments are bundled into a pipe-delimited text file, encrypted, and sent. OTP is required. The bank returns a file_sequence_number to track the batch.

### Step 1-2: Same as Single (User clicks button, JS calls server)

Same as Single Payment Steps 1-2. The difference starts at Step 3.

### Step 3: `make_payment()` — Bank Connector Has bulk_transaction=1

**File:** `bank_connector.py:705-713`

```
bank_connector = get_bank_connector(...)
→ Returns Bank Connector doc with bulk_transaction = 1

bank_connector.make_post_request(payment_order, otp=None, action="initiate_payment")
```

### Step 4: `make_post_request()` — OTP Required!

**File:** `bank_connector.py:85-139`

```
Line 88-90: OTP CHECK
  │
  ├── self.check_otp_enabled(otp=None)
  │     Line 50-54: check_otp_enabled()
  │     → OTP_ENABLED_BANK = [("ICICI Bank", 1)]
  │     → (self.bank="ICICI Bank", self.bulk_transaction=1)
  │     → ("ICICI Bank", 1) IS IN [("ICICI Bank", 1)]  → True!
  │     → otp is None → return True
  │
  └── Line 90: return self.generate_otp(payment_order)
        │
        │  Line 426-441: generate_otp()
        │  │
        │  ├── payment_order.reload()
        │  │
        │  ├── Build OTP payload:
        │  │   self.get_payload(payment_order, "generate_otp")
        │  │   → { doc: {PO dict + bank details}, method: "generate_otp", bulk_transaction: 1 }
        │  │
        │  ├── POST to connector server:
        │  │   response = request.post(self.connector_url, headers=self.headers, data=...)
        │  │
        │  ├── Log: create_api_log(response, "Generate Otp", ...)
        │  │
        │  └── return self.handle_otp_response(response)
        │        │
        │        │  Line 443-457: handle_otp_response()
        │        │  if response.ok AND response_details.status == "success":
        │        │      return {"otp_required": True}    ← RETURNED TO JS
        │        │  else:
        │        │      frappe.throw("OTP Initiation Failed")
        │
        ⚠️ make_post_request RETURNS HERE for bulk+no_otp
        → Returns {"otp_required": True} all the way back to JS
```

### Step 5: OTP Generation on Connector Server

**File:** `icici_connector.py:171-192`

```
generate_otp(self)
  │
  ├── self.update_client_details("generate_otp")
  │   → self.client_key = self.get_password("client_key")
  │
  ├── payment_details = self.doc  (for bulk, uses self.doc = full PO)
  │
  ├── URL: self.urls.generate_otp
  │   → ".../api/Corporate/CIB/v1/Create"
  │
  ├── headers: JSON content-type + x-priority header (bulk)
  │
  ├── Payload built via get_account_config("generate_otp"):
  │   → set_otp_data(data) — line 310-327
  │     unique_id = "PMO00013" (last 10 alnum of PO name)
  │     data = {
  │         "CORPID":   "600336118",
  │         "USERID":   "NEELAVIN",
  │         "AGGRID":   "BULK0092",
  │         "AGGRNAME": "UNIQUE",
  │         "URN":      "SR266115962",
  │         "UNIQUEID": "PMO00013",
  │         "AMOUNT":   "10.0"        ← total of all payments
  │     }
  │
  ├── Encrypted with RSA+AES (double encryption):
  │   get_encrypted_payload("generate_otp") — line 197-220
  │   → RSA encrypts AES key with ICICI's public key
  │   → AES encrypts the OTP payload data
  │   → Returns JSON: { requestId, encryptedKey, encryptedData, iv }
  │
  ├── POST to ICICI API: .../api/Corporate/CIB/v1/Create
  │
  ├── Log: create_api_log(response, "Generate OTP", ...)
  │
  └── Decrypt response:
      get_decrypted_response(response, "generate_otp")
      → RSA decrypt key → AES decrypt data
      → get_formated_response() → handle_bulk_transaction_response()
      │
      │  icici_connector.py:569-573:
      │  if data.get("RESPONSE") == "Success":
      │      res_dict.status = "success"
      │      res_dict.message = data.get("MESSAGE")
      │
      └── Returns { status: "success", message: "OTP sent..." }

→ ICICI Bank sends OTP to the registered mobile number
```

### Step 6: User Enters OTP — JS Calls Again

**File:** `payment_order.js:309-349`

```
verify_otp(frm)
  │
  ├── frappe.prompt({
  │     label: __("Enter OTP"),
  │     fieldname: "otp",
  │     fieldtype: "Data",
  │     reqd: true
  │   }, (values) => {
  │     │
  │     ├── Validates OTP is not blank
  │     │
  │     └── frappe.call({
  │           method: "india_banking...bank_connector.make_payment",
  │           args: {
  │               payment_order: frm.doc.name,
  │               otp: values.otp,          ← NOW OTP IS PROVIDED
  │           },
  │           callback: function (r) {
  │               frm.reload_doc();
  │           }
  │         });
  │   },
  │   __("Sent an OTP to your registered mobile number"),
  │   __("Proceed")
  │ );
```

**Simple English:** A dialog pops up asking the user to enter the OTP they received on their phone. Once entered, the same `make_payment` function is called again, but this time WITH the OTP.

### Step 7: Second `make_post_request()` Call — WITH OTP

**File:** `bank_connector.py:85-139`

```
make_post_request(payment_order, otp="XXXXXX", action="initiate_payment")
  │
  ├── Line 88-90: OTP CHECK
  │     self.check_otp_enabled(otp="XXXXXX")
  │     → ("ICICI Bank", 1) IS in OTP_ENABLED_BANK
  │     → BUT otp is NOT None → check at line 51 returns None (falsy)
  │     → Check at line 53: otp is truthy → passes through
  │     → OTP PROVIDED → CONTINUES ✅
  │
  ├── Line 92-96: PRE-INITIATION STATUS CHECK
  │     → Same as single: recursively calls get_payment_status first
  │     → Then resets action to "initiate_payment"
  │
  ├── Line 100-101: self.verify_otp(payment_order, otp)
  │     → Currently a pass (no-op) in base class — line 56-57
  │
  ├── Line 103: if self.bulk_transaction:  → TRUE ✅
  │
  └── Line 103-116: BULK BRANCH
        │
        ├── url = self.connector_url
        ├── headers = self.headers
        │
        ├── payload = self.get_payload(payment_order, otp="XXXXXX")
        │     │
        │     │  Line 59-77: get_payload()
        │     │  → Builds payload with FULL Payment Order dict
        │     │  → Includes ALL summary rows in doc.summary
        │     │  → doc.otp = "XXXXXX"
        │     │  → bulk_transaction = 1
        │     │  → method = "initiate_payment" (set from self.action or action param)
        │     │
        │     └── payload = {
        │           doc: {
        │               name: "PMO-00013",
        │               company_account_number: "756501000565",
        │               company_bank_account_name: "UNIQUE EDUCATION...",
        │               company_ifsc: "ICIC0007565",
        │               company_bank: "ICICI Bank",
        │               total: 10.0,
        │               otp: "XXXXXX",
        │               summary: [ { name: "ckp0egpov4", party: "Naresh",
        │                            amount: 10.0, bank_account_no: "157588285774",
        │                            account_name: "Naresh Tak",
        │                            branch_code: "INDB0000746",
        │                            bank: "INDUSIND BANK",
        │                            mode_of_transfer: "IMPS" } ],
        │               file_sequence_number: null
        │           },
        │           method: "initiate_payment",
        │           bulk_transaction: 1
        │         }
        │
        ├── response = request.post(url, headers=headers, data=json.dumps(payload))
        │     → ONE single POST for the ENTIRE payment order
        │     → (contrast with single mode: one POST per summary row)
        │
        ├── create_api_log(response, "initiate_payment", ...)
        │
        └── self.verify_response(response, payment_order)
```

### Step 8: Connector Server — Bulk Initiate Payment

**File:** `icici_connector.py:110-140`

```
initiate_payment(self)
  │
  ├── self.update_client_details("make_payment")
  │
  ├── payment_details = self.doc  (for bulk, uses full PO dict, not payment_doc)
  │     → self.doc = { name: "PMO-00013", total: 10.0, summary: [...], otp: "XXXXXX", ... }
  │
  ├── unique_id = "".join(re.findall(r"[0-9a-zA-Z]", "PMO-00013"))[-10:]
  │     → "PMO00013"  (removes hyphen, takes last 10 alnum chars)
  │     → NOT overridden because bulk_transaction=1
  │
  ├── validate_duplicate_payments(unique_id="PMO00013")
  │     → Checks if "PMO00013" already exists in Bank Request Log
  │
  ├── url = self.urls.make_payment
  │     → Queries Bank API Endpoint: bulk_transaction=1
  │     → ".../api/v1/cibbulkpayment/bulkPayment"
  │
  ├── headers = self.headers(mode_of_transfer="IMPS")
  │     │  Line 39-57: For bulk:
  │     │  { "accept": "*/*",
  │     │    "content-type": "application/json",      ← JSON (not text/plain)
  │     │    "apikey": self.client_key,
  │     │    "host": self.urls.host,
  │     │    "x-priority": "0010" }                   ← NEFT priority code
  │
  └── payload = self.get_encrypted_payload(method="make_payment")
```

### Step 9: Building the Bulk File Content

**File:** `icici_connector.py:339-365` (`set_payment_data` — bulk branch)

```
set_payment_data(self, data)
  │
  ├── payment_details = self.doc  (full PO dict)
  ├── file_reference_id = "PMO00013"
  ├── unique_id = "PMO00013"
  │
  └── BULK branch (line 348-364):
        data = {
            "FILE_DESCRIPTION": "PMO00013",
            "CORP_ID":   "600336118",
            "USER_ID":   "NEELAVIN",
            "AGGR_ID":   "BULK0092",
            "AGGR_NAME": "UNIQUE",
            "URN":       "SR266115962",
            "UNIQUE_ID": "PMO00013",
            "AGOTP":     "XXXXXX",              ← The OTP user entered
            "FILE_NAME": "PMO00013.txt",
            "FILE_CONTENT": self.construct_payment_details_content(...)
        }
```

**File:** `icici_connector.py:763-823` (`construct_payment_details_content`)

```
construct_payment_details_content(self, payment_doc, connector_doc)
  │
  │  Builds a pipe-delimited text file with ^ line terminators
  │
  ├── Line 1 — FHR (File Header Record):
  │     FHR|<num_records+1>|<date>|<file_ref>|<total>|INR|<company_acct>|0011^
  │     → FHR|2|10/23/2025|PMO00013|10.0|INR|756501000565|0011^
  │     │
  │     │  num_records+1 = len(summary) + 1 = 2 (includes the MDR line)
  │     │  date = current date in MM/DD/YYYY format
  │     │  file_ref = PMO00013
  │     │  total = sum of all payments
  │     │  company_acct = connector's account number
  │     │  0011 = branch code constant
  │
  ├── Line 2 — MDR (Master Debit Record):
  │     MDR|<company_acct>|0011|<company_name>|<total>|INR|<file_ref>|ICIC0000011|WIB^
  │     → MDR|756501000565|0011|UniqueEducationalandSportsFoundation|10.0|INR|PMO00013|ICIC0000011|WIB^
  │     │
  │     │  company_name = payment_doc.company with spaces removed, max 30 chars
  │     │  ICIC0000011 = fixed ICICI head office IFSC
  │     │  WIB = transfer type
  │
  ├── Line 3+ — MCW or MCO per payment row:
  │
  │     FOR EACH summary row in payment_doc.summary:
  │     │
  │     ├── IF payment_doc.company_bank == payment_row.bank:
  │     │   → SAME BANK (intra-bank) → MCW record
  │     │     MCW|<payee_acct>|<first_4_digits>|<acct_name>|<amount>|INR|<summary_name>|<ifsc>|WIB^
  │     │     │
  │     │     │  payee_acct = payment_row.bank_account_no
  │     │     │  first_4_digits = payee_acct[:4]
  │     │     │  acct_name = account holder name, spaces removed, max 30 chars
  │     │     │  summary_name = payment_row.name → USED AS RECORD ID for tracking
  │     │
  │     └── ELSE:
  │         → DIFFERENT BANK (inter-bank) → MCO record
  │           MCO|<payee_acct>|0011|<acct_name>|<amount>|INR|<summary_name>|NFT|<ifsc>^
  │           │
  │           │  NFT = NEFT transfer type for inter-bank
  │           │  ifsc = payment_row.branch_code (payee's IFSC)
  │
  │     In PMO-00013 example:
  │     → company_bank = "ICICI Bank", payee bank = "INDUSIND BANK"
  │     → Different bank → MCO:
  │       MCO|157588285774|0011|NareshTak|10.0|INR|ckp0egpov4|NFT|INDB0000746^
  │
  └── Final: base64 encode the entire content
        result = "\n".join(content)          # join all lines
        byte_like = str.encode(result)       # encode to bytes
        encode_result = b64encode(byte_like).decode("utf-8")  # base64
        return encode_result
```

### Step 10: Bulk Encryption (RSA+AES Double Encryption)

**File:** `icici_connector.py:197-220` (`get_encrypted_payload` — bulk branch)

```
get_encrypted_payload(self, method="make_payment")
  │
  ├── data = self.get_account_config("make_payment")
  │     → The full payment data dict including FILE_CONTENT
  │
  └── BULK branch (line 204-220):
        │
        ├── (1) RSA encrypt the AES key:
        │     encrypted_key = self.rsa_encrypt_key(
        │         self.AES_KEY,              # "1234567887654321" (16 bytes)
        │         public_key_path             # ICICI's RSA public key
        │     )
        │     │
        │     │  base bank_connector.py:260-267 (connector):
        │     │  → rsa.encrypt(AES_KEY, public_key) → base64 encode
        │
        ├── (2) AES encrypt the payment data:
        │     encrypted_data = self.aes_encrypt_data(data, self.AES_KEY)
        │     │
        │     │  base bank_connector.py:274-290 (connector):
        │     │  → JSON serialize data dict
        │     │  → AES CBC encrypt with key + IV
        │     │  → base64 encode
        │
        └── (3) Return JSON envelope:
              return json.dumps({
                  "requestId": "PMO00013",
                  "service": "",
                  "oaepHashingAlgorithm": "NONE",
                  "encryptedKey": "<base64 RSA encrypted AES key>",
                  "encryptedData": "<base64 AES encrypted payment data>",
                  "clientInfo": "",
                  "optionalParam": "",
                  "iv": "<base64 encoded IV>"      # "0000000000000000"
              })
```

**Simple English:** Two layers of encryption. First, the actual payment data is encrypted using AES (a fast symmetric algorithm) with a fixed key. Then, that AES key itself is encrypted using RSA (a slower asymmetric algorithm) with ICICI's public key. The bank decrypts the RSA layer first to get the AES key, then uses it to decrypt the payment data.

### Step 11: ICICI Bank Response — Bulk

```
ICICI Bank processes the file and returns:
  → Encrypted response (RSA+AES)
  → Decrypted contains: { "FILE_SEQUENCE_NUM": "60595132", "MESSAGE_DESC": "..." }
```

**File:** `icici_connector.py:433-473` (`get_decrypted_response` — bulk branch)

```
get_decrypted_response(self, response, method="make_payment")
  │
  └── BULK branch (line 439-450):
        response = json.loads(response.text)
        │
        ├── (1) RSA decrypt the AES key:
        │     decrypted_key = self.rsa_decrypt_key(
        │         response.get("encryptedKey"),
        │         private_key_path                 # Our RSA private key
        │     )
        │     │  base bank_connector.py:269-272: rsa_decrypt_key()
        │     │  → base64 decode → RSA decrypt with private key
        │
        └── (2) AES decrypt the response data:
              decrypted_data = self.aes_decrypt_data(
                  response.get("encryptedData"),
                  decrypted_key
              )
              │  base bank_connector.py:292-316: aes_decrypt_data()
              │  → base64 decode → AES CBC decrypt → JSON parse
              │  → Returns dict: { FILE_SEQUENCE_NUM: "60595132", MESSAGE_DESC: "..." }
```

**File:** `icici_connector.py:569-628` (`handle_bulk_transaction_response` — make_payment)

```
handle_bulk_transaction_response(self, data, res_dict, method="make_payment")
  │
  └── method == "make_payment" (line 586-604):
        │
        ├── if data.get("FILE_SEQUENCE_NUM"):  → "60595132"
        │     res_dict.payment_status = "ACCEPTED"
        │     res_dict.message = data.get("MESSAGE_DESC")
        │     res_dict.file_sequence_number = "60595132"
        │     res_dict.summary_details = self.get_summary_details("Accepted")
        │     │
        │     │  base bank_connector.py:150-159 (connector):
        │     │  → Loops through self.doc.summary
        │     │  → Returns: { "ckp0egpov4": { payment_status: "Accepted" } }
        │
        └── Returns: {
                payment_status: "ACCEPTED",
                file_sequence_number: "60595132",
                summary_details: { "ckp0egpov4": { payment_status: "Accepted" } }
            }
```

### Step 12: Back on ERP Server — Processing Bulk Response

**File:** `bank_connector.py:149-231` (`verify_payment_response`)

```
verify_payment_response(self, response, payment_order)
  │
  ├── payment_status == "ACCEPTED"
  │
  ├── Line 161: if self.bulk_transaction:  → TRUE
  │     frappe.db.set_value("Payment Order", "PMO-00013", {
  │         "status": "Initiated",
  │         "file_sequence_number": "60595132"    ← STORED on Payment Order
  │     })
  │
  └── Line 171-218: Process summary_details
        for "ckp0egpov4": { payment_status: "Accepted" }
        → frappe.db.set_value("Payment Order Summary", "ckp0egpov4", {
              "payment_status": "Initiated",
              "payment_date": today,
              "payment_initiated": 1,
          })

→ update_payment_status() → sets PO status to "Initiated"
→ frappe.msgprint("Payment Initiated")
→ Returns to JS → OTP callback → frm.reload_doc()
```

### Step 13: Status Check (Later — User Clicks "Get Status")

**File:** `payment_order.js:270-283`

```
frm.add_custom_button(__("Get Status"), () => {
    frappe.call({
        method: "india_banking...bank_connector.get_payment_status",
        args: { payment_order: frm.doc.name },
        callback: function () { frm.reload_doc(); },
    });
});
```

**File:** `bank_connector.py:716-722`

```
get_payment_status(payment_order)
  → frappe.get_doc("Payment Order", payment_order)
  → get_bank_connector(...)
  → bank_connector.make_post_request(payment_order, action="get_payment_status")
```

**File:** `icici_connector.py:142-169` (`get_payment_status`)

```
get_payment_status(self)
  │
  ├── payment_details = self.doc  (for bulk)
  ├── unique_id = "PMO00013"
  │
  ├── URL: self.urls.payment_status
  │     → ".../api/v1/ReverseMis" (bulk status endpoint)
  │
  ├── Payload via set_payment_status_data() — line 403-431:
  │     BULK branch (line 408-421):
  │     data = {
  │         "CORPID":      "600336118",
  │         "USERID":      "600336118.NEELAVIN",    ← status_corp_usr (different!)
  │         "AGGRID":      "BULK0092",
  │         "URN":         "SR266115962",
  │         "UNIQUEID":    "PMO00013",
  │         "FILESEQNUM":  "60595132",              ← from Payment Order record
  │         "ISENCRYPTED": "N"
  │     }
  │
  ├── RSA+AES encryption → POST to ICICI
  │
  └── Response contains XML with file status and per-record details
```

**File:** `icici_connector.py:606-627` (`handle_bulk_transaction_response` — payment_status)

```
method == "payment_status" (line 606):
  │
  ├── data.get("XML").get("FILE_STATUS") → check overall file status
  │     "REJ"/"REC" → "Payment Rejected"
  │     "FAL" → "Payment Failed"
  │
  └── data.XML.FILEUPLOAD_BINARY_OUTPUT.Records.Record → per-record status
        │
        └── self.format_payment_status(records)
              │
              │  icici_connector.py:825-883: format_payment_status()
              │  │
              │  │  Records are pipe-delimited strings like:
              │  │  "...|157588285774|756501000565|INDB0000746|INR|10.0|042068068961|...|...|ckp0egpov4|SUC"
              │  │
              │  │  Parses each row:
              │  │  keys = [transaction_type, network_id, credit_account_number,
              │  │          debit_account_number, ifsc_code, currency, total_amount,
              │  │          host_reference_number, host_response_code,
              │  │          host_response_message, transaction_remarks, transaction_status]
              │  │
              │  │  transaction_remarks = "ckp0egpov4" (= Payment Order Summary name)
              │  │  transaction_status = "SUC" → Processed
              │  │  host_reference_number = "042068068961" (= UTR number)
              │  │
              │  └── Returns: {
              │          "ckp0egpov4": {
              │              status: "Processed",
              │              utr_number: "042068068961",
              │              message: "Payment Accepted"
              │          }
              │      }
```

**File:** `bank_connector.py:233-355` (`verify_status_response` — back on ERP)

```
verify_status_response(self, response, payment_order)
  │
  ├── payment_status == "PROCESSED"
  │
  └── For each summary in payment_order.summary:
        status_details = summary_details.get(summary.name)
        │
        ├── status == "Processed" AND utr_number present (line 245-276):
        │     frappe.db.set_value("Payment Order Summary", summary.name, {
        │         "reference_number": "042068068961",   ← UTR stored
        │         "payment_status": "Processed",
        │         "payment_initiated": 1,
        │     })
        │
        │     # Also update Payment Entry with UTR
        │     if summary.payment_entry:
        │         frappe.db.set_value("Payment Entry", summary.payment_entry, {
        │             "reference_no": "042068068961",
        │             "reference_date": summary.payment_date,
        │         })
        │
        │     # Send email notification to party (if enabled)
        │     self.notify_party(summary)
        │
        ├── status == "Failed" (line 288-314):
        │     → Sets payment_status = "Failed"
        │     → Cancels Payment Entry if exists
        │     → Cancels linked Payment Requests
        │
        └── status == "Rejected" (line 316-342):
              → Sets payment_status = "Rejected"
              → Cancels Payment Entry + Payment Requests

→ update_payment_status()
  → All summaries "Processed" → PO status = "Approved"
```

---

## 6. Naming Conventions - How unique_id Differs Between Single and Bulk

### The Core Logic

**File:** `icici_connector.py:112-115`

```python
# Both modes start with the same expression:
payment_details = self.payment_doc if not self.bulk_transaction else self.doc
unique_id = "".join(re.findall(r"[0-9a-zA-Z]", payment_details.name))[-10:]

# Single mode overrides:
if not self.bulk_transaction:
    unique_id = payment_details.name  # full name, no transformation
```

### What `payment_details` Points To

| Mode | `payment_details` | `.name` | `unique_id` result |
|------|-------------------|---------|-------------------|
| **Single** (bulk=0) | `self.payment_doc` = per-summary payload | Summary row name like `"9jp794m7ar"` | `"9jp794m7ar"` (full name, no truncation) |
| **Bulk** (bulk=1) | `self.doc` = full Payment Order dict | PO name like `"PMO-00013"` | `"PMO00013"` (strip non-alnum, last 10 chars) |

### Why This Matters

1. **For payment initiation:**
   - Single: ICICI tracks by individual payment → uses `UNIQUEID: "9jp794m7ar"`
   - Bulk: ICICI tracks by file → uses `UNIQUE_ID: "PMO00013"` + assigns `FILE_SEQUENCE_NUM`

2. **For status checking:**
   - Single: queries by `UNIQUEID: "9jp794m7ar"` → gets status for that one payment
   - Bulk: queries by `UNIQUEID: "PMO00013"` + `FILESEQNUM: "60595132"` → gets status for all payments in file

3. **For matching responses back to summary rows:**
   - Single: response key IS the summary name → direct mapping
   - Bulk: the `transaction_remarks` field in the bulk status response contains the summary name (from MCW/MCO records) → maps back to each row

### Naming Applied Throughout

| Location | Single | Bulk |
|----------|--------|------|
| `initiate_payment()` unique_id | Full summary name | Last 10 alnum of PO name |
| `get_payment_status()` unique_id | Full summary name | Last 10 alnum of PO name |
| `set_payment_data()` UNIQUEID/UNIQUE_ID | Summary name | PO name (10 char) |
| `set_otp_data()` UNIQUEID | N/A (no OTP) | PO name (10 char) |
| `set_payment_status_data()` UNIQUEID | Summary name | PO name (10 char) |
| Bank Request Log unique_id | Summary name | PO name (10 char) |
| Duplicate check key | Summary name | PO name (10 char) |
| FILE_DESCRIPTION (bulk only) | N/A | PO name (10 char) |
| FILE_NAME (bulk only) | N/A | `"PMO00013.txt"` |
| MCW/MCO record ID (bulk file content) | N/A | Summary name (e.g., `ckp0egpov4`) |

---

## 7. API Endpoints Table - Actual ICICI Production URLs for Single vs Bulk

### How URLs Are Resolved

**File:** `india_banking_connector/connectors/bank_connector.py:28-47` (base BankConnector on connector server)

```python
@property
def urls(self):
    end_point_url = DocType("Endpoint URLs")
    bank_api_endpoint = DocType("Bank API Endpoint")
    urls = (
        frappe.qb.from_(end_point_url)
        .join(bank_api_endpoint)
        .on(end_point_url.parent == bank_api_endpoint.name)
        .select(end_point_url.action, end_point_url.url)
        .where(bank_api_endpoint.bank == self.bank)           # "ICICI Bank"
        .where(bank_api_endpoint.environment ==
               ("Testing" if self.testing else "Production"))  # based on testing flag
        .where(bank_api_endpoint.bulk_transaction ==
               (1 if self.bulk_transaction else 0))            # FILTERS by mode!
    ).run()
    return frappe._dict(dict(urls))  # { "make_payment": "url", "payment_status": "url", ... }
```

**Simple English:** There are separate "Bank API Endpoint" records for single vs bulk. Each has a child table "Endpoint URLs" with the actual URLs. The query filters by bank name, environment (Testing/Production), AND bulk_transaction flag to get the right set of URLs.

### ICICI Production Endpoint URLs

| Action | Single (bulk_transaction=0) | Bulk (bulk_transaction=1) |
|--------|---------------------------|--------------------------|
| **make_payment** | `.../api/Corporate/CIB/v1/Transaction` | `.../api/v1/cibbulkpayment/bulkPayment` |
| **payment_status** | `.../api/Corporate/CIB/v1/TransactionInquiry` | `.../api/v1/ReverseMis` |
| **generate_otp** | `.../api/Corporate/CIB/v1/Create` | `.../api/Corporate/CIB/v1/Create` |
| **register** | `.../api/Corporate/CIB/v1/Create` | _(same)_ |
| **registration_status** | `.../api/Corporate/CIB/v1/Create` | _(same)_ |
| **bank_balance** | `.../api/Corporate/CIB/v1/BalanceInquiry` | `.../api/Corporate/CIB/v1/BalanceInquiry` |
| **bank_statement** | `.../api/Corporate/CIB/v1/AccountStatement` | `.../api/Corporate/CIB/v1/AccountStatement` |

**Key differences:**
- **make_payment**: Completely different endpoints. Single uses the CIB Composite API (`/Transaction`). Bulk uses the bulk payment API (`/bulkPayment`).
- **payment_status**: Single queries individual transaction (`/TransactionInquiry`). Bulk queries by file sequence (`/ReverseMis`).
- **generate_otp, balance, statement**: Same URLs for both modes.

### URL Used in HTTP Headers

The `host` header (from `self.urls.host`) is also derived from the Bank API Endpoint and differs between single and bulk, pointing to different ICICI API gateways.

---

## 8. Encryption Differences - RSA-only vs RSA+AES

### Summary Table

| Aspect | Single (bulk_transaction=0) | Bulk (bulk_transaction=1) |
|--------|---------------------------|--------------------------|
| **Request encryption** | RSA only | RSA encrypt AES key + AES encrypt data |
| **Response decryption** | RSA only | RSA decrypt key + AES decrypt data |
| **Content-Type header** | `text/plain` | `application/json` |
| **Request body format** | Raw base64 RSA-encrypted blob | JSON envelope with encrypted fields |
| **AES Key** | Not used | `"1234567887654321"` (hardcoded 16 bytes) |
| **IV** | Not used | `"0000000000000000"` (16 zero bytes) |

### Single Mode Encryption — RSA Only

**File:** `icici_connector.py:221-223`

```python
# get_encrypted_payload — single path:
public_key_path = self.get_file_relative_path(connector_doc.public_key)
return self.rsa_encrypt_data(data, public_key_path)
```

**File:** `base bank_connector.py:318-328` (connector app — `rsa_encrypt_data`)

```python
def rsa_encrypt_data(self, data, key_path):
    data = json.dumps(data)                    # serialize dict to JSON string
    rsa_key = RSA.importKey(open(key_path).read())  # load ICICI public key
    cipher = Cipher_PKCS1_v1_5.new(rsa_key)   # PKCS1 v1.5 padding
    cipher_text = cipher.encrypt(data.encode())
    return b64encode(cipher_text).decode()     # base64 encode
```

**Flow:**
```
Payment data dict
  → JSON.dumps → string
  → RSA encrypt with ICICI's public key (PKCS1 v1.5)
  → base64 encode
  → Send as text/plain body
```

**Response decryption (`rsa_decrypt_data`):**
```
Encrypted response (base64 string)
  → base64 decode
  → RSA decrypt with our private key (PKCS1 v1.5)
  → JSON.loads → dict
```

### Bulk Mode Encryption — RSA+AES Double Layer

**File:** `icici_connector.py:204-220`

```python
# get_encrypted_payload — bulk path:
encrypted_key = self.rsa_encrypt_key(
    self.AES_KEY,                  # "1234567887654321"
    public_key_path                # ICICI's RSA public key
)
return json.dumps({
    "requestId": unique_id,        # "PMO00013"
    "service": "",
    "oaepHashingAlgorithm": "NONE",
    "encryptedKey": encrypted_key,
    "encryptedData": self.aes_encrypt_data(data, self.AES_KEY),
    "clientInfo": "",
    "optionalParam": "",
    "iv": b64encode(self.IV).decode("utf-8"),
})
```

**Flow:**
```
Step 1: RSA encrypt the AES key
  AES_KEY ("1234567887654321")
    → RSA encrypt with ICICI's public key
    → base64 encode → "encryptedKey"

Step 2: AES encrypt the payment data
  Payment data dict
    → JSON.dumps → string
    → Pad to AES block size
    → AES-CBC encrypt with AES_KEY + IV ("0000000000000000")
    → base64 encode → "encryptedData"

Step 3: Package as JSON
  { requestId, encryptedKey, encryptedData, iv }
  → Send as application/json body
```

**Response decryption:**
```
Step 1: RSA decrypt the AES key
  response.encryptedKey
    → base64 decode
    → RSA decrypt with our private key
    → Get AES key string

Step 2: AES decrypt the response
  response.encryptedData
    → base64 decode
    → AES-CBC decrypt with decrypted key + IV
    → Unpad
    → JSON.loads → dict
```

### Why Two Different Encryption Schemes?

**Simple English:** Single payments use ICICI's CIB (Corporate Internet Banking) Composite API, which is an older/simpler API that accepts RSA-encrypted data directly. Bulk payments use ICICI's newer bulk payment API, which requires the more standard RSA+AES envelope encryption pattern. The envelope approach is more efficient for large payloads because RSA encryption is slow for large data, while AES is fast. RSA encrypts only the small AES key (16 bytes), and AES encrypts the actual large payload.

### Code Location Reference

| Method | File | Lines | Purpose |
|--------|------|-------|---------|
| `rsa_encrypt_key` | connector `bank_connector.py` | 260-267 | RSA encrypt AES key with public key |
| `rsa_decrypt_key` | connector `bank_connector.py` | 269-272 | RSA decrypt AES key with private key |
| `aes_encrypt_data` | connector `bank_connector.py` | 274-290 | AES-CBC encrypt with key+IV |
| `aes_decrypt_data` | connector `bank_connector.py` | 292-316 | AES-CBC decrypt with key, handles IV prefix |
| `rsa_encrypt_data` | connector `bank_connector.py` | 318-328 | RSA encrypt data directly (single mode) |
| `rsa_decrypt_data` | connector `bank_connector.py` | 343-351 | RSA decrypt data directly (single mode) |
| `get_encrypted_payload` | `icici_connector.py` | 197-223 | Chooses encryption path based on `bulk_transaction` |
| `get_decrypted_response` | `icici_connector.py` | 433-473 | Chooses decryption path based on `bulk_transaction` |

---

## 9. Real Working Examples

### Production Environment Details

```
Company:             Unique Educational and Sports Foundation
Bank Connector:      UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank
Connector URL:       https://erp.walnutedu.in

Company Bank Account:
  Name:              UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank
  Account No:        756501000565
  IFSC:              ICIC0007565
  Account Name:      UNIQUE EDUCATION AND SPORTS FOUNDATION

ICICI Connector (ONE record):
  Name/Account No:   756501000565
  Corp ID:           600336118
  Aggr ID:           BULK0092
  Aggr Name:         UNIQUE
  URN:               SR266115962
  Corp User:         NEELAVIN
  Status Corp User:  600336118.NEELAVIN
  Testing:           No (Production)
  Active:            Yes

Connector Map Entries (for ICICI):
  Row 8cs0fguqs9: ICICI Bank → ICICI Connector, bulk_transaction=0 (single)
  Row 8cs5igjtp0: ICICI Bank → ICICI Connector, bulk_transaction=1 (bulk)
  (Both point to same DocType, resolved to same record "756501000565")

India Banking Settings:
  Summarise Based On:          Party
  Background Payment:          Disabled
  Workflow on Bank Account:    Enabled
  Auto Update Payment Status:  Disabled
```

---

### Example 1: Single Payment - PMO-00012

> Processed on **Oct 23, 2025** when Bank Connector had `bulk_transaction=0`

#### Document Records

```
Payment Order: PMO-00012
  Status:                Initiated
  Total:                 Rs 10.00
  Company Bank Account:  UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank
  file_sequence_number:  NULL (not applicable for single)

Payment Order Reference:
  Name:              9jp7ma031n
  Party:             Naresh (Supplier)
  Amount:            Rs 10.00
  Payment Request:   Naresh-408872
  Purchase Order:    PUR-ORD-2025-01269

Payment Order Summary:
  Name:              9jp794m7ar
  Party:             Naresh (Supplier)
  Amount:            Rs 10.00
  Mode of Transfer:  IMPS
  Payment Status:    Initiated
  Payment Initiated: Yes
  Payment Entry:     Naresh-408873
  Bank Account:      Naresh Tak - INDUSIND BANK
  Bank:              INDUSIND BANK
  Payment Date:      2025-10-23

Payee Bank Account (Naresh Tak - INDUSIND BANK):
  Account No:        157588285774
  Account Name:      Naresh Tak
  IFSC:              INDB0000746
  Party:             Naresh (Supplier)
```

#### API Logs

**India Banking Request Log (ERP server):**

| Timestamp | Action | Reference | Status |
|-----------|--------|-----------|--------|
| 16:18:15 | get_payment_status | PMO-00012 | 200 |
| 16:18:16 | initiate_payment | PMO-00012 | 200 |

**Bank Request Log (Connector server):**

| Timestamp | Action | unique_id | Reference | Status |
|-----------|--------|-----------|-----------|--------|
| 16:18:15 | Payment Status | 9jp794m7ar | PMO-00012 | 200 |
| 16:18:16 | Initiate Payment | 9jp794m7ar | PMO-00012 | 200 |

---

### Example 2: Bulk Payment - PMO-00013

> Processed on **Oct 23, 2025** after Bank Connector was switched to `bulk_transaction=1`

#### Document Records

```
Payment Order: PMO-00013
  Status:                Approved (= all payments processed)
  Total:                 Rs 10.00
  Company Bank Account:  UNIQUE EDUCATION AND SPORTS FOUNDATION - ICICI Bank
  file_sequence_number:  60595132  ← assigned by bank for bulk

Payment Order Reference:
  Name:              ckp0s0kh9p
  Party:             Naresh (Supplier)
  Amount:            Rs 10.00
  Payment Request:   Naresh-408874
  Purchase Order:    PUR-ORD-2025-01269

Payment Order Summary:
  Name:              ckp0egpov4
  Party:             Naresh (Supplier)
  Amount:            Rs 10.00
  Mode of Transfer:  IMPS
  Payment Status:    Processed
  Payment Initiated: Yes
  Payment Entry:     Naresh-408875
  Bank Account:      Naresh Tak - INDUSIND BANK
  Bank:              INDUSIND BANK
  Reference Number:  042068068961  ← UTR number from bank
  Payment Date:      2025-10-23
```

#### API Logs

**India Banking Request Log (ERP server):**

| Timestamp | Action | Reference | Status |
|-----------|--------|-----------|--------|
| 16:19:57 | Generate Otp | PMO-00013 | 200 |
| 16:29:05 | Generate Otp | PMO-00013 | 200 |
| 17:46:33 | Generate Otp | PMO-00013 | 200 |
| 17:47:21 | get_payment_status | PMO-00013 | 200 |
| 17:47:22 | initiate_payment | PMO-00013 | 200 |
| 17:48:48 | get_payment_status | PMO-00013 | 200 |
| 17:53:49 | get_payment_status | PMO-00013 | 200 |
| 18:00:50 | get_payment_status | PMO-00013 | 200 |
| 18:09:15 | get_payment_status | PMO-00013 | 200 |

**Bank Request Log (Connector server):**

| Timestamp | Action | unique_id | Reference | Status |
|-----------|--------|-----------|-----------|--------|
| 16:19:57 | Generate OTP | - | PMO-00013 | 400 (FAILED) |
| 16:29:05 | Generate OTP | - | PMO-00013 | 200 |
| 17:46:33 | Generate OTP | - | PMO-00013 | 200 |
| 17:47:21 | Payment Status | PMO00013 | PMO-00013 | 200 |
| 17:47:22 | Initiate Payment | PMO00013 | PMO-00013 | 200 |
| 17:48:47 | Payment Status | PMO00013 | PMO-00013 | 200 |
| 17:53:49 | Payment Status | PMO00013 | PMO-00013 | 200 |
| 18:00:50 | Payment Status | PMO00013 | PMO-00013 | 200 |
| 18:09:15 | Payment Status | PMO00013 | PMO-00013 | 200 |

---

### Side-by-Side Comparison

| Aspect | PMO-00012 (SINGLE) | PMO-00013 (BULK) |
|--------|-------------------|------------------|
| **bulk_transaction** | 0 | 1 |
| **OTP required** | No | Yes (3 attempts: 1 fail + 2 success) |
| **Total API calls** | 2 | 9 (3 OTP + 1 pre-status + 1 initiate + 4 status) |
| **unique_id** | `9jp794m7ar` (summary name) | `PMO00013` (last 10 alnum of PO name) |
| **Bank API URL** | `.../CIB/v1/Transaction` | `.../cibbulkpayment/bulkPayment` |
| **Payload format** | JSON with individual fields | Encrypted pipe-delimited file |
| **Encryption** | RSA only | RSA + AES (double) |
| **file_sequence_number** | NULL | `60595132` |
| **Status check URL** | `.../CIB/v1/TransactionInquiry` | `.../v1/ReverseMis` |
| **Status check key** | Summary name as UNIQUEID | PO name + FILESEQNUM |
| **PAYEENAME** | `Naresh Tak` | `NareshTak` (in MCO, spaces stripped) |
| **Final status** | Initiated | Processed with UTR `042068068961` |
| **Processing time** | Immediate | ~22 minutes (17:47→18:09) |

---

### Bulk Payment Full Lifecycle - PMO-00002

> Complete lifecycle from OTP to final UTR, processed **May 16, 2025**

```
Payment Order: PMO-00002
  Total:                 Rs 8.00
  Party:                 Naresh (Supplier)
  file_sequence_number:  52940152
  Final Status:          Approved
  UTR:                   040294071511

Timeline:
  May 16, 12:30:01 → Generate OTP (200)
  May 16, 12:45:38 → Generate OTP (200) — re-requested after ~15 min
  May 16, 12:45:51 → Payment Status (pre-initiation check)
  May 16, 12:45:52 → Initiate Payment (200)
  May 16, 12:46:03 → Payment Status (200) — pending
  May 16, 12:54:21 → Payment Status (200) — pending
  May 16, 13:20:51 → Payment Status (200) — pending
  May 19, 22:30:54 → Payment Status (200) — 3 days later, processed!
  May 21, 16:34:22 → Payment Status (200) — confirmation check
```
