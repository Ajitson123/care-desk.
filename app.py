import csv
import io
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '').lower() == 'true',
    MAX_CONTENT_LENGTH=1_048_576,
)


def validate_production_configuration():
    if os.environ.get('APP_ENV') != 'production':
        return
    if not os.environ.get('SECRET_KEY'):
        raise RuntimeError('Production requires a stable SECRET_KEY from a secret manager.')
    if os.environ.get('COOKIE_SECURE', '').lower() != 'true':
        raise RuntimeError('Production requires COOKIE_SECURE=true behind HTTPS.')
    raise RuntimeError(
        'Production is disabled until the verified per-hospital PostgreSQL backend '
        'and central account/secret registry are implemented.'
    )


validate_production_configuration()
DATABASE = Path(os.environ.get('DATABASE_PATH', Path(__file__).with_name('appointments.db')))
TRIAL_DAYS = 30


def _trial_period():
    started_at = int(datetime.now(timezone.utc).timestamp())
    return started_at, started_at + TRIAL_DAYS * 24 * 60 * 60


def _trial_is_active(trial_ends_at, now=None):
    if trial_ends_at is None:
        return False
    current_time = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    return int(trial_ends_at) > current_time


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 10000')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _hospital_trial_active(hospital_id):
    if hospital_id is None:
        return False
    with database() as connection:
        hospital = connection.execute(
            'SELECT trial_ends_at FROM hospitals WHERE id = ?', (hospital_id,)
        ).fetchone()
    return bool(hospital and _trial_is_active(hospital['trial_ends_at']))


