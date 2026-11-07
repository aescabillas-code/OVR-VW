"""HPE OVR-VW Operations Workspace hosted in Streamlit, persisted in MongoDB, with a REST API.

    pip install -r requirements.txt
    # connection: .streamlit/secrets.toml  ->  MONGO_URI = "mongodb+srv://..."   MONGO_DB = "ovrvw"   (env vars also work)
    streamlit run app.py

The workspace itself (frontend/index.html) is a bidirectional Streamlit component. Everything entered in it is
sent to this process, written to MongoDB, and pushed to every other open session. The REST API (api.py) reads
and writes the same MongoDB collections.
"""
import json
import pathlib

import streamlit as st
import streamlit.components.v1 as components

import api as api_mod
from bridge import Bridge
from store import make_store

st.set_page_config(page_title="HPE OVR-VW Operations Workspace", page_icon="🛰️", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""<style>
#MainMenu, header[data-testid="stHeader"], footer {visibility: hidden; height: 0;}
.block-container, [data-testid="stMainBlockContainer"] {padding: 0 !important; max-width: 100% !important;}
[data-testid="stVerticalBlock"] {gap: 0 !important;}
iframe {border: 0; display: block;}
</style>""", unsafe_allow_html=True)


def _plain(v):
    return {k: _plain(x) for k, x in v.items()} if hasattr(v, "items") else v


def read_secrets():
    """The MongoDB connection comes from .streamlit/secrets.toml (or the Streamlit Cloud secrets box)."""
    try:
        return {k: _plain(v) for k, v in st.secrets.items()}
    except Exception:  # noqa: BLE001  (no secrets file)
        return {}


@st.cache_resource(show_spinner="Connecting to MongoDB…")
def boot():
    store, err = make_store(read_secrets())
    return store, Bridge(store), err, api_mod.start_background(store)


store, bridge, store_err, api_info = boot()

with st.sidebar:
    st.subheader("Storage")
    (st.success if store.kind == "mongodb" else st.error)(store.describe())
    if store_err:
        st.warning(store_err)
    st.caption(f"Documents: {json.dumps(store.counts())}")
    st.subheader("REST API")
    st.markdown(f"[{api_info['url']}/docs]({api_info['url']}/docs)")
    if api_info.get("note"):
        st.caption(api_info["note"])

component = components.declare_component("ovr_vw", path=str(pathlib.Path(__file__).parent / "frontend"))

# --- sync loop: every browser heartbeat (~1.2 s) arrives here as the component value ---------------------------
prev = st.session_state.get("last_msg")
reply = bridge.reply(prev)
value = component(reply=reply, key="ovr_vw", default=None)

if value is not None and (prev is None or value.get("tick") != prev.get("tick") or value.get("cid") != prev.get("cid")):
    bridge.handle(value)                      # write the browser's changes to MongoDB
    st.session_state["last_msg"] = value
    fresh = bridge.reply(value)
    strip = lambda r: {k: v for k, v in r.items() if k != "now"}  # noqa: E731
    if strip(fresh) != strip(reply):          # something new to push (ack, other users' changes, REST writes)
        st.rerun()
