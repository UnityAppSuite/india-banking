import json

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import get_link_to_form, nowdate

from india_banking.overrides.payment_order import get_party_summary
from india_banking.utils import get_party_bank_account


@frappe.whitelist()
def process_salary_payment(payroll_entry_name, mode="review"):
	"""Orchestrate salary payment: Payroll Entry → Bank Entry JE → Payment Order."""
	payroll_entry = frappe.get_doc("Payroll Entry", payroll_entry_name)
	_validate_prerequisites(payroll_entry)

	bank_entry = payroll_entry.make_bank_entry()
	if not bank_entry:
		frappe.throw(_("Could not create Bank Entry. Ensure salary slip totals are positive."))

	# Bank Entry voucher type requires cheque_no and cheque_date for submission
	bank_entry.cheque_no = bank_entry.cheque_no or payroll_entry_name
	bank_entry.cheque_date = bank_entry.cheque_date or bank_entry.posting_date or nowdate()
	bank_entry.save()

	result = {"journal_entry": bank_entry.name}

	if mode == "review":
		frappe.msgprint(
			_("Bank Entry {0} created in draft. Please review and submit.").format(
				get_link_to_form("Journal Entry", bank_entry.name)
			),
			alert=True,
		)
		return result

	# Direct mode: submit JE and create Payment Order
	try:
		bank_entry.submit()
	except Exception:
		frappe.log_error(title=f"Bank Entry submission failed for {payroll_entry_name}")
		frappe.throw(
			_("Failed to submit Bank Entry {0}. Please review it manually.").format(
				get_link_to_form("Journal Entry", bank_entry.name)
			)
		)

	try:
		po_result = create_payment_order_from_bank_entry(
			journal_entry_name=bank_entry.name,
			company_bank_account=payroll_entry.bank_account,
		)
		result["payment_order"] = po_result["payment_order"]
	except Exception:
		frappe.log_error(title=f"Payment Order creation failed for {payroll_entry_name}")
		frappe.msgprint(
			_("Bank Entry submitted but Payment Order creation failed. "
			  "Create it manually from {0}.").format(
				get_link_to_form("Journal Entry", bank_entry.name)
			),
			indicator="orange",
			alert=True,
		)

	return result


@frappe.whitelist()
def create_payment_order_from_bank_entry(journal_entry_name, company_bank_account=None):
	"""Create a draft Payment Order from a submitted Bank Entry JE with auto-generated summary."""
	from india_banking.overrides.journal_entry import make_payment_order

	bank_entry = frappe.get_doc("Journal Entry", journal_entry_name)
	_validate_bank_entry(bank_entry)
	_validate_no_existing_payment_order(journal_entry_name)
	_validate_party_bank_accounts(journal_entry_name)

	po = make_payment_order(source_name=journal_entry_name)
	if not po.references:
		frappe.throw(
			_("No valid party bank accounts found. "
			  "Ensure all parties have a default, enabled Bank Account with INR currency.")
		)

	# Set company bank account and summary settings
	po.company_bank_account = company_bank_account or _get_company_bank_account(bank_entry)
	summarise_by = frappe.db.get_single_value("India Banking Settings", "summarise_payment_based_on")
	if summarise_by:
		po.summarise_payment_based_on = summarise_by

	# Generate summary before save (validate_summary requires it)
	_populate_summary(po, summarise_by)
	po.save(ignore_permissions=True)

	frappe.msgprint(
		_("Payment Order {0} created successfully.").format(
			get_link_to_form("Payment Order", po.name)
		),
		alert=True,
	)
	return {"payment_order": po.name}


# --- Validation helpers ---

