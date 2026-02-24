frappe.ui.form.on("Journal Entry", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.voucher_type !== "Bank Entry") return;

		frm.add_custom_button(__("Create Payment Order"), () => {
			frappe.call({
				method: "india_banking.overrides.payroll_entry.create_payment_order_from_bank_entry",
				args: { journal_entry_name: frm.doc.name },
				freeze: true,
				freeze_message: __("Creating Payment Order..."),
				callback({ message }) {
					if (message?.payment_order) {
						frappe.set_route("Form", "Payment Order", message.payment_order);
					}
				},
			});
		});
	},
});
