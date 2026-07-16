"""
SCOUT — Data Review Inbox (Phase S-3b dashboard; seeds Phase G.3).

A browser UI for non-technical team members (on the office network) to resolve
staged data-stewardship reviews: field changes, possible duplicates, anomalies.
Nothing in the database changes except when a human clicks Approve/Reject here.

Run (on the Mac mini, served to the office LAN):
    streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501

Talks to the local FastAPI backend (default http://localhost:8000).
"""
import os
import requests
import streamlit as st

API_BASE = os.environ.get("SCOUT_API_BASE", "http://localhost:8000")

RISK_MARK = {"high": "🔴", "low": "🟡", "anomaly": "⚠️", "none": "⚪"}
TYPE_LABEL = {
    "field_update": "Field change",
    "possible_duplicate": "Possible duplicate",
    "anomaly": "Anomaly",
}

st.set_page_config(page_title="SCOUT — Review Inbox", page_icon="🔎", layout="wide")


# ── API helpers ───────────────────────────────────────────────────────────────

def api_get(path, **params):
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Cannot reach the backend at {API_BASE}. Is the API running?\n\n{exc}")
        return None


def api_post(path):
    try:
        r = requests.post(f"{API_BASE}{path}", timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Action failed: {exc}")
        return None


# ── Sidebar filters ───────────────────────────────────────────────────────────

st.sidebar.title("🔎 SCOUT")
st.sidebar.caption("Data Review Inbox")

status = st.sidebar.selectbox("Status", ["pending", "approved", "rejected"], index=0)
type_filter = st.sidebar.selectbox(
    "Type", ["(all)", "field_update", "possible_duplicate", "anomaly"], index=0
)
risk_filter = st.sidebar.selectbox("Risk", ["(all)", "high", "low", "anomaly"], index=0)
if st.sidebar.button("↻ Refresh"):
    st.rerun()

params = {"status": status}
if type_filter != "(all)":
    params["review_type"] = type_filter
if risk_filter != "(all)":
    params["risk_level"] = risk_filter

data = api_get("/reviews", **params)
if data is None:
    st.stop()

reviews = data.get("reviews", [])


# ── Header ────────────────────────────────────────────────────────────────────

st.title("Data Review Inbox")
st.caption(
    "The pipeline never changes existing startup data on its own. Every change and "
    "every possible duplicate waits here for a person to approve or reject."
)

counts = {"high": 0, "low": 0, "anomaly": 0}
for rv in reviews:
    counts[rv.get("risk_level", "low")] = counts.get(rv.get("risk_level", "low"), 0) + 1
c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending items", len(reviews))
c2.metric("🔴 Conflicts", counts.get("high", 0))
c3.metric("🟡 New info", counts.get("low", 0))
c4.metric("⚠️ Anomalies", counts.get("anomaly", 0))

if not reviews:
    st.success("Nothing to review. 🎉")
    st.stop()


# ── Pick a review ─────────────────────────────────────────────────────────────

def _label(rv):
    mark = RISK_MARK.get(rv.get("risk_level"), "⚪")
    t = TYPE_LABEL.get(rv.get("review_type"), rv.get("review_type"))
    if rv["review_type"] == "field_update":
        fields = ", ".join(rv.get("changed_fields") or []) or "—"
        return f"{mark} {t}: {rv['master_name']} ({fields})"
    return f"{mark} {t}: {rv['incoming_name']} ~ {rv['master_name']}"

idx = st.selectbox(
    "Select an item to review",
    range(len(reviews)),
    format_func=lambda i: _label(reviews[i]),
)
selected = reviews[idx]
detail = api_get(f"/reviews/{selected['id']}")
if detail is None:
    st.stop()

st.divider()

mark = RISK_MARK.get(detail.get("risk_level"), "⚪")
st.subheader(f"{mark} {TYPE_LABEL.get(detail['review_type'], detail['review_type'])}")
meta = st.columns(3)
meta[0].write(f"**Source:** {detail.get('source') or '—'}")
meta[1].write(f"**Confidence:** {detail.get('confidence')}")
meta[2].write(f"**Flagged:** {str(detail.get('created_at'))[:19].replace('T', ' ')}")

if detail.get("llm_explanation"):
    st.info(f"**AI explanation (not a decision):** {detail['llm_explanation']}")


# ── Body: field_update vs duplicate/anomaly ───────────────────────────────────

if detail["review_type"] == "field_update":
    st.markdown("#### Proposed changes")
    rows = []
    for field, ch in (detail.get("proposed_changes") or {}).items():
        rows.append({
            "Field": field,
            "Current value": ch.get("old"),
            "Proposed new value": ch.get("new"),
            "From source": ch.get("incoming_source"),
        })
    st.table(rows)
else:
    st.markdown("#### Are these the same company?")
    left, right = st.columns(2)
    master = detail.get("master") or {}
    incoming = detail.get("incoming") or {}
    fields = ["name", "description", "website", "city", "country",
              "funding_stage", "founded_year", "industry"]
    left.markdown("**Existing record**")
    left.table([{"Field": f, "Value": master.get(f)} for f in fields])
    right.markdown("**Incoming record**")
    right.table([{"Field": f, "Value": incoming.get(f if f != "description" else "description")} for f in fields])

with st.expander("Match evidence (per-signal scorecard)"):
    st.json(detail.get("evidence") or {})


# ── Actions ───────────────────────────────────────────────────────────────────

if detail.get("status") == "pending":
    a, b, _ = st.columns([1, 1, 4])
    if detail["review_type"] == "field_update":
        approve_label, reject_label = "✅ Apply changes", "✋ Keep current (reject)"
    else:
        approve_label, reject_label = "✅ Merge (same company)", "✋ Keep separate (different)"

    if a.button(approve_label, type="primary"):
        res = api_post(f"/reviews/{detail['id']}/approve")
        if res:
            st.success(f"Approved. {res}")
            st.rerun()
    if b.button(reject_label):
        res = api_post(f"/reviews/{detail['id']}/reject")
        if res:
            st.success(f"Rejected. Won't be flagged again. {res}")
            st.rerun()
else:
    st.caption(f"This item is already **{detail.get('status')}**.")
