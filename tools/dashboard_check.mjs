/**
 * Drive the deployed FinanceHub dashboard in a real browser.
 *
 * This is the one hop nothing has ever exercised: the SPA is served and the
 * APIs behind it answer, but no browser has rendered it. Every previous
 * "never actually run" gap on this project produced a real bug on contact, so
 * the point here is to look for those, not to produce a green tick.
 *
 * Console errors, page exceptions and failed requests are collected
 * throughout and reported at the end regardless of whether the assertions
 * pass - a panel that renders while logging a 500 is still broken. That is
 * what caught the two bugs in 25906e1: the page "loaded" while throwing
 * `Cannot read properties of undefined` a hundred times.
 *
 * Playwright is deliberately NOT a dependency of frontend/package.json.
 * provision.sh runs `npm ci` on every deploy and pulling a browser download
 * into that path would cost minutes and ~150MB on a 4GB box for something
 * only run by hand. Install it where you want to run this:
 *
 *     mkdir -p /tmp/dashcheck && cd /tmp/dashcheck && npm init -y
 *     npm install playwright && npx playwright install --with-deps chromium
 *     cp /opt/financehub/tools/dashboard_check.mjs .
 *     node dashboard_check.mjs
 *
 * The copy is not optional: node resolves `playwright` relative to the
 * script's own directory, not the working directory, so running it in place
 * from tools/ fails with ERR_MODULE_NOT_FOUND however you set the cwd.
 *
 *     BASE_URL=https://your.host SHOT_DIR=/tmp/shots node dashboard_check.mjs
 *
 * Screenshots and a report.json land in SHOT_DIR. Exits non-zero if any check
 * fails, so it can gate a deploy.
 *
 * It seeds ~60 obligations with a fresh RNG seed to give the reconcile
 * something to announce - without that the WebSocket assertion measures an
 * idle queue rather than the relay.
 */
import { chromium } from 'playwright';
import { writeFileSync } from 'node:fs';

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1';
const SHOTS = process.env.SHOT_DIR ?? '.';

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const badResponses = [];
const wsFrames = [];

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -- ${detail}` : ''}`);
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await context.newPage();

page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', (e) => pageErrors.push(String(e)));
page.on('requestfailed', (r) =>
  failedRequests.push(`${r.method()} ${r.url()} -- ${r.failure()?.errorText}`));
page.on('response', (r) => {
  if (r.status() >= 400) badResponses.push(`${r.status()} ${r.request().method()} ${r.url()}`);
});
page.on('websocket', (ws) => {
  wsFrames.push(`OPEN ${ws.url()}`);
  ws.on('framereceived', (f) => wsFrames.push(`RECV ${String(f.payload).slice(0, 200)}`));
  ws.on('socketerror', (e) => wsFrames.push(`ERROR ${e}`));
  ws.on('close', () => wsFrames.push('CLOSE'));
});

console.log(`\nDriving ${BASE}\n`);

// ---- 1. the page loads at all ------------------------------------------
const resp = await page.goto(BASE, { waitUntil: 'networkidle', timeout: 30000 });
check('page responds 200', resp?.status() === 200, `status ${resp?.status()}`);
check('document title set', (await page.title()).includes('FinanceHub'), await page.title());

// ---- 2. the sign-in gate appears ---------------------------------------
const signInVisible = await page.getByText('Choose the role to view as').isVisible().catch(() => false);
check('sign-in card renders', signInVisible);
await page.screenshot({ path: `${SHOTS}/01-signin.png`, fullPage: true });

const keyField = page.locator('input[type="password"]');
const prefilled = await keyField.inputValue().catch(() => '');
check('API key prefilled from the built bundle', prefilled.length > 0,
  prefilled ? `${prefilled.length} chars` : 'EMPTY - SPA was built without VITE_SERVICE_API_KEY');

// ---- 3. sign in as Finance Manager -------------------------------------
await page.getByRole('radio', { name: /Finance Manager/i }).check().catch(() => {});
await page.getByRole('button', { name: /sign in|continue|view/i }).first().click()
  .catch(async () => { await keyField.press('Enter'); });

await page.waitForTimeout(4000);
const signedIn = !(await page.getByText('Choose the role to view as').isVisible().catch(() => false));
check('sign-in succeeds and the dashboard mounts', signedIn);
await page.screenshot({ path: `${SHOTS}/02-dashboard.png`, fullPage: true });

// ---- 4. the KPI panel shows REAL data ----------------------------------
const body = await page.textContent('body');
for (const label of ['Transaction volume', 'Overall match rate', 'Open exceptions', 'Reconciliation status']) {
  check(`KPI card "${label}" present`, body.includes(label));
}
check('KPI panel is not the empty state', !body.includes('No metrics available'));

