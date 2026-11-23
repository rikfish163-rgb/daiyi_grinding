const fs = require("fs");
const path = require("path");
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);

const ROOT = process.cwd();
const OUTPUT_DIR = path.join(ROOT, "attachments");
const MANIFEST_PATH = path.join(ROOT, "feishu_docs", "_manifest.json");
const CDP_URL = process.env.FEISHU_CDP_URL || "http://127.0.0.1:9224";
const CONCURRENCY = Number(process.env.FEISHU_CONCURRENCY || 2);

function now() {
  return new Date().toISOString();
}

function cleanText(value) {
  return String(value || "")
    .replace(/[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, "")
    .trim();
}

function sanitizeStem(value) {
  return cleanText(value)
    .replace(/\.[A-Za-z0-9]{1,8}$/, "")
    .replace(/[\\/:*?"<>|]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[._\s]+|[._\s]+$/g, "")
    .slice(0, 100);
}

function loadManifest() {
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
}

function saveManifest(manifest) {
  manifest.generated_at = now();
  const tempPath = `${MANIFEST_PATH}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(manifest, null, 2)}\n`);
  fs.renameSync(tempPath, MANIFEST_PATH);
}

async function findVisible(locator, timeout = 15000) {
  const target = locator.filter({ visible: true }).last();
  await target.waitFor({ state: "visible", timeout });
  return target;
}

async function downloadAttachment(page, attachment) {
  await page.goto(attachment.url, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.waitForTimeout(4500);
  const title = await page.title();
  if (title.includes("没有权限访问")) {
    throw new Error("没有权限访问");
  }

  const moreButton = await findVisible(
    page.locator('[data-e2e="suite-more-btn"]'),
    30000,
  );
  await moreButton.click();
  const downloadButton = await findVisible(
    page.getByText("下载", { exact: true }),
    15000,
  );
  const downloadPromise = page.waitForEvent("download", {
    timeout: 600000,
  });
  await downloadButton.click();
  const download = await downloadPromise;
  const failure = await download.failure();
  if (failure) {
    throw new Error(`下载失败: ${failure}`);
  }
  const temporaryPath = await download.path();
  if (!temporaryPath) {
    throw new Error("浏览器未提供附件路径");
  }

  const suggestedName = cleanText(download.suggestedFilename());
  const extensionMatch = suggestedName.match(/(\.[A-Za-z0-9]{1,8})$/);
  const extension = extensionMatch ? extensionMatch[1] : "";
  const stem =
    sanitizeStem(suggestedName) || attachment.token;
  const filename = `${stem}__file-${attachment.token}${extension}`;
  const destination = path.join(OUTPUT_DIR, filename);
  fs.copyFileSync(temporaryPath, destination);
  return {
    filename,
    suggestedName,
    size: fs.statSync(destination).size,
  };
}

async function main() {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const manifest = loadManifest();
  const attachments = Object.values(manifest.attachments);
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  if (!context) {
    throw new Error("未找到 Chrome 浏览器上下文");
  }

  let cursor = 0;
  let succeeded = 0;
  let failed = 0;
  async function worker() {
    const page = await context.newPage();
    try {
      while (true) {
        const index = cursor;
        cursor += 1;
        if (index >= attachments.length) {
          break;
        }
        const attachment = attachments[index];
        if (
          attachment.download_status === "downloaded" &&
          attachment.local_filename &&
          fs.existsSync(path.join(OUTPUT_DIR, attachment.local_filename))
        ) {
          continue;
        }
        attachment.download_status = "downloading";
        saveManifest(manifest);
        try {
          const result = await downloadAttachment(page, attachment);
          attachment.download_status = "downloaded";
          attachment.local_filename = result.filename;
          attachment.downloaded_at = now();
          attachment.size = result.size;
          attachment.download_error = null;
          succeeded += 1;
          console.log(
            `[${index + 1}/${attachments.length}] OK ${result.filename} (${result.size} bytes)`,
          );
        } catch (error) {
          attachment.download_status = "failed";
          attachment.download_error = String(error?.message || error)
            .replace(/\s+/g, " ")
            .slice(0, 500);
          failed += 1;
          console.error(
            `[${index + 1}/${attachments.length}] FAIL ${attachment.key}: ${attachment.download_error}`,
          );
        }
        saveManifest(manifest);
      }
    } finally {
      await page.close().catch(() => {});
    }
  }

  await Promise.all(
    Array.from(
      { length: Math.max(1, Math.min(CONCURRENCY, attachments.length)) },
      () => worker(),
    ),
  );
  await browser.close().catch(() => {});
  console.log(`complete: ${succeeded} downloaded, ${failed} failed`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
