(function () {
  'use strict';
  var f = document.getElementById('f'), u = document.getElementById('u'), p = document.getElementById('p');
  var go = document.getElementById('go'), err = document.getElementById('err'), eye = document.getElementById('eye');
  var card = document.querySelector('.card'), timer = null;
  function show(msg) { err.textContent = msg; err.hidden = !msg; }
  eye.addEventListener('click', function () {
    var on = p.type === 'password'; p.type = on ? 'text' : 'password';
    eye.setAttribute('aria-label', on ? 'hide password' : 'show password'); eye.title = on ? 'hide password' : 'show password'; p.focus();
  });
  function lock(secs) {
    clearInterval(timer); go.disabled = true;
    var left = secs;
    (function tick() {
      if (left <= 0) { clearInterval(timer); go.disabled = false; go.textContent = 'Sign in'; show(''); return; }
      go.textContent = 'Try again in ' + left + ' s'; left -= 1;
    })();
    timer = setInterval(function () {
      if (left <= 0) { clearInterval(timer); go.disabled = false; go.textContent = 'Sign in'; show(''); return; }
      go.textContent = 'Try again in ' + left + ' s'; left -= 1;
    }, 1000);
  }
  f.addEventListener('submit', function (e) {
    e.preventDefault();
    if (go.disabled) return;
    show(''); go.disabled = true; go.textContent = 'Signing in…';
    fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-MD-Web': '1' }, cache: 'no-store',
      body: JSON.stringify({ user: u.value, password: p.value }) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j }; }); })
      .then(function (x) {
        if (x.r.ok) { go.textContent = 'Welcome'; location.reload(); return; }
        go.disabled = false; go.textContent = 'Sign in';
        show(x.j.error || 'sign in failed (' + x.r.status + ')');
        if (x.r.status === 429 && x.j.locked_for) lock(x.j.locked_for);
        p.value = ''; p.focus();
        card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
      })
      .catch(function () { go.disabled = false; go.textContent = 'Sign in'; show('the console did not answer: check the address and try again'); });
  });
})();
