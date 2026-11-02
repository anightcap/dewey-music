const { chromium } = require('playwright');
const R = process.argv[2];
let fails = 0;
const check = (name, ok, extra) => { console.log((ok ? 'PASS ' : 'FAIL ') + name + (!ok && extra !== undefined ? '  -> ' + String(extra).slice(0, 400) : '')); if (!ok) fails++; };
(async () => {
  const b = await chromium.launch();
  // no stored credentials: a real visitor
  const ctx = await b.newContext({ bypassCSP: false, ignoreHTTPSErrors: true, viewport: { width: 1100, height: 900 } });
  const p = await ctx.newPage(); p.on('pageerror', e => { console.log('PAGEERROR', e.message); fails++; });
  let dialogs = 0; p.on('dialog', d => { dialogs++; d.dismiss(); });
  const csp = []; p.on('console', m => { if (/Content Security Policy|Refused to/i.test(m.text())) csp.push(m.text()); });
  await p.goto('https://127.0.0.1:18443/#/run');
  await p.waitForSelector('#f');
  check('opening the console signed out shows the login page', (await p.title()).startsWith('Sign in') && await p.locator('#u').isVisible() && await p.locator('#p').isVisible());
  check('the user name box has the focus and password managers can recognise the fields',
    await p.evaluate(() => document.activeElement && document.activeElement.id === 'u' && document.getElementById('u').autocomplete === 'username' && document.getElementById('p').autocomplete === 'current-password'));
  check('the page loads without breaking the strict content policy (no inline script)', csp.length === 0, csp.join(' | '));
  await p.screenshot({ path: R + '/s_login.png' });
  const m = await ctx.newPage(); await m.setViewportSize({ width: 390, height: 780 }); await m.goto('https://127.0.0.1:18443/');
  await m.waitForSelector('#f');
  check('on a phone the card fits the screen with no sideways scrolling', await m.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), await m.evaluate(() => document.documentElement.scrollWidth));
  await m.screenshot({ path: R + '/s_login_m.png' }); await m.close();

  // eye button
  await p.fill('#p', 'secret');
  await p.click('#eye'); check('the eye button shows the password', await p.locator('#p').getAttribute('type') === 'text');
  await p.click('#eye'); check('and hides it again', await p.locator('#p').getAttribute('type') === 'password');

  // wrong password
  await p.fill('#u', 'tester'); await p.fill('#p', 'not-the-password'); await p.click('#go');
  await p.waitForSelector('#err:not([hidden])');
  check('a wrong password shows an error on the page, clears the box and stays on the login page',
    (await p.textContent('#err')).includes('wrong') && (await p.inputValue('#p')) === '' && await p.locator('#f').isVisible());
  await p.screenshot({ path: R + '/s_login_err.png' });
  check('no browser pop-up appeared', dialogs === 0);

  // right password, by pressing Enter
  await p.fill('#p', 'test-password-1234'); await p.press('#p', 'Enter');
  await p.waitForSelector('#nav a');
  check('the right password opens the console on the page that was asked for (#/run)', p.url().endsWith('#/run') && await p.locator('#f').count() === 0, p.url());
  await p.waitForSelector('.signed');
  check('the sidebar says who is signed in and offers sign out', (await p.textContent('.signed')).includes('signed in as tester') && (await p.textContent('.signed')).includes('sign out'), await p.textContent('.signed'));
  await p.reload(); await p.waitForSelector('#nav a');
  check('reloading keeps you signed in', await p.locator('#f').count() === 0);
  const ck = (await ctx.cookies()).find(c => c.name === 'dewey_session');
  check('the cookie is HttpOnly, Secure and SameSite=Strict, and is a session cookie', !!ck && ck.httpOnly && ck.secure && ck.sameSite === 'Strict' && ck.expires === -1, JSON.stringify(ck));
  check('page script cannot read the cookie', await p.evaluate(() => document.cookie.indexOf('dewey_session') < 0));
  await p.screenshot({ path: R + '/s_login_in.png' });

  // the sign-in ends while the page is open (idle timeout, console restarted): the next request sends you to the login page
  const keep = await ctx.cookies();
  await ctx.clearCookies();
  await p.evaluate(() => { api('status'); });
  await p.waitForSelector('#f', { timeout: 8000 });
  check('when the sign-in ends under an open page, it shows the login page (no pop-up)', dialogs === 0);
  await ctx.addCookies(keep);
  await p.reload(); await p.waitForSelector('.signed');
  check('the old cookie is still good while the session lives (it is the server that decides)', true);

  // sign out
  await p.click('.signed a');
  await p.waitForSelector('#f');
  check('sign out returns to the login page', await p.locator('#u').isVisible());
  await p.reload();
  check('and the console stays closed after a reload', await p.locator('#f').isVisible());
  const gone = await ctx.cookies();
  const r = await ctx.request.get('https://127.0.0.1:18443/api/status', { headers: { Cookie: 'dewey_session=' + (keep.find(c => c.name === 'dewey_session') || {}).value } });
  check('the signed-out cookie is dead on the server', r.status() === 401, r.status());
  await b.close();
  console.log('FAILS: ' + fails); process.exit(fails ? 1 : 0);
})();
