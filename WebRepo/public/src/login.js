(function(){
  const form = document.getElementById('loginForm');
  const errorEl = document.getElementById('error');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorEl && (errorEl.style.display = 'none');
    const action = form.getAttribute('action') || '/auth/jwt/login';
    const body = new URLSearchParams();
    body.set('username', String(form.username?.value || ''));
    body.set('password', String(form.password?.value || ''));
    try {
      const res = await fetch(action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
        credentials: 'include'
      });

      // FastAPI-Users CookieTransport returns 204 No Content on success.
      if (res.status === 204) {
        window.location.href = '/index.html';
        return;
      }
      if (errorEl) errorEl.style.display = 'block';
    } catch (err) {
      if (errorEl) errorEl.style.display = 'block';
    }
  });
})();