/* Shared API helpers */

function getCsrfToken() {
  return document.cookie
    .split('; ')
    .find(r => r.startsWith('csrftoken='))
    ?.split('=')[1] ?? '';
}

async function apiFetch(url, options = {}) {
  const defaults = {
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrfToken(),
    },
    credentials: 'same-origin',
  };
  const resp = await fetch(url, { ...defaults, ...options, headers: { ...defaults.headers, ...(options.headers ?? {}) } });
  if (resp.status === 401 || resp.status === 403) {
    window.location.href = '/login/?next=' + encodeURIComponent(window.location.pathname);
    return null;
  }
  return resp;
}

function showAlert(container, message, type = 'error') {
  const safe = message.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
  container.innerHTML = `<div class="alert alert-${type}">${safe}</div>`;
}

function clearAlert(container) {
  container.innerHTML = '';
}
