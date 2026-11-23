const fs = require("fs");
const path = require("path");
const os = require("os");
const { execFileSync } = require("child_process");
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);

const ROOT = process.cwd();
const OUTPUT_DIR = path.join(ROOT, "feishu_docs");
const MANIFEST_PATH = path.join(OUTPUT_DIR, "_manifest.json");
const CDP_URL = process.env.FEISHU_CDP_URL || "http://127.0.0.1:9224";
const CONCURRENCY = Number(process.env.FEISHU_CONCURRENCY || 4);

function now() {
  return new Date().toISOString();
}

function cleanText(value) {
  return String(value || "")
    .replace(/[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, "")
    .trim();
}

function sanitizeFilename(value) {
  return cleanText(value)
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

function walkFiles(directory) {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      result.push(...walkFiles(fullPath));
    } else if (entry.isFile()) {
      result.push(fullPath);
    }
  }
  return result;
}

async function findVisible(locator, timeout = 15000) {
  const target = locator.filter({ visible: true }).last();
  await target.waitFor({ state: "visible", timeout });
  return target;
}

function processBundle(document, downloadedPath) {
  const stem = document.filename.replace(/\.md$/i, "");
  const assetRoot = path.join(OUTPUT_DIR, "assets", stem);
  const fileHeader = fs.readFileSync(downloadedPath).subarray(0, 4);
  const isZip =
    fileHeader.length === 4 &&
    fileHeader[0] === 0x50 &&
    fileHeader[1] === 0x4b &&
    fileHeader[2] === 0x03 &&
    fileHeader[3] === 0x04;
  if (!isZip) {
    const markdown = fs.readFileSync(downloadedPath, "utf8");
    fs.rmSync(assetRoot, { recursive: true, force: true });
    fs.writeFileSync(path.join(OUTPUT_DIR, document.filename), markdown);
    return {
      assetCount: 0,
      markdownBytes: Buffer.byteLength(markdown),
    };
  }
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "feishu-media-"));
  try {
    execFileSync("unzip", ["-q", downloadedPath, "-d", tempDirectory], {
      stdio: "pipe",
    });
    const files = walkFiles(tempDirectory);
    const markdownFiles = files.filter((file) => /\.md$/i.test(file));
    if (!markdownFiles.length) {
      throw new Error("媒体包内没有 Markdown 文件");
    }
    markdownFiles.sort((left, right) => {
      const leftDepth = left.split(path.sep).length;
      const rightDepth = right.split(path.sep).length;
      return leftDepth - rightDepth || left.length - right.length;
    });
    const markdownPath = markdownFiles[0];
    let markdown = fs.readFileSync(markdownPath, "utf8");

    fs.rmSync(assetRoot, { recursive: true, force: true });
    let assetCount = 0;
    for (const file of files) {
      if (/\.md$/i.test(file)) {
        continue;
      }
      const relativePath = path.relative(tempDirectory, file);
      const destination = path.join(assetRoot, relativePath);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.copyFileSync(file, destination);
      assetCount += 1;
    }

    const assetPrefix = `assets/${stem}/`;
    markdown = markdown
      .replaceAll("](/图片和附件/", `](/${assetPrefix}图片和附件/`)
      .replaceAll("](图片和附件/", `](${assetPrefix}图片和附件/`)
      .replaceAll('src="图片和附件/', `src="${assetPrefix}图片和附件/`);
    fs.writeFileSync(path.join(OUTPUT_DIR, document.filename), markdown);
    return {
      assetCount,
      markdownBytes: Buffer.byteLength(markdown),
    };
  } finally {
    fs.rmSync(tempDirectory, { recursive: true, force: true });
  }
}

async function exportAllContent(page, document) {
  await page.goto(document.url, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.waitForTimeout(4500);

  const bodyText = await page.locator("body").innerText().catch(() => "");
  const title = await page.title();
  if (bodyText.includes("没有权限访问") || title.includes("没有权限访问")) {
    throw new Error("没有权限访问");
  }

  const moreButton = await findVisible(
    page.locator('[data-e2e="suite-more-btn"]'),
    30000,
  );
  await moreButton.click();
  const downloadAs = await findVisible(
    page.getByText("下载为", { exact: true }),
    15000,
  );
  await downloadAs.hover();
  await page.waitForTimeout(700);
  const markdownOption = await findVisible(
    page.getByText("Markdown", { exact: true }),
    15000,
  );
  await markdownOption.click();
  const allContent = await findVisible(
    page.getByText("所有内容", { exact: true }),
    15000,
  );
  await allContent.click();

  const downloadPromise = page.waitForEvent("download", {
    timeout: 300000,
  });
  const exportButton = await findVisible(
    page.getByRole("button", { name: "导出", exact: true }),
    15000,
  );
  await exportButton.click();
  const download = await downloadPromise;
  const failure = await download.failure();
  if (failure) {
    throw new Error(`下载失败: ${failure}`);
  }
  const downloadedPath = await download.path();
  if (!downloadedPath) {
    throw new Error("浏览器未提供媒体包路径");
  }
  return {
    suggestedFilename: cleanText(download.suggestedFilename()),
    downloadedPath,
  };
}

async function main() {
  const manifest = loadManifest();
  const documents = Object.values(manifest.documents).filter(
    (document) =>
      document.status === "downloaded" &&
      document.filename &&
      document.fallback !== "uploaded-docx-to-html-markdown" &&
      document.media_export_status !== "downloaded",
  );
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
        if (index >= documents.length) {
          break;
        }
        const document = documents[index];
        document.media_export_status = "downloading";
        saveManifest(manifest);
        try {
          const result = await exportAllContent(page, document);
          const processed = processBundle(document, result.downloadedPath);
          document.media_export_status = "downloaded";
          document.media_exported_at = now();
          document.media_asset_count = processed.assetCount;
          document.media_markdown_bytes = processed.markdownBytes;
          document.media_error = null;
          succeeded += 1;
          console.log(
            `[${index + 1}/${documents.length}] OK ${document.key}: ${processed.assetCount} assets`,
          );
        } catch (error) {
          document.media_export_status = "failed";
          document.media_error = String(error?.message || error)
            .replace(/\s+/g, " ")
            .slice(0, 500);
          failed += 1;
          console.error(
            `[${index + 1}/${documents.length}] FAIL ${document.key}: ${document.media_error}`,
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
      { length: Math.max(1, Math.min(CONCURRENCY, documents.length)) },
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
