"""Smoke test for the whole flow. Run from backend/:  python test_api.py"""
import os
import tempfile

os.environ["LIFELINK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from main import COMPAT, app  # noqa: E402

with TestClient(app) as c:
    # health + frontend served
    assert c.get("/api/health").json()["ok"]
    assert "LifeLink" in c.get("/").text

    # compatibility: O- patient only gets O- donors, AB+ gets everyone
    assert COMPAT["O-"] == ["O-"] and len(COMPAT["AB+"]) == 8

    # donor registration + validation + duplicate phone
    good = dict(name="Test Donor", phone="9876543210", email="t@x.com", blood_group="O-", city="Bhopal",
                area="MP Nagar", availability="Available now", consent_emergency=True, consent_medical=True)
    r = c.post("/api/donors", json=good)
    assert r.status_code == 201 and r.json()["id"].startswith("LL-D-"), r.text
    assert c.post("/api/donors", json=good).status_code == 409
    bad = c.post("/api/donors", json={**good, "phone": "123", "email": "q@x.com"})
    assert bad.status_code == 422 and "10-digit" in bad.json()["detail"], bad.text
    recent = dict(good, phone="9876543211", email="r@x.com", last_donated="2999-01-01")
    assert c.post("/api/donors", json=recent).status_code == 422

    # markup is rejected in free-text fields (stored-XSS guard)
    xss = c.post("/api/donors", json=dict(good, phone="9876543299", email="x@x.com", name="<img src=x onerror=alert(1)>"))
    assert xss.status_code == 422, xss.text

    # hospital login
    assert c.post("/api/hospital/login", json={"email": "demo@citycare.com", "password": "wrong"}).status_code == 401
    lg = c.post("/api/hospital/login", json={"email": "demo@citycare.com", "password": "hospital123"}).json()
    H = {"Authorization": "Bearer " + lg["token"]}

    # emergency request for O-: only O- donors, nearest/most ready first, contact hidden
    r = c.post("/api/requests", headers=H, json=dict(blood_group="O-", units=2, urgency="CRITICAL", radius_km=5,
                                                      context="Accident / trauma", rare_mode=True))
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["id"] == "LL-2042", req["id"]
    assert req["donors"] and all(d["g"] == "O-" for d in req["donors"])
    assert all("phone" not in d for d in req["donors"])
    pend = [d for d in req["donors"] if d["status"] == "pending"]
    assert pend and all(d["km"] <= 5 for d in pend)
    assert all(d["av"] == "A" for d in pend)
    print("O- request:", req["counts"])

    # Rh+ patient can receive Rh- blood (B+ -> B-, O- included)
    b = c.post("/api/requests", headers=H, json=dict(blood_group="B+", units=1, radius_km=15)).json()
    assert {"B+", "B-", "O+", "O-"} >= {d["g"] for d in b["donors"]} and any(d["g"] in ("B-", "O-") for d in b["donors"])

    # donor accepts -> contact shared, counts update
    did = pend[0]["id"]
    r = c.post(f"/api/requests/{req['id']}/respond", json={"donor_id": did, "response": "accepted"})
    assert r.status_code == 200 and r.json()["counts"]["accepted"] == 1
    # the open (no-login) respond endpoint must never leak donor details
    assert "donors" not in r.json() and "phone" not in r.text
    # the hospital sees the phone only after acceptance
    full = c.get(f"/api/requests/{req['id']}", headers=H).json()
    acc = [d for d in full["donors"] if d["id"] == did][0]
    assert acc["phone"].startswith("9")
    assert all("phone" not in d for d in full["donors"] if d["status"] != "accepted")
    # same donor cannot respond twice
    assert c.post(f"/api/requests/{req['id']}/respond", json={"donor_id": did, "response": "declined"}).status_code == 404

    # expand radius alerts more donors
    before = req["counts"]["alerted"]
    r = c.post(f"/api/requests/{req['id']}/expand", headers=H, json={"radius_km": 10}).json()
    assert r["radius_km"] >= 10 and r["counts"]["alerted"] >= before

    # fulfil -> closed, further responses blocked
    r = c.patch(f"/api/requests/{req['id']}/status", headers=H, json={"status": "Fulfilled"})
    assert r.json()["status"] == "Fulfilled"
    assert c.post(f"/api/requests/{req['id']}/respond", json={"donor_id": did, "response": "accepted"}).status_code == 409

    # listing + stats + auth edge cases
    assert len(c.get("/api/requests", headers=H).json()) == 2
    assert c.get("/api/requests", headers={"Authorization": "Bearer nope"}).status_code == 401
    s = c.get("/api/stats").json()
    assert s["donors"] == 21 and s["requests"] == 2 and s["fulfilled"] == 1, s
    assert c.put(f"/api/donors/{r and 'LL-D-4201'}/availability", json={"availability": "Busy"}).status_code == 200
    assert c.get("/api/donors", params={"blood_group": "O-", "available_only": True}).status_code == 200

print("ALL TESTS PASSED")
