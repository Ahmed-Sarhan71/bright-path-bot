import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

_worksheet = None


def _get_worksheet():
    """Connect to the Google Sheet once and reuse the connection."""
    global _worksheet
    if _worksheet is None:
        # On Railway there is no credentials file, so the JSON is passed as an env variable.
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
        else:
            gc = gspread.service_account(filename=os.environ["GOOGLE_CREDENTIALS_FILE"])
        _worksheet = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"]).sheet1
    return _worksheet


def save_enquiry(name: str, phone: str, course: str) -> None:
    """Add one enquiry as a new row. Columns: Timestamp, Name, Phone, Course, Registered."""
    timestamp = datetime.now(ZoneInfo("Asia/Dubai")).strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, name, phone, course, "No"]
    # RAW keeps the phone number as text, so "+971..." is not turned into a formula.
    _get_worksheet().append_row(row, value_input_option="RAW")