def _add_column(connection, table, name, definition):
    columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
    if name not in columns:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS hospitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                staff_email TEXT UNIQUE,
                password_hash TEXT,
                trial_started_at INTEGER,
                trial_ends_at INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                UNIQUE(hospital_id, name)
            );
            CREATE TABLE IF NOT EXISTS doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                department_id INTEGER NOT NULL REFERENCES departments(id),
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                email TEXT,
                password_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS doctor_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
                weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                slot_minutes INTEGER NOT NULL DEFAULT 30,
                UNIQUE(doctor_id, weekday, start_time)
            );
            CREATE TABLE IF NOT EXISTS doctor_day_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
                appointment_date TEXT NOT NULL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                slot_minutes INTEGER NOT NULL DEFAULT 30,
                UNIQUE(doctor_id, appointment_date)
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                department TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                hospital_id INTEGER REFERENCES hospitals(id),
                doctor_id INTEGER REFERENCES doctors(id),
                doctor_name TEXT,
                confirmation_code TEXT
            );
        ''')
        _add_column(connection, 'doctors', 'email', 'TEXT')
        _add_column(connection, 'doctors', 'password_hash', 'TEXT')
        _add_column(connection, 'hospitals', 'trial_started_at', 'INTEGER')
        _add_column(connection, 'hospitals', 'trial_ends_at', 'INTEGER')
        _add_column(connection, 'appointments', 'hospital_id', 'INTEGER REFERENCES hospitals(id)')
        _add_column(connection, 'appointments', 'doctor_id', 'INTEGER REFERENCES doctors(id)')
        _add_column(connection, 'appointments', 'doctor_name', 'TEXT')
        _add_column(connection, 'appointments', 'confirmation_code', 'TEXT')
        _add_column(connection, 'appointments', 'patient_id', 'INTEGER REFERENCES patients(id)')
        trial_started_at, trial_ends_at = _trial_period()
        connection.execute(
            '''UPDATE hospitals SET trial_started_at = ?, trial_ends_at = ?
               WHERE staff_email IS NOT NULL AND password_hash IS NOT NULL AND trial_ends_at IS NULL''',
            (trial_started_at, trial_ends_at),
        )
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_doctor_email ON doctors(email) WHERE email IS NOT NULL')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_doctor_appointment_slot ON appointments(doctor_id, date, time) WHERE doctor_id IS NOT NULL')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_appointment_confirmation ON appointments(confirmation_code) WHERE confirmation_code IS NOT NULL')
        hospitals = connection.execute('SELECT id FROM hospitals').fetchall()
        if len(hospitals) == 1:
            connection.execute(
                'UPDATE appointments SET hospital_id = ? WHERE hospital_id IS NULL',
                (hospitals[0]['id'],),
            )


init_db()


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


@app.before_request
def validate_csrf():
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        submitted = request.form.get('csrf_token', '')
        if not expected or not secrets.compare_digest(expected, submitted):
            abort(400, description='The form expired. Reload the page and try again.')


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    if not request.path.startswith('/static/') and request.path != '/service-worker.js':
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.context_processor
def inject_template_helpers():
    return {'csrf_token': csrf_token}


def hospital_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('account_type') != 'hospital_admin':
            flash('Sign in with your hospital account to continue.', 'error')
            return redirect(url_for('hospital_login'))
        return view(*args, **kwargs)
    return wrapped


def doctor_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('account_type') != 'doctor':
            flash('Sign in with your doctor account to continue.', 'error')
            return redirect(url_for('hospital_login'))
        return view(*args, **kwargs)
    return wrapped


def patient_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('account_type') != 'patient':
            flash('Sign in or create a patient account to book an appointment.', 'error')
            return redirect(url_for('patient_signup'))
        return view(*args, **kwargs)
    return wrapped


def _parse_date(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_time(value):
    try:
        parsed = datetime.strptime(value, '%H:%M').time()
    except (TypeError, ValueError):
        return None
    return parsed if parsed.strftime('%H:%M') == value else None


def _valid_period(start_time, end_time, slot_minutes):
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    try:
        slot_minutes = int(slot_minutes)
    except (TypeError, ValueError):
        return False
    return bool(start and end and start < end and 5 <= slot_minutes <= 240)


def _available_slots(connection, doctor_id, appointment_date):
    day_override = connection.execute(
        'SELECT * FROM doctor_day_overrides WHERE doctor_id = ? AND appointment_date = ?',
        (doctor_id, appointment_date.isoformat()),
    ).fetchone()
    if day_override and day_override['is_closed']:
        return []

    if day_override:
        schedules = [day_override]
    else:
        schedules = connection.execute(
            'SELECT start_time, end_time, slot_minutes FROM doctor_schedules WHERE doctor_id = ? AND weekday = ?',
            (doctor_id, appointment_date.weekday()),
        ).fetchall()

    occupied = {
        row['time'] for row in connection.execute(
            'SELECT time FROM appointments WHERE doctor_id = ? AND date = ?',
            (doctor_id, appointment_date.isoformat()),
        )
    }
    current_time = datetime.now().strftime('%H:%M') if appointment_date == date.today() else None
    slots = set()
    for schedule in schedules:
        start = _parse_time(schedule['start_time'])
        end = _parse_time(schedule['end_time'])
        slot_minutes = schedule['slot_minutes']
        if not start or not end or start >= end or not 5 <= slot_minutes <= 240:
            continue
        current = start.hour * 60 + start.minute
        end_minute = end.hour * 60 + end.minute
        while current + slot_minutes <= end_minute:
            slot = f'{current // 60:02d}:{current % 60:02d}'
            if slot not in occupied and (current_time is None or slot > current_time):
                slots.add(slot)
            current += slot_minutes
    return sorted(slots)


def _hospital_slug(name):
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug or f'hospital-{secrets.token_hex(3)}'


def _import_roster(connection, hospital_id, uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        raise ValueError('Choose a CSV file to import.')
    if Path(uploaded_file.filename).suffix.lower() != '.csv':
        raise ValueError('Roster imports must be CSV files.')
    content = uploaded_file.stream.read(1_048_577)
    if len(content) > 1_048_576:
        raise ValueError('The CSV file must be smaller than 1 MB.')
    try:
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    except UnicodeDecodeError as error:
        raise ValueError('Save the roster as a UTF-8 CSV file.') from error

    required = {'department', 'doctor_name', 'email', 'temporary_password', 'weekday', 'start_time', 'end_time', 'slot_minutes'}
    if not reader.fieldnames or not required.issubset({field.strip() for field in reader.fieldnames if field}):
        raise ValueError('The CSV needs department, doctor_name, email, temporary_password, weekday, start_time, end_time, and slot_minutes columns.')
    weekdays = {day.lower(): index for index, day in enumerate(('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'))}
    doctors = {}
    for row_number, row in enumerate(reader, start=2):
        values = {field: (row.get(field) or '').strip() for field in required}
        if not all(values.values()):
            raise ValueError(f'Row {row_number} has a missing value.')
        if len(doctors) >= 500 and values['email'].lower() not in doctors:
            raise ValueError('A roster can contain no more than 500 doctors.')
        email = values['email'].lower()
        if len(values['doctor_name']) > 120 or len(values['department']) > 80 or email.count('@') != 1:
            raise ValueError(f'Row {row_number} has an invalid doctor, department, or email.')
        if len(values['temporary_password']) < 10 or len(values['temporary_password']) > 128:
            raise ValueError(f'Row {row_number} needs a temporary password between 10 and 128 characters.')
        weekday_text = values['weekday'].lower()
        if weekday_text in weekdays:
            weekday = weekdays[weekday_text]
        else:
            try:
                weekday = int(weekday_text)
            except ValueError as error:
                raise ValueError(f'Row {row_number} has an invalid weekday.') from error
        slot_minutes = values['slot_minutes']
        if weekday not in range(7) or not _valid_period(values['start_time'], values['end_time'], slot_minutes):
            raise ValueError(f'Row {row_number} has an invalid weekday, OPD time, or slot length.')

        doctor = doctors.setdefault(email, {
            'name': values['doctor_name'],
            'department': values['department'],
            'password': values['temporary_password'],
            'schedules': set(),
        })
        if (doctor['name'], doctor['department'], doctor['password']) != (
            values['doctor_name'], values['department'], values['temporary_password']
        ):
            raise ValueError(f'Rows for {email} must use the same name, department, and temporary password.')
        schedule_key = (weekday, values['start_time'], values['end_time'], int(slot_minutes))
        if any(existing[0] == weekday and existing[1] == values['start_time'] for existing in doctor['schedules']):
            raise ValueError(f'Row {row_number} repeats a weekly OPD time for {email}.')
        doctor['schedules'].add(schedule_key)

    if not doctors:
        raise ValueError('The CSV does not contain any doctor rows.')
    for email in doctors:
        if connection.execute(
            'SELECT 1 FROM doctors WHERE email = ? UNION SELECT 1 FROM hospitals WHERE staff_email = ?',
            (email, email),
        ).fetchone():
            raise ValueError(f'{email} already belongs to an account.')

    department_ids = {}
    for doctor in doctors.values():
        department_name = doctor['department']
        if department_name.lower() not in department_ids:
            existing_department = connection.execute(
                'SELECT id FROM departments WHERE hospital_id = ? AND name = ? COLLATE NOCASE',
                (hospital_id, department_name),
            ).fetchone()
            if existing_department:
                department_ids[department_name.lower()] = existing_department['id']
            else:
                cursor = connection.execute(
                    'INSERT INTO departments (hospital_id, name) VALUES (?, ?)',
                    (hospital_id, department_name),
                )
                department_ids[department_name.lower()] = cursor.lastrowid
        cursor = connection.execute(
            'INSERT INTO doctors (hospital_id, department_id, name, email, password_hash) VALUES (?, ?, ?, ?, ?)',
            (hospital_id, department_ids[department_name.lower()], doctor['name'], email,
             generate_password_hash(doctor['password'])),
        )
        doctor_id = cursor.lastrowid
        for weekday, start_time, end_time, slot_minutes in sorted(doctor['schedules']):
            connection.execute(
                'INSERT INTO doctor_schedules (doctor_id, weekday, start_time, end_time, slot_minutes) VALUES (?, ?, ?, ?, ?)',
                (doctor_id, weekday, start_time, end_time, slot_minutes),
            )
    return len(doctors), sum(len(doctor['schedules']) for doctor in doctors.values())


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/patient/signup', methods=['GET', 'POST'])
def patient_signup():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not name or len(name) > 120 or not phone or len(phone) > 40 or email.count('@') != 1 or len(password) < 10:
            flash('Enter your name, contact number, valid email, and a password of at least 10 characters.', 'error')
            return render_template('patient_auth.html', mode='signup')

        with database() as connection:
            if connection.execute('SELECT 1 FROM patients WHERE email = ?', (email,)).fetchone():
                flash('That email already has a patient account. Sign in instead.', 'error')
                return render_template('patient_auth.html', mode='signup')
            cursor = connection.execute(
                'INSERT INTO patients (name, phone, email, password_hash) VALUES (?, ?, ?, ?)',
                (name, phone, email, generate_password_hash(password)),
            )
            patient_id = cursor.lastrowid

        session.clear()
        session.update(account_type='patient', patient_id=patient_id)
        flash('Patient account created. You can now book an appointment.', 'success')
        return redirect(url_for('appointment'))
    return render_template('patient_auth.html', mode='signup')


@app.route('/patient/login', methods=['GET', 'POST'])
def patient_login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        with database() as connection:
            patient = connection.execute(
                'SELECT id, password_hash FROM patients WHERE email = ?', (email,)
            ).fetchone()
        if patient and check_password_hash(patient['password_hash'], password):
            session.clear()
            session.update(account_type='patient', patient_id=patient['id'])
            return redirect(url_for('appointment'))
        flash('Email or password was not recognized.', 'error')
    return render_template('patient_auth.html', mode='login')


@app.route('/patient/logout', methods=['POST'])
def patient_logout():
    if session.get('account_type') != 'patient':
        abort(403)
    session.clear()
    flash('You have signed out.', 'success')
    return redirect(url_for('home'))


@app.route('/hospital/signup', methods=['GET', 'POST'])
def hospital_signup():
    if request.method == 'POST':
        name = request.form.get('hospital_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        claim_code = request.form.get('claim_code', '')
        trial_started_at, trial_ends_at = _trial_period()
        if not name or len(name) > 120 or '@' not in email or len(password) < 10:
            flash('Enter a hospital name, valid email, and password of at least 10 characters.', 'error')
            return render_template('hospital_auth.html', mode='signup')

        with database() as connection:
            existing = connection.execute(
                'SELECT * FROM hospitals WHERE name = ? COLLATE NOCASE', (name,)
            ).fetchone()
            email_used = connection.execute(
                'SELECT 1 FROM hospitals WHERE staff_email = ? UNION SELECT 1 FROM doctors WHERE email = ?',
                (email, email),
            ).fetchone()
            if email_used:
                flash('That email is already connected to an account.', 'error')
                return render_template('hospital_auth.html', mode='signup')

            if existing and not existing['staff_email'] and not existing['password_hash']:
                expected_claim_code = os.environ.get('HOSPITAL_CLAIM_CODE', '')
                if not expected_claim_code or not secrets.compare_digest(expected_claim_code, claim_code):
                    flash('This existing hospital workspace requires a setup code from the platform owner.', 'error')
                    return render_template('hospital_auth.html', mode='signup')
                hospital_id = existing['id']
                connection.execute(
                    '''UPDATE hospitals SET staff_email = ?, password_hash = ?,
                       trial_started_at = ?, trial_ends_at = ? WHERE id = ?''',
                    (email, generate_password_hash(password), trial_started_at, trial_ends_at, hospital_id),
                )
            elif existing:
                flash('A hospital with that name is already registered. Sign in or contact support.', 'error')
                return render_template('hospital_auth.html', mode='signup')
            else:
                base_slug = _hospital_slug(name)
                slug = base_slug
                while connection.execute('SELECT 1 FROM hospitals WHERE slug = ?', (slug,)).fetchone():
                    slug = f'{base_slug}-{secrets.token_hex(2)}'
                cursor = connection.execute(
                    '''INSERT INTO hospitals
                       (name, slug, staff_email, password_hash, trial_started_at, trial_ends_at)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (name, slug, email, generate_password_hash(password), trial_started_at, trial_ends_at),
                )
                hospital_id = cursor.lastrowid

        session.clear()
        session.update(account_type='hospital_admin', hospital_id=hospital_id)
        flash('Hospital account created. Your free 30-day trial has started. Add departments, doctors, and weekly hours to get started.', 'success')
        return redirect(url_for('staff_dashboard'))
    return render_template('hospital_auth.html', mode='signup')


