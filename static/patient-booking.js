document.addEventListener('DOMContentLoaded', () => {
    const form = document.querySelector('[data-booking-form]');
    if (!form) return;

    const hospital = document.querySelector('#hospital-choice');
    const department = document.querySelector('#department-choice');
    const doctor = document.querySelector('#doctor-choice');
    const appointmentDate = document.querySelector('#appointment-date');
    const appointmentTime = document.querySelector('#appointment-time');
    const status = document.querySelector('[data-slot-status]');

    const resetSelect = (select, prompt) => {
        select.replaceChildren(new Option(prompt, ''));
        select.disabled = true;
    };

    const loadOptions = async (url, select, prompt, emptyMessage) => {
        resetSelect(select, 'Loading...');
        try {
            const response = await fetch(url, { headers: { Accept: 'application/json' } });
            if (!response.ok) throw new Error('Request failed');
            const items = await response.json();
            resetSelect(select, items.length ? prompt : emptyMessage);
            items.forEach((item) => select.add(new Option(item.name, item.id)));
            select.disabled = items.length === 0;
            return items.length;
        } catch {
            resetSelect(select, 'Could not load options');
            status.textContent = 'Please check your connection and try again.';
            status.dataset.state = 'error';
            return 0;
        }
    };

    const loadSlots = async () => {
        resetSelect(appointmentTime, 'Checking available times...');
        status.dataset.state = '';
        if (!doctor.value || !appointmentDate.value) {
            status.textContent = 'Choose a doctor and date to see available times.';
            return;
        }
        try {
            const query = new URLSearchParams({ date: appointmentDate.value });
            const response = await fetch(`/api/doctors/${doctor.value}/availability?${query}`, { headers: { Accept: 'application/json' } });
            if (!response.ok) throw new Error('Request failed');
            const data = await response.json();
            resetSelect(appointmentTime, data.slots.length ? 'Select an available time' : 'No times available');
            data.slots.forEach((slot) => appointmentTime.add(new Option(slot, slot)));
            appointmentTime.disabled = data.slots.length === 0;
            status.textContent = data.slots.length ? `${data.slots.length} open times available.` : 'No times are available for this date. Try another date.';
        } catch {
            status.textContent = 'Could not check availability. Please try again.';
            status.dataset.state = 'error';
        }
    };

    hospital.addEventListener('change', async () => {
        resetSelect(department, 'Choose a hospital first');
        resetSelect(doctor, 'Choose a department first');
        appointmentDate.disabled = true;
        appointmentDate.value = '';
        resetSelect(appointmentTime, 'Choose a doctor and date first');
        const slug = hospital.selectedOptions[0]?.dataset.slug;
        if (!slug) {
            status.textContent = 'Select a hospital to see its departments.';
            return;
        }
        const count = await loadOptions(`/api/hospitals/${encodeURIComponent(slug)}/departments`, department, 'Select a department', 'No departments listed');
        status.textContent = count ? 'Choose a department to find a doctor.' : 'This hospital has not added any departments yet.';
    });

    department.addEventListener('change', async () => {
        resetSelect(doctor, 'Choose a department first');
        appointmentDate.disabled = true;
        appointmentDate.value = '';
        resetSelect(appointmentTime, 'Choose a doctor and date first');
        const slug = hospital.selectedOptions[0]?.dataset.slug;
        if (!slug || !department.value) return;
        const count = await loadOptions(`/api/hospitals/${encodeURIComponent(slug)}/departments/${department.value}/doctors`, doctor, 'Select a doctor', 'No doctors listed');
        if (count) {
            status.textContent = 'Choose a doctor and date to see available times.';
        } else {
            status.textContent = 'No active doctors are listed for this department yet.';
        }
    });

    doctor.addEventListener('change', () => {
        appointmentDate.disabled = !doctor.value;
        appointmentDate.value = '';
        resetSelect(appointmentTime, 'Choose a date first');
        status.textContent = doctor.value ? 'Choose a date to see available times.' : 'Choose a doctor and date to see available times.';
    });
    appointmentDate.addEventListener('change', loadSlots);
});