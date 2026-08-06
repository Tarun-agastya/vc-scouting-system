"""
Instagram metric-support probe (Phase IG-0, 6 Aug 2026).

Asks OUR OWN Instagram Business account, through the OFFICIAL Meta Graph API,
which insight metrics it actually returns — instead of trusting documentation.

Why this exists rather than a hardcoded metric list: Meta renames and retires
metrics between API versions (`impressions` and `video_views` were both retired
in v22.0 in favour of `views`; sources disagree on whether account-level
`profile_views` still exists at all). Building a database schema and a report
around a guessed metric name is how this breaks on day one. So we measure.

This is the same discipline already proven twice in this codebase: Phase R's
"inspect the source, THEN choose the strategy", and H-1's "never invent a value
that isn't literally there".

SAFETY
------
Strictly READ-ONLY. Makes GET requests to the official Graph API host only.
Writes nothing to Instagram and nothing to our database — its only side effect
is the local metric_support.json report file. It never touches instagram.com,
never uses a password, and stops immediately (rather than retrying) on a rate
limit or a bad token. See instagram_insights/README.md for the full rules.

Usage
-----
  python3 scripts/ig_probe.py
  python3 scripts/ig_probe.py --api-version v24.0
  python3 scripts/ig_probe.py --json-only        # suppress the human summary
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import settings  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OUT_PATH = _PROJECT_ROOT / "instagram_insights" / "metric_support.json"

# The ONLY hosts this module may ever contact — both are Meta's official
# Graph API, never the consumer website. Asserted at call time so no future
# edit can quietly retarget a request at instagram.com — scraping/automating
# the website is the one realistic way to get a business account restricted,
# and it is prohibited outright (instagram_insights/README.md).
#
# Two hosts because Meta has two distinct auth paths, and which one applies
# depends on whether the account has a linked Facebook Page:
#   - "instagram_login" (settings.ig_auth_mode default): the account
#     authenticates directly with Instagram via "Business Login for
#     Instagram" — no Facebook Page required. Data host: graph.instagram.com.
#     This is the path for an Instagram-only business account (no Page),
#     confirmed live 6 Aug 2026 to be this project's actual situation.
#   - "facebook_login": the older path, requires a linked Facebook Page and
#     (optionally) a Meta Business Manager System User token that never
#     expires. Data host: graph.facebook.com. Kept for completeness in case
#     a Page is linked later — NOT the path currently in use.
_GRAPH_HOSTS = {
    "instagram_login": "graph.instagram.com",
    "facebook_login": "graph.facebook.com",
}


class ProbeStop(Exception):
    """Raised for conditions where continuing would be unsafe or pointless
    (bad token, rate limited, call budget exhausted) — never for a single
    metric being unsupported, which is the normal, expected outcome."""


# ── What the owner actually asked for, mapped to candidate API metrics ───────
# Each entry: the owner's own words -> the Graph API call(s) that could satisfy
# it. The report is written in these terms (not raw metric names) so the answer
# to "can we get what I asked for?" is legible without knowing Meta's taxonomy.
REQUESTED = {
    "accounts reached":                 ["reach"],
    "reach":                            ["reach"],
    "followers gained and lost":        ["follows_and_unfollows", "follower_count"],
    "profile visits":                   ["profile_views", "profile_links_taps"],
    "views on posts and reels":         ["media.views", "media.reach"],
    "engagement: followers vs non-followers": ["reach.follow_type"],
    "audience age":                     ["follower_demographics.age"],
    "audience gender":                  ["follower_demographics.gender"],
    "top locations":                    ["follower_demographics.city",
                                         "follower_demographics.country"],
    "overall engagement":               ["accounts_engaged", "total_interactions"],
}


def _load_token() -> tuple[str, str]:
    """Returns (access_token, token_type). Never logs or prints the token."""
    path = Path(settings.ig_token_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    if not path.exists():
        raise ProbeStop(
            f"No token file at {path}.\n"
            "Follow instagram_insights/README.md §2 to create the Meta app and\n"
            "generate a token, then save it as described in §2.5."
        )
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise ProbeStop(f"Token file at {path} is not valid JSON: {exc}")

    token = (data.get("access_token") or "").strip()
    if not token:
        raise ProbeStop(f"Token file at {path} has no 'access_token' value.")
    return token, data.get("token_type", "unknown")


class Probe:
    def __init__(self, token: str, api_version: str, budget: int, auth_mode: str):
        if auth_mode not in _GRAPH_HOSTS:
            raise ProbeStop(
                f"Unknown ig_auth_mode={auth_mode!r} — must be one of "
                f"{list(_GRAPH_HOSTS)}. Check .env / config/__init__.py."
            )
        self.token = token
        self.auth_mode = auth_mode
        self.host = _GRAPH_HOSTS[auth_mode]
        self.base = f"https://{self.host}/{api_version}"
        self.calls = 0
        self.budget = budget
        self._client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def close(self):
        self._client.close()

    def get(self, path: str, params: dict) -> tuple[bool, dict]:
        """One read-only Graph call. Returns (ok, payload_or_error).

        A metric being unsupported comes back as ok=False with Meta's own error
        text — that is data, not a failure. Only genuinely unsafe conditions
        (budget exhausted, rate limit, invalid token) raise ProbeStop, and they
        stop the run immediately rather than retrying: hammering after a
        rejection is what escalates throttling into enforcement.
        """
        if self.calls >= self.budget:
            raise ProbeStop(
                f"Per-run call budget ({self.budget}) exhausted — stopping rather "
                "than risking the 200/hour account limit. Raise ig_max_calls_per_run "
                "only if you understand why."
            )
        url = f"{self.base}{path}"
        assert url.startswith(f"https://{self.host}/"), f"refusing non-Graph host: {url}"
        assert self.host in _GRAPH_HOSTS.values(), f"refusing unrecognized host: {self.host}"

        self.calls += 1
        try:
            resp = self._client.get(url, params={**params, "access_token": self.token})
        except Exception as exc:
            return False, {"error": f"network error: {exc}"}

        try:
            payload = resp.json()
        except Exception:
            return False, {"error": f"HTTP {resp.status_code}, non-JSON body"}

        if "error" in payload:
            err = payload["error"]
            code = err.get("code")
            msg = err.get("message", "")
            # 190 = token invalid/expired. 4/17/32/613 = rate limiting.
            # Both mean "stop now", never "try again harder".
            if code == 190:
                raise ProbeStop(
                    f"Access token rejected by Meta (code 190): {msg}\n"
                    "Regenerate it per instagram_insights/README.md §2.4."
                )
            if code in (4, 17, 32, 613):
                raise ProbeStop(
                    f"Rate limited by Meta (code {code}): {msg}\n"
                    "Stopping deliberately. Wait an hour before re-running."
                )
            return False, {"error": msg, "code": code,
                           "subcode": err.get("error_subcode"),
                           "type": err.get("type")}

        return True, payload

    # ── discovery ───────────────────────────────────────────────────────────
    def resolve_account(self) -> dict:
        """Find the IG Business account behind this token.

        Two genuinely different mechanisms depending on ig_auth_mode — there
        is no single call that works for both, so this branches explicitly
        rather than silently guessing, since a wrong guess here would look
        like a scope problem and send someone chasing the wrong fix.
        """
        if settings.ig_user_id:
            # Already pinned (e.g. by a prior successful probe) — skip
            # discovery entirely, one fewer call.
            ok, payload = self.get(
                f"/{settings.ig_user_id}",
                {"fields": "id,username,name,followers_count,media_count"},
            )
            if ok:
                return payload
            raise ProbeStop(
                f"Configured ig_user_id={settings.ig_user_id} could not be read: "
                f"{payload.get('error')}"
            )

        if self.auth_mode == "instagram_login":
            # "Business Login for Instagram" — the account authenticates
            # directly, no Facebook Page in the loop. The token itself
            # resolves straight to the account via GET /me.
            ok, payload = self.get(
                "/me",
                {"fields": "user_id,username,name,account_type,followers_count,media_count"},
            )
            if not ok:
                raise ProbeStop(
                    f"GET /me failed: {payload.get('error')}\n"
                    "The token needs instagram_business_basic + "
                    "instagram_business_manage_insights (README §2.4), and the "
                    "account must be Business/Creator, not Personal (README §2.1)."
                )
            payload["id"] = payload.get("id") or payload.get("user_id")
            return payload

        # auth_mode == "facebook_login" — the older, Page-mediated path.
        # Not this project's current setup (no linked Page, confirmed 6 Aug
        # 2026) but kept working in case a Page is linked later.
        ok, payload = self.get(
            "/me/accounts",
            {"fields": "id,name,instagram_business_account{id,username,followers_count,media_count}"},
        )
        if not ok:
            raise ProbeStop(
                "Could not list Facebook Pages for this token: "
                f"{payload.get('error')}\n"
                "The token needs pages_read_engagement, and the Instagram account "
                "must be linked to a Page you admin (README §2.2)."
            )

        pages = payload.get("data", [])
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            raise ProbeStop(
                f"Token can see {len(pages)} Facebook Page(s), but none has a linked "
                "Instagram Business account.\n"
                "Check README §2.1 (account must be Business/Creator) and §2.2 "
                "(must be linked to the Page) — or set ig_auth_mode=instagram_login "
                "if this account has no Facebook Page at all."
            )
        if len(linked) > 1:
            names = ", ".join(f"{p['name']} -> @{p['instagram_business_account'].get('username')}"
                              for p in linked)
            print(f"  ! Multiple linked IG accounts found ({names}).")
            print(f"  ! Using the first. Pin the right one via IG_USER_ID in .env.")

        page = linked[0]
        acct = dict(page["instagram_business_account"])
        acct["_page_id"] = page["id"]
        acct["_page_name"] = page.get("name")
        return acct


def _account_probes(since: int, until: int) -> list[dict]:
    """Candidate account-level calls. `key` matches the REQUESTED map above."""
    day = {"period": "day", "since": since, "until": until}
    return [
        # Plain time-series metrics.
        {"key": "reach", "params": {"metric": "reach", **day}},
        {"key": "views", "params": {"metric": "views", **day}},
        {"key": "profile_views", "params": {"metric": "profile_views", **day}},
        {"key": "website_clicks", "params": {"metric": "website_clicks", **day}},
        # follower_count is only retained for ~30 days even though other
        # metrics go back 90 — worth knowing separately.
        {"key": "follower_count", "params": {"metric": "follower_count", **day}},

        # Newer metrics require an explicit metric_type.
        {"key": "accounts_engaged",
         "params": {"metric": "accounts_engaged", "metric_type": "total_value", **day}},
        {"key": "total_interactions",
         "params": {"metric": "total_interactions", "metric_type": "total_value", **day}},
        {"key": "profile_links_taps",
         "params": {"metric": "profile_links_taps", "metric_type": "total_value", **day}},
        # THE metric for "followers gained and lost" as one signed series.
        {"key": "follows_and_unfollows",
         "params": {"metric": "follows_and_unfollows", "metric_type": "total_value", **day}},

        # THE mechanism for "engagement based on followers vs non-followers".
        {"key": "reach.follow_type",
         "params": {"metric": "reach", "metric_type": "total_value",
                    "breakdown": "follow_type", **day}},

        # Demographics: lifetime snapshots, NOT time series. Require 100+
        # followers, and Meta caps the response at the top 45 segments.
        *[
            {"key": f"follower_demographics.{dim}",
             "params": {"metric": "follower_demographics", "period": "lifetime",
                        "metric_type": "total_value", "breakdown": dim}}
            for dim in ("age", "gender", "city", "country")
        ],
        *[
            {"key": f"engaged_audience_demographics.{dim}",
             "params": {"metric": "engaged_audience_demographics", "period": "lifetime",
                        "metric_type": "total_value", "breakdown": dim}}
            for dim in ("age", "gender")
        ],
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Instagram metric-support probe")
    ap.add_argument("--api-version", default=settings.ig_api_version)
    ap.add_argument("--auth-mode", default=settings.ig_auth_mode,
                    choices=list(_GRAPH_HOSTS),
                    help="instagram_login (no Facebook Page — this project's setup) "
                         "or facebook_login (Page + Business Manager)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    say = (lambda *a: None) if args.json_only else print

    say("Instagram metric-support probe — READ-ONLY, official Graph API only")
    say(f"Auth mode: {args.auth_mode}  ({_GRAPH_HOSTS[args.auth_mode]})")
    say("=" * 72)

    try:
        token, token_type = _load_token()
    except ProbeStop as stop:
        # Expected on a fresh machine before the one-time Meta setup — this is
        # guidance, not a crash, so no traceback.
        say(f"\nNot set up yet:\n{stop}")
        return 1

    try:
        probe = Probe(token, args.api_version, settings.ig_max_calls_per_run, args.auth_mode)
    except ProbeStop as stop:
        say(f"\nSTOPPED: {stop}")
        return 1

    result: dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "api_version": args.api_version,
        "auth_mode": args.auth_mode,
        "token_type": token_type,
        "account": {},
        "metrics": {},
        "requested": {},
        "notes": [],
    }

    try:
        acct = probe.resolve_account()
        followers = acct.get("followers_count")
        result["account"] = {
            "ig_user_id": acct.get("id"),
            "username": acct.get("username"),
            "followers_count": followers,
            "media_count": acct.get("media_count"),
            "page_name": acct.get("_page_name"),
        }
        say(f"\nAccount:   @{acct.get('username')}  (id {acct.get('id')})")
        say(f"Followers: {followers}   Media: {acct.get('media_count')}")
        say(f"Token:     {token_type}   API: {args.api_version}")

        if isinstance(followers, int) and followers < 100:
            note = (f"Account has {followers} followers. Meta returns NO audience "
                    "demographics (age/gender/city/country) below 100 followers — "
                    "those requested metrics will be unavailable until it grows.")
            result["notes"].append(note)
            say(f"\n  ! {note}")

        ig_id = acct["id"]
        until = int(datetime.now(timezone.utc).timestamp())
        since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())

        # ── account-level ────────────────────────────────────────────────────
        say("\nAccount-level metrics")
        say("-" * 72)
        for spec in _account_probes(since, until):
            ok, payload = probe.get(f"/{ig_id}/insights", spec["params"])
            entry = {"supported": ok, "params": spec["params"]}
            if ok:
                data = payload.get("data", [])
                entry["returned_series"] = len(data)
                entry["sample"] = data[:1]
                has_values = bool(data) and bool(
                    data[0].get("values") or data[0].get("total_value")
                )
                entry["has_values"] = has_values
                mark = "OK  " if has_values else "EMPTY"
                say(f"  {mark} {spec['key']}")
                if not has_values:
                    entry["supported"] = False
                    entry["error"] = "accepted by API but returned no values"
            else:
                say(f"  FAIL {spec['key']:<40} {str(payload.get('error'))[:60]}")
                entry["error"] = payload.get("error")
                entry["code"] = payload.get("code")
            result["metrics"][spec["key"]] = entry

        # ── media-level ──────────────────────────────────────────────────────
        say("\nMedia-level metrics (sampled from recent posts/reels)")
        say("-" * 72)
        ok, payload = probe.get(
            f"/{ig_id}/media",
            {"fields": "id,media_type,media_product_type,timestamp,permalink", "limit": 5},
        )
        if not ok:
            say(f"  FAIL could not list media: {payload.get('error')}")
            result["metrics"]["media.list"] = {"supported": False, "error": payload.get("error")}
        else:
            media = payload.get("data", [])
            result["metrics"]["media.list"] = {"supported": True, "returned": len(media)}
            say(f"  OK   listed {len(media)} recent media")
            if not media:
                result["notes"].append("Account has no media yet — per-post/reel "
                                       "metrics could not be probed.")
            else:
                # Probe one feed post and one reel if both exist, since reels
                # expose a different metric set.
                by_kind: dict[str, dict] = {}
                for m in media:
                    by_kind.setdefault(m.get("media_product_type") or m.get("media_type"), m)
                for kind, m in list(by_kind.items())[:3]:
                    ok, mp = probe.get(
                        f"/{m['id']}/insights",
                        {"metric": "views,reach,likes,comments,saved,shares,total_interactions"},
                    )
                    key = f"media.{kind}"
                    if ok:
                        names = [d.get("name") for d in mp.get("data", [])]
                        result["metrics"][key] = {"supported": True, "available": names}
                        say(f"  OK   {key:<28} -> {', '.join(names)}")
                    else:
                        result["metrics"][key] = {"supported": False, "error": mp.get("error")}
                        say(f"  FAIL {key:<28} {str(mp.get('error'))[:50]}")

    except ProbeStop as stop:
        say(f"\nSTOPPED: {stop}")
        result["stopped"] = str(stop)
        _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OUT_PATH.write_text(json.dumps(result, indent=2))
        probe.close()
        return 1
    finally:
        result["calls_used"] = probe.calls

    probe.close()

    # ── answer the owner's actual question ──────────────────────────────────
    for ask, candidates in REQUESTED.items():
        got = [c for c in candidates if result["metrics"].get(c, {}).get("supported")]
        # media.* candidates are probed per media_product_type, so match by prefix
        if not got:
            got = [k for k in result["metrics"]
                   if any(k.startswith(c) for c in candidates)
                   and result["metrics"][k].get("supported")]
        result["requested"][ask] = {"available": bool(got), "via": got,
                                    "candidates_tried": candidates}

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(result, indent=2))

    say("\n" + "=" * 72)
    say("WHAT YOU ASKED FOR — can we actually get it?")
    say("=" * 72)
    unavailable = []
    for ask, info in result["requested"].items():
        if info["available"]:
            say(f"  YES  {ask:<42} via {', '.join(info['via'])}")
        else:
            say(f"  NO   {ask:<42} (tried: {', '.join(info['candidates_tried'])})")
            unavailable.append(ask)

    if unavailable:
        say(f"\n  {len(unavailable)} requested metric(s) NOT available on this account.")
        say("  Do not design the report around them — see the notes above for why.")
    for n in result["notes"]:
        say(f"\n  NOTE: {n}")

    say(f"\nGraph API calls used: {probe.calls} / {settings.ig_max_calls_per_run}")
    say(f"Written: {_OUT_PATH.relative_to(_PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