@app.route('/hospital/login', methods=['GET', 'POST'])
def hospital_login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        account = None
        account_type = None
        with database() as connection:
            hospital = connection.execute(
                'SELECT id, password_hash FROM hospitals WHERE staff_email = ?', (email,)
            ).fetchone()
            if hospital and hospital['password_hash'] and check_password_hash(hospital['password_hash'], password):
                account = {'hospital_id': hospital['id']}
                account_type = 'hospital_admin'
            else:
                doctor = connection.execute(
                    'SELECT id, hospital_id, password_hash FROM doctors WHERE email = ? AND active = 1',
                    (email,),
                ).fetchone()
                if doctor and doctor['password_hash'] and check_password_hash(doctor['password_hash'], password):
                    account = {'hospital_id': doctor['hospital_id'], 'doctor_id': doctor['id']}
                    account_type = 'doctor'

        if account:
            session.clear()
            session.update(account_type=account_type, **account)
            return redirect(url_for('doctor_dashboard' if account_type == 'doctor' else 'staff_dashboard'))
        flash('Email or password was not recognized.', 'error')
    return render_template('hospital_auth.html', mode='login')


@app.route('/hospital/logout', methods=['POST'])
def hospital_logout():
    session.clear()
    flash('You have signed out.', 'success')
    return redirect(url_for('home'))


