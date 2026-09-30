# care.desk

Multi-hospital appointment and OPD scheduling prototype. Patients create an account to book appointments; hospital admins manage departments and doctors; doctors can change their own OPD for a particular date.

> **Not production-ready:** the current prototype stores every hospital's records in one SQLite file. It does not yet connect to hospital-owned databases. Do not expose it publicly or enter real patient information. `APP_ENV=production` deliberately refuses to start until the verified per-hospital PostgreSQL connector is implemented.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
python app.py
```

Open `http://127.0.0.1:5000`. The home page offers two separate registration paths: **Sign up as a patient** and **Register hospital**. Patients create an account and then book a visit. Hospital admins register and sign in to add departments, doctors, and weekly hours. Doctors sign in through the hospital portal.

New hospital registrations and verified workspace claims start a free 30-day trial. When it expires, staff can still view existing records, but workspace changes and new patient bookings are paused. Billing and trial extensions are not implemented yet.

## Container status

The Docker image is packaged for deployment, but production startup is intentionally blocked. This prototype still uses one shared SQLite database; do not expose it publicly or use real patient information. A verified per-hospital PostgreSQL connector, central account/secret registry, and operational safeguards must be implemented before a hosted production launch. The current Docker Compose configuration will fail closed until that backend exists.

To claim an existing unconfigured hospital, the platform owner must set `HOSPITAL_CLAIM_CODE` before starting the app and give the code privately to the verified hospital administrator. In PowerShell, generate a local code with `$env:HOSPITAL_CLAIM_CODE = (python -c "import secrets; print(secrets.token_urlsafe(32))")`, then display it with `$env:HOSPITAL_CLAIM_CODE` and enter it in the existing workspace setup-code field. New hospital workspaces do not need this code. Unclaimed workspaces are hidden from patient booking until claimed.

Signed-in patients select a hospital, department, doctor, date, and live available time. Their appointment is linked to their patient account. Accounts, bookings, and schedules are stored in `appointments.db` in this local prototype.

Hospital admins can add doctors individually or import a UTF-8 CSV roster. The import columns are `department,doctor_name,email,temporary_password,weekday,start_time,end_time,slot_minutes`; repeat a doctor's email, name, department, and temporary password for each weekly OPD block. Download the template from the dashboard. Temporary passwords are hashed immediately, but the CSV contains them in plain text, so keep it private and delete it after import.

## Android

This is a responsive Progressive Web App (PWA), not a native APK. Deploy it behind HTTPS, open its HTTPS URL in Android Chrome, then use Chrome's **Install app** or **Add to Home screen** menu. The service worker caches only static app assets; patient and staff pages always require a live connection and are not cached offline.

For production Android access, use a public HTTPS origin such as a managed VM, Render, Railway, or a reverse-proxied container behind a TLS certificate. Once the site is served over HTTPS, Android users can install it like a normal app and use it without needing a separate APK build.

For a device on the same network during development, set `HOST=0.0.0.0` before starting the server and open the computer's LAN address from the phone. HTTP on a LAN is for layout testing; Android PWA installation requires a secure HTTPS origin.

## Deployment notes

- The Android PWA needs a public HTTPS domain; localhost is not a public website and does not provide uptime guarantees.
- Recommended first hosting target for India: AWS Mumbai (`ap-south-1`) with ECS/Fargate, TLS at an Application Load Balancer, AWS Secrets Manager/KMS, strict outbound allowlists for verified hospital PostgreSQL hosts, health monitoring, and tested backups.
- Hospital-owned PostgreSQL endpoints must require TLS certificate verification, least-privilege database users, network allowlisting, and hospital-managed backups. Connection details must be added only through a verified operator workflow and stored encrypted; never accept them in a public signup form or browser storage.
- Public launch is blocked until the tenant database adapter, central account/secret registry, connection rotation, rate limiting, audit logging, recovery workflow, and operational monitoring are implemented and tested. Do not claim uninterrupted service or healthcare compliance until the hosting, hospital databases, incident response, and applicable India privacy requirements have been independently reviewed.

## Tests

```powershell
python -m unittest discover -s tests -v
```