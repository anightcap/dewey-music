const { chromium } = require('playwright');
const fs = require('fs'); const cp = require('child_process');
const R = process.argv[2];
const code = () => cp.execSync(`python3 -c "import importlib.util,time;s=importlib.util.spec_from_file_location('r','${process.env.DEWEY_REPO}/runner.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.totp_code(open('${R}/state/totp.key').read().strip(),int(time.time()//30)))"`).toString().trim();
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ bypassCSP: true, ignoreHTTPSErrors: true, httpCredentials: { username: 'tester', password: 'test-password-1234' }, viewport: { width: 1100, height: 1000 } });
  const p = await ctx.newPage(); p.on('pageerror', e => console.log('PAGEERROR', e.message)); p.on('console', m => { if (m.type() === 'error') console.log('CONSOLE', m.text()); });
  await p.goto('https://127.0.0.1:18443/#/run');
  await p.waitForSelector('.armbox');
  console.log('arm enabled:', !(await p.$eval('.armbox .btn', e => e.disabled)));
  await p.fill('.armcode', '000000'); await p.click('.armbox .btn.primary');
  await p.waitForTimeout(800); console.log('toast after wrong code:', await p.textContent('#toast'));
  await p.fill('.armcode', code()); await p.click('.armbox .btn.primary');
  await p.waitForSelector('.armbox.on', { timeout: 8000 });
  console.log('armed banner:', (await p.textContent('.armbox')).slice(0, 120));
  console.log('sidebar:', (await p.textContent('#safety')).replace(/\s+/g, ' '));
  // dry run on the playlist card: enter the password, run
  const card = p.locator('#cmd-playlistfix');
  console.log('real button disabled before dry run:', await card.locator('.real .btn.danger').isDisabled());
  await card.locator('input[type=password]').first().fill('uiPassw0rd');
  await card.getByRole('button', { name: 'Run dry run' }).click();
  await p.waitForSelector('.console-head', { timeout: 8000 });
  await p.waitForFunction(() => document.querySelector('.console .after') && !document.querySelector('.console .after').hidden, null, { timeout: 20000 });
  await p.goto('https://127.0.0.1:18443/#/run?cmd=playlistfix'); await p.reload(); await p.waitForSelector('#cmd-playlistfix .real');
  const real = p.locator('#cmd-playlistfix .real');
  console.log('real text:', (await real.textContent()).slice(0, 260));
  console.log('real button disabled after dry run:', await real.locator('.btn.danger').isDisabled());
  await p.locator('#cmd-playlistfix').scrollIntoViewIfNeeded();
  await p.screenshot({ path: R + '/s_armed.png', fullPage: false });
  // wrong confirmation
  await real.locator('input[type=password]').fill('uiPassw0rd');
  await real.locator('input[type=text]').fill('nope'); await real.locator('.btn.danger').click();
  await p.waitForTimeout(400); console.log('toast wrong confirm:', await p.textContent('#toast'));
  await real.locator('input[type=text]').fill('FIX PLAYLISTS'); await real.locator('.btn.danger').click();
  await p.waitForSelector('.console-head', { timeout: 8000 });
  await p.waitForFunction(() => document.querySelector('.console .after') && !document.querySelector('.console .after').hidden, null, { timeout: 20000 });
  console.log('console:', (await p.textContent('.console pre')).slice(0, 300).replace(/\n/g, ' | '));
  console.log('badge:', (await p.textContent('.console-head')).slice(0, 160));
  await p.screenshot({ path: R + '/s_ran.png' });
  // disarm
  await p.goto('https://127.0.0.1:18443/#/run'); await p.reload(); await p.waitForSelector('.armbox.on');
  await p.getByRole('button', { name: 'Disarm now' }).click(); await p.waitForSelector('.armbox.idle', { timeout: 8000 });
  console.log('after disarm:', (await p.textContent('#safety')).replace(/\s+/g, ' '));
  // mobile
  const m = await b.newContext({ bypassCSP: true, ignoreHTTPSErrors: true, httpCredentials: { username: 'tester', password: 'test-password-1234' }, viewport: { width: 390, height: 800 } });
  const mp = await m.newPage(); await mp.goto('https://127.0.0.1:18443/#/run'); await mp.waitForSelector('.armbox'); await mp.screenshot({ path: R + '/s_idle_m.png' });
  await b.close();
})();
