"""Read-only Delta account dashboard for Streamlit Community Cloud."""

import streamlit as st

from dashboard import CONTRACTS, activity_row, delta_request
from delta_strategy import RESOLUTIONS


st.set_page_config(page_title="Delta Strategy Dashboard", layout="wide")
st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] { background: #87ceeb; }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stMainBlockContainer"] { max-width: 66rem; padding-top: 4rem; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-cloud-controls),
    [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-cloud-account),
    .st-key-cloud-controls, .st-key-cloud-account {
        background: #151f2e;
        color: #eef4fa;
        border-radius: 1rem;
    }
    .st-key-cloud-controls, .st-key-cloud-account {
        --text-color: #eef4fa;
        --background-color: #151f2e;
        --secondary-background-color: #1d2e40;
    }
    .st-key-cloud-controls h1, .st-key-cloud-controls h2,
    .st-key-cloud-account h2, .st-key-cloud-controls label, .st-key-cloud-account label {
        color: #eef4fa;
    }
    .st-key-cloud-controls [data-baseweb="select"] > div,
    .st-key-cloud-controls input, .st-key-cloud-account input {
        background: #1d2e40;
        color: #eef4fa;
    }
    .st-key-cloud-controls button, .st-key-cloud-account button {
        background: #155a56;
        color: #fff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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


def select_all_contracts():
    for symbol in CONTRACTS:
        st.session_state[f"contract_{symbol}"] = st.session_state.select_all_contracts


def update_all_contracts():
    st.session_state.select_all_contracts = all(
        st.session_state.get(f"contract_{symbol}", False) for symbol in CONTRACTS
    )


main, sidebar = st.columns([3.4, 1.25], gap="medium")
with sidebar, st.container(border=True, key="cloud-account"):
    st.subheader("Delta connection")
    if "delta_credentials" not in st.session_state:
        st.caption("Not connected")
        with st.form("delta_account", clear_on_submit=True):
            key = st.text_input("Read-only Delta API key", type="password")
            secret = st.text_input("Delta API secret", type="password")
            if st.form_submit_button("Connect"):
                if key.strip() and secret.strip():
                    st.session_state.delta_credentials = (key.strip(), secret.strip())
                    st.rerun()
                else:
                    st.warning("Enter both credentials.")
    else:
        connection_status = st.empty()
        if st.button("Disconnect"):
            del st.session_state.delta_credentials
            st.rerun()
        st.button("Refresh account data")

positions = orders = None
if "delta_credentials" in st.session_state:
    try:
        positions = read_account("/v2/positions/margined")
        orders = read_account("/v2/orders/history?page_size=50")
    except (OSError, ValueError, KeyError) as error:
        connection_status.error(str(error))
    else:
        connection_status.success("Connected to Delta India")

with main, st.container(border=True, key="cloud-controls"):
    st.title("Delta Strategy Dashboard")
    st.caption("Read-only cloud view. Use the local dashboard for live strategy controls.")
    with st.form("timeframe_form", border=False):
        timeframe = st.selectbox("Timeframe", tuple(RESOLUTIONS), index=1)
        if st.form_submit_button("Submit timeframe"):
            st.session_state.selected_timeframe = timeframe
    st.caption(f"Selected timeframe · {st.session_state.get('selected_timeframe', '3 min')}")

    st.subheader("Strategy contracts")
    st.checkbox("Select all", key="select_all_contracts", on_change=select_all_contracts)
    contract_columns = st.columns(3)
    selected = []
    for index, symbol in enumerate(sorted(CONTRACTS)):
        with contract_columns[index % len(contract_columns)]:
            if st.checkbox(symbol, key=f"contract_{symbol}", on_change=update_all_contracts):
                selected.append(symbol)
    lots = st.number_input("Lots per contract", min_value=1, step=1, value=1)
    if st.button("Save contract selection"):
        if selected:
            st.session_state.contract_selection = (selected, lots)
        else:
            st.warning("Select at least one contract.")
    if "contract_selection" in st.session_state:
        saved, saved_lots = st.session_state.contract_selection
        st.caption(f"Saved · {len(saved)} contract(s) · {saved_lots} lot(s) per contract")

    st.subheader("Open positions")
    if positions is None:
        st.caption("Connect to Delta to load open positions.")
    else:
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
            st.caption("No open positions.")

    st.subheader("Recent activity")
    if orders is None:
        st.caption("Connect to Delta to load order history.")
    elif orders:
        st.dataframe([activity_row(order) for order in orders], hide_index=True)
    else:
        st.caption("No recent orders.")
