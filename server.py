import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import qrcode
from qrcode.image.svg import SvgPathImage


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SKN_DATA_DIR", str(ROOT / "data"))).resolve()
DATABASE = DATA_DIR / "bookings.sqlite3"
UPI_ID = os.environ.get("SKN_UPI_ID", "chowkekarnitin@okaxis").strip()
BUSINESS_NAME = "Sadguru Krupa Network"
ADMIN_TOKEN = os.environ.get("SKN_ADMIN_TOKEN", "")
MAX_REQUEST_BYTES = 16_384
RATE_WINDOW_SECONDS = 60 * 60
RATE_LIMIT = 10
rate_lock = threading.Lock()
booking_attempts = {}

EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 ()-]{6,18}[0-9]$")
UPI_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,100}@[A-Za-z0-9.-]{2,50}$")
BOOKING_STATUSES = {"requested", "confirmed", "declined", "completed"}
PAYMENT_STATUSES = {"not_paid", "verification_pending", "paid"}


def connect_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def database_connection():
    connection = connect_database()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    with database_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                reference TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                preferred_datetime TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'requested',
                payment_status TEXT NOT NULL DEFAULT 'not_paid',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def upi_payment_url(reference):
    return (
        "upi://pay?"
        f"pa={quote(UPI_ID, safe='@.-_')}"
        f"&pn={quote(BUSINESS_NAME)}"
        "&cu=INR"
        f"&tn={quote('Visit request ' + reference)}"
    )


def valid_datetime(value):
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def allow_booking_request(client_ip):
    now = time.monotonic()
    with rate_lock:
        attempts = [
            timestamp
            for timestamp in booking_attempts.get(client_ip, [])
            if now - timestamp < RATE_WINDOW_SECONDS
        ]
        if len(attempts) >= RATE_LIMIT:
            booking_attempts[client_ip] = attempts
            return False
        attempts.append(now)
        booking_attempts[client_ip] = attempts
        return True


