import json

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import get_link_to_form, nowdate

from india_banking.overrides.payment_order import get_party_summary
from india_banking.utils import get_party_bank_account


def _log_and_throw(title, message, ref_dt=None, ref_name=None, msg=None):
	"""Log error with reference linking and throw with Error Log link in popup."""
	log = frappe.log_error(title=title, message=message, reference_doctype=ref_dt, reference_name=ref_name)
	error_link = get_link_to_form("Error Log", log.name)
	frappe.throw((msg or message) + f"<br><br>Error Log: {error_link}", title=_(title))


def _log_and_msgprint(title, message, ref_dt=None, ref_name=None, msg=None):
	"""Log error with reference linking and show non-blocking msgprint with Error Log link."""
	log = frappe.log_error(title=title, message=message, reference_doctype=ref_dt, reference_name=ref_name)
	error_link = get_link_to_form("Error Log", log.name)
	frappe.msgprint((msg or message) + f"<br><br>Error Log: {error_link}", indicator="orange", alert=True)


def _check_party_bank_accounts(parties, ref_dt, ref_name):
	"""Validate parties have valid bank accounts. Throws with employee names + Error Log link if invalid.

	Args:
		parties: list of dicts with party_type and party keys
		ref_dt: reference doctype for Error Log linking
		ref_name: reference name for Error Log linking
	"""
	if not parties:
		_log_and_throw(
			f"No parties found: {ref_name}",
			f"{ref_dt}: {ref_name}\nNo party/employee rows found.",
			ref_dt, ref_name,
			_("No employee/party rows found in {0}.<br><br>"
			  "Ensure <b>'Process Payroll Accounting Entry Based on Employee'</b> is enabled in {1}.").format(
				get_link_to_form(ref_dt, ref_name),
				get_link_to_form("Payroll Settings", "Payroll Settings")),
		)

	invalid = [p for p in parties if not get_party_bank_account(p.get("party_type"), p.get("party"))]
	if not invalid:
		return

	# Build employee name + ID display lines
	name_map = {"Employee": "employee_name", "Supplier": "supplier_name", "Customer": "customer_name"}
	for p in invalid:
		p["display_name"] = frappe.db.get_value(p["party_type"], p["party"], name_map.get(p["party_type"], "name")) or p["party"]

	error_lines = [f"{i+1}. <b>{p['display_name']}</b> ({p['party']})" for i, p in enumerate(invalid)]
	log_lines = [f"{p['party_type']}: {p['party']} - {p['display_name']}" for p in invalid]

	_log_and_throw(
		f"Invalid Party Bank Accounts ({len(invalid)}): {ref_name}",
		f"{ref_dt}: {ref_name}\nTotal: {len(parties)}, Invalid: {len(invalid)}\n\n" + "\n".join(log_lines),
		ref_dt, ref_name,
		_("Cannot proceed. The following {0} employees do not have a valid "
		  "bank account (default, enabled, approved):<br><br>").format(len(invalid)) + "<br>".join(error_lines),
	)


@frappe.whitelist()
def process_salary_payment(payroll_entry_name, mode="review"):
	"""Orchestrate salary payment: Payroll Entry → Bank Entry JE → Payment Order."""
	payroll_entry = frappe.get_doc("Payroll Entry", payroll_entry_name)
	_validate_prerequisites(payroll_entry)

	# For direct mode, validate employee bank accounts BEFORE creating Bank Entry
	if mode == "direct":
		employees = [{"party_type": "Employee", "party": e.employee} for e in payroll_entry.employees]
		_check_party_bank_accounts(employees, "Payroll Entry", payroll_entry_name)

	bank_entry = payroll_entry.make_bank_entry()
	if not bank_entry:
		frappe.throw(_("Could not create Bank Entry. Ensure salary slip totals are positive."))

	bank_entry.cheque_no = bank_entry.cheque_no or payroll_entry_name
	bank_entry.cheque_date = bank_entry.cheque_date or bank_entry.posting_date or nowdate()
	bank_entry.save()

	result = {"journal_entry": bank_entry.name}

	if mode == "review":
		frappe.msgprint(
			_("Bank Entry {0} created in draft. Please review and submit.").format(
				get_link_to_form("Journal Entry", bank_entry.name)
			), alert=True,
		)
		return result

	# Direct mode: submit JE and create Payment Order
	try:
		bank_entry.submit()
	except Exception:
		_log_and_throw(
			f"Bank Entry submission failed: {payroll_entry_name}",
			f"Payroll Entry: {payroll_entry_name}\nJournal Entry: {bank_entry.name}",
			"Journal Entry", bank_entry.name,
			_("Failed to submit Bank Entry {0}. Review it manually.").format(
				get_link_to_form("Journal Entry", bank_entry.name)),
		)

	try:
		po_result = create_payment_order_from_bank_entry(
			journal_entry_name=bank_entry.name,
			company_bank_account=payroll_entry.bank_account,
		)
		result["payment_order"] = po_result["payment_order"]
	except Exception:
		_log_and_msgprint(
			f"Payment Order creation failed: {payroll_entry_name}",
			f"Payroll Entry: {payroll_entry_name}\nJournal Entry: {bank_entry.name}",
			"Payroll Entry", payroll_entry_name,
			_("Bank Entry submitted but Payment Order creation failed. Create it manually from {0}.").format(
				get_link_to_form("Journal Entry", bank_entry.name)),
		)

	return result


