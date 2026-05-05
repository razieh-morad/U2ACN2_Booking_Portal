# U2ACN2 Nanolab Portal — with Chemical Inventory

This is the merged version of the Nanolab Booking Portal and Chemical Inventory.

---

## What's New: Chemical Inventory

The chemical inventory from the standalone `chemical-inventory` repo has been fully integrated into this Flask app.

### Public features
| URL | Description |
|-----|-------------|
| `/chemicals` | Browse & search all chemicals by name or formula (case-insensitive) |
| `/chemicals?q=NaCl` | Search results |
| `/chemicals/request` (POST) | Submit an in-stock request |
| `/chemicals/purchase` | Submit a purchase request for a missing chemical |

### Admin features
| URL | Description |
|-----|-------------|
| `/admin/chemicals` | Full admin dashboard (requires password) |
| `/admin/chemicals/add` (POST) | Add or update a chemical |
| `/admin/chemicals/delete/<id>` (POST) | Delete a chemical |
| `/admin/chemicals/reserve/<id>` (POST) | Reserve for a specific user |
| `/admin/chemicals/request/<id>/status` (POST) | Approve / reject / fulfill a request |
| `/admin/chemicals/purchase/<id>/status` (POST) | Approve / reject / mark purchased |
| `/admin/chemicals/export.csv` | Download full inventory as CSV |

---

## New Environment Variables

```env
# Chemical Inventory Admin
CHEM_ADMIN_PASSWORD=your-secret-password    # password for /admin/chemicals

# Email notifications
CHEM_ADMIN_EMAIL=admin@yourlab.ac.za        # receives in-stock request emails

# Purchase request notifications (comma-separated — all receive every purchase request)
PURCHASE_NOTIFY_EMAILS=admin@lab.ac.za,procurement@lab.ac.za,pi@lab.ac.za
```

All existing booking portal env vars remain unchanged.

---

## Reservation Feature

Chemicals can be reserved for a specific person/project via the admin panel.

- Set **Reserved For** (email or name) and **Reserved Label** (e.g., "PhD Project — MXene synthesis")
- In the public inventory, reserved chemicals are shown with an **amber banner** and the reservation label
- The chemical remains searchable and visible — it is not hidden
- Other users can still submit a request; the admin decides how to handle it

---

## Email Notifications Summary

| Event | Who receives email |
|-------|--------------------|
| User submits in-stock request | `CHEM_ADMIN_EMAIL` + user (confirmation) |
| User submits purchase request | All `PURCHASE_NOTIFY_EMAILS` + user (confirmation) |
| Admin approves/rejects a request | User |

---

## Chemical Data

83 chemicals pre-loaded from the lab's inventory CSV, including:
- Name, formula, MW, CAS number, supplier
- Amount, expiry date, storage group
- Fully editable/deletable in the admin panel
- Bulk CSV import available via admin panel

---

## Per-lab Admin Login (unchanged)

Set Render env vars:
- `ADMIN_<LAB>_USERNAME` and `ADMIN_<LAB>_PASSWORD`

Admin URLs: `/admin/<lab_slug>` (redirects to login if not authenticated)

## Existing Lab Slugs
furnace, xps, manual-drying-oven, automated-drying-oven, sputtering,
auto-lab, uv-vis-currie-500, centrifuge, pelletizer,
thermal-conductivity-system, freeze-dryer, spin-coater

---

## Requirements

Add to `requirements.txt`:
```
flask
gunicorn
psycopg2-binary   # only needed for PostgreSQL deployments
openpyxl          # only needed for PostgreSQL export
```

No pandas or streamlit needed — the inventory runs on plain Flask + SQLite/PostgreSQL.

---

## Database Migration

The existing `bookings.sqlite3` / PostgreSQL `bookings` table is untouched.
Three new tables are auto-created on first startup:
- `chemicals`
- `chemical_requests`
- `purchase_requests`

---

## Deleting the old `chemical-inventory` repo

1. Export the existing data via the Streamlit app's Download CSV button
2. Import the CSV into the new admin panel at `/admin/chemicals`
3. Archive or delete the `chemical-inventory` GitHub repo

---

## Health Check
`GET /health` → `{"status":"ok"}`