class BookingHandler(BaseHTTPRequestHandler):
    server_version = "SadguruNetwork/1.0"

    def log_message(self, format_string, *args):
        # Do not log request bodies or customer information.
        super().log_message(format_string, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request length.") from None
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request must be smaller than 16 KB.")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ValueError("Send booking details as JSON.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid JSON request.") from None
        if not isinstance(payload, dict):
            raise ValueError("Invalid booking details.")
        return payload

    def require_admin(self):
        if not ADMIN_TOKEN:
            self.send_json(503, {"error": "Admin access is not configured on this server."})
            return False
        expected = f"Bearer {ADMIN_TOKEN}"
        supplied = self.headers.get("Authorization", "")
        if not secrets.compare_digest(supplied, expected):
            self.send_json(401, {"error": "Admin sign-in required."})
            return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self.serve_file("index.html")
        elif parsed.path == "/admin":
            self.serve_file("admin.html")
        elif parsed.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif parsed.path == "/api/config":
            self.send_json(200, {"upi_id": UPI_ID, "business_name": BUSINESS_NAME})
        elif parsed.path == "/api/payment-qr.svg":
            self.serve_payment_qr(parse_qs(parsed.query).get("reference", [""])[0])
        elif parsed.path == "/api/admin/bookings":
            if not self.require_admin():
                return
            with database_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, reference, name, email, phone, address,
                           preferred_datetime, details, status, payment_status,
                           created_at, updated_at
                    FROM bookings ORDER BY created_at DESC LIMIT 500
                    """
                ).fetchall()
            self.send_json(200, {"bookings": [dict(row) for row in rows]})
        else:
            self.send_json(404, {"error": "Not found."})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/bookings":
            self.send_json(404, {"error": "Not found."})
            return
        client_ip = self.client_address[0]
        if not allow_booking_request(client_ip):
            self.send_json(429, {"error": "Too many requests. Please try again later."})
            return
        try:
            payload = self.read_json()
            fields = {
                key: payload.get(key, "").strip()
                if isinstance(payload.get(key, ""), str)
                else None
                for key in (
                    "name",
                    "email",
                    "phone",
                    "address",
                    "preferred_datetime",
                    "details",
                )
            }
            if any(value is None for value in fields.values()):
                raise ValueError("All booking fields must be text.")
            if not fields["name"] or len(fields["name"]) > 100:
                raise ValueError("Enter your name (up to 100 characters).")
            if len(fields["email"]) > 254 or not EMAIL_PATTERN.fullmatch(fields["email"]):
                raise ValueError("Enter a valid email address.")
            if len(fields["phone"]) > 20 or not PHONE_PATTERN.fullmatch(fields["phone"]):
                raise ValueError("Enter a valid phone number.")
            if not fields["address"] or len(fields["address"]) > 300:
                raise ValueError("Enter a service address (up to 300 characters).")
            if not valid_datetime(fields["preferred_datetime"]):
                raise ValueError("Choose a valid preferred visit date and time.")
            if len(fields["details"]) > 1000:
                raise ValueError("The service description must be under 1,000 characters.")
            if not UPI_PATTERN.fullmatch(UPI_ID):
                raise ValueError("The business UPI ID is not configured correctly.")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        booking_id = secrets.token_hex(16)
        reference = "SKN-" + secrets.token_hex(4).upper()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with database_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO bookings (
                        id, reference, name, email, phone, address,
                        preferred_datetime, details, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        booking_id,
                        reference,
                        fields["name"],
                        fields["email"],
                        fields["phone"],
                        fields["address"],
                        fields["preferred_datetime"],
                        fields["details"],
                        now,
                        now,
                    ),
                )
        except sqlite3.Error:
            self.send_json(500, {"error": "We couldn't save your request. Please call us."})
            return

        self.send_json(
            201,
            {
                "reference": reference,
                "upi_url": upi_payment_url(reference),
                "payment_qr_url": f"/api/payment-qr.svg?reference={quote(reference)}",
            },
        )

    def do_PATCH(self):
        parsed = urlsplit(self.path)
        prefix = "/api/admin/bookings/"
        if not parsed.path.startswith(prefix):
            self.send_json(404, {"error": "Not found."})
            return
        if not self.require_admin():
            return
        booking_id = parsed.path[len(prefix):]
        if not re.fullmatch(r"[a-f0-9]{32}", booking_id):
            self.send_json(400, {"error": "Invalid booking."})
            return
        try:
            payload = self.read_json()
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        status = payload.get("status")
        payment_status = payload.get("payment_status")
        if (
            not isinstance(status, str)
            or status not in BOOKING_STATUSES
            or not isinstance(payment_status, str)
            or payment_status not in PAYMENT_STATUSES
        ):
            self.send_json(400, {"error": "Choose valid booking and payment statuses."})
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with database_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bookings
                SET status = ?, payment_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, payment_status, now, booking_id),
            )
        if cursor.rowcount == 0:
            self.send_json(404, {"error": "Booking not found."})
            return
        self.send_json(200, {"ok": True})

    def serve_payment_qr(self, reference):
        if not re.fullmatch(r"SKN-[A-F0-9]{8}", reference):
            self.send_json(404, {"error": "Booking not found."})
            return
        with database_connection() as connection:
            row = connection.execute(
                "SELECT reference FROM bookings WHERE reference = ?", (reference,)
            ).fetchone()
        if row is None:
            self.send_json(404, {"error": "Booking not found."})
            return
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=7,
            border=4,
        )
        qr.add_data(upi_payment_url(reference))
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        self.send_text(200, "image/svg+xml; charset=utf-8", image.to_string())

    def serve_file(self, name):
        try:
            content = (ROOT / name).read_bytes()
        except OSError:
            self.send_json(404, {"error": "Page not found."})
            return
        self.send_text(200, "text/html; charset=utf-8", content)


def main():
    if not UPI_PATTERN.fullmatch(UPI_ID):
        raise SystemExit("Set SKN_UPI_ID to a valid UPI ID before starting the server.")
    if ADMIN_TOKEN and len(ADMIN_TOKEN) < 32:
        raise SystemExit("SKN_ADMIN_TOKEN must be at least 32 characters long.")
    initialize_database()
    host = os.environ.get("SKN_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("SKN_PORT", "8000")))
    server = ThreadingHTTPServer((host, port), BookingHandler)
    print(f"{BUSINESS_NAME} booking server listening on http://{host}:{port}")
    if not ADMIN_TOKEN:
        print("Admin access is disabled. Set SKN_ADMIN_TOKEN to enable /admin.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping booking server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
