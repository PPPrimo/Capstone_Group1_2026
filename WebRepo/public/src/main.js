// Simple tab switching + admin gate for Feature 3
const tabs = document.querySelectorAll('.tab');
const frame = document.getElementById('panelFrame');

const tabMap = {
  feature1: './feature1.html',
  feature2: './feature2.html',
  feature3: './feature3.html',
};

let currentTab = null;

tabs.forEach(t => {
  t.addEventListener('click', () => {
    const tab = t.dataset.tab;
    if (tab === currentTab) return;          // already showing — skip reload
    currentTab = tab;
    tabs.forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    frame.setAttribute('src', tabMap[tab]);
  });
});

// Hide admin-only tabs until we confirm the user is a superuser
(async () => {
  try {
    const res = await fetch('/api/me', { credentials: 'same-origin' });
    if (!res.ok) return;
    const me = await res.json();
    if (!me.is_superuser) {
      tabs.forEach(t => {
        if (t.dataset.tab === 'feature3') t.style.display = 'none';
      });
    }
  } catch (_) { /* leave tabs visible on network error */ }
})();