def _validate_prerequisites(payroll_entry):
	"""Validate Payroll Entry is ready for salary payment processing."""
	if payroll_entry.docstatus != 1:
		frappe.throw(_("Payroll Entry must be submitted."))

	if not payroll_entry.salary_slips_submitted:
		frappe.throw(_("Salary Slips must be submitted before processing payment."))

	if not payroll_entry.payment_account:
		frappe.throw(_("Payment Account is mandatory. Please set it on the Payroll Entry."))

	if not frappe.db.get_single_value(
		"Payroll Settings", "process_payroll_accounting_entry_based_on_employee"
	):
		frappe.throw(
			_("'Process Payroll Accounting Entry Based on Employee' must be enabled in {0} "
			  "for the Payment Order flow to work.").format(
				get_link_to_form("Payroll Settings", "Payroll Settings")
			)
		)

	if payroll_entry.has_bank_entries().get("has_bank_entries"):
		frappe.throw(_("Bank Entry already exists for this Payroll Entry."))


def _validate_bank_entry(bank_entry):
	"""Ensure Journal Entry is a submitted Bank Entry."""
	errors = []
	if bank_entry.docstatus != 1:
		errors.append(_("Journal Entry {0} is not submitted (docstatus: {1}).").format(
			bank_entry.name, bank_entry.docstatus
		))

	if bank_entry.voucher_type != "Bank Entry":
		errors.append(_("Journal Entry {0} has voucher type '{1}', expected 'Bank Entry'.").format(
			bank_entry.name, bank_entry.voucher_type
		))

	if errors:
		msg = "<br>".join(errors)
		frappe.log_error(
			title=f"Bank Entry Validation Failed: {bank_entry.name}",
			message=f"Journal Entry: {bank_entry.name}\n" + "\n".join(errors),
		)
		frappe.throw(msg, title=_("Invalid Bank Entry"))


def _validate_no_existing_payment_order(journal_entry_name):
	"""Ensure no Payment Order already exists for this Bank Entry."""
	existing_po = frappe.db.get_value(
		"Payment Order Reference",
		{"reference_name": journal_entry_name, "docstatus": ["!=", 2]},
		"parent",
	)
	if existing_po:
		msg = _("Payment Order {0} already exists for Bank Entry {1}.").format(
			get_link_to_form("Payment Order", existing_po), journal_entry_name
		)
		frappe.log_error(
			title=f"Duplicate Payment Order: {journal_entry_name}",
			message=f"Journal Entry: {journal_entry_name}\nExisting Payment Order: {existing_po}",
		)
		frappe.throw(msg, title=_("Duplicate Payment Order"))


def _validate_party_bank_accounts(journal_entry_name):
	"""Validate all parties in the JE have a valid bank account (default, enabled, approved)."""
	JEA = DocType("Journal Entry Account")

	parties = (
		frappe.qb.from_(JEA)
		.select(JEA.party_type, JEA.party)
		.where(
			(JEA.parent == journal_entry_name)
			& (JEA.party.isnotnull())
			& (JEA.party != "")
			& (JEA.debit > 0)
			& (JEA.payment_status.notin(["Paid", "Ordered", "Payment Ordered"]))
		)
		.run(as_dict=True)
	)

	# Uses india_banking.utils.get_party_bank_account which respects workflow approval state
	invalid_parties = [
		p for p in parties
		if not get_party_bank_account(p.party_type, p.party)
	]

	if invalid_parties:
		error_lines = [f"{p.party_type}: <b>{p.party}</b>" for p in invalid_parties]
		log_lines = [f"{p.party_type}: {p.party}" for p in invalid_parties]

		frappe.log_error(
			title=f"Invalid Party Bank Accounts: {journal_entry_name}",
			message=(
				f"Journal Entry: {journal_entry_name}\n"
				f"Total parties: {len(parties)}, Invalid: {len(invalid_parties)}\n\n"
				+ "\n".join(log_lines)
			),
		)
		frappe.throw(
			_("Cannot create Payment Order. The following parties do not have a valid "
			  "bank account (default, enabled, approved):<br><br>") + "<br>".join(error_lines),
			title=_("Invalid Party Bank Accounts"),
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
			return frappe.db.get_value(
				"Bank Account", {"account": row.account, "is_company_account": 1}
			)
	return None
