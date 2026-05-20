# U2ACN2 Booking Portal - Code Review & Improvement Suggestions

## Executive Summary
Your booking portal is well-structured with good separation of concerns (DB layer, business logic, templates). It handles multiple labs, user roles, chemical inventory, and purchase requests. Below are the 3 specific changes you requested + comprehensive suggestions for long-term improvements.

---

## 🔴 REQUIRED CHANGES (Priority 1)

### 1. **Oven Labs: Restrict to 2 Time Slots (Morning 8-12, Afternoon 12-16)**

**Current Issue:** Lines 876-884 show furnace has 2 slots, but other ovens (manual-drying-oven, automated-drying-oven) use the generic SLOT_MINUTES system and get many slots.

**Solution:**
```python
# In build_slots_for_day() function (around line 876):

def build_slots_for_day(d: date, lab_slug: str) -> List[Tuple[time, time]]:
    oven_labs = ["furnace", "manual-drying-oven", "automated-drying-oven"]
    if lab_slug in oven_labs:
        return [(time(8, 0), time(12, 0)), (time(12, 0), time(16, 0))]
    slots: List[Tuple[time, time]] = []
    cur = datetime.combine(d, WORKDAY_START)
    end = datetime.combine(d, WORKDAY_END)
    while cur < end:
        nxt = cur + timedelta(minutes=SLOT_MINUTES)
        if nxt > end: break
        slots.append((cur.time(), nxt.time()))
        cur = nxt
    return slots
```

**Also update** the furnace-specific validation function `is_valid_furnace_block()` to check all ovens:
```python
def is_valid_oven_block(lab_slug: str, start_time: str, end_time: str) -> bool:
    """Validate that slot is 8-12 or 12-16 for oven labs"""
    oven_labs = ["furnace", "manual-drying-oven", "automated-drying-oven"]
    if lab_slug not in oven_labs:
        return True
    s = parse_time(start_time)
    e = parse_time(end_time)
    valid_blocks = [(time(8, 0), time(12, 0)), (time(12, 0), time(16, 0))]
    return (s, e) in valid_blocks
```

---

### 2. **Admin Review Page: Add Green "Approve" & Red "Reject" Buttons**

**Current Issue:** Lines 1748-1780 show the admin booking form, but buttons are likely missing or unclear. 

**Location:** `templates/admin_booking.html`

**Add these buttons beside the review section:**
```html
<form method="POST" style="display: flex; gap: 10px; margin-top: 20px;">
    <!-- APPROVAL SECTION -->
    <div style="border: 2px solid #28a745; padding: 15px; border-radius: 5px; flex: 1;">
        <h4 style="color: #28a745;">✓ Approve</h4>
        <textarea name="approval_note" placeholder="Add optional approval note..." 
                  style="width: 100%; height: 80px; padding: 10px; margin: 10px 0; border: 1px solid #28a745;"></textarea>
        <button type="submit" name="action" value="approve" 
                style="background-color: #28a745; color: white; padding: 10px 20px; border: none; border-radius: 3px; cursor: pointer; font-weight: bold;">
            ✓ APPROVE
        </button>
    </div>

    <!-- REJECTION SECTION -->
    <div style="border: 2px solid #dc3545; padding: 15px; border-radius: 5px; flex: 1;">
        <h4 style="color: #dc3545;">✕ Reject</h4>
        <textarea name="rejection_reason" placeholder="Reason for rejection (sent to user)..." 
                  style="width: 100%; height: 80px; padding: 10px; margin: 10px 0; border: 1px solid #dc3545;"></textarea>
        <button type="submit" name="action" value="reject" 
                style="background-color: #dc3545; color: white; padding: 10px 20px; border: none; border-radius: 3px; cursor: pointer; font-weight: bold;">
            ✕ REJECT
        </button>
    </div>
</form>
```

---

### 3. **Pending Bookings Visibility Issue**

**Current Issue:** YES, this is a **security/privacy concern**. Lines 1327-1346 show `db_pending_counts()` returns counts for ALL labs, but lines 1453+ show these are displayed on the public admin portal.

