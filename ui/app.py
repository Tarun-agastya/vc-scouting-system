"""
SCOUT — Team Dashboard (Phase G.3 + Review Inbox).

Browser UI for non-technical team members on the office LAN:
  • Browse & Search — find, view, edit and delete startups
  • Review Inbox    — approve/reject staged data changes & possible duplicates

Nothing in the database changes except through explicit actions here.

Run (on the Mac mini, served to the office network):
    python3 -m streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501

Talks to the local FastAPI backend (default http://localhost:8000).
"""
import os
import requests
import streamlit as st

API_BASE = os.environ.get("SCOUT_API_BASE", "http://localhost:8000")

RISK_MARK = {"high": "🔴", "low": "🟡", "anomaly": "⚠️", "none": "⚪"}
TYPE_LABEL = {"field_update": "Field change", "possible_duplicate": "Possible duplicate", "anomaly": "Anomaly"}

st.set_page_config(page_title="SCOUT — Team Dashboard", page_icon="🔎", layout="wide")


# ── API helpers ───────────────────────────────────────────────────────────────

def _err(exc):
    st.error(f"Cannot reach the backend at {API_BASE}. Is the API running?\n\n{exc}")

def api_get(path, **params):
    try:
        r = requests.get(f"{API_BASE}{path}", params={k: v for k, v in params.items() if v not in (None, "", "(all)")}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        _err(exc); return None

def api_post(path):
    try:
        r = requests.post(f"{API_BASE}{path}", timeout=60); r.raise_for_status(); return r.json()
    except Exception as exc:
        st.error(f"Action failed: {exc}"); return None

def api_patch(path, body):
    try:
        r = requests.patch(f"{API_BASE}{path}", json=body, timeout=60); r.raise_for_status(); return r.json()
    except Exception as exc:
        st.error(f"Save failed: {exc}"); return None

def api_delete(path, **params):
    try:
        r = requests.delete(f"{API_BASE}{path}", params=params, timeout=30); r.raise_for_status(); return r.json()
    except Exception as exc:
        st.error(f"Delete failed: {exc}"); return None


# ── Browse & Search page ──────────────────────────────────────────────────────

_EDITABLE = ["name", "short_description", "description", "website", "industry",
             "sub_industry", "tech_cluster", "country", "city", "address",
             "funding_stage", "founded_year", "employee_count", "contact_info", "linkedin"]

def page_browse():
    st.title("Browse & Search startups")
    st.caption("Search the database, open a startup to see everything we know, and edit or delete it.")

    st.sidebar.markdown("### Filters")
    q = st.sidebar.text_input("Keyword (name, summary, description, tags)")
    industry = st.sidebar.text_input("Industry")
    country = st.sidebar.text_input("Country")
    tech_cluster = st.sidebar.text_input("Tech cluster")
    funding_stage = st.sidebar.text_input("Funding stage")
    score_tier = st.sidebar.selectbox("Score tier", ["(all)", "PRIORITY", "HIGH_QUALITY_LEAD",
                                                      "INTERESTING", "EARLY_DISCOVERY", "WEAK_SIGNAL"])
    sort = st.sidebar.selectbox("Sort by", ["created_at", "extracted_at", "score", "name"])
    order = st.sidebar.selectbox("Order", ["desc", "asc"])

    data = api_get("/scout/list", q=q, industry=industry, country=country,
                   tech_cluster=tech_cluster, funding_stage=funding_stage,
                   score_tier=score_tier, sort=sort, order=order, limit=200)
    if data is None:
        return
    rows = data.get("startups", [])
    st.write(f"**{data.get('total', 0)}** startups match.")

    if rows:
        st.dataframe(
            [{"Name": s["name"], "Industry": s.get("industry"), "Cluster": s.get("tech_cluster"),
              "Country": s.get("country"), "City": s.get("city"), "Stage": s.get("funding_stage"),
              "Employees": s.get("employee_count"), "Tier": s.get("score_tier")} for s in rows],
            use_container_width=True, hide_index=True,
        )
        names = {f"{s['name']}  ·  {s.get('city') or '?'}, {s.get('country') or '?'}": s["id"] for s in rows}
        pick = st.selectbox("Open a startup", ["—"] + list(names.keys()))
        if pick != "—":
            _startup_detail(names[pick])


def _startup_detail(sid):
    s = api_get(f"/scout/startup/{sid}")
    if not s:
        return
    st.divider()
    st.subheader(s["name"])
    meta = st.columns(3)
    meta[0].write(f"**Score:** {s.get('enrichment_score')} ({s.get('score_tier') or '—'})")
    meta[1].write(f"**Extracted:** {str(s.get('extracted_at') or '')[:19].replace('T',' ') or '—'}")
    meta[2].write(f"**Source:** {s.get('source') or '—'}")

    with st.expander("Where this came from (source history)"):
        for h in s.get("source_history") or []:
            who = h.get("source_name") or h.get("source")
            when = (h.get("extracted_at") or h.get("date") or "")[:19].replace("T", " ")
            extra = f" — {h.get('subject')}" if h.get("subject") else ""
            st.write(f"- **{who}** ({when}){extra}")

    with st.form(f"edit_{sid}"):
        st.markdown("#### Edit")
        vals = {}
        c1, c2 = st.columns(2)
        vals["name"] = c1.text_input("Name", s.get("name") or "")
        vals["website"] = c2.text_input("Website", s.get("website") or "")
        vals["short_description"] = st.text_input("One-line summary", s.get("short_description") or "")
        vals["description"] = st.text_area("Description", s.get("description") or "")
        c3, c4, c5 = st.columns(3)
        vals["industry"] = c3.text_input("Industry", s.get("industry") or "")
        vals["tech_cluster"] = c4.text_input("Tech cluster", s.get("tech_cluster") or "")
        vals["funding_stage"] = c5.text_input("Funding stage", s.get("funding_stage") or "")
        c6, c7, c8 = st.columns(3)
        vals["city"] = c6.text_input("City", s.get("city") or "")
        vals["country"] = c7.text_input("Country", s.get("country") or "")
        vals["employee_count"] = c8.text_input("Employees", s.get("employee_count") or "")
        c9, c10 = st.columns(2)
        vals["address"] = c9.text_input("Address", s.get("address") or "")
        vals["contact_info"] = c10.text_input("Contact", s.get("contact_info") or "")
        vals["founded_year"] = st.text_input("Founded year", str(s.get("founded_year") or ""))

        if st.form_submit_button("💾 Save changes", type="primary"):
            changed = {}
            for k, v in vals.items():
                old = s.get(k)
                if k == "founded_year":
                    v = int(v) if str(v).strip().isdigit() else None
                if (v or None) != (old or None):
                    changed[k] = v
            if not changed:
                st.info("No changes to save.")
            else:
                res = api_patch(f"/scout/startup/{sid}", changed)
                if res:
                    st.success(f"Saved: {', '.join(changed.keys())}")
                    st.rerun()

    st.markdown("#### Danger zone")
    confirm = st.checkbox("I understand this permanently deletes the startup")
    if st.button("🗑️ Delete startup", disabled=not confirm):
        res = api_delete(f"/scout/startup/{sid}", confirm="true")
        if res:
            st.success(f"Deleted '{res.get('name')}'.")
            st.rerun()


# ── Review Inbox page ─────────────────────────────────────────────────────────

def page_reviews():
    st.title("Data Review Inbox")
    st.caption("The pipeline never changes existing data on its own. Every change and possible "
               "duplicate waits here for a person to approve or reject.")

    st.sidebar.markdown("### Filters")
    status = st.sidebar.selectbox("Status", ["pending", "approved", "rejected"])
    type_filter = st.sidebar.selectbox("Type", ["(all)", "field_update", "possible_duplicate", "anomaly"])
    risk_filter = st.sidebar.selectbox("Risk", ["(all)", "high", "low", "anomaly"])

    data = api_get("/reviews", status=status, review_type=type_filter, risk_level=risk_filter)
    if data is None:
        return
    reviews = data.get("reviews", [])

    counts = {"high": 0, "low": 0, "anomaly": 0}
    for rv in reviews:
        counts[rv.get("risk_level", "low")] = counts.get(rv.get("risk_level", "low"), 0) + 1
    a, b, c, d = st.columns(4)
    a.metric("Pending", len(reviews)); b.metric("🔴 Conflicts", counts.get("high", 0))
    c.metric("🟡 New info", counts.get("low", 0)); d.metric("⚠️ Anomalies", counts.get("anomaly", 0))

    if not reviews:
        st.success("Nothing to review. 🎉"); return

    def _label(rv):
        mark = RISK_MARK.get(rv.get("risk_level"), "⚪")
        t = TYPE_LABEL.get(rv.get("review_type"), rv.get("review_type"))
        if rv["review_type"] == "field_update":
            return f"{mark} {t}: {rv['master_name']} ({', '.join(rv.get('changed_fields') or []) or '—'})"
        return f"{mark} {t}: {rv['incoming_name']} ~ {rv['master_name']}"

    idx = st.selectbox("Select an item", range(len(reviews)), format_func=lambda i: _label(reviews[i]))
    detail = api_get(f"/reviews/{reviews[idx]['id']}")
    if not detail:
        return

    st.divider()
    st.subheader(f"{RISK_MARK.get(detail.get('risk_level'), '⚪')} "
                 f"{TYPE_LABEL.get(detail['review_type'], detail['review_type'])}")
    if detail.get("llm_explanation"):
        st.info(f"**AI explanation (not a decision):** {detail['llm_explanation']}")

    if detail["review_type"] == "field_update":
        st.markdown("#### Proposed changes")
        st.table([{"Field": f, "Current": c.get("old"), "Proposed": c.get("new"),
                   "From": c.get("incoming_source")} for f, c in (detail.get("proposed_changes") or {}).items()])
    else:
        l, r = st.columns(2)
        m, inc = detail.get("master") or {}, detail.get("incoming") or {}
        flds = ["name", "description", "website", "city", "country", "funding_stage", "founded_year", "industry"]
        l.markdown("**Existing record**"); l.table([{"Field": f, "Value": m.get(f)} for f in flds])
        r.markdown("**Incoming record**"); r.table([{"Field": f, "Value": inc.get(f)} for f in flds])

    with st.expander("Match evidence (per-signal scorecard)"):
        st.json(detail.get("evidence") or {})

    if detail.get("status") == "pending":
        ca, cb, _ = st.columns([1, 1, 4])
        if detail["review_type"] == "field_update":
            al, rl = "✅ Apply changes", "✋ Keep current (reject)"
        else:
            al, rl = "✅ Merge (same company)", "✋ Keep separate (different)"
        if ca.button(al, type="primary"):
            if api_post(f"/reviews/{detail['id']}/approve"):
                st.success("Approved."); st.rerun()
        if cb.button(rl):
            if api_post(f"/reviews/{detail['id']}/reject"):
                st.success("Rejected. Won't be flagged again."); st.rerun()
    else:
        st.caption(f"Already **{detail.get('status')}**.")


# ── Navigation ────────────────────────────────────────────────────────────────

st.sidebar.title("🔎 SCOUT")
review_data = api_get("/reviews", status="pending")
pending_n = review_data.get("total", 0) if review_data else 0
page = st.sidebar.radio("Page", [f"Review Inbox ({pending_n})", "Browse & Search"])
if st.sidebar.button("↻ Refresh"):
    st.rerun()
st.sidebar.divider()

if page.startswith("Review Inbox"):
    page_reviews()
else:
    page_browse()
