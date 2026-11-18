"""Delta account dashboard and authenticated live strategy controls for Streamlit."""

from ipaddress import IPv4Address, ip_address
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from delta_client import CONTRACTS, DELTA_OPENER, activity_row, dated_futures, delta_request, public_delta
from delta_strategy import RESOLUTIONS
from live_strategy import LiveStrategy


st.set_page_config(page_title="Delta Strategy Dashboard", layout="wide")
st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] { background: #87ceeb; }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stMainBlockContainer"] {
        max-width: 62rem;
        padding: 3rem clamp(1rem, 4vw, 2rem) 2rem;
    }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-cloud-controls),
    [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-cloud-account),
    .st-key-cloud-controls, .st-key-cloud-account {
        background: #151f2e;
        color: #eef4fa;
        border-radius: 1rem;
    }
    [data-testid="stVerticalBlockBorderWrapper"]:has(> .st-key-cloud-controls),
    [data-testid="stVerticalBlockBorderWrapper"]:has(> .st-key-cloud-account) {
        border: 1px solid #2d4055;
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
    .st-key-cloud-controls button:disabled { opacity: .55; }
    .st-key-cloud-controls [data-testid="stCaptionContainer"],
    .st-key-cloud-account [data-testid="stCaptionContainer"] { color: #d0deea; }
    .st-key-cloud-account [data-testid="stExpander"] { color: #eef4fa; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60, show_spinner=False)
def public_ipv4():
    try:
        with DELTA_OPENER.open("https://api.ipify.org", timeout=2) as response:
            return str(IPv4Address(response.read(128).decode().strip()))
    except (OSError, ValueError):
        return "Unavailable"


class DeltaConnectionError(ValueError):
    def __init__(self, message, address=None):
        super().__init__(message)
        self.address = address


def read_account(path, credentials, *, timeout=10):
    result = delta_request("GET", path, credentials=credentials, timeout=timeout)
    if result.get("success") is not True:
        details = result.get("error") or {}
        code = details.get("code", "unknown_error")
        if code == "ip_not_whitelisted_for_api_key":
            address = (details.get("context") or {}).get("client_ip")
            try:
                address = str(ip_address(address))
            except ValueError:
                address = None
            raise DeltaConnectionError("Delta rejected this server IP for the configured API key.", address)
        raise DeltaConnectionError(f"Delta returned {code}.")
    return result["result"]


def connection_check(credentials):
    try:
        read_account("/v2/positions/margined", credentials, timeout=1)
    except DeltaConnectionError as error:
        return False, str(error), error.address
    except (OSError, ValueError, KeyError):
        return False, "Could not reach or read the Delta API.", None
    return True, None, None


@st.fragment(run_every="1s")
def show_connection(credentials, refresh_controls):
    connected, problem, address = connection_check(credentials)
    previous = st.session_state.get("delta_connected")
    st.session_state.delta_connected = connected
    if connected:
        st.success("Connected to Delta India")
    else:
        st.error(f"Disconnected. {problem}")
        if address:
            st.code(address)
            st.caption("Allowlist the IP reported by Delta for this API key.")
    if refresh_controls and previous is not None and previous != connected:
        st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def available_contracts():
    try:
        return sorted(CONTRACTS | set(dated_futures()))
    except (OSError, ValueError, TypeError):
        return sorted(CONTRACTS)


def select_all_contracts(symbols):
    for symbol in symbols:
        st.session_state[f"contract_{symbol}"] = st.session_state.select_all_contracts


def update_all_contracts(symbols):
    st.session_state.select_all_contracts = all(
        st.session_state.get(f"contract_{symbol}", False) for symbol in symbols
    )


def live_secrets():
    try:
        key = st.secrets.get("DELTA_API_KEY")
        secret = st.secrets.get("DELTA_API_SECRET")
        access = st.secrets.get("DASHBOARD_ACCESS_CODE")
    except StreamlitSecretNotFoundError:
        return None
    if all(isinstance(value, str) and value.strip() for value in (key, secret)):
        return (key.strip(), secret.strip()), access if isinstance(access, str) and access.strip() else None
    return None


@st.cache_resource(show_spinner=False)
def cloud_engine(account_id, _credentials):
    def signed_request(method, path, order=None):
        return delta_request(method, path, order, credentials=_credentials)

    journal = Path(__file__).with_name(f".delta-cloud-{account_id}.json")
    engine = LiveStrategy(signed_request, public_delta, journal)
    engine.launch()
    return engine


configured_account = live_secrets()
configured_live = bool(configured_account and configured_account[1])
engine = None
credentials = st.session_state.get("delta_credentials")
if configured_live:
    live_credentials, access_code = configured_account
    account_id = sha256(live_credentials[0].encode()).hexdigest()[:20]
    access_digest = sha256(access_code.encode()).hexdigest()
    engine = cloud_engine(account_id, live_credentials)
    authorized = st.session_state.get("live_account") == (account_id, access_digest)
    credentials = live_credentials if authorized else None
else:
    authorized = False

main, sidebar = st.columns([3.4, 1], gap="small")
with sidebar, st.container(border=True, key="cloud-account"):
    st.subheader("Public IPv4 address")
    st.write(public_ipv4())
    st.subheader("Delta connection")
    connection_credentials = credentials or (configured_account[0] if configured_account else None)
    if connection_credentials:
        show_connection(connection_credentials, authorized)
    else:
        st.session_state.pop("delta_connected", None)
        st.caption("Disconnected")
    if configured_live and not authorized:
        st.caption("Live controls locked")
        with st.form("unlock_live_controls", clear_on_submit=True, border=False):
            entered_code = st.text_input("Dashboard access code", type="password")
            if st.form_submit_button("Unlock"):
                if compare_digest(sha256(entered_code.encode()).hexdigest(), access_digest):
                    st.session_state.live_account = (account_id, access_digest)
                    st.rerun()
                st.error("Incorrect dashboard access code.")
    elif credentials is None and configured_account:
        st.caption("Add DASHBOARD_ACCESS_CODE in Streamlit Secrets to unlock live controls.")
    elif credentials is None:
        with st.expander("Connect read-only account"), st.form("delta_account", clear_on_submit=True, border=False):
            key = st.text_input("Read-only Delta API key", type="password")
            secret = st.text_input("Delta API secret", type="password")
            if st.form_submit_button("Connect"):
                if key.strip() and secret.strip():
                    st.session_state.delta_credentials = (key.strip(), secret.strip())
                    st.rerun()
                else:
                    st.warning("Enter both credentials.")
    else:
        if st.button("Disconnect"):
            st.session_state.pop("delta_credentials", None)
            st.session_state.pop("live_account", None)
            st.rerun()
        st.button("Refresh account data")

positions = orders = None
connected = st.session_state.get("delta_connected", False)
if credentials and connected:
    try:
        positions = read_account("/v2/positions/margined", credentials)
    except (OSError, ValueError, KeyError):
        pass
    try:
        orders = read_account("/v2/orders/history?page_size=50", credentials)
    except (OSError, ValueError, KeyError):
        pass

with main, st.container(border=True, key="cloud-controls"):
    st.title("Delta Strategy Dashboard")
    with st.form("timeframe_form", border=False):
        interval_col, submit_col, _ = st.columns([1, 1.7, 2], vertical_alignment="bottom")
        with interval_col:
            timeframe = st.selectbox("Timeframe", tuple(RESOLUTIONS), index=1)
        with submit_col:
            apply_timeframe = st.form_submit_button("Submit timeframe")
        if apply_timeframe:
            st.session_state.selected_timeframe = timeframe
    st.caption(f"Selected timeframe · {st.session_state.get('selected_timeframe', '3 min')}")

    st.subheader("Strategy controls")
    strategy_state = engine.status() if authorized else None
    running = bool(strategy_state and strategy_state["running"])
    start_col, stop_col, lots_col = st.columns([1, 1, 2], vertical_alignment="bottom")
    with start_col:
        start_clicked = st.button("Start strategy", disabled=not authorized or not connected or running)
    with stop_col:
        stop_clicked = st.button("Stop strategy", disabled=not running)
    with lots_col:
        lots = st.number_input("Lots per contract", min_value=1, step=1, value=1)
        save_selection = st.button("Save contract selection")
    if stop_clicked and authorized:
        engine.stop()
        st.session_state.pop("confirm_live_start", None)
        st.rerun()
    if strategy_state:
        st.caption(f"{'Running live' if running else 'Entries stopped'} · {strategy_state['positions']} tracked position(s)")
        for symbol, error in strategy_state["errors"].items():
            st.error(f"{symbol}: {error}")
    elif configured_live:
        st.caption("Unlock the Delta panel to use live controls.")
    else:
        st.caption("Live controls require Delta credentials and an access code in Streamlit Secrets.")

    st.subheader("Strategy contracts")
    symbols = available_contracts()
    st.checkbox("Select all", key="select_all_contracts", on_change=select_all_contracts, args=(symbols,))
    contract_columns = st.columns(5)
    selected = []
    for index, symbol in enumerate(symbols):
        with contract_columns[index % len(contract_columns)]:
            if st.checkbox(symbol, key=f"contract_{symbol}", on_change=update_all_contracts, args=(symbols,)):
                selected.append(symbol)
    if save_selection:
        if selected:
            st.session_state.contract_selection = (selected, lots)
        else:
            st.warning("Select at least one contract.")
    if "contract_selection" in st.session_state:
        saved, saved_lots = st.session_state.contract_selection
        st.caption(f"Saved · {len(saved)} contract(s) · {saved_lots} lot(s) per contract")
    if start_clicked and authorized and connected:
        if selected:
            st.session_state.confirm_live_start = (
                selected, st.session_state.get("selected_timeframe", "3 min"), lots,
            )
        else:
            st.warning("Select at least one contract.")
    if authorized and connected and not running and "confirm_live_start" in st.session_state:
        contracts, interval, order_lots = st.session_state.confirm_live_start
        st.warning(f"Enable real Delta orders for {', '.join(contracts)} · {interval} · {order_lots} lot(s) per contract.")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("Confirm live start"):
            try:
                ready, problem, _ = connection_check(credentials)
                if not ready:
                    raise ValueError(problem)
                engine.start(contracts, interval, order_lots)
            except (OSError, ValueError, KeyError, ArithmeticError) as error:
                st.error(str(error))
            else:
                del st.session_state.confirm_live_start
                st.rerun()
        if cancel_col.button("Cancel"):
            del st.session_state.confirm_live_start
            st.rerun()

    st.subheader("Open positions")
    if positions is None and credentials and connected:
        st.caption("Delta positions are unavailable.")
    elif positions is None:
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
    if orders is None and credentials and connected:
        st.caption("Delta order history is unavailable.")
    elif orders is None:
        st.caption("Connect to Delta to load order history.")
    elif orders:
        st.dataframe([activity_row(order) for order in orders], hide_index=True)
    else:
        st.caption("No recent orders.")
