const MODE_DESCRIPTIONS = {
	"Review Bank Entry":
		"Creates a Bank Entry (Journal Entry) in <b>draft</b> state. " +
		"You can review the entries, make adjustments if needed, and submit it manually to proceed for Payment Order.",
	"Create Payment Order":
		"<span style='color:red'><b>Note:</b></span> The Bank Entry (Journal Entry) will be " +
		"<span style='color:red'><b>automatically submitted</b></span> " +
		"and a Payment Order will be created in draft state, ready for final review and payment initiation.",
};

frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		// Add button after HRMS clears custom buttons and calls add_context_buttons
		// Using setTimeout to ensure HRMS's async has_bank_entries callback runs first
		if (frm.doc.docstatus !== 1) return;
		if (!frm.doc.salary_slips_submitted && !(frm.doc.__onload && frm.doc.__onload.submitted_ss)) return;

		frm.call("has_bank_entries").then(({ message }) => {
			if (message && !message.has_bank_entries) {
				frm.add_custom_button(__("Process Salary Payment"), () => {
					_show_process_salary_dialog(frm);
				});
			}
		});
	},
});

function _show_process_salary_dialog(frm) {
	if (!frm.doc.payment_account) {
		frappe.msgprint(__("Please set the Payment Account before processing salary payment."));
		frm.scroll_to_field("payment_account");
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __("Process Salary Payment"),
		fields: [
			{
				label: __("Processing Mode"),
				fieldname: "mode",
				fieldtype: "Select",
				options: "Review Bank Entry\nCreate Payment Order",
				default: "Review Bank Entry",
				reqd: 1,
				onchange() {
					d.fields_dict.mode_description.$wrapper.html(
						MODE_DESCRIPTIONS[d.get_value("mode")] || ""
					);
				},
			},
			{
				fieldname: "mode_description",
				fieldtype: "HTML",
				options: MODE_DESCRIPTIONS["Review Bank Entry"],
			},
		],
		primary_action_label: __("Proceed"),
		primary_action({ mode }) {
			d.hide();
			const is_direct = mode === "Create Payment Order";

			frappe.call({
				method: "india_banking.overrides.payroll_entry.process_salary_payment",
				args: { payroll_entry_name: frm.doc.name, mode: is_direct ? "direct" : "review" },
				freeze: true,
				freeze_message: is_direct
					? __("Submitting Bank Entry and creating Payment Order...")
					: __("Creating Bank Entry in draft..."),
				callback({ message: r }) {
					if (!r) return;
					const [dt, name] = r.payment_order
						? ["Payment Order", r.payment_order]
						: ["Journal Entry", r.journal_entry];
					frappe.set_route("Form", dt, name);
				},
			});
		},
	});
	d.show();
}
