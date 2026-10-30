# LifeLink: Local Blood Donation Network

Connects hospitals with compatible, eligible, nearby blood donors during emergencies.

- `frontend/index.html` single-page UI
- `backend/main.py` FastAPI + SQLite API (also serves the frontend)

## Run it

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://localhost:8000/ for the app and http://localhost:8000/docs for the API docs.
Live demo: https://life-link-s631.onrender.com/ (the free Render plan sleeps when idle, so the first load can take about a minute).
Hospitals can register themselves on the Hospital Portal page. A seeded demo hospital also exists for testing: `demo@citycare.com` / `hospital123` (not shown on the website). 20 demo donors are seeded on first run.

Test: `pip install httpx` then `python test_api.py`.

## How matching works

1. **Compatibility**: only donors whose blood group is compatible with the patient's (`GET /api/compatibility`).
2. **Eligibility**: must be "Available now" and 90+ days since last donation.
3. **Distance**: haversine from the hospital; only donors inside the search radius are alerted.
4. **Readiness Score /100**: distance 40, availability 25, eligibility 20, past response 15.
5. **Rare mode**: AB-, B-, A-, O- donors are ranked first.
6. Donor phone numbers are shown to the hospital only after the donor accepts.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/donors` | Register a donor |
| GET | `/api/donors` | List donors (no contact details) |
| PUT | `/api/donors/{id}/availability` | Donor changes availability |
| POST | `/api/hospitals` | Register a hospital |
| POST | `/api/hospital/login` | Login, returns token |
| POST | `/api/requests` | Create emergency request, matches and alerts donors |
| GET | `/api/requests` | List this hospital's requests |
| GET | `/api/requests/{id}` | Request with ranked donors |
| POST | `/api/requests/{id}/respond` | Donor accepts or declines |
| POST | `/api/requests/{id}/expand` | Widen the search radius |
| PATCH | `/api/requests/{id}/status` | Mark Fulfilled or Cancelled |
| GET | `/api/stats` | Platform numbers |

## Known limitations (good "future work" slide)

- Alerts are recorded in the database, but no real SMS/WhatsApp is sent yet (plug in Twilio or WhatsApp Cloud API in `create_request` and `expand_radius`).
- Donor endpoints have no login or OTP. Set `LIFELINK_REQUIRE_AUTH=1` to force hospital login.
- The hospital dashboard reads live data from the API. The analytics page and the donor dashboard still show sample numbers (labelled "Sample data").
- Donor replies in the Command Center are simulated for the demo (labelled "Demo simulation"). Real replies arrive through `POST /api/requests/{id}/respond`.
- The Command Center uses a real OpenStreetMap map (Leaflet) with donor and hospital positions. Donor positions are rounded to about 1 km.
- Contact buttons unlock only after a donor accepts, and placeholder numbers of sample donors are never dialled.
- Hospital sessions expire after 24 hours. Text fields reject `<` and `>`.
- Final medical eligibility is always decided by the hospital or blood bank.
