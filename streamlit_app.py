"""Read-only Delta account dashboard for Streamlit Community Cloud."""

import streamlit as st

from dashboard import CONTRACTS, activity_row, delta_request
from delta_strategy import RESOLUTIONS


st.set_page_config(page_title="Delta Strategy Dashboard", layout="wide")
st.title("Delta Strategy Dashboard")
st.caption("Read-only cloud dashboard. Run dashboard.py locally for live orders and strategy controls.")

with st.sidebar:
    st.header("Strategy settings")
    st.selectbox("Timeframe", tuple(RESOLUTIONS), index=1)
    st.multiselect("Contracts", sorted(CONTRACTS))
    st.number_input("Lots per contract", min_value=1, step=1, value=1)
    st.caption("Selections here are for reference. The local dashboard controls the live strategy.")

st.subheader("Delta connection")
with st.form("delta_account", clear_on_submit=True):
    key = st.text_input("Read-only Delta API key", type="password")
    secret = st.text_input("Delta API secret", type="password")
    connect = st.form_submit_button("Connect")
if connect:
    st.session_state.pop("delta_credentials", None)
    if key.strip() and secret.strip():
        st.session_state.delta_credentials = (key.strip(), secret.strip())
    else:
        st.warning("Enter both credentials.")
if "delta_credentials" not in st.session_state:
    st.info("Connect with a read-only Delta India key to view positions and recent orders.")
    st.stop()
if st.button("Disconnect"):
    del st.session_state.delta_credentials
    st.rerun()
st.button("Refresh account data")


def read_account(path):
    result = delta_request("GET", path, credentials=st.session_state.delta_credentials)
    if result.get("success") is not True:
        details = result.get("error") or {}
        code = details.get("code", "unknown_error")
        if code == "ip_not_whitelisted_for_api_key":
            address = (details.get("context") or {}).get("client_ip")
            message = "Delta rejected the cloud server's IP for this key."
            if address:
                message += f" Delta reported {address}."
            raise ValueError(message)
        raise ValueError(f"Delta returned {code}.")
    return result["result"]


try:
    positions = read_account("/v2/positions/margined")
    orders = read_account("/v2/orders/history?page_size=50")
except (OSError, ValueError, KeyError) as error:
    st.error(f"Could not load Delta account data. {error}")
    st.stop()

st.success("Connected to Delta India")
st.subheader("Open positions")
position_rows = []
for position in positions:
    size = int(position["size"])
    if size:
        position_rows.append({
            "Contract": position.get("product_symbol") or position.get("product_id"),
            "Side": "Long" if size > 0 else "Short",
            "Lots": abs(size),
            "Entry": position.get("entry_price"),
            "Mark": position.get("mark_price"),
            "Unrealized P&L": position.get("unrealized_pnl"),
        })
if position_rows:
    st.dataframe(position_rows, hide_index=True)
else:
    st.info("No open positions.")

st.subheader("Recent activity")
if orders:
    st.dataframe([activity_row(order) for order in orders], hide_index=True)
else:
    st.info("No recent orders.")