@frappe.whitelist()
def create_payment_order_from_bank_entry(journal_entry_name, company_bank_account=None):
	"""Create a draft Payment Order from a submitted Bank Entry JE with auto-generated summary."""
	from india_banking.overrides.journal_entry import make_payment_order

	bank_entry = frappe.get_doc("Journal Entry", journal_entry_name)
	_validate_bank_entry(bank_entry)
	_validate_no_existing_payment_order(journal_entry_name)

	# Validate party bank accounts from JE rows
	JEA = DocType("Journal Entry Account")
	parties = (
		frappe.qb.from_(JEA)
		.select(JEA.party_type, JEA.party)
		.where(
			(JEA.parent == journal_entry_name) & (JEA.party.isnotnull()) & (JEA.party != "")
			& (JEA.debit > 0)
			& (JEA.payment_status.notin(["Paid", "Ordered", "Payment Ordered"]))
		).run(as_dict=True)
	)
	_check_party_bank_accounts(parties, "Journal Entry", journal_entry_name)

	po = make_payment_order(source_name=journal_entry_name)
	if not po.references:
		_log_and_throw(
			f"No Payment References: {journal_entry_name}",
			f"Journal Entry: {journal_entry_name}\nNo party rows with valid bank accounts.",
			"Journal Entry", journal_entry_name,
			_("No valid payment references found in {0}.<br><br>"
			  "1. <b>'Process Payroll Accounting Entry Based on Employee'</b> must be enabled in {1}<br>"
			  "2. Or all employees lack a valid bank account (default, enabled, INR)").format(
				get_link_to_form("Journal Entry", journal_entry_name),
				get_link_to_form("Payroll Settings", "Payroll Settings")),
		)

	po.company_bank_account = company_bank_account or _get_company_bank_account(bank_entry)
	summarise_by = frappe.db.get_single_value("India Banking Settings", "summarise_payment_based_on")
	if summarise_by:
		po.summarise_payment_based_on = summarise_by

	_populate_summary(po, summarise_by)
	po.save(ignore_permissions=True)

	frappe.msgprint(
		_("Payment Order {0} created successfully.").format(get_link_to_form("Payment Order", po.name)),
		alert=True,
	)
	return {"payment_order": po.name}


# --- Validation helpers ---

def _validate_prerequisites(pe):
	"""Validate Payroll Entry is ready for salary payment processing."""
	if pe.docstatus != 1:
		_log_and_throw("PE not submitted", f"Payroll Entry: {pe.name}, docstatus: {pe.docstatus}",
			"Payroll Entry", pe.name, _("Payroll Entry must be submitted."))

	if not pe.salary_slips_submitted:
		_log_and_throw("Salary Slips not submitted", f"Payroll Entry: {pe.name}",
			"Payroll Entry", pe.name, _("Salary Slips must be submitted before processing payment."))

	if not pe.payment_account:
		_log_and_throw("Payment Account missing", f"Payroll Entry: {pe.name}",
			"Payroll Entry", pe.name, _("Payment Account is mandatory. Please set it on the Payroll Entry."))

	if not frappe.db.get_single_value("Payroll Settings", "process_payroll_accounting_entry_based_on_employee"):
		_log_and_throw("Employee-wise accounting disabled", f"Payroll Entry: {pe.name}",
			"Payroll Entry", pe.name,
			_("'Process Payroll Accounting Entry Based on Employee' must be enabled in {0}.").format(
				get_link_to_form("Payroll Settings", "Payroll Settings")))

	if pe.has_bank_entries().get("has_bank_entries"):
		_log_and_throw("Bank Entry already exists", f"Payroll Entry: {pe.name}",
			"Payroll Entry", pe.name, _("Bank Entry already exists for this Payroll Entry."))


def _validate_bank_entry(be):
	"""Ensure Journal Entry is a submitted Bank Entry."""
	errors = []
	if be.docstatus != 1:
		errors.append(_("Journal Entry {0} is not submitted.").format(be.name))
	if be.voucher_type != "Bank Entry":
		errors.append(_("Journal Entry {0} has voucher type '{1}', expected 'Bank Entry'.").format(be.name, be.voucher_type))

	if errors:
		_log_and_throw(f"Invalid Bank Entry: {be.name}", "\n".join(errors),
			"Journal Entry", be.name, "<br>".join(errors))


def _validate_no_existing_payment_order(je_name):
	"""Ensure no Payment Order already exists for this Bank Entry."""
	existing_po = frappe.db.get_value(
		"Payment Order Reference", {"reference_name": je_name, "docstatus": ["!=", 2]}, "parent",
	)
	if existing_po:
		_log_and_throw(
			f"Duplicate Payment Order: {je_name}",
			f"Journal Entry: {je_name}\nExisting PO: {existing_po}",
			"Journal Entry", je_name,
			_("Payment Order {0} already exists for Bank Entry {1}.").format(
				get_link_to_form("Payment Order", existing_po), je_name),
		)


# --- Data helpers ---

def _populate_summary(po, summarise_by):
	"""Generate and populate Payment Order summary child table from references."""
	summary_items = get_party_summary(
		references=json.dumps([ref.as_dict() for ref in po.references]),
		company_bank_account=po.company_bank_account,
		summarise_payment_based_on=summarise_by,
	)
	if summary_items:
		po.set("summary", [])
		for item in summary_items:
			po.append("summary", item)
		po.total = sum(item.get("amount", 0) for item in summary_items)


def _get_company_bank_account(bank_entry):
	"""Derive company bank account from the JE's credit (bank) account row."""
	for row in bank_entry.accounts:
		if row.credit > 0 and row.account:
			return frappe.db.get_value("Bank Account", {"account": row.account, "is_company_account": 1})
	return None
