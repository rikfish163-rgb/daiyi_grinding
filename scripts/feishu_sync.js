const fs = require("fs");
const path = require("path");
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE_PATH || "playwright-core",
);

const ROOT = process.cwd();
const SOURCES = [
  "/home/dianrobot/下载/具身打磨项目组会.md",
  "/home/dianrobot/下载/具身打磨工作会议.md",
];
const OUTPUT_DIR = path.join(ROOT, "feishu_docs");
const MANIFEST_PATH = path.join(OUTPUT_DIR, "_manifest.json");
const CDP_URL = process.env.FEISHU_CDP_URL || "http://127.0.0.1:9223";
const CONCURRENCY = Number(process.env.FEISHU_CONCURRENCY || 2);
const RECURSIVE_ROUNDS = Number(process.env.FEISHU_RECURSIVE_ROUNDS || 2);
const MAX_DEPTH = 1 + RECURSIVE_ROUNDS;
const MAX_ATTEMPTS = 3;

const LINK_RE =
  /\[([^\]\n]*)\]\(\s*<?(https:\/\/hustdian\.feishu\.cn\/(wiki|docx|file)\/[A-Za-z0-9]+(?:[?#][^\s)>]*)?)>?(?:\s+"[^"]*")?\)|(https:\/\/hustdian\.feishu\.cn\/(wiki|docx|file)\/[A-Za-z0-9]+(?:[?#][^\s)<>"']*)?)/g;

function now() {
  return new Date().toISOString();
}

function cleanText(value) {
  return String(value || "")
    .replace(/[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, "")
    .trim();
}

function sanitizeFilename(value) {
  const cleaned = cleanText(value)
    .replace(/\.md$/i, "")
    .replace(/[\\/:*?"<>|]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[._\s]+|[._\s]+$/g, "");
  return (cleaned || "untitled").slice(0, 80);
}

function normalizeFeishuUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (parsed.hostname !== "hustdian.feishu.cn") {
    return null;
  }
  const match = parsed.pathname.match(/^\/(wiki|docx|file)\/([A-Za-z0-9]+)\/?$/);
  if (!match) {
    return null;
  }
  return {
    kind: match[1],
    token: match[2],
    key: `${match[1]}:${match[2]}`,
    url: `https://hustdian.feishu.cn/${match[1]}/${match[2]}`,
  };
}

function addSource(record, source) {
  if (!source) {
    return;
  }
  const normalized = {
    name: source.name || "",
    label: cleanText(source.label || ""),
    url: source.url || "",
  };
  const exists = record.sources.some(
    (item) =>
      item.name === normalized.name &&
      item.label === normalized.label &&
      item.url === normalized.url,
  );
  if (!exists) {
    record.sources.push(normalized);
  }
}

function extractFeishuLinks(text) {
  const links = [];
  for (const match of text.matchAll(LINK_RE)) {
    const rawUrl = match[2] || match[4];
    const normalized = normalizeFeishuUrl(rawUrl);
    if (!normalized) {
      continue;
    }
    links.push({
      ...normalized,
      label: cleanText(match[1] || ""),
      rawUrl,
    });
  }
  return links;
}

function emptyManifest() {
  return {
    generated_at: now(),
    source_files: SOURCES,
    recursive_rounds: RECURSIVE_ROUNDS,
    documents: {},
    attachments: {},
  };
}

function loadManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    return emptyManifest();
  }
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  manifest.documents ||= {};
  manifest.attachments ||= {};
  manifest.source_files ||= SOURCES;
  manifest.recursive_rounds = RECURSIVE_ROUNDS;
  return manifest;
}

function saveManifest(manifest) {
  manifest.generated_at = now();
  const tempPath = `${MANIFEST_PATH}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(manifest, null, 2)}\n`);
  fs.renameSync(tempPath, MANIFEST_PATH);
}

function ensureDocument(manifest, link, depth, source) {
  let document = manifest.documents[link.key];
  if (!document) {
    document = {
      kind: link.kind,
      token: link.token,
      key: link.key,
      url: link.url,
      depth,
      status: depth > MAX_DEPTH ? "depth_limit" : "pending",
      attempts: 0,
      title: "",
      filename: null,
      error: null,
      downloaded_at: null,
      scanned_depth: null,
      sources: [],
    };
    manifest.documents[link.key] = document;
  } else if (depth < document.depth) {
    document.depth = depth;
    if (document.status === "depth_limit") {
      document.status = "pending";
    }
  }
  addSource(document, source);
  return document;
}

function ensureAttachment(manifest, link, source) {
  let attachment = manifest.attachments[link.key];
  if (!attachment) {
    attachment = {
      kind: "file",
      token: link.token,
      key: link.key,
      url: link.url,
      labels: [],
      sources: [],
    };
    manifest.attachments[link.key] = attachment;
  }
  if (link.label && !attachment.labels.includes(link.label)) {
    attachment.labels.push(link.label);
  }
  addSource(attachment, source);
  return attachment;
}

function ingestLinks(manifest, text, source, depth) {
  let newDocuments = 0;
  let newAttachments = 0;
  for (const link of extractFeishuLinks(text)) {
    if (link.kind === "file") {
      const before = manifest.attachments[link.key];
      ensureAttachment(manifest, link, source);
      if (!before) {
        newAttachments += 1;
      }
      continue;
    }
    const before = manifest.documents[link.key];
    ensureDocument(manifest, link, depth, source);
    if (!before) {
      newDocuments += 1;
    }
  }
  return { newDocuments, newAttachments };
}

async function findVisible(locator, timeout = 15000) {
  const visible = locator.filter({ visible: true });
  const target = visible.last();
  await target.waitFor({ state: "visible", timeout });
  return target;
}

async function exportMarkdown(page, document) {
  await page.goto(document.url, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.waitForTimeout(4500);

  const bodyText = await page.locator("body").innerText().catch(() => "");
  const pageTitle = await page.title();
  if (
    bodyText.includes("没有权限访问") ||
    bodyText.includes("申请阅读权限") ||
    pageTitle.includes("没有权限访问")
  ) {
    throw new Error("没有权限访问");
  }
  if (
    bodyText.includes("你访问的页面不存在") ||
    pageTitle.includes("Wiki - 404")
  ) {
    throw new Error("页面不存在");
  }

  const moreButton = await findVisible(
    page.locator('[data-e2e="suite-more-btn"]'),
    30000,
  ).catch(() =>
    findVisible(page.locator("button.more-btn"), 30000),
  );

  await moreButton.click();
  const downloadAs = await findVisible(
    page.getByText("下载为", { exact: true }),
    10000,
  );
  await downloadAs.hover();
  await page.waitForTimeout(700);
  const markdownOption = await findVisible(
    page.getByText("Markdown", { exact: true }),
    10000,
  );
  await markdownOption.click();

  const textOnly = await findVisible(page.getByText(/仅文本/), 15000);
  await textOnly.click();

  const downloadPromise = page.waitForEvent("download", {
    timeout: 120000,
  });
  const exportButton = await findVisible(
    page.getByRole("button", { name: "导出", exact: true }),
    10000,
  );
  await exportButton.click();
  const download = await downloadPromise;
  const failure = await download.failure();
  if (failure) {
    throw new Error(`下载失败: ${failure}`);
  }
  const temporaryPath = await download.path();
  if (!temporaryPath) {
    throw new Error("浏览器未提供下载文件路径");
  }

  const suggestedTitle = cleanText(download.suggestedFilename())
    .replace(/\.md$/i, "")
    .trim();
  if (!document.filename) {
    const stem = sanitizeFilename(suggestedTitle || document.token);
    document.filename = `${stem}__${document.kind}-${document.token}.md`;
  }
  const destination = path.join(OUTPUT_DIR, document.filename);
  fs.copyFileSync(temporaryPath, destination);

  return {
    title: suggestedTitle || document.token,
    filename: document.filename,
    size: fs.statSync(destination).size,
  };
}

async function startBrowser() {
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  if (!context) {
    throw new Error("未找到已连接的 Chrome 浏览器上下文");
  }
  return { browser, context };
}

async function downloadBatch(context, manifest, documents) {
  const queue = documents.filter(
    (document) =>
      document.status !== "downloaded" &&
      document.status !== "depth_limit" &&
      document.attempts < MAX_ATTEMPTS,
  );
  if (!queue.length) {
    return { succeeded: 0, failed: 0 };
  }

  let cursor = 0;
  let succeeded = 0;
  let failed = 0;
  const total = queue.length;

  async function worker() {
    const page = await context.newPage();
    try {
      while (true) {
        const index = cursor;
        cursor += 1;
        if (index >= queue.length) {
          break;
        }
        const document = queue[index];
        document.attempts += 1;
        document.status = "downloading";
        document.error = null;
        saveManifest(manifest);
        try {
          const result = await exportMarkdown(page, document);
          document.status = "downloaded";
          document.title = result.title;
          document.filename = result.filename;
          document.downloaded_at = now();
          document.error = null;
          succeeded += 1;
          console.log(
            `[${index + 1}/${total}] OK depth=${document.depth} ${result.filename} (${result.size} bytes)`,
          );
        } catch (error) {
          document.status =
            document.attempts >= MAX_ATTEMPTS ? "failed" : "pending";
          document.error = String(error?.message || error)
            .replace(/\s+/g, " ")
            .slice(0, 500);
          failed += 1;
          console.error(
            `[${index + 1}/${total}] FAIL depth=${document.depth} ${document.key}: ${document.error}`,
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
      { length: Math.max(1, Math.min(CONCURRENCY, queue.length)) },
      () => worker(),
    ),
  );
  return { succeeded, failed };
}

function readDocumentMarkdown(document) {
  if (!document.filename) {
    return "";
  }
  const filePath = path.join(OUTPUT_DIR, document.filename);
  return fs.existsSync(filePath) ? fs.readFileSync(filePath, "utf8") : "";
}

async function main() {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const manifest = loadManifest();
  for (const sourcePath of SOURCES) {
    const text = fs.readFileSync(sourcePath, "utf8");
    const result = ingestLinks(
      manifest,
      text,
      {
        name: path.basename(sourcePath),
        label: path.basename(sourcePath),
        url: "",
      },
      1,
    );
    console.log(
      `source ${path.basename(sourcePath)}: +${result.newDocuments} docs, +${result.newAttachments} attachments`,
    );
  }
  saveManifest(manifest);

  const { browser, context } = await startBrowser();
  try {
    for (let depth = 1; depth <= MAX_DEPTH; depth += 1) {
      const documentsAtDepth = Object.values(manifest.documents).filter(
        (document) => document.depth === depth,
      );
      const pending = documentsAtDepth.filter(
        (document) =>
          document.status === "pending" ||
          document.status === "downloading" ||
          document.status === "download_failed",
      );
      console.log(
        `depth ${depth}: ${documentsAtDepth.length} known, ${pending.length} to download`,
      );
      if (pending.length) {
        const result = await downloadBatch(context, manifest, pending);
        console.log(
          `depth ${depth}: ${result.succeeded} downloaded, ${result.failed} failed`,
        );
      }

      let newDocuments = 0;
      let newAttachments = 0;
      for (const document of documentsAtDepth) {
        if (
          document.status !== "downloaded" ||
          document.scanned_depth === depth
        ) {
          continue;
        }
        const markdown = readDocumentMarkdown(document);
        const result = ingestLinks(
          manifest,
          markdown,
          {
            name: document.filename || document.key,
            label: document.title || "",
            url: document.url,
          },
          depth + 1,
        );
        document.scanned_depth = depth;
        newDocuments += result.newDocuments;
        newAttachments += result.newAttachments;
      }
      saveManifest(manifest);
      console.log(
        `depth ${depth}: recursive scan found +${newDocuments} docs, +${newAttachments} attachments`,
      );
    }
  } finally {
    await browser.close().catch(() => {});
  }

  const documents = Object.values(manifest.documents);
  const downloaded = documents.filter(
    (document) => document.status === "downloaded",
  );
  const failed = documents.filter(
    (document) =>
      document.status === "failed" || document.status === "depth_limit",
  );
  console.log(
    `complete: ${downloaded.length} downloaded, ${failed.length} not downloaded, ${Object.keys(
      manifest.attachments,
    ).length} attachment links`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
