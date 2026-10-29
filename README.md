# Sadguru Krupa Network bookings

This project includes a small Python/SQLite backend, the English business website,
UPI QR generation, and a token-protected admin page.

## Run locally on Windows

Install Python 3.10 or later, then open PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
$env:SKN_ADMIN_TOKEN = (py -c "import secrets; print(secrets.token_urlsafe(32))")
Write-Output $env:SKN_ADMIN_TOKEN
py server.py
```

Open `http://127.0.0.1:8000` for the website and
`http://127.0.0.1:8000/admin` for the booking dashboard. Keep the token printed
in the PowerShell console available for the browser sign-in. Booking data
is stored in `data/bookings.sqlite3`; keep this directory on persistent storage
when hosting the app.

The public UPI ID is configured as `chowkekarnitin@okaxis`. To change it, set
`SKN_UPI_ID` before starting the server. Set `SKN_DATA_DIR` to a persistent
directory if the hosting platform uses an ephemeral application filesystem.

## Prepare a free Render deployment

The `render.yaml` file creates a free Render web service, configures its web
port and health check, and asks Render to generate the admin token. To launch:

1. Push this project to a GitHub repository.
2. In Render, choose **New → Blueprint**, connect that repository, and deploy
   the `render.yaml` blueprint.
3. After the service is live, open its public URL. Open `/admin` and sign in
   using `SKN_ADMIN_TOKEN` from the service's **Environment** settings.

**Important:** this free setup has no persistent disk. SQLite bookings can be
lost whenever Render restarts or redeploys the service. Treat it as a preview,
not reliable storage for live customer bookings. Before accepting real
bookings, upgrade to a paid service with a persistent disk or move bookings to
a managed database, and configure backups. The app has not been published;
deployment still requires your Render and GitHub accounts.

## Payments and customer data

The QR code opens a UPI payment request without a preset amount. Confirm the
visit and charge with the customer before asking them to pay. This UPI setup
does not send payment webhooks or verify transactions automatically; check
your bank/UPI app and mark a payment as paid in the admin page only after
verifying receipt.

The booking form collects a name, email, phone number, service address,
preferred visit time, and optional service details. It does not collect
Aadhaar images, payment PINs, or identity documents. Booking requests are
limited per IP in each server process. The admin API requires the
`SKN_ADMIN_TOKEN` environment variable.

For a reliable public deployment, use a hosting provider with persistent
storage and HTTPS, set `SKN_ADMIN_TOKEN` to a long random secret in the hosting
dashboard, set `SKN_HOST` to `0.0.0.0`, and set `SKN_UPI_ID` there if needed.
Do not expose the admin token in website code or commit it to source control.
