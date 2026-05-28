import json

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt, get_link_to_form, nowdate

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
	"""Validate parties have valid Bank Accounts. Throws with per-employee
	specific reason (no account / disabled / not approved / not default)
	if any are invalid.

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

	workflow_enabled = bool(frappe.db.get_single_value(
		"India Banking Settings", "activate_workflow_on_bank_account"
	))

	invalid = []
	for p in parties:
		if get_party_bank_account(p.get("party_type"), p.get("party")):
			continue
		reason = _diagnose_party_bank_account(p.get("party_type"), p.get("party"), workflow_enabled)
		invalid.append({**p, **reason})

	if not invalid:
		return

	# Build display lines (human-facing HTML and plain-text log)
	error_lines = []
	log_lines = []
	# Group by reason category for the operator
	categorized = {}
	for i, p in enumerate(invalid, 1):
		cat = p["category"]
		categorized.setdefault(cat, []).append(p)

	for cat, items in categorized.items():
		error_lines.append(f"<br><b>{cat}</b> ({len(items)}):")
		for p in items:
			ba_part = f", BA: <code>{p['ba_name']}</code>" if p.get("ba_name") else ""
			error_lines.append(
				f"&nbsp;&nbsp;&bull; <b>{p['display_name']}</b> "
				f"(<code>{p['party']}</code>) — {p['detail']}{ba_part}"
			)
			log_lines.append(f"[{cat}] {p['party_type']}: {p['party']} - {p['display_name']} — {p['detail']}")

	# Per-category fix recipes
	fix_lines = []
	if "No Bank Account exists" in categorized:
		fix_lines.append("&bull; <b>No Bank Account exists</b>: create one with party_type=Employee, party=&lt;employee&gt;, is_default=1.")
	if "Not marked as default (is_default=0)" in categorized:
		fix_lines.append("&bull; <b>Not default</b>: open the Bank Account and tick <b>Is Default</b>.")
	if "Disabled" in categorized:
		fix_lines.append("&bull; <b>Disabled</b>: open the Bank Account and untick <b>Disabled</b>.")
	if "Workflow state is not 'Approved'" in categorized:
		fix_lines.append("&bull; <b>Not approved</b>: open the Bank Account and run it through the workflow until <b>workflow_state=Approved</b>.")
	if "Multiple issues" in categorized:
		fix_lines.append("&bull; <b>Multiple issues</b>: address every flag listed above on each Bank Account.")

	fix_block = ("<br><br><b>How to fix:</b><br>" + "<br>".join(fix_lines)) if fix_lines else ""

	_log_and_throw(
		f"Invalid Party Bank Accounts ({len(invalid)}): {ref_name}",
		f"{ref_dt}: {ref_name}\nTotal parties: {len(parties)}, Invalid: {len(invalid)}\n\n"
		+ "\n".join(log_lines),
		ref_dt, ref_name,
		_("Cannot proceed. {0} of {1} employees do not have a usable Bank Account.<br><br>"
		  "A usable Bank Account must satisfy ALL of:<br>"
		  "&nbsp;&nbsp;&bull; <code>is_default = 1</code><br>"
		  "&nbsp;&nbsp;&bull; <code>disabled = 0</code><br>"
		  + ("&nbsp;&nbsp;&bull; <code>workflow_state = Approved</code> (because Bank Account workflow is enabled)<br>"
		     if workflow_enabled else "")
		  + "<br><b>Failures by category:</b>{2}{3}").format(
			len(invalid), len(parties),
			"".join(error_lines),
			fix_block,
		),
	)


def _diagnose_party_bank_account(party_type, party, workflow_enabled):
	"""Look at every Bank Account belonging to this party and figure out the
	single best reason why none qualifies as 'default, enabled, approved'.

	Returns a dict: {category, detail, ba_name, display_name}
	    category: short label suitable for grouping (e.g. 'Disabled')
	    detail: human-readable specifics (e.g. 'BA exists but workflow_state=Pending')
	    ba_name: the closest matching Bank Account name (if any)
	    display_name: human-readable name of the party
	"""
	name_map = {"Employee": "employee_name", "Supplier": "supplier_name", "Customer": "customer_name"}
	display_name = (
		frappe.db.get_value(party_type, party, name_map.get(party_type, "name"))
		or party
	)

	# All Bank Accounts for this party, regardless of state
	all_bas = frappe.get_all(
		"Bank Account",
		filters={"party_type": party_type, "party": party},
		fields=["name", "is_default", "disabled", "workflow_state", "currency"],
	)

	if not all_bas:
		return {
			"category": "No Bank Account exists",
			"detail": "no Bank Account record exists for this party",
			"ba_name": None,
			"display_name": display_name,
		}

	# Try to find the closest-to-valid one, and report what's wrong with IT
	# Preference: default > not-default; enabled > disabled; approved > others
	def score(ba):
		s = 0
		if ba["is_default"]:
			s += 4
		if not ba["disabled"]:
			s += 2
		if not workflow_enabled or ba["workflow_state"] == "Approved":
			s += 1
		return s

	all_bas.sort(key=score, reverse=True)
	best = all_bas[0]

	issues = []
	if not best["is_default"]:
		issues.append("is_default=0")
	if best["disabled"]:
		issues.append("disabled=1")
	if workflow_enabled and best["workflow_state"] != "Approved":
		issues.append(f"workflow_state='{best['workflow_state'] or '(blank)'}' (need 'Approved')")

	if len(issues) == 0:
		# Shouldn't happen — get_party_bank_account returned None but we found a valid BA?
		# Fall through with a generic category so we don't crash.
		return {
			"category": "Lookup mismatch",
			"detail": f"BA '{best['name']}' looks valid but get_party_bank_account didn't return it",
			"ba_name": best["name"],
			"display_name": display_name,
		}
	elif len(issues) == 1:
		cat_map = {
			"is_default=0": "Not marked as default (is_default=0)",
			"disabled=1": "Disabled",
		}
		one = issues[0]
		category = cat_map.get(
			one,
			"Workflow state is not 'Approved'" if one.startswith("workflow_state") else "Other",
		)
		return {
			"category": category,
			"detail": one,
			"ba_name": best["name"],
			"display_name": display_name,
		}
	else:
		return {
			"category": "Multiple issues",
			"detail": ", ".join(issues),
			"ba_name": best["name"],
			"display_name": display_name,
		}


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
	_set_debits_to_rounded_total(bank_entry, payroll_entry_name)
	bank_entry.save()

	# Replace HRMS auto-accrual with manager-spec Salary JE in draft state
	salary_je = _create_salary_je(payroll_entry, submit_now=False)

	result = {"journal_entry": bank_entry.name, "salary_je": salary_je.name if salary_je else None}

	if mode == "review":
		frappe.msgprint(
			_("Bank Entry {0} and Salary JE {1} created in draft. Please review and submit.").format(
				get_link_to_form("Journal Entry", bank_entry.name),
				get_link_to_form("Journal Entry", salary_je.name) if salary_je else "—",
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

	# Submit the Salary JE alongside the Bank Entry
	if salary_je:
		try:
			salary_je.reload()
			salary_je.submit()
		except Exception:
			_log_and_msgprint(
				f"Salary JE submission failed: {payroll_entry_name}",
				f"Payroll Entry: {payroll_entry_name}\nSalary JE: {salary_je.name}",
				"Journal Entry", salary_je.name,
				_("Salary JE {0} could not be auto-submitted. Submit it manually.").format(
					get_link_to_form("Journal Entry", salary_je.name)),
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
	if not po.company_bank_account:
		diag = _diagnose_missing_company_bank_account(bank_entry)
		_log_and_throw(
			f"Company Bank Account not derivable: {journal_entry_name}",
			f"Journal Entry: {journal_entry_name}\n\nDiagnostic:\n{frappe.utils.strip_html(diag)}",
			"Journal Entry", journal_entry_name,
			_("Could not resolve the Company Bank Account for {0}.<br><br>"
			  "We look up a <b>Bank Account</b> doc where:<br>"
			  "&nbsp;&nbsp;1. Its <code>account</code> field equals the JE credit row's account, OR<br>"
			  "&nbsp;&nbsp;2. Its <code>bank_account_no</code> equals the COA Account's "
			  "<code>account_number</code> (for the same Company)<br>"
			  "AND <code>is_company_account = 1</code> AND not disabled.<br><br>"
			  "<b>Diagnostic for this JE:</b><br>{1}<br><br>"
			  "Most common cause: a Bank Account exists with the correct "
			  "<code>bank_account_no</code> but its <code>account</code> link points to a "
			  "<b>parent/group COA account</b> instead of the specific leaf. Open that Bank "
			  "Account and set its <b>Account</b> field to the JE row's specific account.").format(
				get_link_to_form("Journal Entry", journal_entry_name), diag,
			),
		)

	summarise_by = frappe.db.get_single_value("India Banking Settings", "summarise_payment_based_on")
	if summarise_by:
		po.summarise_payment_based_on = summarise_by

	_populate_summary(po, summarise_by)

	# Pre-save validation: make sure summary built and every row resolves to a Mode of Transfer
	if not po.summary:
		_log_and_throw(
			f"Empty Summary on PO build: {journal_entry_name}",
			f"Journal Entry: {journal_entry_name}\nReferences: {len(po.references)}\nCompany BA: {po.company_bank_account}",
			"Journal Entry", journal_entry_name,
			_("Could not build the Payment Order summary for {0}. "
			  "Check that the JE has party debit rows with valid default Bank Accounts "
			  "and that the company bank account is set.").format(
				get_link_to_form("Journal Entry", journal_entry_name)),
		)

	# Last-resort safety net: if any row still has no MoT (e.g. operator hasn't set
	# a default in India Banking Settings AND the master data has gaps), force-apply
	# the PO header default if available, else log a non-blocking warning.
	missing_mot = [s for s in po.summary if not s.get("mode_of_transfer")]
	if missing_mot:
		fallback = po.default_mode_of_transfer or _get_default_mode_of_transfer()
		if fallback:
			for s in missing_mot:
				s.mode_of_transfer = fallback
			po.default_mode_of_transfer = fallback
		else:
			# Nothing to fall back on — log so it's traceable, but don't block save.
			# validate_summary will surface the issue if it's actually unresolvable.
			_log_and_msgprint(
				f"MoT unresolved (no default set): {journal_entry_name}",
				f"Journal Entry: {journal_entry_name}\nRows without Mode of Transfer: {len(missing_mot)}\n\n"
				+ "\n".join(f"{s.get('party')} amount={s.get('amount')}" for s in missing_mot),
				"Journal Entry", journal_entry_name,
				_("{0} summary row(s) have no Mode of Transfer and no default is set "
				  "on India Banking Settings. Set <b>Default Mode of Transfer</b> there to auto-fill.").format(
					len(missing_mot)),
			)

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
	"""Generate and populate Payment Order summary child table from references.

	Bulk-friendly MoT policy (only used when this PO is created from a Bank Entry,
	i.e. the bulk-salary flow): pick ONE Mode of Transfer for the whole batch so
	the entire transfer goes out as a single bulk file in that mode.

	Selection order:
	    1. The default configured in India Banking Settings (`default__mode_of_transfer`,
	       double-underscore custom field) if its limit window covers the batch's
	       largest individual amount.
	    2. The smallest non-bank-specific, non-disabled Mode of Transfer whose
	       maximum_limit covers the batch's largest amount (cheapest viable mode).

	If neither resolves we leave each row's mode_of_transfer as whatever
	`get_party_summary` produced (per-row resolution) — `validate_summary` will
	still flag any unresolved rows clearly via the existing pre-save checks.
	"""
	default_mot = _get_default_mode_of_transfer()

	summary_items = get_party_summary(
		references=json.dumps([ref.as_dict() for ref in po.references]),
		company_bank_account=po.company_bank_account,
		summarise_payment_based_on=summarise_by,
		default_mode_of_transfer=default_mot,
	)
	if not summary_items:
		return

	# Bulk-friendly: one MoT for the whole batch, sized to cover the largest row
	batch_max = max((flt(s.get("amount") or 0) for s in summary_items), default=0)
	chosen = _pick_bulk_mot(batch_max, default_mot)

	if chosen:
		for item in summary_items:
			item["mode_of_transfer"] = chosen
		po.default_mode_of_transfer = chosen
	else:
		# Fallback to per-row resolution (legacy behaviour) so save still has
		# something. validate_summary's pre-save check will surface unresolved rows.
		for item in summary_items:
			if item.get("mode_of_transfer"):
				continue
			picked = _pick_mot_by_amount(item.get("amount") or 0) or default_mot
			if picked:
				item["mode_of_transfer"] = picked
		if default_mot:
			po.default_mode_of_transfer = default_mot

	po.set("summary", [])
	for item in summary_items:
		po.append("summary", item)
	po.total = sum(item.get("amount", 0) for item in summary_items)


def _pick_bulk_mot(batch_max, configured_default):
	"""Pick a single Mode of Transfer covering all rows in a bulk batch.

	1. Use `configured_default` if its [min..max] window covers `batch_max`.
	2. Else pick the smallest non-bank-specific, non-disabled MoT whose
	   `maximum_limit >= batch_max`.
	"""
	if configured_default:
		limits = frappe.db.get_value(
			"Mode of Transfer", configured_default,
			["minimum_limit", "maximum_limit", "disabled"],
			as_dict=True,
		)
		if limits and not limits.get("disabled"):
			min_lim = flt(limits.get("minimum_limit"))
			max_lim = flt(limits.get("maximum_limit"))
			if min_lim <= batch_max <= max_lim:
				return configured_default

	return frappe.db.get_value(
		"Mode of Transfer",
		{
			"is_bank_specific": 0,
			"disabled": 0,
			"maximum_limit": [">=", batch_max],
		},
		"name",
		order_by="maximum_limit asc, priority asc",
	)


def _pick_mot_by_amount(amount):
	"""Pick a non-bank-specific Mode of Transfer whose limit window covers `amount`.

	Used as a per-row fallback when the bulk-friendly selection didn't resolve
	(e.g. no MoT covers the batch_max at all).
	"""
	return frappe.db.get_value(
		"Mode of Transfer",
		{
			"minimum_limit": ["<=", amount],
			"maximum_limit": [">", amount],
			"is_bank_specific": 0,
			"disabled": 0,
		},
		"name",
		order_by="priority asc",
	)


def _get_default_mode_of_transfer():
	"""Read the operator-configured fallback MoT from India Banking Settings.

	The custom field uses a double-underscore (`default__mode_of_transfer`) per the
	current admin setup. We tolerate single-underscore as a safety net in case the
	field gets renamed in future.
	"""
	for field in ("default__mode_of_transfer", "default_mode_of_transfer"):
		try:
			val = frappe.db.get_single_value("India Banking Settings", field)
			if val:
				return val
		except Exception:
			continue
	return None


def _get_company_bank_account(bank_entry):
	"""Derive company Bank Account from the JE's credit (bank) account row.

	Two strategies in order, so a Bank Account whose `account` link points to a
	parent/group COA account (instead of the specific leaf) still resolves:
	  1. Direct: Bank Account.account == JE row's account, is_company_account=1
	  2. Fallback: match COA Account.account_number to Bank Account.bank_account_no
	     scoped to the same Company.
	"""
	for row in bank_entry.accounts:
		if not (row.credit > 0 and row.account):
			continue

		# 1. Direct match — Bank Account.account points to the JE's credit account
		ba = frappe.db.get_value(
			"Bank Account",
			{"account": row.account, "is_company_account": 1},
			"name",
		)
		if ba:
			return ba

		# 2. Fallback — match COA Account.account_number to Bank Account.bank_account_no
		coa_acc_no = frappe.db.get_value("Account", row.account, "account_number")
		if coa_acc_no:
			ba = frappe.db.get_value(
				"Bank Account",
				{
					"bank_account_no": coa_acc_no,
					"is_company_account": 1,
					"company": bank_entry.company,
				},
				"name",
			)
			if ba:
				return ba

	return None


def _diagnose_missing_company_bank_account(bank_entry):
	"""Build a diagnostic showing why _get_company_bank_account couldn't resolve.

	Returns a short HTML string describing each credit row's account, what would
	have matched, and what's misconfigured. Used in the user-facing error so the
	operator can fix the data without digging through logs.
	"""
	lines = []
	for row in bank_entry.accounts:
		if not (row.credit > 0 and row.account):
			continue

		coa = frappe.db.get_value(
			"Account", row.account,
			["account_number", "account_type", "is_group", "company"],
			as_dict=True,
		) or {}

		# Show any Bank Accounts whose `account` field links to this row's account
		direct_matches = frappe.get_all(
			"Bank Account",
			filters={"account": row.account},
			fields=["name", "is_company_account", "disabled", "bank_account_no"],
		)
		# Show any Bank Accounts whose bank_account_no equals the COA account_number
		num_matches = []
		if coa.get("account_number"):
			num_matches = frappe.get_all(
				"Bank Account",
				filters={
					"bank_account_no": coa["account_number"],
					"company": bank_entry.company,
				},
				fields=["name", "account", "is_company_account", "disabled"],
			)

		segment = []
		segment.append(f"<b>JE credit row account</b>: <code>{row.account}</code>")
		segment.append(
			f"&nbsp;&nbsp;COA: account_number=<code>{coa.get('account_number') or '(none)'}</code>, "
			f"account_type=<code>{coa.get('account_type') or '(none)'}</code>, "
			f"is_group=<code>{coa.get('is_group')}</code>"
		)

		if direct_matches:
			for d in direct_matches:
				flag = "" if d["is_company_account"] and not d["disabled"] else " &lt;-- is_company_account=0 or disabled"
				segment.append(
					f"&nbsp;&nbsp;Direct match: {d['name']} "
					f"(is_company_account={d['is_company_account']}, disabled={d['disabled']}){flag}"
				)
		else:
			segment.append("&nbsp;&nbsp;Direct match (Bank Account.account = this row): <b>none</b>")

		if num_matches:
			for n in num_matches:
				flag = ""
				if not n["is_company_account"]:
					flag = " &lt;-- is_company_account=0 (set this to 1)"
				elif n["disabled"]:
					flag = " &lt;-- disabled"
				else:
					flag = (
						f" &lt;-- this Bank Account exists with the right bank_account_no but "
						f"its <code>account</code> field points to <code>{n['account']}</code> "
						f"instead of <code>{row.account}</code>. Fix: open Bank Account "
						f"<b>{n['name']}</b> and set its <b>Account</b> field to "
						f"<code>{row.account}</code>."
					)
				segment.append(
					f"&nbsp;&nbsp;By account_number={coa.get('account_number')}: {n['name']} "
					f"(is_company_account={n['is_company_account']}, account={n['account']}){flag}"
				)
		elif coa.get("account_number"):
			segment.append(
				f"&nbsp;&nbsp;By account_number={coa['account_number']}: <b>no Bank Account exists</b> "
				f"with this bank_account_no for company <code>{bank_entry.company}</code>."
			)

		lines.append("<br>".join(segment))

	return "<br><br>".join(lines) if lines else "(no credit rows with an account on this JE)"


def _set_debits_to_rounded_total(bank_entry, payroll_entry_name):
	"""Replace per-employee debit with Salary Slip rounded_total and rebalance bank credit.

	HRMS' make_bank_entry computes per-employee debit by summing earnings minus
	deductions, which silently includes employer-side statutory components
	(e.g. Employer EPF) as part of the employee debit. The actual amount that
	should be wired to the employee bank account is rounded_total on the Salary
	Slip. Our companion Salary JE credits Payroll Payable with Σ rounded_total,
	so the Bank Entry's per-employee debits and the Salary JE's Payroll Payable
	credit reconcile cleanly.
	"""
	employee_rows = [r for r in bank_entry.accounts if r.party_type == "Employee" and r.party]
	if not employee_rows:
		return

	SS = DocType("Salary Slip")
	rows = (
		frappe.qb.from_(SS)
		.select(SS.employee, SS.rounded_total)
		.where(
			(SS.payroll_entry == payroll_entry_name)
			& (SS.docstatus == 1)
			& (SS.employee.isin([r.party for r in employee_rows]))
		)
		.run(as_dict=True)
	)
	rounded_map = {r.employee: flt(r.rounded_total) for r in rows}

	new_debit_total = 0
	for row in employee_rows:
		amt = rounded_map.get(row.party)
		if amt is None:
			new_debit_total += flt(row.debit)
			continue
		row.debit = amt
		row.debit_in_account_currency = amt
		new_debit_total += amt

	for row in bank_entry.accounts:
		if not row.party and flt(row.credit) > 0:
			row.credit = new_debit_total
			row.credit_in_account_currency = new_debit_total
			break


def _delete_existing_accrual_je(payroll_entry_name):
	"""Cancel + delete any HRMS auto-accrual JE for this PE so we can replace it
	with the manager-spec Salary JE. Bank Entry JEs are NOT touched."""
	jes = frappe.db.sql_list("""
		SELECT DISTINCT je.name
		FROM `tabJournal Entry` je
		JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		WHERE jea.reference_type = 'Payroll Entry'
		  AND jea.reference_name = %s
		  AND je.voucher_type != 'Bank Entry'
		  AND je.docstatus != 2
	""", payroll_entry_name)

	for je_name in jes:
		try:
			doc = frappe.get_doc("Journal Entry", je_name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.flags.ignore_links = True
				doc.cancel()
				frappe.db.commit()
			# Re-fetch in case docstatus is now 2
			ds = frappe.db.get_value("Journal Entry", je_name, "docstatus")
			if ds == 1:
				# cancel didn't persist (e.g. validation rolled back) — force
				frappe.db.set_value("Journal Entry", je_name, "docstatus", 2, update_modified=False)
				frappe.db.sql("UPDATE `tabJournal Entry Account` SET docstatus = 2 WHERE parent = %s", je_name)
				frappe.db.commit()
			frappe.delete_doc(
				"Journal Entry", je_name,
				force=True, ignore_permissions=True, delete_permanently=True,
				ignore_missing=True, ignore_on_trash=True,
			)
			frappe.db.commit()
		except Exception:
			# Raw SQL fallback so we don't block the salary flow on cleanup hiccups
			frappe.db.sql("DELETE FROM `tabJournal Entry Account` WHERE parent = %s", je_name)
			frappe.db.sql("DELETE FROM `tabGL Entry` WHERE voucher_no = %s", je_name)
			frappe.db.sql("DELETE FROM `tabJournal Entry` WHERE name = %s", je_name)
			frappe.db.commit()


def _create_salary_je(payroll_entry, submit_now=False):
	"""Build the manager-spec Salary JE for a Payroll Entry.

	Structure:
	    Dr  payment_account                = Σ Salary Slip.gross_pay
	        Cr  <deduction component accounts>  (PT, PF, IT, ...)
	        Cr  payroll_payable_account     = Σ Salary Slip.rounded_total

	HRMS's auto-accrual JE for this PE is cancelled + deleted first to avoid
	double-booking the salary expense.
	"""
	pe_name = payroll_entry.name

	if not payroll_entry.payment_account:
		_log_and_throw(
			f"Salary JE: payment_account missing on {pe_name}",
			f"Payroll Entry {pe_name} has no payment_account set.",
			"Payroll Entry", pe_name,
			_("Cannot build Salary JE — Payroll Entry {0} has no <b>Payment Account</b> set. "
			  "Set it and retry.").format(get_link_to_form("Payroll Entry", pe_name)),
		)
	if not payroll_entry.payroll_payable_account:
		_log_and_throw(
			f"Salary JE: payroll_payable_account missing on {pe_name}",
			f"Payroll Entry {pe_name} has no payroll_payable_account set.",
			"Payroll Entry", pe_name,
			_("Cannot build Salary JE — Payroll Entry {0} has no <b>Payroll Payable Account</b> set.").format(
				get_link_to_form("Payroll Entry", pe_name)),
		)

	# Aggregate from Salary Slips
	slips = frappe.db.sql("""
		SELECT name, gross_pay, rounded_total
		FROM `tabSalary Slip`
		WHERE payroll_entry = %s AND docstatus = 1
	""", pe_name, as_dict=True)
	if not slips:
		_log_and_throw(
			f"Salary JE: no submitted slips for {pe_name}",
			f"Payroll Entry {pe_name} has no submitted Salary Slips.",
			"Payroll Entry", pe_name,
			_("No submitted Salary Slips found for {0} — cannot build Salary JE.").format(
				get_link_to_form("Payroll Entry", pe_name)),
		)

	gross_total = flt(sum(flt(s.gross_pay) for s in slips), 2)
	net_total = flt(sum(flt(s.rounded_total) for s in slips), 2)

	# Per-component deduction totals
	deductions = frappe.db.sql("""
		SELECT sd.salary_component, SUM(sd.amount) AS total
		FROM `tabSalary Detail` sd
		JOIN `tabSalary Slip` ss ON ss.name = sd.parent
		WHERE ss.payroll_entry = %s
		  AND ss.docstatus = 1
		  AND sd.parentfield = 'deductions'
		  AND sd.amount > 0
		GROUP BY sd.salary_component
	""", pe_name, as_dict=True)

	mapped = []
	missing = []
	for d in deductions:
		acc = frappe.db.get_value(
			"Salary Component Account",
			{"parent": d.salary_component, "company": payroll_entry.company},
			"account",
		)
		if acc:
			mapped.append({"component": d.salary_component, "account": acc, "amount": flt(d.total, 2)})
		else:
			missing.append({"component": d.salary_component, "amount": flt(d.total, 2)})

	# Replace HRMS auto-accrual
	_delete_existing_accrual_je(pe_name)

	# Build the JE
	# Note: don't set title here — ERPNext's Journal Entry.validate() overwrites it
	# during insert via get_title(). We set the title via db_set after save below.
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.posting_date = payroll_entry.posting_date or nowdate()
	je.company = payroll_entry.company
	je.user_remark = _("Salary accrual for Payroll Entry {0} ({1} to {2})").format(
		pe_name, payroll_entry.start_date, payroll_entry.end_date,
	)
	je.cheque_no = pe_name
	je.cheque_date = je.posting_date

	# Dr Salary expense (= Gross Pay)
	je.append("accounts", {
		"account": payroll_entry.payment_account,
		"debit_in_account_currency": gross_total,
		"cost_center": payroll_entry.cost_center,
		"reference_type": "Payroll Entry",
		"reference_name": pe_name,
	})

	# Cr each deduction component
	credits_total = 0
	for c in mapped:
		je.append("accounts", {
			"account": c["account"],
			"credit_in_account_currency": c["amount"],
			"cost_center": payroll_entry.cost_center,
			"reference_type": "Payroll Entry",
			"reference_name": pe_name,
		})
		credits_total = credits_total + c["amount"]

	# Cr Payroll Payable (= Net Pay = matches Bank Entry)
	je.append("accounts", {
		"account": payroll_entry.payroll_payable_account,
		"credit_in_account_currency": net_total,
		"cost_center": payroll_entry.cost_center,
		"reference_type": "Payroll Entry",
		"reference_name": pe_name,
	})
	credits_total = flt(credits_total + net_total, 2)
	diff = flt(gross_total - credits_total, 2)

	# Pre-save validation: any deduction component without an account mapping → throw early
	if missing:
		miss_lines = "<br>".join(
			f"<b>{m['component']}</b> — total: {m['amount']} (no account mapped on {payroll_entry.company})"
			for m in missing
		)
		log_msg = (
			f"Payroll Entry: {pe_name}\n"
			f"Gross total (Dr): {gross_total}\n"
			f"Credits total: {credits_total}\n"
			f"Difference: {diff}\n\n"
			f"Missing component accounts:\n"
			+ "\n".join(f"{m['component']}: {m['amount']}" for m in missing)
		)
		_log_and_throw(
			f"Salary JE: deduction components missing accounts ({len(missing)}): {pe_name}",
			log_msg,
			"Payroll Entry", pe_name,
			_("Cannot build Salary JE — the following deduction components have no "
			  "<b>Salary Component Account</b> mapped for company {0}. Map them and retry:<br><br>{1}").format(
				payroll_entry.company, miss_lines),
		)

	# Auto-balance any rounding difference using Company.round_off_account
	if diff:
		round_off_account, round_off_cost_center = frappe.db.get_value(
			"Company", payroll_entry.company,
			["round_off_account", "round_off_cost_center"],
		) or (None, None)

		if not round_off_account:
			_log_and_throw(
				f"Salary JE unbalanced by {diff} (no Round Off account): {pe_name}",
				f"Payroll Entry: {pe_name}\nGross: {gross_total}\nCredits: {credits_total}\nDiff: {diff}\n"
				f"Company {payroll_entry.company} has no round_off_account configured.",
				"Payroll Entry", pe_name,
				_("Salary JE is unbalanced by {0} and Company {1} has no <b>Round Off Account</b> set. "
				  "Configure it under Company → Accounting Defaults and retry.").format(
					diff, payroll_entry.company),
			)

		# diff = gross_total - credits_total
		#   diff > 0 → debits exceed credits → Cr Round Off (diff)
		#   diff < 0 → credits exceed debits → Dr Round Off (|diff|)
		round_row = {
			"account": round_off_account,
			"cost_center": round_off_cost_center or payroll_entry.cost_center,
			"reference_type": "Payroll Entry",
			"reference_name": pe_name,
		}
		if diff > 0:
			round_row["credit_in_account_currency"] = diff
		else:
			round_row["debit_in_account_currency"] = abs(diff)
		je.append("accounts", round_row)

	je.save(ignore_permissions=True)

	# ERPNext's Journal Entry.validate() forcibly sets `title` from get_title()
	# during insert (because is_new() is True), so any title we set on the doc
	# pre-save gets overwritten. Set it post-save via db_set so it persists.
	salary_title = _build_salary_je_title(payroll_entry)
	if salary_title:
		je.db_set("title", salary_title, update_modified=True)
		je.title = salary_title

	# Repoint Salary Slips' journal_entry FK to the new JE (HRMS uses this for traceability)
	frappe.db.sql(
		"UPDATE `tabSalary Slip` SET journal_entry = %s WHERE payroll_entry = %s AND docstatus = 1",
		(je.name, pe_name),
	)
	frappe.db.commit()

	if submit_now:
		je.submit()

	return je


def _build_salary_je_title(payroll_entry):
	"""Build the human-readable title for the Salary JE.

	Format: "Salary - {Month} - {school or company}"
	    - Month: full month name from start_date (e.g. "March")
	    - School: PE.custom_schools if set, else PE.company
	    - "Walnut School at <X>" is shortened to "<X>" for brevity
	"""
	start = payroll_entry.start_date
	month_name = start.strftime("%B") if start else ""

	school = getattr(payroll_entry, "custom_schools", None) or getattr(payroll_entry, "custom_school", None)
	prefix = school or payroll_entry.company

	# Shorten "Walnut School at Shivane" -> "Shivane" for cleaner titles
	marker = "Walnut School at "
	if prefix and prefix.startswith(marker):
		prefix = prefix[len(marker):]

	return f"Salary - {month_name} - {prefix}".strip(" -")
