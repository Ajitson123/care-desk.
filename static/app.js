document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-closed-form]').forEach((form) => {
        const checkbox = form.querySelector('[data-closed-toggle]');
        const hours = form.querySelector('[data-hours-fields]');
        if (!checkbox || !hours) return;
        const update = () => {
            hours.hidden = checkbox.checked;
            hours.querySelectorAll('input, select').forEach((field) => {
                field.disabled = checkbox.checked;
            });
        };
        checkbox.addEventListener('change', update);
        update();
    });

    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/service-worker.js').catch(() => {});
    }
});