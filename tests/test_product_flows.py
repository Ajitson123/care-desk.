import io
import sqlite3
import tempfile
import unittest
import os
from datetime import date, timedelta
from pathlib import Path

import app as product


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.original_database = product.DATABASE
        self.temporary_directory = tempfile.TemporaryDirectory()
        product.DATABASE = Path(self.temporary_directory.name) / 'test.sqlite3'
        product.init_db()
        product.app.config['TESTING'] = True
        self.client = product.app.test_client()

    def tearDown(self):
        product.DATABASE = self.original_database
        product.app.config['TESTING'] = False
        self.temporary_directory.cleanup()

    def csrf(self, client, path):
        client.get(path)
        with client.session_transaction() as session:
            return session['csrf_token']

    def query_value(self, statement, parameters=()):
        connection = sqlite3.connect(product.DATABASE)
        try:
            return connection.execute(statement, parameters).fetchone()[0]
        finally:
            connection.close()

    def register_hospital(self, client, name, email):
        token = self.csrf(client, '/hospital/signup')
        response = client.post('/hospital/signup', data={
            'csrf_token': token,
            'hospital_name': name,
            'email': email,
            'password': 'secure-passphrase-10',
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            return session['hospital_id']

    def register_patient(self, client, name='Sample Patient', email='patient@example.test'):
        token = self.csrf(client, '/patient/signup')
        response = client.post('/patient/signup', data={
            'csrf_token': token,
            'name': name,
            'phone': '5550100',
            'email': email,
            'password': 'patient-passphrase-10',
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            return session['patient_id']

    def test_hospital_doctor_booking_and_tenant_isolation(self):
        hospital_id = self.register_hospital(self.client, 'North Clinic', 'admin@north.test')

        token = self.csrf(self.client, '/staff/dashboard')
        self.client.post('/staff/dashboard', data={
            'csrf_token': token, 'action': 'add_department', 'name': 'Cardiology',
        })
        department_id = self.query_value(
            'SELECT id FROM departments WHERE hospital_id = ?', (hospital_id,)
        )

        token = self.csrf(self.client, '/staff/dashboard')
        self.client.post('/staff/dashboard', data={
            'csrf_token': token,
            'action': 'add_doctor',
            'name': 'Ada Rivers',
            'department_id': department_id,
            'email': 'ada@north.test',
            'password': 'doctor-passphrase-10',
        })
        doctor_id = self.query_value(
            'SELECT id FROM doctors WHERE hospital_id = ?', (hospital_id,)
        )

        appointment_date = date.today() + timedelta(days=1)
        token = self.csrf(self.client, '/staff/dashboard')
        self.client.post('/staff/dashboard', data={
            'csrf_token': token,
            'action': 'add_weekly_schedule',
            'doctor_id': doctor_id,
            'weekday': appointment_date.weekday(),
            'start_time': '09:00',
            'end_time': '10:00',
            'slot_minutes': '30',
        })

        slug = self.query_value('SELECT slug FROM hospitals WHERE id = ?', (hospital_id,))
        response = self.client.get(
            f'/api/hospitals/{slug}/departments/{department_id}/doctors'
        )
        self.assertEqual(response.json, [{'id': doctor_id, 'name': 'Ada Rivers'}])
        response = self.client.get(
            f'/api/doctors/{doctor_id}/availability?date={appointment_date.isoformat()}'
        )
        self.assertEqual(response.json['slots'], ['09:00', '09:30'])

        patient_client = product.app.test_client()
        patient_id = self.register_patient(patient_client)
        token = self.csrf(patient_client, '/patient/dashboard')
        booking = patient_client.post('/patient/dashboard', data={
            'csrf_token': token,
            'name': 'Sample Patient',
            'phone': '5550100',
            'hospital_id': hospital_id,
            'department_id': department_id,
            'doctor_id': doctor_id,
            'date': appointment_date.isoformat(),
            'time': '09:00',
        })
        self.assertEqual(booking.status_code, 302)
        confirmation = patient_client.get(booking.location)
        self.assertIn(b'Sample Patient', confirmation.data)
        self.assertIn(b'09:00', confirmation.data)
        self.assertEqual(self.query_value('SELECT patient_id FROM appointments LIMIT 1'), patient_id)

        doctor_client = product.app.test_client()
        token = self.csrf(doctor_client, '/hospital/login')
        login = doctor_client.post('/hospital/login', data={
            'csrf_token': token,
            'email': 'ada@north.test',
            'password': 'doctor-passphrase-10',
        })
        self.assertEqual(login.status_code, 302)
        self.assertIn('/doctor/dashboard', login.location)
        token = self.csrf(doctor_client, '/doctor/dashboard')
        doctor_client.post('/doctor/dashboard', data={
            'csrf_token': token,
            'appointment_date': appointment_date.isoformat(),
            'is_closed': '1',
        })
        response = doctor_client.get(
            f'/api/doctors/{doctor_id}/availability?date={appointment_date.isoformat()}'
        )
        self.assertEqual(response.json['slots'], [])

        other_client = product.app.test_client()
        self.register_hospital(other_client, 'South Clinic', 'admin@south.test')
        dashboard = other_client.get('/staff/dashboard')
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(b'Sample Patient', dashboard.data)
        self.assertNotIn(b'Ada Rivers', dashboard.data)

    def test_staff_dashboard_requires_hospital_sign_in(self):
        response = self.client.get('/staff/dashboard')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/hospital/login', response.location)

    def test_hospital_signup_starts_a_30_day_trial(self):
        hospital_id = self.register_hospital(self.client, 'Trial Clinic', 'trial@example.test')
        trial_started_at = self.query_value(
            'SELECT trial_started_at FROM hospitals WHERE id = ?', (hospital_id,)
        )
        trial_ends_at = self.query_value(
            'SELECT trial_ends_at FROM hospitals WHERE id = ?', (hospital_id,)
        )
        self.assertEqual(trial_ends_at - trial_started_at, 30 * 24 * 60 * 60)

    def test_home_has_two_distinct_registration_options_and_booking_requires_patient(self):
        home = self.client.get('/')
        self.assertIn(b'Sign up as a patient', home.data)
        self.assertIn(b'Register hospital', home.data)
        patient = self.client.get('/patient/dashboard')
        self.assertEqual(patient.status_code, 302)
        self.assertIn('/patient/signup', patient.location)

        patient_client = product.app.test_client()
        patient_id = self.register_patient(patient_client, 'Signed In Patient', 'signed-in@example.test')
        with patient_client.session_transaction() as session:
            session.clear()
        token = self.csrf(patient_client, '/patient/login')
        response = patient_client.post('/patient/login', data={
            'csrf_token': token,
            'email': 'signed-in@example.test',
            'password': 'patient-passphrase-10',
        })
        self.assertEqual(response.status_code, 302)
        with patient_client.session_transaction() as session:
            self.assertEqual(session['patient_id'], patient_id)
        booking_page = patient_client.get('/patient/dashboard')
        self.assertIn(b'Book appointment', booking_page.data)
        self.assertIn(b'Sign out', booking_page.data)

    def test_pwa_assets_are_served_for_android_installation(self):
        manifest = self.client.get('/static/manifest.webmanifest')
        self.assertEqual(manifest.status_code, 200)
        self.assertIn(b'hospital-192.png', manifest.data)
        manifest.close()
        icon_192 = self.client.get('/static/hospital-192.png')
        self.assertEqual(icon_192.status_code, 200)
        icon_192.close()
        icon_512 = self.client.get('/static/hospital-512.png')
        self.assertEqual(icon_512.status_code, 200)
        icon_512.close()
        worker = self.client.get('/service-worker.js')
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.headers['Service-Worker-Allowed'], '/')
        worker.close()

    def test_roster_csv_import_groups_doctor_and_weekly_hours(self):
        hospital_id = self.register_hospital(self.client, 'Roster Clinic', 'admin@roster.test')
        token = self.csrf(self.client, '/staff/dashboard')
        roster = (
            'department,doctor_name,email,temporary_password,weekday,start_time,end_time,slot_minutes\n'
            'Cardiology,Dr Ada Rivers,ada@roster.test,doctor-passphrase-10,Monday,09:00,12:00,30\n'
            'Cardiology,Dr Ada Rivers,ada@roster.test,doctor-passphrase-10,Tuesday,10:00,13:00,30\n'
        )
        response = self.client.post('/staff/dashboard', data={
            'csrf_token': token,
            'action': 'import_roster',
            'roster': (io.BytesIO(roster.encode('utf-8')), 'doctors.csv'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.query_value('SELECT COUNT(*) FROM departments WHERE hospital_id = ?', (hospital_id,)),
            1,
        )
        self.assertEqual(
            self.query_value('SELECT COUNT(*) FROM doctors WHERE hospital_id = ?', (hospital_id,)),
            1,
        )
        self.assertEqual(
            self.query_value('SELECT COUNT(*) FROM doctor_schedules'),
            2,
        )
        password_hash = self.query_value('SELECT password_hash FROM doctors LIMIT 1')
        self.assertNotEqual(password_hash, 'doctor-passphrase-10')

    def test_expired_trial_is_read_only_and_cannot_receive_bookings(self):
        hospital_id = self.register_hospital(self.client, 'Expired Clinic', 'admin@expired.test')
        appointment_date = date.today() + timedelta(days=1)
        with product.database() as connection:
            department_id = connection.execute(
                'INSERT INTO departments (hospital_id, name) VALUES (?, ?)',
                (hospital_id, 'Cardiology'),
            ).lastrowid
            doctor_id = connection.execute(
                'INSERT INTO doctors (hospital_id, department_id, name) VALUES (?, ?, ?)',
                (hospital_id, department_id, 'Dr Trial'),
            ).lastrowid
            connection.execute(
                'INSERT INTO doctor_schedules (doctor_id, weekday, start_time, end_time, slot_minutes) VALUES (?, ?, ?, ?, ?)',
                (doctor_id, appointment_date.weekday(), '09:00', '10:00', 30),
            )
            connection.execute(
                'UPDATE hospitals SET trial_ends_at = 0 WHERE id = ?', (hospital_id,)
            )

        dashboard = self.client.get('/staff/dashboard')
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b'free trial has ended', dashboard.data)
        token = self.csrf(self.client, '/staff/dashboard')
        self.client.post('/staff/dashboard', data={
            'csrf_token': token, 'action': 'add_department', 'name': 'Neurology',
        })
        self.assertEqual(
            self.query_value('SELECT COUNT(*) FROM departments WHERE hospital_id = ?', (hospital_id,)),
            1,
        )

        slug = self.query_value('SELECT slug FROM hospitals WHERE id = ?', (hospital_id,))
        patient_client = product.app.test_client()
        patient_id = self.register_patient(patient_client, 'Trial Patient', 'trial-patient@example.test')
        token = self.csrf(patient_client, '/appointment')
        patient_client.post('/appointment', data={
            'csrf_token': token,
            'hospital_id': hospital_id,
            'department_id': department_id,
            'doctor_id': doctor_id,
            'date': appointment_date.isoformat(),
            'time': '09:00',
        })
        self.assertEqual(self.query_value('SELECT COUNT(*) FROM appointments'), 0)
        self.assertEqual(
            self.client.get(f'/api/hospitals/{slug}/departments').json,
            [],
        )
        self.assertEqual(self.query_value('SELECT id FROM patients WHERE id = ?', (patient_id,)), patient_id)

    def test_unclaimed_hospital_cannot_receive_direct_patient_booking(self):
        appointment_date = date.today() + timedelta(days=1)
        with product.database() as connection:
            hospital_id = connection.execute(
                'INSERT INTO hospitals (name, slug) VALUES (?, ?)',
                ('Unclaimed Clinic', 'unclaimed-clinic'),
            ).lastrowid
            department_id = connection.execute(
                'INSERT INTO departments (hospital_id, name) VALUES (?, ?)',
                (hospital_id, 'Orthopedics'),
            ).lastrowid
            doctor_id = connection.execute(
                'INSERT INTO doctors (hospital_id, department_id, name) VALUES (?, ?, ?)',
                (hospital_id, department_id, 'Dr Unclaimed'),
            ).lastrowid
            connection.execute(
                'INSERT INTO doctor_schedules (doctor_id, weekday, start_time, end_time, slot_minutes) VALUES (?, ?, ?, ?, ?)',
                (doctor_id, appointment_date.weekday(), '09:00', '10:00', 30),
            )

        slots = self.client.get(
            f'/api/doctors/{doctor_id}/availability?date={appointment_date.isoformat()}'
        )
        self.assertEqual(slots.json['slots'], [])
        patient_client = product.app.test_client()
        self.register_patient(patient_client, 'Unauthorized Patient', 'unauthorized@example.test')
        token = self.csrf(patient_client, '/patient/dashboard')
        patient_client.post('/patient/dashboard', data={
            'csrf_token': token,
            'name': 'Unauthorized Patient',
            'phone': '5550199',
            'hospital_id': hospital_id,
            'department_id': department_id,
            'doctor_id': doctor_id,
            'date': appointment_date.isoformat(),
            'time': '09:00',
        })
        self.assertEqual(self.query_value('SELECT COUNT(*) FROM appointments'), 0)

    def test_production_mode_rejects_shared_sqlite_backend(self):
        previous_environment = os.environ.get('APP_ENV')
        previous_secret = os.environ.get('SECRET_KEY')
        previous_cookie = os.environ.get('COOKIE_SECURE')
        previous_opt_in = os.environ.get('ALLOW_SHARED_SQLITE')
        os.environ.update({
            'APP_ENV': 'production',
            'SECRET_KEY': 'stable-test-secret',
            'COOKIE_SECURE': 'true',
        })
        try:
            with self.assertRaisesRegex(RuntimeError, 'per-hospital PostgreSQL backend'):
                product.validate_production_configuration()
        finally:
            for key, previous_value in (
                ('APP_ENV', previous_environment),
                ('SECRET_KEY', previous_secret),
                ('COOKIE_SECURE', previous_cookie),
                ('ALLOW_SHARED_SQLITE', previous_opt_in),
            ):
                if previous_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous_value

    def test_production_mode_rejects_sqlite_even_with_legacy_opt_in(self):
        previous_environment = os.environ.get('APP_ENV')
        previous_secret = os.environ.get('SECRET_KEY')
        previous_cookie = os.environ.get('COOKIE_SECURE')
        previous_opt_in = os.environ.get('ALLOW_SHARED_SQLITE')
        os.environ.update({
            'APP_ENV': 'production',
            'SECRET_KEY': 'stable-test-secret',
            'COOKIE_SECURE': 'true',
            'ALLOW_SHARED_SQLITE': 'true',
        })
        try:
            with self.assertRaisesRegex(RuntimeError, 'per-hospital PostgreSQL backend'):
                product.validate_production_configuration()
        finally:
            for key, previous_value in (
                ('APP_ENV', previous_environment),
                ('SECRET_KEY', previous_secret),
                ('COOKIE_SECURE', previous_cookie),
                ('ALLOW_SHARED_SQLITE', previous_opt_in),
            ):
                if previous_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous_value

    def test_existing_hospital_requires_setup_code_and_keeps_bookings(self):
        with product.database() as connection:
            hospital_id = connection.execute(
                'INSERT INTO hospitals (name, slug) VALUES (?, ?)',
                ('Legacy Hospital', 'legacy-hospital'),
            ).lastrowid
            connection.execute(
                'INSERT INTO appointments (name, phone, department, date, time, hospital_id) VALUES (?, ?, ?, ?, ?, ?)',
                ('Legacy Patient', '5550101', 'Cardiology', '2026-10-10', '09:00', hospital_id),
            )
        hidden_hospital = self.client.get('/patient/dashboard')
        self.assertNotIn(b'Legacy Hospital', hidden_hospital.data)

        previous_code = os.environ.get('HOSPITAL_CLAIM_CODE')
        os.environ['HOSPITAL_CLAIM_CODE'] = 'one-time-private-code'
        try:
            token = self.csrf(self.client, '/hospital/signup')
            denied = self.client.post('/hospital/signup', data={
                'csrf_token': token,
                'hospital_name': 'Legacy Hospital',
                'email': 'owner@legacy.test',
                'password': 'secure-passphrase-10',
            })
            self.assertEqual(denied.status_code, 200)
            self.assertIn(b'requires a setup code', denied.data)

            token = self.csrf(self.client, '/hospital/signup')
            claimed = self.client.post('/hospital/signup', data={
                'csrf_token': token,
                'hospital_name': 'Legacy Hospital',
                'email': 'owner@legacy.test',
                'password': 'secure-passphrase-10',
                'claim_code': 'one-time-private-code',
            })
            self.assertEqual(claimed.status_code, 302)
            with self.client.session_transaction() as session:
                self.assertEqual(session['hospital_id'], hospital_id)
            trial_started_at = self.query_value(
                'SELECT trial_started_at FROM hospitals WHERE id = ?', (hospital_id,)
            )
            trial_ends_at = self.query_value(
                'SELECT trial_ends_at FROM hospitals WHERE id = ?', (hospital_id,)
            )
            self.assertEqual(trial_ends_at - trial_started_at, 30 * 24 * 60 * 60)
            patient_client = product.app.test_client()
            self.register_patient(patient_client, 'Legacy Patient Account', 'legacy-patient@example.test')
            visible_hospital = patient_client.get('/patient/dashboard')
            self.assertIn(b'Legacy Hospital', visible_hospital.data)
            self.assertEqual(
                self.query_value('SELECT COUNT(*) FROM appointments WHERE hospital_id = ?', (hospital_id,)),
                1,
            )
        finally:
            if previous_code is None:
                os.environ.pop('HOSPITAL_CLAIM_CODE', None)
            else:
                os.environ['HOSPITAL_CLAIM_CODE'] = previous_code


if __name__ == '__main__':
    unittest.main()