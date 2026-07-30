/**
 * Screenshot a LedgerLens page once it has finished rendering real data.
 *
 * Drives headless Chrome over the DevTools Protocol using Node's built-in WebSocket,
 * so capturing the dashboard needs no Puppeteer or Playwright dependency. It waits on
 * a DOM condition rather than a fixed sleep: the dashboard polls the API and animates
 * its counters, so a timed capture reliably catches a page mid-count with zeros in
 * the KPI cards — a screenshot that misrepresents the product in the direction of
 * looking broken.
 *
 *   node scripts/shot.mjs <url> <out.png> [width] [height] [waitForText] [afterJs]
 *
 * `afterJs` runs once the wait condition holds and before the capture, for the shots
 * that are behind an interaction — opening the audit drawer, scrolling to the ledger.
 * Anything it needs to wait for it can await itself; its result is awaited here.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, out, width = "1512", height = "1100", waitFor = "", afterJs = ""] =
  process.argv.slice(2);
if (!url || !out) {
  console.error("usage: node scripts/shot.mjs <url> <out.png> [w] [h] [waitForText]");
  process.exit(2);
}

const CHROME =
  process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const profile = mkdtempSync(join(tmpdir(), "ledgerlens-shot-"));
const port = 9200 + Math.floor(Math.random() * 400);

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--hide-scrollbars",
    "--no-first-run",
    "--force-color-profile=srgb",
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/list`);
      const pages = (await res.json()).filter((t) => t.type === "page");
      if (pages.length) return pages[0].webSocketDebuggerUrl;
    } catch {
      /* chrome not up yet */
    }
    await sleep(250);
  }
  throw new Error("chrome did not expose a debugging target");
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      const entry = pending.get(msg.id);
      if (!entry) return;
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
    });
    ws.addEventListener("error", reject);
    ws.addEventListener("open", () =>
      resolve({
        send(method, params = {}) {
          id += 1;
          const messageId = id;
          return new Promise((res, rej) => {
            pending.set(messageId, { resolve: res, reject: rej });
            ws.send(JSON.stringify({ id: messageId, method, params }));
          });
        },
        close: () => ws.close(),
      }),
    );
  });
}

let timedOut = false;
try {
  const client = await connect(await target());
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Page.navigate", { url });

  const deadline = Date.now() + 60_000;
  for (;;) {
    await sleep(400);
    // Case-insensitive: CSS text-transform is reflected in innerText, so a heading
    // styled uppercase would never match its source-cased string.
    const expression = waitFor
      ? `!!document.body && document.body.innerText.toLowerCase().includes(${JSON.stringify(
          waitFor.toLowerCase(),
        )})`
      : `document.readyState === "complete"`;
    const { result } = await client.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
    });
    if (result.value === true) break;
    if (Date.now() > deadline) {
      timedOut = true;
      console.error(`timed out waiting for: ${waitFor || "load"}`);
      break;
    }
  }
  if (afterJs) {
    const { result, exceptionDetails } = await client.send("Runtime.evaluate", {
      expression: afterJs,
      awaitPromise: true,
      returnByValue: true,
    });
    if (exceptionDetails) {
      throw new Error(`afterJs threw: ${exceptionDetails.text} ${result?.description ?? ""}`);
    }
    if (result?.value !== undefined) console.error(`afterJs -> ${result.value}`);
  }

  // Let the counter animations and the entrance transition settle, so the capture
  // is the resting state rather than a frame partway through it.
  await sleep(1_800);

  const { data } = await client.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  writeFileSync(out, Buffer.from(data, "base64"));
  console.log(`wrote ${out}`);
  client.close();
} finally {
  chrome.kill("SIGKILL");
  // SIGKILL returns before the kernel has finished reaping Chrome, which is still
  // flushing its profile directory. Removing it in that window raises ENOTEMPTY and
  // fails the command after the screenshot was already written successfully.
  await sleep(400);
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}

// A screenshot taken after a timeout is a screenshot of the wrong thing, and it
// would be committed as if it were the right thing.
if (timedOut) process.exit(1);
