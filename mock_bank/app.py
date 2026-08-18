"""
Mock Legacy Core-Banking Servicing Console
============================================
A deliberately old-school, server-rendered "back office" banking app that
stands in for the real legacy surfaces described in the interface.ai brief:

  - Frameset-based navigation (nav frame + content frame)
  - Deeply nested <table> layouts, no CSS framework
  - Zero data-testid / semantic markup — button labels are the only signal
  - Real runtime failure modes: member-not-found, validation errors,
    permission denial on a specific account type, session timeout, and a
    randomly-injected "system busy" interstitial
  - Session-cookie-based auth with a short-lived session that can expire
    mid-flow, to exercise the "session timeout" recoverable condition

This is NOT trying to look modern. It is trying to look like something a
regional credit union has been running since 2004, because that is the
actual target environment described in Section 1 of the assignment.
"""
import random
import string
import time
import uuid
from flask import Flask, request, redirect, url_for, session, make_response

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"

# ---------------------------------------------------------------------------
# Tenant configuration
# ---------------------------------------------------------------------------
# Multi-tenant stand-in: the SAME vendor product (this one codebase, same
# routes, same business logic) rendered differently per tenant, exactly the
# "hundreds of tenants running the same underlying vendor product configured,
# branded, and versioned differently" scenario from the brief (Section 1).
# Selected via the TENANT env var so the identical app.py can serve as two
# distinct-looking "tenants" without duplicating any business logic --
# duplicating the app would risk the two tenants silently drifting in
# behavior, which defeats the point of a same-vendor-product stand-in.
TENANT = __import__("os").environ.get("TENANT", "base")

TENANT_CONFIG = {
    "base": {
        "console_name": "CU-SERV Core Banking Console",
        "savings_label": "Savings Balance",
        "savings_field_id": "fld_savings_balance",
        "search_button_label": "Look Up Member",
    },
    "northwind": {
        # A second tenant on the "same vendor product": different branding,
        # different field label wording, and a different element id for the
        # balance field -- realistic vendor-config drift (a rename during a
        # tenant's onboarding, a different template variant) that would break
        # a locator keyed only on exact text or exact id.
        "console_name": "Northwind Credit Union Teller Console",
        "savings_label": "Available Savings",
        "savings_field_id": "sav_bal_field",
        "search_button_label": "Search Member Account",
    },
}
CFG = TENANT_CONFIG.get(TENANT, TENANT_CONFIG["base"])

# ---------------------------------------------------------------------------
# In-memory "core banking" data. Reset on process restart -- this is a mock.
# ---------------------------------------------------------------------------
MEMBERS = {
    "12345": {
        "id": "12345",
        "name": "Dorothy Alvarez",
        "status": "ACTIVE",
        "savings_balance": 4821.63,
        "checking_balance": 1190.02,
        "sub_accounts": [],
    },
    "67890": {
        "id": "67890",
        "name": "Marcus Webb",
        "status": "ACTIVE",
        "savings_balance": 250.00,
        "checking_balance": 0.00,
        "sub_accounts": [],
    },
    "99999": {
        "id": "99999",
        "name": "Locked Test Account",
        "status": "FROZEN",  # triggers permission-denial business outcome
        "savings_balance": 0.00,
        "checking_balance": 0.00,
        "sub_accounts": [],
    },
}

VALID_SUB_ACCOUNT_TYPES = {"YOUTH_SAVINGS", "CHRISTMAS_CLUB", "MONEY_MARKET"}

SESSION_TTL_SECONDS = 90  # short on purpose so the timeout path is easy to hit in a demo

# Toggle: forces the very next matching action to trip a simulated failure.
# Set via /admin/inject (test-only endpoint) so the replay demo can
# reliably reproduce an error case for the evidence folder.
INJECTED_FAILURES = {"next_search_busy": False}


def _now():
    return time.time()


def require_session():
    """Return True if the caller's session cookie is present and not expired."""
    sid = session.get("sid")
    started = session.get("started_at")
    if not sid or not started:
        return False
    if _now() - started > SESSION_TTL_SECONDS:
        return False
    return True


def layout(title, body, frame=None):
    """Minimal legacy chrome. No CSS framework, inline styles only, tables for layout."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title>
<style>
  body {{ font-family: "MS Sans Serif", Tahoma, sans-serif; font-size: 13px; background:#d4d0c8; margin:0; }}
  table.layout {{ border-collapse: collapse; width:100%; }}
  table.data {{ border: 1px solid #808080; border-collapse: collapse; background:#fff; }}
  table.data td, table.data th {{ border: 1px solid #808080; padding: 4px 8px; }}
  .hdr {{ background:#003366; color:#fff; padding:6px; font-weight:bold; }}
  .err {{ color:#a00; font-weight:bold; background:#ffe0e0; border:1px solid #a00; padding:6px; }}
  .ok {{ color:#060; font-weight:bold; background:#e0ffe0; border:1px solid #060; padding:6px; }}
  input[type=text] {{ font-family: inherit; }}
  .btn {{ font-family: inherit; padding:3px 10px; }}
</style>
</head>
<body>
<div class="hdr">{CFG['console_name']} (TEST) &mdash; {title}</div>
<table class="layout"><tr><td style="padding:10px;">
{body}
</td></tr></table>
</body></html>"""