**Problems:**
- ✗ Anyone who finds the admin portal can see how many pending bookings each lab has
- ✗ Pending bookings contain user names, emails, and sample info
- ✗ No access control on who can VIEW pending bookings (only on who can APPROVE them)

**Solution - Add visibility control:**
```python
# In admin_lab() function (around line 1739):

@app.route("/admin/<lab_slug>")
def admin_lab(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    redir = require_admin(lab_slug)
    if redir: return redir
    
    # Only show pending bookings to the lab's admin
    all_bookings = db_list_bookings(lab_slug)
    
    # Option A: Filter out pending from public view (recommended)
    # bookings = [b for b in all_bookings if b['status'] != 'pending' or session.get('is_admin')]
    
    return render_template("admin.html", lab_slug=lab_slug,
                           lab_title=LABS[lab_slug]["title"],
                           bookings=all_bookings,
                           admin_username=session.get("admin_username",""))
```

**Also add to admin_portal():**
```python
@app.get("/admin")
def admin_portal():
    # Check if user is logged in as an admin
    if not session.get("is_admin"):
        return redirect(url_for("admin_login_lab", lab_slug="furnace", 
                               next=url_for("admin_portal")))
    
    pending = db_pending_counts()
    # ... rest of code
```

---

## 🟡 IMPORTANT IMPROVEMENTS (Priority 2)

### Security & Access Control

**Issue 1: Admin Login Required Per-Lab (Line 1700)**
- ✗ You must login separately to each lab's admin panel
- ✗ No central admin dashboard for someone managing multiple labs
- ✓ **Suggestion:** Add a "master admin" role that can see all labs. Store admin role in session:
```python
session["admin_level"] = "master"  # or "lab-specific"
session["accessible_labs"] = ["furnace", "xps"]  # if lab-specific
```

**Issue 2: Email Verification Missing**
- ✗ Anyone can book with any email address (no verification)
- ✗ Users could book under someone else's email
- ✓ **Suggestion:** Send confirmation email with unique token before booking is "confirmed"

**Issue 3: No Rate Limiting**
- ✗ Users could spam bookings repeatedly
- ✓ **Suggestion:** Add rate limiting (max 5 bookings per email per day)

---

### Data Quality & Validation

**Issue 4: Optional Fields Not Validated (Lines 820-850)**
- ✗ Many fields are optional (nanomaterial_type, melting_point, etc.) but critical for sample tracking
- ✗ No warning when user leaves important fields empty
- ✓ **Suggestion:** Add soft warnings for empty lab-specific fields:
```python
warnings = []
if lab_slug == "furnace" and not payload.get("nanomaterial_type"):
    warnings.append("⚠ Nanomaterial type not specified")
# Show warnings but allow booking
```

**Issue 5: Date/Time Edge Cases**
- ✗ No check for bookings in the past
- ✗ No check for bookings on weekends (if not allowed)
- ✓ **Suggestion:** Add validation:
```python
def check_booking_rules(lab_slug: str, d: str, s: str, e: str) -> List[str]:
    errors = []
    booking_date = parse_date(d)
    today = datetime.now(TZ).date()
    
    # Past dates
    if booking_date and booking_date < today:
        errors.append("Cannot book in the past.")
    
    # Weekends
    if booking_date and booking_date.weekday() >= 5:
        errors.append("Bookings are only available Monday–Friday.")
    
    return errors
```

---

### User Experience

**Issue 6: No Booking Confirmation Number**
- ✗ Users get email but no reference number to check status
- ✓ **Suggestion:** Display booking confirmation with unique ID when submitted:
```python
flash(f"✓ Booking confirmed! Ref: {booking_id}. Check your email for details.", "success")
```

**Issue 7: Pending Status Not Explained to Users**
- ✗ Email says "Your request is pending review" but doesn't say when they'll hear back
- ✓ **Suggestion:** Add SLA to email:
```
"Your booking is pending approval. Lab admin will review within 24 hours."
```