@app.route('/staff/dashboard', methods=['GET', 'POST'])
@hospital_admin_required
def staff_dashboard():
    hospital_id = session['hospital_id']
    if request.method == 'POST':
        if not _hospital_trial_active(hospital_id):
            flash('Your free trial has ended. Existing records remain available, but setup changes and new bookings are paused.', 'error')
            return redirect(url_for('staff_dashboard'))
        action = request.form.get('action')
        try:
            with database() as connection:
                if action == 'import_roster':
                    try:
                        doctor_count, schedule_count = _import_roster(
                            connection, hospital_id, request.files.get('roster')
                        )
                    except ValueError as error:
                        flash(str(error), 'error')
                    else:
                        flash(f'Imported {doctor_count} doctors and {schedule_count} weekly OPD blocks.', 'success')
                elif action == 'add_department':
                    name = request.form.get('name', '').strip()
                    if not name or len(name) > 80:
                        flash('Enter a department name up to 80 characters.', 'error')
                    else:
                        connection.execute(
                            'INSERT INTO departments (hospital_id, name) VALUES (?, ?)',
                            (hospital_id, name),
                        )
                        flash('Department added.', 'success')
                elif action == 'add_doctor':
                    name = request.form.get('name', '').strip()
                    email = request.form.get('email', '').strip().lower()
                    password = request.form.get('password', '')
                    department_id = request.form.get('department_id', type=int)
                    department = connection.execute(
                        'SELECT id FROM departments WHERE id = ? AND hospital_id = ?',
                        (department_id, hospital_id),
                    ).fetchone()
                    email_used = connection.execute(
                        'SELECT 1 FROM doctors WHERE email = ? UNION SELECT 1 FROM hospitals WHERE staff_email = ?',
                        (email, email),
                    ).fetchone()
                    if not name or len(name) > 120 or not department or '@' not in email or len(password) < 10:
                        flash('Enter a doctor, department, valid email, and password of at least 10 characters.', 'error')
                    elif email_used:
                        flash('That email is already connected to an account.', 'error')
                    else:
                        connection.execute(
                            'INSERT INTO doctors (hospital_id, department_id, name, email, password_hash) VALUES (?, ?, ?, ?, ?)',
                            (hospital_id, department_id, name, email, generate_password_hash(password)),
                        )
                        flash('Doctor added. Share their sign-in details securely.', 'success')
                elif action == 'add_weekly_schedule':
                    doctor_id = request.form.get('doctor_id', type=int)
                    weekday = request.form.get('weekday', type=int)
                    start_time = request.form.get('start_time', '')
                    end_time = request.form.get('end_time', '')
                    slot_minutes = request.form.get('slot_minutes', type=int)
                    doctor = connection.execute(
                        'SELECT id FROM doctors WHERE id = ? AND hospital_id = ? AND active = 1',
                        (doctor_id, hospital_id),
                    ).fetchone()
                    if not doctor or weekday not in range(7) or not _valid_period(start_time, end_time, slot_minutes):
                        flash('Choose a doctor, weekday, and valid OPD hours.', 'error')
                    else:
                        connection.execute(
                            'INSERT INTO doctor_schedules (doctor_id, weekday, start_time, end_time, slot_minutes) VALUES (?, ?, ?, ?, ?)',
                            (doctor_id, weekday, start_time, end_time, slot_minutes),
                        )
                        flash('Weekly OPD hours added.', 'success')
                elif action == 'set_day_override':
                    doctor_id = request.form.get('doctor_id', type=int)
                    appointment_date = _parse_date(request.form.get('appointment_date'))
                    is_closed = request.form.get('is_closed') == '1'
                    start_time = request.form.get('start_time', '00:00') or '00:00'
                    end_time = request.form.get('end_time', '00:00') or '00:00'
                    slot_minutes = request.form.get('slot_minutes', type=int) or 30
                    doctor = connection.execute(
                        'SELECT id FROM doctors WHERE id = ? AND hospital_id = ? AND active = 1',
                        (doctor_id, hospital_id),
                    ).fetchone()
                    valid_override = is_closed or _valid_period(start_time, end_time, slot_minutes)
                    if not doctor or not appointment_date or appointment_date < date.today() or not valid_override:
                        flash('Choose a doctor, a current or future date, and valid hours.', 'error')
                    else:
                        connection.execute('''
                            INSERT INTO doctor_day_overrides
                                (doctor_id, appointment_date, is_closed, start_time, end_time, slot_minutes)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(doctor_id, appointment_date) DO UPDATE SET
                                is_closed = excluded.is_closed,
                                start_time = excluded.start_time,
                                end_time = excluded.end_time,
                                slot_minutes = excluded.slot_minutes
                        ''', (doctor_id, appointment_date.isoformat(), int(is_closed), start_time, end_time, slot_minutes))
                        flash('That day’s OPD schedule was updated. Existing bookings stay unchanged; contact affected patients directly.', 'success')
                elif action == 'deactivate_doctor':
                    doctor_id = request.form.get('doctor_id', type=int)
                    connection.execute(
                        'UPDATE doctors SET active = 0 WHERE id = ? AND hospital_id = ?',
                        (doctor_id, hospital_id),
                    )
                    flash('Doctor deactivated; their schedule is no longer bookable.', 'success')
                else:
                    flash('Choose a valid dashboard action.', 'error')
        except sqlite3.IntegrityError:
            flash('That department or schedule already exists, or the account email is already in use.', 'error')
        return redirect(url_for('staff_dashboard'))

    with database() as connection:
        hospital = connection.execute('SELECT * FROM hospitals WHERE id = ?', (hospital_id,)).fetchone()
        departments = connection.execute(
            'SELECT * FROM departments WHERE hospital_id = ? ORDER BY name', (hospital_id,)
        ).fetchall()
        doctors = connection.execute('''
            SELECT d.id, d.name, d.email, d.active, dep.name AS department_name
            FROM doctors d JOIN departments dep ON dep.id = d.department_id
            WHERE d.hospital_id = ? ORDER BY d.active DESC, d.name
        ''', (hospital_id,)).fetchall()
        schedules = connection.execute('''
            SELECT s.*, d.name AS doctor_name
            FROM doctor_schedules s JOIN doctors d ON d.id = s.doctor_id
            WHERE d.hospital_id = ? ORDER BY d.name, s.weekday, s.start_time
        ''', (hospital_id,)).fetchall()
        overrides = connection.execute('''
            SELECT o.*, d.name AS doctor_name
            FROM doctor_day_overrides o JOIN doctors d ON d.id = o.doctor_id
            WHERE d.hospital_id = ? AND o.appointment_date >= ?
            ORDER BY o.appointment_date, d.name LIMIT 50
        ''', (hospital_id, date.today().isoformat())).fetchall()
        appointments = connection.execute('''
            SELECT a.id, a.name, a.phone, a.department, a.date, a.time,
                   COALESCE(a.doctor_name, d.name, 'Not assigned') AS doctor_name,
                   a.confirmation_code
            FROM appointments a LEFT JOIN doctors d ON d.id = a.doctor_id
            WHERE a.hospital_id = ?
            ORDER BY a.date DESC, a.time DESC LIMIT 200
        ''', (hospital_id,)).fetchall()
        total_appointments = connection.execute(
            'SELECT COUNT(*) FROM appointments WHERE hospital_id = ?', (hospital_id,)
        ).fetchone()[0]
        total_patients = connection.execute(
            'SELECT COUNT(DISTINCT phone) FROM appointments WHERE hospital_id = ?', (hospital_id,)
        ).fetchone()[0]
        today_count = connection.execute(
            'SELECT COUNT(*) FROM appointments WHERE hospital_id = ? AND date = ?',
            (hospital_id, date.today().isoformat()),
        ).fetchone()[0]
    trial_active = _trial_is_active(hospital['trial_ends_at'])
    trial_end_date = (
        datetime.fromtimestamp(hospital['trial_ends_at'], timezone.utc).date().isoformat()
        if hospital['trial_ends_at'] else None
    )
    return render_template(
        'appointments.html', hospital=hospital, departments=departments, doctors=doctors,
        schedules=schedules, overrides=overrides, appointments=appointments,
        total_appointments=total_appointments, total_patients=total_patients, today_count=today_count,
        today=date.today().isoformat(), trial_active=trial_active, trial_end_date=trial_end_date,
    )


