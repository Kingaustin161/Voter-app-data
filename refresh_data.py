"""
Daily data refresh script
============================

Pulls NC House candidates from OpenFEC, matches sitting members to
Congress.gov for photos/voting records, pulls real donors from OpenFEC,
and saves everything to one data.json file.

This is built from the exact logic already tested by hand in earlier
scripts (state-name matching, vote deduplication, donor entity typing)
— just combined into one run and reading keys from the environment
instead of having them pasted into the file, since this version is
meant to run unattended via GitHub Actions.

Local test run:
  set OPENFEC_API_KEY=your_key_here      (Windows)
  set CONGRESS_API_KEY=your_key_here
  python refresh_data.py

On GitHub Actions, these are supplied as repository secrets instead.

Known scope limits (honest, not bugs):
  - Photos are left blank here. Wikipedia has good photos but no
    reliable, predictable URL pattern across all candidates, so
    automating photo lookup at scale needs a different source later.
  - Bio, education, and campaign website still aren't available from
    any free source connected so far.
"""

import os
import json
import requests

OPENFEC_API_KEY = os.environ["OPENFEC_API_KEY"]
CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]

OPENFEC_BASE = "https://api.open.fec.gov/v1"
CONGRESS_BASE = "https://api.congress.gov/v3"

CONGRESS_NUM = 119
SESSION = 2
CYCLE = 2026

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

LEGISLATION_TYPE_LABELS = {
    "HR": "H.R.", "HRES": "H.Res.", "HJRES": "H.J.Res.", "HCONRES": "H.Con.Res.",
    "S": "S.", "SRES": "S.Res.", "SJRES": "S.J.Res.", "SCONRES": "S.Con.Res.",
}


def get_nc_house_candidates():
    params = {
        "api_key": OPENFEC_API_KEY, "cycle": CYCLE, "office": "H",
        "state": "NC", "candidate_status": "C", "sort": "name", "per_page": 50,
    }
    resp = requests.get(f"{OPENFEC_BASE}/candidates/", params=params)
    resp.raise_for_status()
    return resp.json()["results"]


def get_all_congress_members():
    members = []
    for offset in (0, 250):
        params = {"api_key": CONGRESS_API_KEY, "currentMember": "true", "limit": 250, "offset": offset}
        resp = requests.get(f"{CONGRESS_BASE}/member", params=params)
        resp.raise_for_status()
        members.extend(resp.json().get("members", []))
    return members


def match_congress_member(candidate_name, state_code, members):
    last_name = candidate_name.split(",")[0].strip().lower()
    state_full = STATE_NAMES.get(state_code, state_code)
    for m in members:
        if m.get("state") == state_full and last_name in m.get("name", "").lower():
            return m
    return None


def get_voting_record(bioguide_id, max_votes=8):
    params = {"api_key": CONGRESS_API_KEY, "limit": max_votes}
    resp = requests.get(f"{CONGRESS_BASE}/house-vote/{CONGRESS_NUM}/{SESSION}", params=params)
    if resp.status_code != 200:
        return []
    data = resp.json()
    votes_list = None
    for key in ("houseRollCallVotes", "results", "votes"):
        if key in data:
            votes_list = data[key]
            break
    if not votes_list:
        return []

    by_bill = {}
    for rc in votes_list:
        roll_number = rc.get("rollCallNumber")
        leg_type = rc.get("legislationType")
        leg_number = rc.get("legislationNumber")
        if not (leg_type and leg_number):
            continue
        bill = f"{LEGISLATION_TYPE_LABELS.get(leg_type, leg_type)} {leg_number}"

        mv_resp = requests.get(
            f"{CONGRESS_BASE}/house-vote/{CONGRESS_NUM}/{SESSION}/{roll_number}/members",
            params={"api_key": CONGRESS_API_KEY, "limit": 500},
        )
        if mv_resp.status_code != 200:
            continue
        mv_data = mv_resp.json()
        member_votes = None
        for key in ("houseRollCallVoteMemberVotes", "results", "votes"):
            if key in mv_data:
                block = mv_data[key]
                member_votes = block.get("results", block) if isinstance(block, dict) else block
                break
        if not member_votes:
            continue

        vote = None
        for v in member_votes:
            if (v.get("bioguideID") or v.get("bioguideId")) == bioguide_id:
                vote = v.get("voteCast")
                break
        if not vote:
            continue

        normalized_vote = "Yes" if vote in ("Yea", "Aye") else "No"
        existing = by_bill.get(bill)
        if existing is None or roll_number > existing["roll_call_number"]:
            by_bill[bill] = {"bill": bill, "vote": normalized_vote, "roll_call_number": roll_number}

    return [{"bill": v["bill"], "vote": v["vote"]} for v in by_bill.values()]


def get_principal_committee(candidate_id):
    resp = requests.get(
        f"{OPENFEC_BASE}/candidate/{candidate_id}/committees/",
        params={"api_key": OPENFEC_API_KEY, "cycle": CYCLE},
    )
    if resp.status_code != 200:
        return None
    committees = resp.json().get("results", [])
    return next((c for c in committees if c.get("designation") == "P"), None) or (
        committees[0] if committees else None
    )


def get_top_donors(committee_id, max_donors=5):
    params = {
        "api_key": OPENFEC_API_KEY, "two_year_transaction_period": CYCLE,
        "sort": "-contribution_receipt_amount", "per_page": max_donors,
        "committee_id": committee_id,
    }
    resp = requests.get(f"{OPENFEC_BASE}/schedules/schedule_a/", params=params)
    if resp.status_code != 200:
        return []
    return [
        {
            "name": d.get("contributor_name", "Unknown"),
            "amount": f"${d.get('contribution_receipt_amount', 0):,.0f}",
            "type": (d.get("entity_type_desc") or "Contribution").title(),
        }
        for d in resp.json().get("results", [])
    ]


def build_all_candidates():
    print("Fetching NC House candidates from OpenFEC...")
    candidates = get_nc_house_candidates()
    print(f"Found {len(candidates)} candidates.")

    print("Fetching current Congress.gov members...")
    members = get_all_congress_members()

    output = []
    for c in candidates:
        name = c.get("name", "")
        candidate_id = c.get("candidate_id")
        print(f"\nProcessing {name}...")

        entry = {
            "name": name,
            "party": c.get("party_full"),
            "candidate_id": candidate_id,
            "incumbent_challenger": c.get("incumbent_challenge_full"),
            "photo_url": None,
            "district": None,
            "voting_record": None,
            "donors": [],
        }

        member = match_congress_member(name, "NC", members)
        if member:
            bioguide_id = member.get("bioguideId")
            entry["district"] = member.get("district")
            entry["bioguide_id"] = bioguide_id
            print(f"  Matched to Congress.gov member (bioguideId {bioguide_id})")
            entry["voting_record"] = get_voting_record(bioguide_id)
            print(f"  Got {len(entry['voting_record'])} voting record entries")
        else:
            print("  No current Congress.gov match (likely a non-incumbent challenger)")

        committee = get_principal_committee(candidate_id)
        if committee:
            entry["donors"] = get_top_donors(committee["committee_id"])
            print(f"  Got {len(entry['donors'])} donor records")

        output.append(entry)

    return output


if __name__ == "__main__":
    all_data = build_all_candidates()
    with open("data.json", "w") as f:
        json.dump(all_data, f, indent=2)
    print(f"\nSaved {len(all_data)} candidates to data.json")
