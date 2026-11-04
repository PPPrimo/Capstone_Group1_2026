const frame = document.getElementById('panelFrame');
const pages = {
  feature1: './feature1.html',
  feature2: './feature2.html',
  feature3: './feature3.html',
};

const account = fetch('/api/me', { credentials: 'same-origin' })
  .then(response => response.ok ? response.json() : null)
  .catch(error => { console.error('Could not load account:', error); return null; });

frame.addEventListener('load', async () => {
  const menu = frame.contentDocument.getElementById('featureSelect');
  if (!menu) return; // The initial blank frame or a login redirect has no feature menu.
  menu.addEventListener('change', () => {
    frame.title = menu.selectedOptions[0].textContent;
    frame.src = pages[menu.value];
  });
  const user = await account;
  menu.querySelector('[value="feature3"]').hidden = !user?.is_superuser;
});

frame.src = pages.feature1;
