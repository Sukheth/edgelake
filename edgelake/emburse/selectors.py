"""
All Chrome River CSS/XPath selectors live here.
When the UI changes, this is the only file to update.

These are PLACEHOLDERS — they need to be filled in during the first
guided run by inspecting the live DOM. The uploader will pause and
prompt for confirmation at each step until selectors are verified.
"""

# Login detection — heuristic: if URL contains okta or login form is visible
LOGIN_URL_FRAGMENTS = ("okta", "login", "signin", "sso")

# Dashboard / landed-in indicators (any one means we're logged in)
DASHBOARD_INDICATORS = [
    'text="Expenses"',
    'text="Create"',
    'text="Dashboard"',
]

# Create new expense report
CREATE_REPORT_BUTTON = 'button:has-text("Create")'
NEW_EXPENSE_BUTTON = 'button:has-text("New Expense")'

# Expense form fields (TBD — verify on first run)
FIELD_DATE = 'input[name="transactionDate"]'
FIELD_AMOUNT = 'input[name="amount"]'
FIELD_MERCHANT = 'input[name="merchant"]'
FIELD_CATEGORY = 'input[name="expenseType"]'
FIELD_DESCRIPTION = 'textarea[name="description"]'

# Receipt attachment
ATTACH_RECEIPT_BUTTON = 'button:has-text("Attach Receipt")'
FILE_INPUT = 'input[type="file"]'

# Save as draft (NOT submit)
SAVE_DRAFT_BUTTON = 'button:has-text("Save")'
