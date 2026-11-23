from __future__ import annotations

import json
from collections import Counter
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path.cwd()
OUTPUT_DIR = ROOT / "feishu_docs"
MANIFEST_PATH = OUTPUT_DIR / "_manifest.json"
INDEX_PATH = ROOT / "INDEX_LOCAL.md"
ATTACHMENTS_PATH = ROOT / "attachments.md"
SOURCES = [
    Path("/home/dianrobot/下载/具身打磨项目组会.md"),
    Path("/home/dianrobot/下载/具身打磨工作会议.md"),
]
LIVE_SOURCE_DIR = ROOT / "source_snapshots"
FEISHU_URL_RE = re.compile(
    r"https://hustdian\.feishu\.cn/(wiki|docx|file)/[A-Za-z0-9]+"
    r"(?:[?#][^\s)<>\"']*)?"
)


def normalize_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname != "hustdian.feishu.cn":
        return None
    match = re.fullmatch(r"/(wiki|docx|file)/([A-Za-z0-9]+)/?", parsed.path)
    if not match:
        return None
    return match.group(1), match.group(2)


def markdown_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def source_label(row: dict) -> str:
    name = row.get("name") or ""
    label = row.get("label") or ""
    if label and label != name:
        return f"{name}（{label}）"
    return name


def main() -> None:
    effective_sources = []
    for source_path in SOURCES:
        live_path = LIVE_SOURCE_DIR / source_path.name
        effective_sources.append(live_path if live_path.is_file() else source_path)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    documents: dict[str, dict] = manifest["documents"]
    attachments: dict[str, dict] = manifest["attachments"]

    downloaded = {
        key: row
        for key, row in documents.items()
        if row.get("status") == "downloaded"
        and row.get("filename")
        and (OUTPUT_DIR / row["filename"]).is_file()
    }
    media_files = list((OUTPUT_DIR / "assets").rglob("*"))
    media_files = [path for path in media_files if path.is_file()]
    media_types = Counter(
        path.suffix.lower().lstrip(".") or "(none)" for path in media_files
    )
    downloaded_attachments = [
        row
        for row in attachments.values()
        if row.get("download_status") == "downloaded"
        and row.get("local_filename")
        and (ROOT / "attachments" / row["local_filename"]).is_file()
    ]
    attachment_bytes = sum(
        row.get("size", 0) for row in downloaded_attachments
    )

    def replacement(url: str, from_document: bool = False) -> str:
        normalized = normalize_url(url)
        if not normalized:
            return url
        kind, token = normalized
        key = f"{kind}:{token}"
        if kind == "file":
            attachment = attachments.get(key)
            if (
                attachment
                and attachment.get("download_status") == "downloaded"
                and attachment.get("local_filename")
                and (ROOT / "attachments" / attachment["local_filename"]).is_file()
            ):
                prefix = "../" if from_document else "./"
                return f"{prefix}attachments/{attachment['local_filename']}"
            prefix = "../" if from_document else "./"
            return f"{prefix}attachments.md#att-{token}"
        row = downloaded.get(key)
        if not row:
            return url
        if from_document:
            return row["filename"]
        return f"./feishu_docs/{row['filename']}"

    rendered_sources: list[str] = []
    unresolved_source_links: list[str] = []
    for source_path in effective_sources:
        text = source_path.read_text(encoding="utf-8")

        def replace_source_link(match: re.Match[str]) -> str:
            original = match.group(0)
            local = replacement(original)
            if local == original:
                unresolved_source_links.append(original)
            return local

        rendered = FEISHU_URL_RE.sub(replace_source_link, text).strip()
        rendered_sources.append(
            f"## 来源文件：{source_path.name}\n\n{rendered}"
        )

    downloaded_count = len(downloaded)
    depth_limit_count = sum(
        row.get("status") == "depth_limit" for row in documents.values()
    )
    failed_rows = [
        row for row in documents.values() if row.get("status") == "failed"
    ]
    unresolved_count = depth_limit_count + len(failed_rows)
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    index_header = (
        "# 具身打磨项目会议文档本地索引\n\n"
        f"> 生成时间：{generated_at}\n"
        f"> 已下载文档：{downloaded_count} 篇\n"
        f"> 文档媒体：{len(media_files)} 个文件，"
        f"其中图片 {media_types['png'] + media_types['jpg'] + media_types['jpeg']} 个、"
        f"视频 {media_types['mp4'] + media_types['mov'] + media_types['webm']} 个\n"
        f"> 附件文件：{len(downloaded_attachments)}/{len(attachments)} 个，"
        f"合计 {attachment_bytes / (1024**3):.2f} GiB，"
        "见 [attachments.md](./attachments.md)\n"
        f"> 递归范围：根链接 + 2 轮二级链接\n"
        f"> 未下载文档：{unresolved_count} 篇"
        f"（递归上限外 {depth_limit_count} 篇，访问失败 {len(failed_rows)} 篇）\n"
        "> 完整下载状态见 [feishu_docs/_manifest.json](./feishu_docs/_manifest.json)。\n\n"
        "以下内容由两份源 Markdown 合并，原飞书文档链接已替换为本地相对路径。\n"
    )
    INDEX_PATH.write_text(
        index_header + "\n\n---\n\n" + "\n\n---\n\n".join(rendered_sources) + "\n",
        encoding="utf-8",
    )

    source_names = {source_path.name for source_path in SOURCES}

    def is_source_attachment(row: dict) -> bool:
        return any(
            source.get("name") in source_names and not source.get("url")
            for source in row.get("sources", [])
        )

    root_attachments = [
        row for row in attachments.values() if is_source_attachment(row)
    ]
    recursive_attachments = [
        row for row in attachments.values() if not is_source_attachment(row)
    ]
    attachment_sections = [
        "# 飞书附件链接\n",
        f"> 生成时间：{generated_at}\n",
        "> 按 `/file/` 路径 Token 归一化去重。以下链接保留为飞书原始地址。\n",
        f"> 已下载 {len(downloaded_attachments)}/{len(attachments)} 个，"
        f"合计 {attachment_bytes / (1024**3):.2f} GiB；"
        "未下载项保留原始飞书链接。\n",
    ]

    def append_attachment_group(title: str, rows: list[dict]) -> None:
        attachment_sections.extend([f"## {title}（{len(rows)} 个）", ""])
        for row in sorted(
            rows,
            key=lambda item: (
                item["labels"][0] if item.get("labels") else item["token"],
                item["token"],
            ),
        ):
            label = row["labels"][0] if row.get("labels") else row["token"]
            local_filename = row.get("local_filename")
            local_path = ROOT / "attachments" / local_filename if local_filename else None
            heading_url = (
                f"./attachments/{local_filename}"
                if local_path and local_path.is_file()
                else row["url"]
            )
            sources = "；".join(
                markdown_table(source_label(source))
                for source in row.get("sources", [])
                if source_label(source)
            )
            attachment_sections.extend(
                [
                    f'<a id="att-{row["token"]}"></a>',
                    "",
                    f"### [{markdown_table(label)}]({heading_url})",
                    "",
                    f"- 文件 Token：`{row['token']}`",
                    f"- 原始链接：{row['url']}",
                    f"- 本地文件：{heading_url if heading_url != row['url'] else '未下载'}",
                    f"- 来源：{sources or '未记录'}",
                    "",
                ]
            )

    append_attachment_group("源文件中的附件", root_attachments)
    append_attachment_group("递归文档中的附件", recursive_attachments)
    ATTACHMENTS_PATH.write_text(
        "\n".join(attachment_sections).rstrip() + "\n",
        encoding="utf-8",
    )

    rewritten_files = 0
    unresolved_local_links = 0
    for filename in sorted(
        {row["filename"] for row in downloaded.values() if row.get("filename")}
    ):
        file_path = OUTPUT_DIR / filename
        text = file_path.read_text(encoding="utf-8")

        def replace_local_link(match: re.Match[str]) -> str:
            nonlocal unresolved_local_links
            original = match.group(0)
            local = replacement(original, from_document=True)
            if local == original:
                unresolved_local_links += 1
            return local

        rewritten = FEISHU_URL_RE.sub(replace_local_link, text)
        if rewritten != text:
            file_path.write_text(rewritten, encoding="utf-8")
            rewritten_files += 1

    remaining_index_links = FEISHU_URL_RE.findall(INDEX_PATH.read_text(encoding="utf-8"))
    local_index_targets = re.findall(
        r"\]\((\./feishu_docs/[^)#]+\.md)\)",
        INDEX_PATH.read_text(encoding="utf-8"),
    )
    missing_local_targets = sorted(
        {
            target
            for target in local_index_targets
            if not (ROOT / target.removeprefix("./")).is_file()
        }
    )

    report = {
        "generated_at": generated_at,
        "source_files": [str(path) for path in effective_sources],
        "root_documents": sum(row.get("depth") == 1 for row in documents.values()),
        "root_documents_downloaded": sum(
            row.get("depth") == 1 and row.get("status") == "downloaded"
            for row in documents.values()
        ),
        "downloaded_documents": downloaded_count,
        "not_downloaded_documents": unresolved_count,
        "depth_limit_documents": depth_limit_count,
        "failed_documents": [
            {
                "key": row.get("key"),
                "url": row.get("url"),
                "error": row.get("error"),
            }
            for row in sorted(failed_rows, key=lambda item: item.get("key", ""))
        ],
        "attachment_links": len(attachments),
        "attachments_downloaded": len(downloaded_attachments),
        "attachment_bytes": attachment_bytes,
        "source_attachment_links": len(root_attachments),
        "recursive_attachment_links": len(recursive_attachments),
        "document_media_files": len(media_files),
        "document_media_types": dict(sorted(media_types.items())),
        "rewritten_document_files": rewritten_files,
        "unresolved_links_in_downloaded_documents": unresolved_local_links,
        "unresolved_source_links_in_index": len(unresolved_source_links),
        "remaining_feishu_links_in_index": len(remaining_index_links),
        "missing_local_targets_in_index": missing_local_targets,
    }
    (OUTPUT_DIR / "_index_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