@app.route('/doctor/dashboard', methods=['GET', 'POST'])
@doctor_required
def doctor_dashboard():
    doctor_id = session['doctor_id']
    if request.method == 'POST':
        if not _hospital_trial_active(session.get('hospital_id')):
            flash('Your hospital free trial has ended. Existing records remain available, but OPD changes and new bookings are paused.', 'error')
            return redirect(url_for('doctor_dashboard'))
        appointment_date = _parse_date(request.form.get('appointment_date'))
        is_closed = request.form.get('is_closed') == '1'
        start_time = request.form.get('start_time', '00:00') or '00:00'
        end_time = request.form.get('end_time', '00:00') or '00:00'
        slot_minutes = request.form.get('slot_minutes', type=int) or 30
        if not appointment_date or appointment_date < date.today() or (not is_closed and not _valid_period(start_time, end_time, slot_minutes)):
            flash('Choose a current or future date and valid OPD hours.', 'error')
            return redirect(url_for('doctor_dashboard'))
        with database() as connection:
            connection.execute('''
                INSERT INTO doctor_day_overrides
                    (doctor_id, appointment_date, is_closed, start_time, end_time, slot_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(doctor_id, appointment_date) DO UPDATE SET
                    is_closed = excluded.is_closed,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    slot_minutes = excluded.slot_minutes
            ''', (doctor_id, appointment_date.isoformat(), int(is_closed), start_time, end_time, slot_minutes))
        flash('Your OPD schedule was updated. Existing bookings stay unchanged; contact affected patients directly.', 'success')
        return redirect(url_for('doctor_dashboard'))

    with database() as connection:
        doctor = connection.execute('''
            SELECT d.*, dep.name AS department_name, h.name AS hospital_name
            FROM doctors d JOIN departments dep ON dep.id = d.department_id
            JOIN hospitals h ON h.id = d.hospital_id WHERE d.id = ? AND d.active = 1
        ''', (doctor_id,)).fetchone()
        if not doctor:
            session.clear()
            return redirect(url_for('hospital_login'))
        schedules = connection.execute(
            'SELECT * FROM doctor_schedules WHERE doctor_id = ? ORDER BY weekday, start_time',
            (doctor_id,),
        ).fetchall()
        overrides = connection.execute(
            'SELECT * FROM doctor_day_overrides WHERE doctor_id = ? AND appointment_date >= ? ORDER BY appointment_date LIMIT 30',
            (doctor_id, date.today().isoformat()),
        ).fetchall()
        appointments = connection.execute(
            'SELECT name, department, date, time FROM appointments WHERE doctor_id = ? AND date >= ? ORDER BY date, time LIMIT 100',
            (doctor_id, date.today().isoformat()),
        ).fetchall()
    return render_template(
        'doctor_dashboard.html', doctor=doctor, schedules=schedules, overrides=overrides,
        appointments=appointments, today=date.today().isoformat(),
        trial_active=_hospital_trial_active(session.get('hospital_id')),
    )