**Issue 8: No Booking Cancellation by User**
- ✗ Only admins can delete bookings
- ✗ Users can't cancel their own approved bookings
- ✓ **Suggestion:** Add self-cancellation with token (you already store `cancel_token`):
```python
@app.route("/booking/cancel/<token>", methods=["GET","POST"])
def user_cancel_booking(token: str):
    b = db_get_booking_by_cancel_token(token)
    if not b: abort(404)
    if request.method == "POST":
        db_set_booking_status(b["id"], "cancelled", cancelled_at=datetime.now(TZ))
        notify_admin_cancellation(b)
        flash("Booking cancelled. Admin notified.", "success")
        return redirect(url_for("index"))
    return render_template("cancel_booking.html", booking=b)
```

---

### Data Export & Reporting

**Issue 9: CSV Export Doesn't Filter by Status**
- ✗ Exporting all bookings mixes approved, rejected, and pending
- ✓ **Suggestion:** Add filter options to export:
```python
@app.route("/admin/<lab_slug>/export")
def admin_export_bookings(lab_slug: str):
    status = request.args.get("status", "approved")  # default to approved
    bookings = db_list_bookings(lab_slug, status=status)
    # ... generate CSV
```

**Issue 10: No Audit Trail for Admin Actions**
- ✗ No record of who approved/rejected what or when
- ✓ **Suggestion:** Add `audit_log` table:
```sql
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    admin_username TEXT,
    action TEXT,  -- 'approve', 'reject', 'delete'
    booking_id INTEGER,
    reason TEXT,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 🟢 NICE-TO-HAVE IMPROVEMENTS (Priority 3)

### 1. **Booking Reminders**
- Add a job to send reminder emails 24h before approved booking
- You already have `reminder_sent` column but it's not used
- Use APScheduler to run periodically

### 2. **Availability Calendar View**
- Current availability list is text-based
- Add visual calendar (FullCalendar.js) showing free/booked slots per day

### 3. **Recurring Bookings**
- Some labs might have recurring needs
- Add option to book same slot weekly/monthly

### 4. **Admin Notifications**
- Admin gets new booking email but no way to see all pending at once
- Add dashboard widget showing "5 pending approvals this week"

### 5. **User Dashboard**
- Current users have no way to see their own booking history
- Add `/my-bookings` page showing their approved/rejected/pending bookings

### 6. **Lab Capacity Planning**
- No data on which labs are most booked
- Add analytics dashboard showing booking trends by lab

### 7. **Chemical/Equipment Maintenance**
- No mechanism to block bookings during maintenance windows
- Add "maintenance mode" for each lab that blocks new bookings

---

## Code Quality Notes

### ✓ What's Good:
1. **Separation of concerns** - DB logic, business logic, templates are separate
2. **Parameterized queries** - Protection against SQL injection
3. **Token-based approval/cancellation** - Secure links without login
4. **Multi-database support** - Works with both PostgreSQL and SQLite
5. **Async email sending** - Doesn't block requests

### ⚠ What Could Improve:
1. **Function naming** - Some functions are long (e.g., `notify_admin_new_booking`)
2. **Magic strings** - Status values ("pending", "approved") hardcoded in many places
3. **No logging** - Errors silently caught with `except Exception: pass`
4. **No type hints** - Only partial type hints; full typing would catch bugs
5. **Database migrations** - No version control for schema changes

---

## Implementation Priority

**Week 1 (Critical):**
1. ✅ Change #1: Oven time slots (2 slots only)
2. ✅ Change #2: Admin approve/reject buttons
3. ✅ Change #3: Hide pending from non-admin users

**Week 2 (Important):**
1. Email verification for bookings
2. Admin master login for all labs
3. Add booking confirmation numbers
4. User cancellation feature

**Month 2+ (Nice-to-have):**
1. Analytics dashboard
2. Availability calendar view
3. Reminders/notifications system

---

## Questions to Consider

1. **How many concurrent admins** do you have per lab? Should they have a shared inbox?
2. **Should pending bookings be visible** to the user who made the booking? (Probably yes)
3. **Do you want email reminders** before scheduled bookings?
4. **Should users be able to edit** their own pending bookings?
5. **Is there a booking deadline** (e.g., 24h before you must approve/reject)?