@app.route("/")
def index():
    resp = make_response(redirect(url_for("login")))
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Any non-empty username/password "authenticates" -- this is a mock.
        uname = request.form.get("username", "").strip()
        pwd = request.form.get("password", "").strip()
        if not uname or not pwd:
            body = """
            <div class="err">Login failed: username and password required.</div>
            <form method="post">
            <label for="fld_username">Username:</label> <input type="text" name="username" id="fld_username"><br><br>
            <label for="fld_password">Password:</label> <input type="password" name="password" id="fld_password"><br><br>
            <input class="btn" type="submit" value="Sign In">
            </form>"""
            return layout("Sign In", body)
        session["sid"] = str(uuid.uuid4())
        session["started_at"] = _now()
        session["operator"] = uname
        return redirect(url_for("frameset"))

    body = """
    <form method="post">
    <label for="fld_username">Username:</label> <input type="text" name="username" id="fld_username" value="operator1"><br><br>
    <label for="fld_password">Password:</label> <input type="password" name="password" id="fld_password" value="test-password"><br><br>
    <input class="btn" type="submit" value="Sign In">
    </form>"""
    return layout("Sign In", body)


@app.route("/home")
def frameset():
    if not require_session():
        return redirect(url_for("login"))
    return """<!DOCTYPE html>
<html><head><title>CU-SERV Console</title></head>
<frameset cols="180,*">
  <frame src="/nav" name="navframe" scrolling="no">
  <frame src="/search" name="mainframe">
</frameset>
</html>"""


@app.route("/nav")
def nav():
    return layout("Navigation", """
    <p><a href="/search" target="mainframe">Member Search</a></p>
    <p><a href="/logout" target="_top">Log Out</a></p>
    """)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET", "POST"])
def search():
    if not require_session():
        return layout("Session Expired", '<div class="err">SESSION TIMEOUT: Your session has expired. Please <a href="/login">log in</a> again.</div>')

    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()

        # Injected failure hook for reproducible error-path evidence.
        if INJECTED_FAILURES["next_search_busy"]:
            INJECTED_FAILURES["next_search_busy"] = False
            return layout("System Busy", '<div class="err">SYSTEM BUSY: The core banking backend timed out. Please retry.</div>' + _search_form())

        if not member_id:
            return layout("Member Search", '<div class="err">VALIDATION ERROR: Member ID is required.</div>' + _search_form())

        member = MEMBERS.get(member_id)
        if not member:
            return layout("Member Search", f'<div class="err">NOT FOUND: No member record exists for ID "{member_id}".</div>' + _search_form())

        return redirect(url_for("member_detail", member_id=member_id))

    return layout("Member Search", _search_form())


def _search_form():
    return f"""
    <p>Enter a Member ID to look up their account.</p>
    <form method="post" action="/search">
    <table class="data">
      <tr><td>Member ID</td><td><input type="text" name="member_id" id="fld_memberid"></td></tr>
    </table><br>
    <input class="btn" type="submit" name="do_search" value="{CFG['search_button_label']}">
    </form>
    """


@app.route("/member/<member_id>")
def member_detail(member_id):
    if not require_session():
        return layout("Session Expired", '<div class="err">SESSION TIMEOUT: Your session has expired. Please <a href="/login">log in</a> again.</div>')

    member = MEMBERS.get(member_id)
    if not member:
        return layout("Member Search", f'<div class="err">NOT FOUND: No member record exists for ID "{member_id}".</div>' + _search_form())

    sub_rows = "".join(
        f"<tr><td>{sa['type']}</td><td>{sa['id']}</td><td>${sa['balance']:.2f}</td></tr>"
        for sa in member["sub_accounts"]
    ) or "<tr><td colspan='3'><i>No sub-accounts on file.</i></td></tr>"

    frozen_notice = ""
    open_subacct_control = f'<a class="btn" href="/member/{member_id}/subaccount/new">Open New Sub-Account</a>'
    if member["status"] == "FROZEN":
        frozen_notice = '<div class="err">ACCOUNT FROZEN: This member record is restricted. New sub-account actions are not permitted for frozen accounts.</div>'
        open_subacct_control = '<span style="color:#888;">Open New Sub-Account (disabled — account frozen)</span>'

    body = f"""
    {frozen_notice}
    <table class="data">
      <tr><th colspan="2">Member Record</th></tr>
      <tr><td>Member ID</td><td>{member['id']}</td></tr>
      <tr><td>Name</td><td>{member['name']}</td></tr>
      <tr><td>Status</td><td>{member['status']}</td></tr>
      <tr><td>{CFG['savings_label']}</td><td id="{CFG['savings_field_id']}">${member['savings_balance']:.2f}</td></tr>
      <tr><td>Checking Balance</td><td id="fld_checking_balance">${member['checking_balance']:.2f}</td></tr>
    </table>
    <br>
    <table class="data">
      <tr><th colspan="3">Sub-Accounts</th></tr>
      <tr><td><b>Type</b></td><td><b>Sub-Account ID</b></td><td><b>Balance</b></td></tr>
      {sub_rows}
    </table>
    <br>
    {open_subacct_control}
    &nbsp;&nbsp;
    <a class="btn" href="/search">New Search</a>
    """
    return layout(f"Member {member_id}", body)