@app.route('/patient/dashboard', methods=['GET', 'POST'])
@app.route('/appointment', methods=['GET', 'POST'])
@patient_required
def appointment():
    if request.method == 'POST':
        with database() as connection:
            patient = connection.execute(
                'SELECT id, name, phone FROM patients WHERE id = ?', (session.get('patient_id'),)
            ).fetchone()
        if not patient:
            session.clear()
            flash('Sign in or create a patient account to book an appointment.', 'error')
            return redirect(url_for('patient_signup'))
        name = patient['name']
        phone = patient['phone']
        hospital_id = request.form.get('hospital_id', type=int)
        department_id = request.form.get('department_id', type=int)
        doctor_id = request.form.get('doctor_id', type=int)
        appointment_date = _parse_date(request.form.get('date'))
        appointment_time = request.form.get('time', '')
        if not name or len(name) > 120 or not phone or len(phone) > 40 or not appointment_date or appointment_date < date.today():
            flash('Enter your name and contact number, then choose a valid future appointment date.', 'error')
            return redirect(url_for('appointment'))

        with database() as connection:
            now = int(datetime.now(timezone.utc).timestamp())
            doctor = connection.execute('''
                SELECT d.id, d.name, d.hospital_id, d.department_id, dep.name AS department_name
                FROM doctors d JOIN departments dep ON dep.id = d.department_id
                WHERE d.id = ? AND d.hospital_id = ? AND d.department_id = ? AND d.active = 1
            ''', (doctor_id, hospital_id, department_id)).fetchone()
            hospital = connection.execute('''
                SELECT id FROM hospitals
                                WHERE id = ? AND staff_email IS NOT NULL AND password_hash IS NOT NULL
                                    AND trial_ends_at > ?
                        ''', (hospital_id, now)).fetchone()
            if not doctor or not hospital or appointment_time not in _available_slots(connection, doctor_id, appointment_date):
                flash('That appointment time is not available. Choose another open time.', 'error')
                return redirect(url_for('appointment'))
            confirmation_code = secrets.token_hex(5).upper()
            try:
                connection.execute('''
                    INSERT INTO appointments
                        (name, phone, department, date, time, hospital_id, doctor_id, doctor_name, confirmation_code, patient_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, phone, doctor['department_name'], appointment_date.isoformat(), appointment_time,
                      hospital_id, doctor_id, doctor['name'], confirmation_code, patient['id']))
            except sqlite3.IntegrityError:
                flash('That time was just booked. Please choose another slot.', 'error')
                return redirect(url_for('appointment'))
        return redirect(url_for('confirmation', code=confirmation_code))

    with database() as connection:
        now = int(datetime.now(timezone.utc).timestamp())
        patient = connection.execute(
            'SELECT name, phone, email FROM patients WHERE id = ?', (session.get('patient_id'),)
        ).fetchone()
        hospitals = connection.execute('''
            SELECT id, name, slug FROM hospitals
                        WHERE staff_email IS NOT NULL AND password_hash IS NOT NULL
                            AND trial_ends_at > ? ORDER BY name
                ''', (now,)).fetchall()
    return render_template('appointment.html', hospitals=hospitals, patient=patient, today=date.today().isoformat())


@app.get('/api/hospitals/<slug>/departments')
def hospital_departments(slug):
    with database() as connection:
        now = int(datetime.now(timezone.utc).timestamp())
        rows = connection.execute('''
            SELECT d.id, d.name FROM departments d JOIN hospitals h ON h.id = d.hospital_id
            WHERE h.slug = ? AND h.staff_email IS NOT NULL AND h.password_hash IS NOT NULL
              AND h.trial_ends_at > ?
            ORDER BY d.name
        ''', (slug, now)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get('/api/hospitals/<slug>/departments/<int:department_id>/doctors')
def department_doctors(slug, department_id):
    with database() as connection:
        now = int(datetime.now(timezone.utc).timestamp())
        rows = connection.execute('''
            SELECT d.id, d.name FROM doctors d
            JOIN hospitals h ON h.id = d.hospital_id
            WHERE h.slug = ? AND h.staff_email IS NOT NULL AND h.password_hash IS NOT NULL
              AND h.trial_ends_at > ? AND d.department_id = ? AND d.active = 1
            ORDER BY d.name
        ''', (slug, now, department_id)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get('/api/doctors/<int:doctor_id>/availability')
def doctor_availability(doctor_id):
    appointment_date = _parse_date(request.args.get('date'))
    if not appointment_date or appointment_date < date.today():
        return jsonify({'slots': []})
    with database() as connection:
        now = int(datetime.now(timezone.utc).timestamp())
        doctor = connection.execute('''
            SELECT d.id FROM doctors d JOIN hospitals h ON h.id = d.hospital_id
            WHERE d.id = ? AND d.active = 1
              AND h.staff_email IS NOT NULL AND h.password_hash IS NOT NULL
              AND h.trial_ends_at > ?
        ''', (doctor_id, now)).fetchone()
        slots = _available_slots(connection, doctor_id, appointment_date) if doctor else []
    return jsonify({'slots': slots})


@app.get('/healthz')
def health_check():
    return jsonify({'status': 'ok', 'database': str(DATABASE)})


@app.route('/confirmation/<code>')
def confirmation(code):
    with database() as connection:
        appointment_record = connection.execute('''
            SELECT a.name, a.department, a.date, a.time,
                   COALESCE(a.doctor_name, d.name, 'To be confirmed') AS doctor_name, h.name AS hospital_name
            FROM appointments a LEFT JOIN doctors d ON d.id = a.doctor_id
            LEFT JOIN hospitals h ON h.id = a.hospital_id
            WHERE a.confirmation_code = ?
        ''', (code,)).fetchone()
    if not appointment_record:
        abort(404)
    return render_template('confirmation.html', appointment=appointment_record, code=code)


@app.get('/service-worker.js')
def service_worker():
    response = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG') == '1',
    )