// The database holds ~4,697 transactions and ~1,659 open exceptions. A
// dashboard wired to live data has to show numbers of that magnitude; zeroes
// mean it rendered but is not reading the gateway.
const digitGroups = (body.match(/\b\d[\d,]{2,}\b/g) ?? []).map((s) => Number(s.replace(/,/g, '')));
const biggest = digitGroups.length ? Math.max(...digitGroups) : 0;
check('dashboard shows non-trivial live figures', biggest > 100, `largest number rendered: ${biggest}`);

// ---- 5. the exception queue lists real rows -----------------------------
const hasCategory = /MISSING_REFERENCE_CODE|PARTIAL_PAYMENT|SPLIT_SETTLEMENT|TIMING_DIFFERENCE|Missing reference|Partial payment|Split settlement|Timing difference/i.test(body);
check('exception queue renders real categories', hasCategory);
await page.screenshot({ path: `${SHOTS}/03-exceptions.png`, fullPage: true });

// ---- 6. WebSocket: does a reconcile reach the browser? -----------------
const framesBefore = wsFrames.filter((f) => f.startsWith('RECV')).length;
check('WebSocket opened', wsFrames.some((f) => f.startsWith('OPEN')),
  wsFrames.find((f) => f.startsWith('OPEN')) ?? 'no /ws connection attempted');

// Seed first: a reconcile with nothing left to reconcile has nothing to
// announce, so an empty WS feed would be a property of the data, not of the
// relay. This puts genuinely new work in front of it.
const { execSync } = await import('node:child_process');
try {
  execSync("sudo -u financehub /opt/financehub/.venv/bin/python "
    + `/opt/financehub/tools/seed.py --count 60 --sink http --seed ${Date.now() % 100000} `
    + "--validate-url http://127.0.0.1:8001 --out /tmp/ws-probe",
    { stdio: 'pipe', timeout: 180000 });
  console.log('   (seeded 60 fresh obligations)');
} catch (e) { console.log('   (seed FAILED: ' + String(e.stderr ?? e.message).slice(0, 300) + ')'); }
const rec = await fetch('http://127.0.0.1:8002/reconcile', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
}).then((r) => r.json()).catch((e) => ({ error: e.message }));
console.log(`   (reconcile: ${JSON.stringify(rec).slice(0, 160)})`);
await page.waitForTimeout(10000);
const framesAfter = wsFrames.filter((f) => f.startsWith('RECV')).length;
check('WebSocket carried a live event after a reconcile', framesAfter > framesBefore,
  `${framesBefore} -> ${framesAfter} frames`);

// ---- 7. RBAC in the UI, not just the API -------------------------------
await page.screenshot({ path: `${SHOTS}/04-before-role-switch.png`, fullPage: true });
// The switcher is a dropdown: the trigger shows the current role's short
// name ("Manager"), and the Auditor option only exists once it is open.
await page.evaluate(() => window.scrollTo(0, 0));
await page.waitForTimeout(500);
let switched = false;
const trigger = page.getByRole('button', { name: /manager|admin|auditor/i }).first();
if (await trigger.isVisible().catch(() => false)) {
  await trigger.click().catch(() => {});
  await page.waitForTimeout(1200);
  // Matched on text, not accessible name: the label and blurb are adjacent
  // spans with no whitespace between them, so the accessible name is the
  // run-together "AuditorRead-only visibility..." and getByRole misses it.
  const option = page.locator('button').filter({ hasText: /^Auditor/ }).first();
  if (await option.isVisible().catch(() => false)) {
    await option.click().catch(() => {});
    await page.waitForTimeout(4000);
    switched = true;
  }
}
check('role switcher reachable', switched, switched ? '' : 'could not find an Auditor control');
if (switched) {
  await page.screenshot({ path: `${SHOTS}/05-auditor.png`, fullPage: true });
  const auditorBody = await page.textContent('body');
  const resolveOffered = /\bResolve\b/i.test(auditorBody);
  check('Auditor is not offered a Resolve action', !resolveOffered,
    resolveOffered ? 'a Resolve control is still visible to AUDITOR' : '');
}

// ---- report -------------------------------------------------------------
console.log('\n--- browser diagnostics ---');
const dump = (title, arr) => {
  console.log(`  ${title}: ${arr.length}`);
  arr.slice(0, 12).forEach((x) => console.log(`    - ${x}`));
  if (arr.length > 12) console.log(`    ... and ${arr.length - 12} more`);
};
dump('console errors', consoleErrors);
dump('uncaught page exceptions', pageErrors);
dump('failed requests', failedRequests);
dump('HTTP >=400', badResponses);
dump('websocket activity', wsFrames);

writeFileSync(`${SHOTS}/report.json`, JSON.stringify(
  { results, consoleErrors, pageErrors, failedRequests, badResponses, wsFrames }, null, 2));

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await browser.close();
process.exit(failed.length ? 1 : 0);