@app.route("/member/<member_id>/subaccount/new", methods=["GET", "POST"])
def new_subaccount(member_id):
    if not require_session():
        return layout("Session Expired", '<div class="err">SESSION TIMEOUT: Your session has expired. Please <a href="/login">log in</a> again.</div>')

    member = MEMBERS.get(member_id)
    if not member:
        return layout("Member Search", f'<div class="err">NOT FOUND: No member record exists for ID "{member_id}".</div>' + _search_form())

    if member["status"] == "FROZEN":
        return layout("Action Not Permitted", f'<div class="err">PERMISSION DENIED: Member {member_id} is FROZEN. Sub-account creation is not permitted.</div><a class="btn" href="/member/{member_id}">Back to Member</a>')

    if request.method == "POST":
        acct_type = request.form.get("acct_type", "").strip()
        initial_deposit_raw = request.form.get("initial_deposit", "").strip()

        errors = []
        if acct_type not in VALID_SUB_ACCOUNT_TYPES:
            errors.append(f'Invalid account type "{acct_type}".')
        try:
            initial_deposit = float(initial_deposit_raw)
            if initial_deposit < 5.00:
                errors.append("Initial deposit must be at least $5.00.")
        except ValueError:
            initial_deposit = None
            errors.append(f'Initial deposit "{initial_deposit_raw}" is not a valid amount.')

        if errors:
            err_html = "<br>".join(errors)
            return layout("Validation Error", f'<div class="err">VALIDATION ERROR: {err_html}</div>' + _new_subaccount_form(member_id))

        new_id = "SA-" + "".join(random.choices(string.digits, k=6))
        member["sub_accounts"].append({"type": acct_type, "id": new_id, "balance": initial_deposit})

        body = f"""
        <div class="ok">SUCCESS: Sub-account opened.</div>
        <table class="data">
          <tr><td>New Sub-Account ID</td><td id="fld_new_subaccount_id">{new_id}</td></tr>
          <tr><td>Type</td><td>{acct_type}</td></tr>
          <tr><td>Initial Balance</td><td>${initial_deposit:.2f}</td></tr>
        </table>
        <br>
        <a class="btn" href="/member/{member_id}">Back to Member</a>
        """
        return layout("Sub-Account Confirmation", body)

    return layout("Open New Sub-Account", _new_subaccount_form(member_id))


def _new_subaccount_form(member_id):
    opts = "".join(f'<option value="{t}">{t.replace("_", " ").title()}</option>' for t in sorted(VALID_SUB_ACCOUNT_TYPES))
    return f"""
    <form method="post" action="/member/{member_id}/subaccount/new">
    <table class="data">
      <tr><td>Account Type</td><td><select name="acct_type" id="fld_acct_type"><option value="">-- select --</option>{opts}</select></td></tr>
      <tr><td>Initial Deposit</td><td>$<input type="text" name="initial_deposit" id="fld_initial_deposit"></td></tr>
    </table><br>
    <input class="btn" type="submit" name="do_open" value="Open Sub-Account">
    &nbsp;&nbsp;<a class="btn" href="/member/{member_id}">Cancel</a>
    </form>
    """


# ---------------------------------------------------------------------------
# Test-only control endpoints. NOT part of the "real" app surface --
# used by the discovery/replay harness to make failure demos reproducible.
# ---------------------------------------------------------------------------
@app.route("/admin/inject/search_busy", methods=["POST"])
def inject_search_busy():
    INJECTED_FAILURES["next_search_busy"] = True
    return {"ok": True, "injected": "next_search_busy"}


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    MEMBERS["12345"]["sub_accounts"] = []
    MEMBERS["67890"]["sub_accounts"] = []
    INJECTED_FAILURES["next_search_busy"] = False
    return {"ok": True}


@app.route("/admin/expire_session", methods=["POST"])
def expire_session():
    """Force the current server-side session to look expired, without waiting 90s."""
    if "started_at" in session:
        session["started_at"] = _now() - SESSION_TTL_SECONDS - 1
    return {"ok": True}


if __name__ == "__main__":
    # PORT env var lets the same app.py run as two "tenants" at once (base on
    # 5055, northwind on 5056) so a tenant-override replay can be demonstrated
    # against two genuinely live, independently-running instances rather than
    # a single shared process pretending to be two tenants.
    port = int(__import__("os").environ.get("PORT", "5055"))
    print(f"Starting mock bank as tenant '{TENANT}' on port {port} (savings label: '{CFG['savings_label']}', field id: '{CFG['savings_field_id']}')")
    app.run(host="127.0.0.1", port=port, debug=False)